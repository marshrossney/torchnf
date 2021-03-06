"""
"""
from collections.abc import Iterator
import functools
from typing import Optional

from jsonargparse.typing import PositiveInt
import torch
from torch.distributions import Distribution
import pytorch_lightning as pl

from torchnf.abc import DensityTransform, TargetDistribution
import torchnf.metrics
from torchnf.utils.distribution import IterableDistribution

__all__ = [
    "BijectiveAutoEncoder",
    "BoltzmannGenerator",
]


def eval_mode(meth):
    """
    Decorator which sets a model to eval mode for the duration of the method.
    """

    @functools.wraps(meth)
    def wrapper(model: torch.nn.Module, *args, **kwargs):
        original_state = model.training
        model.eval()
        out = meth(model, *args, **kwargs)
        model.train(original_state)
        return out

    return wrapper


class _FlowBasedModel(pl.LightningModule):
    """
    Base LightningModule for Normalizing Flows.

    Args:
        flow:
            A Normalizing Flow. If this is not provided then
            :meth:`flow_forward` and :meth:`flow_inverse` should be
            overridden to implement the flow.

    :meta public:
    """

    def __init__(self, flow: DensityTransform) -> None:
        super().__init__()
        self.flow = flow
        self.configure_metrics()

    def flow_forward(
        self, x: torch.Tensor
    ) -> tuple[torch.Tensor, torch.Tensor]:
        """
        Forward pass of the Normalizing Flow.

        Unless overridden, this simply returns ``self.flow(x)``.
        """
        return self.flow(x)

    def flow_inverse(
        self, y: torch.Tensor
    ) -> tuple[torch.Tensor, torch.Tensor]:
        """
        Inverse pass of the Normalizing Flow.

        Unless overridden, this simply returns ``self.flow.inverse(y)``.
        """
        return self.flow.inverse(y)

    def configure_metrics(self) -> None:
        """
        Instantiate metrics (called in constructor).
        """
        ...


class BijectiveAutoEncoder(_FlowBasedModel):
    """
    Base LightningModule for flow-based latent variable models.

    Args:
        flow:
            A Normalizing Flow
        prior:
            The distribution from which latent variables are drawn.
        forward_is_encode:
            If True, the ``forward`` method of the Normalizing Flow
            performs the encoding step, and the ``inverse`` method
            performs the decoding; if False, the converse
    """

    def __init__(
        self,
        flow: DensityTransform,
        prior: Distribution,
        *,
        forward_is_encode: bool = True,
    ) -> None:
        super().__init__(flow)
        self.prior = prior

        self._encode, self._decode = (
            (self.flow_forward, self.flow_inverse)
            if forward_is_encode
            else (self.flow_inverse, self.flow_forward)
        )

    def encode(self, x: torch.Tensor) -> torch.Tensor:
        r"""
        Encodes the input data and computes the log likelihood.

        The log likelihood of the point :math:`x` under the model is

        .. math::

            \log \ell(x) = \log q(z)
            + \log \left\lvert \frac{\partial z}{\partial x} \right\rvert

        where :math:`z` is the corresponding point in latent space,
        :math:`q` is the prior distribution, and the Jacobian is that
        of the encoding transformation.

        Args:
            x:
                A batch of data drawn from the target distribution

        Returns:
            Tuple containing the encoded data and the log likelihood
            under the model
        """
        z, log_det_jacob = self._encode(x)
        log_prob_z = self.prior.log_prob(z)
        log_prob_x = log_prob_z + log_det_jacob
        return z, log_prob_x

    def decode(self, z: torch.Tensor) -> torch.Tensor:
        r"""
        Decodes the latent data and computes the log statistical weights.

        The log likelihood of the point :math:`x` under the model is

        .. math::

            \log \ell(x) = \log q(z)
            - \log \left\lvert \frac{\partial x}{\partial z} \right\rvert

        where :math:`z` is the corresponding point in latent space,
        :math:`q` is the prior distribution, and the Jacobian is that
        of the decoding transformation.

        Args:
            z:
                A batch of latent variables drawn from the prior
                distribution

        Returns:
            Tuple containing the decoded data and the log likelihood
            under the model
        """
        log_prob_z = self.prior.log_prob(z)
        x, log_det_jacob = self._decode(z)
        log_prob_x = log_prob_z - log_det_jacob
        return x, log_prob_x

    def training_step(
        self, batch: list[torch.Tensor], batch_idx: int
    ) -> torch.Tensor:
        r"""
        Performs a single training step, returning the loss.

        The loss returned is the mean of the negative log-likelihood
        of the inputs, under the model, i.e.

        .. math::

            L(\{x\}) = \frac{1}{N} \sum_{\{x\}} -\log q(x)
        """
        (x,) = batch
        z, log_prob_x = self.encode(x)
        loss = log_prob_x.mean().neg()  # forward KL
        self.log("Train/loss", loss, on_step=True, on_epoch=False)
        # self.logger.experiment.add_scalars(
        #    "loss", {"train": loss}, self.global_step
        # )
        return loss

    def validation_step(
        self, batch: list[torch.Tensor], batch_idx: int
    ) -> torch.Tensor:
        """
        Performs a single validation step, returning the encoded data.
        """
        (x,) = batch
        z, log_prob_x = self.encode(x)
        loss = log_prob_x.mean().neg()  # forward KL
        self.log("Validation/loss", loss, on_step=False, on_epoch=True)
        # self.logger.experiment.add_scalars(
        #    "loss", {"validation": loss}, self.global_step
        # )
        return z

    def test_step(
        self, batch: list[torch.Tensor], batch_idx: int
    ) -> torch.Tensor:
        """
        Performs a single test step, returning the encoded data.
        """
        (x,) = batch
        z, log_prob_x = self.encode(x)
        loss = log_prob_x.mean().neg()  # forward KL
        self.log("Test/loss", loss, on_step=False, on_epoch=True)
        return z

    @torch.no_grad()
    def sample(
        self, batch_size: PositiveInt, batches: PositiveInt = 1
    ) -> torch.Tensor:
        """
        Generate synthetic data by sampling from the model.
        """
        z = self.prior.sample([batch_size])
        x, _ = self._decode(z)
        return x


# TODO: should inherit from common parent which does not implement _step
# methods, not BijectiveAutoEncoder itself
class BoltzmannGenerator(BijectiveAutoEncoder):
    r"""
    Latent Variable Model whose target distribution is a known functional.

    If the target distribution has a known functional form,

    .. math::

        \int \mathrm{d} x p(x) = \frac{1}{Z} \int \mathrm{d} x e^{-E(x)}

    then we can estimate the 'reverse' Kullbach-Leibler divergence
    between the model :math:`q(x)` and the target,

    .. math::

        D_{KL}(q \Vert p) = \int \mathrm{d} x q(x)
        \log \frac{q(x)}{p(x)}

    up to an unimportant normalisation due to :math:`\log Z`, using

    .. math::

        \hat{D}_{KL} = \mathrm{E}_{x \sim q} \left[
        -E(x) - \log q(x) \right]

    This serves as a loss function for 'reverse-KL training'.

    Furthermore, data generated from the model can be assigned an
    un-normalised statistical weight

    .. math::

        \log w(x) = -E(x) - \log q(x)

    which allows for (asymptotically) unbiased inference.
    """

    def __init__(
        self,
        flow: DensityTransform,
        prior: Distribution,
        target: TargetDistribution,
    ) -> None:
        super().__init__(flow, prior, forward_is_encode=False)
        self.target = target

    def configure_metrics(self) -> None:
        self.metrics = torchnf.metrics.LogStatWeightMetricCollection()

    def forward(self, z: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
        r"""
        Decodes a batch of latent variables and computes the log weights.

        This decodes the latent variables, computes the log probability
        of the decoded data under the model and the target, and returns
        the decoded batch along with the logarithm of un-normalised
        statistical weights.

        The statistical weights are defined

        .. math::

            \log w(x) = \log p(x) - \log q(x)
        """
        x, log_prob_x = self.decode(z)
        log_prob_target = self.target.log_prob(x)
        log_stat_weight = log_prob_target - log_prob_x
        return x, log_stat_weight

    def training_step_rev_kl(self, z: torch.Tensor) -> torch.Tensor:
        r"""
        Performs a single 'reverse' KL step.

        This decodes the latent variables, computes the log probability
        of the decoded data under the model and the target, and returns
        an estimate of the 'reverse' Kullbach-Leibler divergence, up to
        the unknown shift due to normalisations.

        The loss returned is defined by

        .. math::

            L(\{x\}) = \frac{1}{N} \sum_{\{x\}} \left[
            \log p(x) - \log q(x) \right]

        Args:
            z:
                A batch of latent variables drawn from the prior
                distribution

        Returns:
            The mean of the negative log-likelihood of the inputs,
            under the model

        .. note:: This relies on :meth:`forward`.
        """
        x, log_stat_weight = self(z)
        loss = log_stat_weight.mean().neg()
        return loss

    def configure_training(
        self,
        batch_size: PositiveInt,
        epoch_length: PositiveInt,
        val_batch_size: Optional[PositiveInt] = None,
        val_epoch_length: Optional[PositiveInt] = None,
        test_batch_size: Optional[PositiveInt] = None,
        test_epoch_length: Optional[PositiveInt] = None,
    ) -> None:
        """
        Sets the batch sizes and epoch lengths for reverse KL training.

        Before training in reverse KL mode (i.e. decoding latent
        variables and evaluating log p - log q), this method must be
        executed in order to set the batch sizes and epoch lengths.
        """
        self.batch_size = batch_size
        self.epoch_length = epoch_length
        self.val_batch_size = val_batch_size or batch_size
        self.val_epoch_length = val_epoch_length or epoch_length
        self.test_batch_size = test_batch_size or batch_size
        self.test_epoch_length = test_epoch_length or epoch_length

    def _prior_as_dataloader(
        self, batch_size: PositiveInt, epoch_length: PositiveInt
    ) -> IterableDistribution:
        """
        Returns an iterable version of the prior distribution.
        """
        if not hasattr(self, "batch_size"):
            raise Exception("First, run 'configure_training'")
        return IterableDistribution(
            self.prior,
            batch_size,
            epoch_length,
        )

    def train_dataloader(self) -> IterableDistribution:
        """
        An iterable version of the prior distribution.
        """
        return self._prior_as_dataloader(self.batch_size, self.epoch_length)

    def val_dataloader(self) -> IterableDistribution:
        """
        An iterable version of the prior distribution.
        """
        return self._prior_as_dataloader(
            self.val_batch_size, self.val_epoch_length
        )

    def test_dataloader(self) -> IterableDistribution:
        """
        An iterable version of the prior distribution.
        """
        return self._prior_as_dataloader(
            self.test_batch_size, self.test_epoch_length
        )

    def predict_dataloader(self) -> IterableDistribution:
        """
        An iterable version of the prior distribution.
        """
        return self._prior_as_dataloader(
            self.pred_batch_size, self.pred_epoch_length
        )

    def training_step(
        self, batch: torch.Tensor, batch_idx: int
    ) -> torch.Tensor:
        """
        Single training step.

        Unless overridden, this just calls :meth:`training_step_rev_kl`.
        """
        # TODO: flag to switch to forward KL training?
        loss = self.training_step_rev_kl(batch)
        return loss

    def validation_step(self, batch: torch.Tensor, batch_idx: int) -> None:
        """
        Single validation step.

        Unless overridden, this simply does the following:

        .. code-block:: python

            y, log_stat_weights = self(batch)
            self.metrics.update(log_stat_weights)

        """
        y, log_stat_weights = self(batch)
        self.metrics.update(log_stat_weights)

    def validation_epoch_end(self, val_outputs):
        """
        Compute and log metrics at the end of an epoch.
        """
        metrics = self.metrics.compute()
        self.log_dict(metrics)
        self.metrics.reset()

    @torch.no_grad()
    @eval_mode
    def weighted_sample(
        self, batch_size: PositiveInt, batches: PositiveInt = 1
    ) -> tuple[torch.Tensor, torch.Tensor]:
        """
        Generate a weighted sample by sampling from the model.

        Essentially, this does

        .. code:: python

            for _ in range(batches):
                z = self.prior.sample([batch_size])
                x, log_prob_x = self.decode(z)
                log_prob_target = self.target.log_prob(x)
                log_stat_weight = log_prob_target - log_prob_x
                ...

        The returned tuple ``(x, log_stat_weight)`` contains the
        concatenation of all of the batches.

        .. note:: This calls :meth:`forward`.
        """
        out = []
        for _ in range(batches):
            z = self.prior.sample([batch_size])
            out.append(self(z))
        return torchnf.utils.tuple_concat(*out)

    def __iter__(self) -> Iterator:
        return self.generator()

    @torch.no_grad()
    @eval_mode
    def generator(
        self, batch_size: PositiveInt = 64
    ) -> Iterator[tuple[torch.Tensor, torch.Tensor]]:
        """
        Returns an infinite iterator over states drawn from the model.
        """
        batch = zip(*self([batch_size]))
        while True:
            try:
                yield next(batch)
            except StopIteration:
                batch = zip(*self([batch_size]))
