import logging
import numpy as np
from typing import Tuple

import wandb

import optax
import jax
from jax import random
import jax.numpy as jnp
from flax.training import train_state
from flax.core import FrozenDict

from evojax.models import CNN, Mask, cnn_final_layer_name, create_train_state
from evojax.datasets import dataset_names, DatasetUtilClass, full_data_loader
from evojax.util import cross_entropy_loss, compute_metrics


def get_batch_masks(state, task_labels, mask_params=None, l1_pruning_proportion=None):
    if mask_params is not None:
        linear_weights = state.params[cnn_final_layer_name]["kernel"]
        mask_size = linear_weights.shape[0]
        batch_masks = Mask(mask_size=mask_size).apply({'params': mask_params}, task_labels)
    elif l1_pruning_proportion:
        batch_masks = None
    else:
        batch_masks = None

    return batch_masks


@jax.jit
def train_step(state,
               batch,
               mask_params: FrozenDict = None,
               task_labels: jnp.ndarray = None,
               l1_pruning_proportion: float = None,
               l1_reg_lambda: float = None,
               dropout_rate: float = None,
               ):

    class_labels = batch['label'][:, 0]
    batch_masks = get_batch_masks(state, task_labels, mask_params, l1_pruning_proportion)

    def loss_fn(params):
        output_logits = CNN(dropout_rate=dropout_rate).apply({'params': params},
                                                             batch['image'],
                                                             batch_masks,
                                                             task_labels)

        loss = cross_entropy_loss(logits=output_logits, labels=class_labels)

        if l1_reg_lambda:
            loss += l1_reg_lambda * jnp.sum(jnp.abs(params[cnn_final_layer_name]["kernel"]))

        return loss, output_logits

    grad_fn = jax.value_and_grad(loss_fn, has_aux=True)
    (_, logits), grads = grad_fn(state.params)
    state = state.apply_gradients(grads=grads)
    metrics = compute_metrics(logits=logits, labels=class_labels)
    return state, metrics


@jax.jit
def eval_step(state: train_state.TrainState,
              batch: dict,
              mask_params: FrozenDict = None,
              task_labels: jnp.ndarray = None,
              l1_pruning_proportion: float = None,
              l1_reg_lambda: float = None,
              dropout_rate: float = None,
              ) -> Tuple[train_state.TrainState, dict]:

    params = state.params
    class_labels = batch['label'][:, 0]
    # task_labels = batch['label'][:, 1] if use_task_labels else None

    batch_masks = get_batch_masks(state, task_labels, mask_params, l1_pruning_proportion)

    logits = CNN().apply({'params': params},
                         batch['image'],
                         batch_masks,
                         task_labels)

    return state, compute_metrics(logits=logits, labels=class_labels)

#
# def train_epoch(state, train_ds, batch_size, rng,
#                 mask_params=None, pixel_input=False, cnn_labels=False):
#     """Train for a single epoch."""
#     train_ds_size = len(train_ds['image'])
#     steps_per_epoch = train_ds_size // batch_size
#
#     perms = jax.random.permutation(rng, train_ds_size)
#     perms = perms[:steps_per_epoch * batch_size]  # skip incomplete batch
#     perms = perms.reshape((steps_per_epoch, batch_size))
#     batch_metrics = []
#
#     for perm in perms:
#         batch = {k: v[perm, ...] for k, v in train_ds.items()}
#         label_input = batch['label'][:, 1] if cnn_labels else None
#         state, metrics = train_step(state, batch, mask_params, pixel_input, label_input)
#         batch_metrics.append(metrics)
#
#     # compute mean of metrics across each batch in epoch.
#     batch_metrics_np = jax.device_get(batch_metrics)
#     epoch_metrics_np = {
#         k: np.mean([metrics[k] for metrics in batch_metrics_np])
#         for k in batch_metrics_np[0]}
#
#     return state, epoch_metrics_np


def epoch_step(test: bool,
               state: train_state.TrainState,
               dataset_class: DatasetUtilClass,
               batch_size: int,
               rng,
               mask_params: FrozenDict = None,
               use_task_labels: bool = False,
               l1_pruning_proportion: float = None,
               l1_reg_lambda: float = None,
               dropout_rate: float = None,
               ) -> Tuple[train_state.TrainState, DatasetUtilClass]:

    for dataset_name, dataset in dataset_class.dataset_holder.items():
        ds_size = dataset['image'].shape[0]
        steps_per_epoch = ds_size // batch_size

        if test:
            step_func = eval_step
        else:
            step_func = train_step

        perms = jax.random.permutation(rng, ds_size)
        perms = perms[:steps_per_epoch * batch_size]  # skip incomplete batch
        perms = perms.reshape((steps_per_epoch, batch_size))

        batch_metrics = []
        for perm in perms:
            batch = {k: v[perm, ...] for k, v in dataset.items()}
            task_labels = batch['label'][:, 1] if use_task_labels else None
            state, metrics = step_func(state,
                                       batch,
                                       mask_params,
                                       task_labels,
                                       l1_pruning_proportion,
                                       l1_reg_lambda,
                                       dropout_rate)

            batch_metrics.append(metrics)

        batch_metrics_np = jax.device_get(batch_metrics)
        epoch_metrics_np = {
            k: np.mean([metrics[k] for metrics in batch_metrics_np])
            for k in batch_metrics_np[0]}

        # Save the metrics for that datas
        dataset_class.metrics_holder[dataset_name] = {'loss': epoch_metrics_np['loss'],
                                                      'accuracy': epoch_metrics_np['accuracy']}

    return state, dataset_class


def calc_and_log_metrics(dataset_class: DatasetUtilClass, logger: logging.Logger, epoch: int) -> float:
    total_accuracy = np.mean([i['accuracy'] for i in dataset_class.metrics_holder.values()])
    total_loss = np.mean([i['loss'] for i in dataset_class.metrics_holder.values()])

    logger.debug(f'{dataset_class.split.upper()}, epoch={epoch}, loss={total_loss}, accuracy={total_accuracy}')

    if dataset_class.split == 'test':
        for dataset_name in dataset_names:
            ds_test_accuracy = dataset_class.metrics_holder[dataset_name].get("accuracy")
            logger.debug(f'TEST, {dataset_name} 'f'accuracy={ds_test_accuracy:.2f}')

            # wandb.log({f'{dataset_name} Test Accuracy': ds_test_accuracy}, step=relative_epoch, commit=False)
            wandb.log({f'{dataset_name} Test Accuracy': ds_test_accuracy})

    return total_accuracy.item()


def run_mnist_training(
        logger: logging.Logger,
        eval_only: bool = False,
        seed: int = 0,
        num_epochs: int = 20,
        evo_epoch: int = 0,
        learning_rate: float = 1e-3,
        cnn_batch_size: int = 1024,
        state: train_state.TrainState = None,
        mask_params: FrozenDict = None,
        datasets_tuple: Tuple[DatasetUtilClass, DatasetUtilClass, DatasetUtilClass] = None,
        early_stopping: bool = False,
        # These are the parameters for the other sparsity baseline types
        use_task_labels: bool = False,
        l1_pruning_proportion: float = None,
        l1_reg_lambda: float = None,
        dropout_rate: float = None,
) -> Tuple[train_state.TrainState, float]:

    logger.info('Starting training MNIST CNN')

    rng = random.PRNGKey(seed)

    # Allow passing of a state, so only init if this is none
    if state is None:
        rng, init_rng = random.split(rng)
        task_labels = jnp.ones([1, ]) if use_task_labels else None
        state = create_train_state(init_rng, learning_rate, task_labels)
        del init_rng  # Must not be used anymore.

    if datasets_tuple:
        train_dataset_class, validation_dataset_class, test_dataset_class = datasets_tuple
    else:
        train_dataset_class, validation_dataset_class, test_dataset_class = full_data_loader()

    if eval_only:
        state, test_dataset_class = epoch_step(test=True,
                                               state=state,
                                               dataset_class=test_dataset_class,
                                               batch_size=cnn_batch_size,
                                               rng=rng,
                                               mask_params=mask_params,
                                               use_task_labels=use_task_labels,
                                               l1_pruning_proportion=l1_pruning_proportion,
                                               l1_reg_lambda=l1_reg_lambda,
                                               dropout_rate=dropout_rate)

        return state, np.mean([i['accuracy'] for i in test_dataset_class.metrics_holder.values()])[0]

    previous_state = None
    current_test_accuracy = previous_test_accuracy = previous_validation_accuracy = 0.
    for epoch in range(1, num_epochs + 1):
        # Since there can be multiple evo epochs count from the start of them
        relative_epoch = evo_epoch * num_epochs + epoch - 1

        logger.info(f'Starting epoch {relative_epoch} of CNN training')

        # Use a separate PRNG key to permute image data during shuffling
        rng, input_rng = jax.random.split(rng)

        # Run an optimization step over a training batch
        state, train_dataset_class = epoch_step(test=False,
                                                state=state,
                                                dataset_class=train_dataset_class,
                                                batch_size=cnn_batch_size,
                                                rng=input_rng,
                                                mask_params=mask_params,
                                                use_task_labels=use_task_labels,
                                                l1_pruning_proportion=l1_pruning_proportion,
                                                l1_reg_lambda=l1_reg_lambda,
                                                dropout_rate=dropout_rate)

        current_train_accuracy = calc_and_log_metrics(validation_dataset_class, logger, epoch)

        # Check the validation dataset
        state, validation_dataset_class = epoch_step(test=True,
                                                     state=state,
                                                     dataset_class=validation_dataset_class,
                                                     batch_size=cnn_batch_size,
                                                     rng=input_rng,
                                                     mask_params=mask_params,
                                                     use_task_labels=use_task_labels,
                                                     l1_pruning_proportion=l1_pruning_proportion,
                                                     l1_reg_lambda=l1_reg_lambda,
                                                     dropout_rate=dropout_rate)

        current_validation_accuracy = calc_and_log_metrics(validation_dataset_class, logger, epoch)

        # Evaluate on the test set after each training epoch
        state, test_dataset_class = epoch_step(test=True,
                                               state=state,
                                               dataset_class=test_dataset_class,
                                               batch_size=cnn_batch_size,
                                               rng=input_rng,
                                               mask_params=mask_params,
                                               use_task_labels=use_task_labels,
                                               l1_pruning_proportion=l1_pruning_proportion,
                                               l1_reg_lambda=l1_reg_lambda,
                                               dropout_rate=dropout_rate)

        current_test_accuracy = calc_and_log_metrics(validation_dataset_class, logger, epoch)

        wandb.log({'Combined Train Accuracy': current_train_accuracy,
                   'Combined Validation Accuracy': current_validation_accuracy,
                   'Combined Test Accuracy': current_test_accuracy})

        # If the validation accuracy decreases will want to end if doing early stopping
        if current_validation_accuracy > previous_validation_accuracy and early_stopping:
            previous_validation_accuracy = current_validation_accuracy
            previous_test_accuracy = current_test_accuracy
            previous_state = state
        elif early_stopping:
            logger.info(f'Validation accuracy decreased on epoch {epoch}, stopping early')
            return previous_state, previous_test_accuracy
        else:
            pass

    return state, current_test_accuracy


if __name__ == '__main__':

    log = logging.Logger(level=logging.INFO, name='mnist_logger')
    _ = run_mnist_training(logger=log)
