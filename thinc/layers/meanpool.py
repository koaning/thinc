from typing import Tuple, Callable, TypeVar

from ..types import Array
from ..model import Model


InputType = TypeVar("InputType", bound=Tuple[Array, Array])
OutputType = TypeVar("OutputType", bound=Tuple[Array, Array])


def MeanPool() -> Model:
    return Model("mean_pool", forward)


def forward(
    model: Model, X_lengths: InputType, is_train: bool
) -> Tuple[OutputType, Callable]:
    X, lengths = X_lengths
    Y = model.ops.mean_pool(X, lengths)

    def backprop(dY: OutputType) -> InputType:
        return model.ops.backprop_mean_pool(dY, lengths), lengths

    return Y, backprop
