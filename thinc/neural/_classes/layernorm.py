from ... import describe
from .model import Model


def _init_to_one(W, ops):
    W.fill(1.)

def _run_child_hooks(model, X, y=None):
    for hook in model.child.on_data_hooks:
        hook(model.child, X, y)
    #model.nO = model.child.nO


@describe.on_data(_run_child_hooks)
@describe.attributes(
    G=describe.Weights("Scaling vector",
        lambda obj: (obj.nO,), _init_to_one),
    b=describe.Biases("Bias vector",
        lambda obj: (obj.nO,)),
    d_G=describe.Gradient("G"),
    d_b=describe.Gradient("b")
)
class LayerNorm(Model):
    name = 'layernorm'

    def __init__(self, child, **kwargs):
        self.child = child
        self._layers = [child]
        Model.__init__(self, **kwargs)
        if 'nO' in kwargs:
            self.nO = kwargs['nO']
        elif getattr(child, 'nO', None):
            self.nO = child.nO
        self.nr_upd = 0

    def predict(self, X):
        X = self.child.predict(X)
        N, mu, var = _get_moments(self.ops, X)
        Xh = _forward(self.ops, X, mu, var)
        y = Xh * self.G + self.b
        return y

    def begin_update(self, X, drop=0.):
        X, backprop_child = self.child.begin_update(X, drop=0.)
        N, mu, var = _get_moments(self.ops, X)

        Xhat = _forward(self.ops, X, mu, var)

        y, backprop_rescale = self._begin_update_scale_shift(Xhat)

        def finish_update(dy, sgd=None):
            dy = backprop_rescale(dy, sgd)
            dist, sum_dy, sum_dy_dist = _get_d_moments(self.ops, dy, X, mu)
            d_xhat = N * dy - sum_dy - dist * var**(-1.) * sum_dy_dist
            d_xhat *= var ** (-1. / 2)
            d_xhat /= N
            return backprop_child(d_xhat, sgd)
        drop *= getattr(self.child, 'drop_factor', self.ops.asarray([1.0], dtype='f'))
        y, bp_dropout = self.ops.dropout(y, drop)
        assert y.dtype == 'float32'
        return y, bp_dropout(finish_update)

    def _begin_update_scale_shift(self, input__BI):
        def finish_update(gradient__BI, sgd=None):
            self.d_b += gradient__BI.sum(axis=0)
            d_G = self.d_G
            d_G += (gradient__BI * input__BI).sum(axis=0)
            if sgd is not None:
                sgd(self._mem.weights, self._mem.gradient, key=self.id)
            return gradient__BI * self.G
        return input__BI * self.G + self.b, finish_update


def _get_moments(ops, X):
    mu = X.mean(axis=1, keepdims=True)
    var = X.var(axis=1, keepdims=True) + 1e-08
    return ops.asarray([X.shape[1]], dtype='f'), mu, var


def _get_d_moments(ops, dy, X, mu):
    dist = X-mu
    return dist, ops.xp.sum(dy, axis=1, keepdims=True), ops.xp.sum(dy * dist, axis=1, keepdims=True)


def _forward(ops, X, mu, var):
    return (X-mu) * var ** (-1./2.)
