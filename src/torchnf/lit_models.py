"""
Alternative implementations of the models from :mod:`torchnf.models`, based
on :py:class:`pytorch_lightning.LightningModule` rather than the standard
:py:class:`torch.nn.Module`.

.. attention:: This is a work in progress. Do not use.
"""
from typing import Callable
import torch

# import pytorch_lightning as pl

import torchnf.distributions
import torchnf.flow


class LitBoltzmannGenerator:  # (pl.LightningModule):
    """Test"""

    def __init__(
        self,
        prior: torchnf.distributions.Prior,
        target: Callable[torch.Tensor, torch.Tensor],
        flow: torchnf.flow.Flow,
    ) -> None:
        super().__init__()
        self.prior = prior
        self.target = target
        self.flow = flow

    def forward(self, batch):
        """."""
        x, log_prob_prior = batch
        y, log_det_jacob = self.flow(x)
        log_prob_target = self.target(y)
        log_weights = log_prob_target - log_prob_prior + log_det_jacob
        return y, log_weights

    def train_dataloader(self):
        """."""
        return self.prior

    def training_step(self, batch, batch_idx):
        """."""
        y, log_weights = self.forward(batch)
        loss = log_weights.mean().neg()
        return loss

    def training_step_end(self, outputs):
        """."""
        print(outputs)

    def val_dataloader(self):
        """."""
        return self.prior

    def validation_step(self, batch, batch_idx):
        """."""
        y, log_weights = self.forward(batch)

    def validation_epoch_end(self, metrics):
        """."""
        # combine metrics into mean and std. dev.
        # log metrics
        pass

    def configure_optimizers(self):
        """."""
        # raise NotImplementedError
        optimizer = torch.optim.Adam(self.flow.parameters())
        return optimizer

    @torch.no_grad()
    def sample(self) -> tuple[torch.Tensor]:
        return self.forward(self.prior())