import itertools
import pathlib
import types

import jsonargparse
from jsonargparse.typing import PositiveInt
import matplotlib.pyplot as plt
import pytorch_lightning as pl
import torch

from torchnf.abc import Transformer
from torchnf.conditioners import MaskedConditioner
from torchnf.models import BijectiveAutoEncoder
from torchnf.networks import DenseNet
from torchnf.layers import FlowLayer, Composition
from torchnf.utils.datasets import Moons
from torchnf.utils.distribution import diagonal_gaussian
from torchnf.utils.optim import OptimizerConfig


default_config = (
    pathlib.Path(__file__)
    .parent.joinpath("default_config")
    .joinpath("moons.yaml")
)


def make_flow(
    transformer: Transformer,
    net: DenseNet,
    flow_depth: PositiveInt,
) -> Composition:
    mask = torch.tensor([True, False], dtype=bool)

    conditioner = lambda mask_: MaskedConditioner(  # noqa: E731
        net(1, transformer.n_params), mask_
    )

    layers = [
        FlowLayer(transformer, conditioner(m))
        for _, m in zip(range(flow_depth), itertools.cycle([mask, ~mask]))
    ]
    return Composition(*layers)


Flow = jsonargparse.class_from_function(make_flow)

parser = jsonargparse.ArgumentParser(
    prog="Moons", default_config_files=[str(default_config)]
)

parser.add_class_arguments(Flow, "flow")
parser.add_argument("--optimizer", type=OptimizerConfig)
parser.add_argument("--epochs", type=PositiveInt)
parser.add_class_arguments(Moons, "moons")
parser.add_class_arguments(pl.Trainer, "trainer")

parser.add_argument("-c", "--config", action=jsonargparse.ActionConfigFile)


def main(config: dict = {}):

    # Parse args and instantiate classes
    config = parser.parse_object(config) if config else parser.parse_args()
    config = parser.instantiate_classes(config)
    flow, optimizer, trainer, moons = (
        config.flow,
        config.optimizer,
        config.trainer,
        config.moons,
    )

    # Build model - a bijective auto-encoder
    prior = diagonal_gaussian([2])
    model = BijectiveAutoEncoder(flow, prior)

    # Add an extra method which collects all of the data generated
    # during validation and plots a scatter
    def validation_epoch_end(self, data: list[torch.Tensor]):
        x, y = torch.cat(data).T
        fig, ax = plt.subplots()
        ax.scatter(x, y)
        self.logger.experiment.add_figure(
            "Validation/encoded_data", fig, self.global_step
        )

    model.validation_epoch_end = types.MethodType(validation_epoch_end, model)

    # Add the optimizer and lr scheduler
    optimizer.add_to(model)

    # Train
    trainer.fit(model, datamodule=moons)

    (metrics,) = trainer.test(model, datamodule=moons)

    return metrics


if __name__ == "__main__":
    print(main())
