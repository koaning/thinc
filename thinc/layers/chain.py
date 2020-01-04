from typing import Tuple, Callable, Optional, TypeVar

from ..model import Model
from ..util import get_width


InputType = TypeVar("InputType")
OutputType = TypeVar("OutputType")


def chain(*layers: Model) -> Model:
    """Compose two models `f` and `g` such that they become layers of a single
    feed-forward model that computes `g(f(x))`.
    """
    if layers and layers[0]._func is forward:
        layers[0].layers.extend(layers[1:])
        return layers[0]

    model = Model(
        ">>".join(layer.name for layer in layers),
        forward,
        init=init,
        dims={"nO": None, "nI": None},
        layers=layers,
    )
    if layers and layers[0].has_dim("nI") and layers[-1].has_dim("nO"):
        model.initialize()
    return model


def forward(model: Model, X: InputType, is_train: bool) -> Tuple[OutputType, Callable]:
    """Apply the layers of `model` in sequence, feeding the output from one
    layer into the next.
    """
    callbacks = []
    for layer in model.layers:
        Y, inc_layer_grad = layer(X, is_train=is_train)
        callbacks.append(inc_layer_grad)
        X = Y

    def backprop(dY: OutputType) -> InputType:
        for callback in reversed(callbacks):
            dX = callback(dY)
            dY = dX
        return dX

    return Y, backprop


def init(
    model: Model, X: Optional[InputType] = None, Y: Optional[OutputType] = None
) -> None:
    if not model.layers:
        return
    if X is None and Y is None:
        for layer in model.layers:
            layer.initialize()
        if model.layers[0].has_dim("nI"):
            model.set_dim("nI", model.layers[0].get_dim("nI"))
        if model.layers[-1].has_dim("nO"):
            model.set_dim("nO", model.layers[-1].get_dim("nO"))
        return
    # Try to set nO on each layer, where available.
    nO = None
    if Y is not None:
        nO = get_width(Y)
    elif model.has_dim("nO"):
        nO = model.get_dim("nO")
    for layer in reversed(model.layers):
        if nO is not None and layer.has_dim("nO") is None:
            layer.set_dim("nO", nO)
        if layer.has_dim("nI"):
            nO = layer.get_dim("nI")
        else:
            break
    for layer in model.layers[:-1]:
        layer.initialize(X=X)
        X = layer.predict(X)
    model.layers[-1].initialize(X=X, Y=Y)
    if model.layers[0].has_dim("nI"):
        model.set_dim("nI", model.layers[0].get_dim("nI"))
    if model.layers[-1].has_dim("nO"):
        model.set_dim("nO", model.layers[-1].get_dim("nO"))
