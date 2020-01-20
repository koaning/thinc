from typing import Dict, List, Optional
import catalogue
import contextlib
import copy
import itertools
from io import BytesIO

import numpy

from ..backends import Ops, get_current_ops
from ..types import ArgsKwargs, Array
from ..util import tensorflow2xp
from .shim import Shim

try:
    import cupy
except ImportError:
    cupy = None

try:
    import tensorflow as tf
except ImportError:  # pragma: no cover
    pass

try:
    import h5py
except ImportError:  # pragma: no cover
    pass

keras_model_fns = catalogue.create("thinc", "keras", entry_points=True)


class TensorFlowShim(Shim):
    """Interface between a TensorFlow model and a Thinc Model. This container is
    *not* a Thinc Model subclass itself.

    Reference for custom training:
    https://www.tensorflow.org/tutorials/customization/custom_training_walkthrough
    """

    def __str__(self):
        lines: List[str] = []

        def accumulate(line: str):
            lines.append(line)

        self._model.summary(print_fn=accumulate)
        return "\n".join(lines)

    def __call__(self, X: ArgsKwargs, is_train: bool):
        if is_train:
            return self.begin_update(X)
        else:
            return self.predict(X)

    def predict(self, X: ArgsKwargs):
        tf.keras.backend.set_learning_phase(0)
        Y = self._model.predict(*X.args, **X.kwargs)
        tf.keras.backend.set_learning_phase(1)
        return Y

    def begin_update(self, X: ArgsKwargs):
        tf.keras.backend.set_learning_phase(1)
        tape = tf.GradientTape()
        tape.__enter__()
        tape.watch(X.args)  # watch the input layers
        output = self._model(*X.args, **X.kwargs)

        def backprop(d_output):
            # d_args[0] contains derivative of loss wrt output (d_loss/d_output)
            tape.__exit__(None, None, None)
            # We need to handle a tuple of inputs
            if len(X.args) == 1:
                wrt_tensors = [X.args[0]]  # add the input layer also for d_loss/d_input
            else:
                wrt_tensors = list(X.args[0])
            wrt_tensors.extend(self._model.trainable_variables)
            all_gradients = tape.gradient(
                output, wrt_tensors, output_gradients=d_output[0]
            )
            dX = all_gradients[: len(X.args)]
            self.grads_for_optimization = all_gradients[1:]
            return ArgsKwargs(args=tuple(dX), kwargs={})

        return output, backprop

    def finish_update(self, optimizer):
        if not self._optimizer:
            self._optimizer = self._create_optimizer(optimizer)
        self._optimizer.apply_gradients(
            zip(self.grads_for_optimization, self._model.trainable_variables)
        )
        self._update_tensorflow_averages(optimizer)

    def _create_optimizer(self, sgd):
        if sgd.b1 != 0 and sgd.b2 != 0:
            optimizer = tf.keras.optimizers.Adam(
                learning_rate=sgd.alpha, beta_1=sgd.b1, beta_2=sgd.b2
            )
        elif sgd.b2 == 0:
            optimizer = tf.keras.optimizers.SGD(
                learning_rate=sgd.alpha, momentum=sgd.b1
            )
        else:
            raise NotImplementedError
        return optimizer

    def _load_weights_from_state_dict(
        self, state_dict: Optional[Dict[str, Array]] = None
    ):
        if state_dict is None:
            state_dict = self._create_state_dict()
        for layer in self._model.layers:
            current_layer_weights = []
            for weight in layer.weights:
                current_layer_weights.append(state_dict[weight.name])
            layer.set_weights(current_layer_weights)

    # Create a state dict similar to PyTorch
    def _create_state_dict(self):
        # key as variable name and value as numpy arrays
        state_dict = {}
        for layer in self._model.layers:
            for weight in layer.weights:
                state_dict[weight.name] = weight.numpy()
        return state_dict

    @contextlib.contextmanager
    def use_params(self, params):
        key_prefix = f"tensorflow_{self.id}_"
        # state dict stores key as name and value as numpy array
        state_dict = {}
        for k, v in params.items():
            if hasattr(k, "startswith") and k.startswith(key_prefix):
                if cupy is None:
                    assert isinstance(v, numpy.ndarray)
                else:  # pragma: no cover
                    if isinstance(v, cupy.core.core.ndarray):
                        v = cupy.asnumpy(v)
                    assert isinstance(v, numpy.ndarray)
                state_dict[k.replace(key_prefix, "")] = v
        if state_dict:
            backup = self._create_state_dict()
            self._load_weights_from_state_dict(state_dict)
            yield
            self._load_weights_from_state_dict(backup)
        else:
            yield

    def _update_tensorflow_averages(self, sgd, *, init_steps=1):
        if getattr(sgd, "averages", None) is None:
            return
        # Collect parameters if we don't have them
        layers = [l.weights for l in self._model.layers]
        layers = itertools.chain(*layers)
        for layer in layers:
            key = f"tensorflow_{self.id}_{layer.name}"
            sgd.nr_update[key] += 1
            xp_param = tensorflow2xp(layer)
            if key in sgd.averages:
                sgd.ops.update_averages(sgd.averages[key], xp_param, sgd.nr_update[key])
            else:
                sgd.averages[key] = xp_param.copy()
                sgd.nr_update[key] = init_steps

    def _clone_model(self):
        """similar to tf.keras.models.clone_model()
        But the tf.keras.models.clone_model changes the names of tf.Variables.
        This method even preserves that
        """
        model_json_config = self._model.to_json()
        tf.keras.backend.clear_session()
        self._model = tf.keras.models.model_from_json(model_json_config)
        self._load_weights_from_state_dict()

    def copy(self):
        model_json_config = self._model.to_json()
        self._model = None
        tf.keras.backend.clear_session()
        copied = copy.deepcopy(self)
        copied._model = tf.keras.models.model_from_json(model_json_config)
        copied._load_weights_from_state_dict()
        return copied

    def to_device(self, device):  # pragma: no cover
        if device == "cpu":
            with tf.device("/CPU"):  # pragma: no cover
                self._clone_model()
        else:
            with tf.device("/GPU:{}".format(device)):
                self._clone_model()

    def to_bytes(self):
        filelike = BytesIO()
        try:
            with h5py.File(filelike, "w") as f:
                self._model.save(f, save_format="h5")
            return filelike.getvalue()
        except NotImplementedError:
            if not hasattr(self._model, "catalogue_name"):
                raise ValueError(
                    "Couldn't serialize to h5, and model has no factory "
                    "function for component serialization."
                )
            # Check the factory function and throw ValueError if it doesn't exist
            keras_model_fns.get(self._model.catalogue_name)
            return self._model.catalogue_name, self._model.get_weights()

    def from_bytes(self, data):
        tf.keras.backend.clear_session()
        ops: Ops = get_current_ops()
        if ops.device_type == "cpu":
            device = "CPU"
        else:  # pragma: no cover
            device = tf.test.gpu_device_name()

        # Plain bytes
        if isinstance(data, (str, bytes)):
            filelike = BytesIO(data)
            filelike.seek(0)
            with h5py.File(filelike, "r") as f:
                with tf.device(device):
                    self._model = tf.keras.models.load_model(f)
                return

        catalogue_name, model_weights = data
        model_fn = keras_model_fns.get(catalogue_name)
        with tf.device(device):
            if hasattr(self._model, "eg_args"):
                ak: ArgsKwargs = self._model.eg_args
                new_model = model_fn(*ak.args, **ak.kwargs)
            else:
                new_model = model_fn()
        # Calling predict creates layers and weights for subclassed models
        new_model.compile(**new_model.eg_compile)
        new_model.build(new_model.eg_shape)
        new_model.predict(new_model.eg_x)
        # Once the weights are created we can overwrite them.
        new_model.set_weights(model_weights)

        self._model = new_model
