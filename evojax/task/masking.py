# Copyright 2022 The EvoJAX Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from typing import Tuple
import numpy as np

import jax
import jax.numpy as jnp
from jax import random
from flax.core import freeze, unfreeze
from flax.struct import dataclass

from evojax.task.base import VectorizedTask
from evojax.task.base import TaskState

from evojax.datasets import read_data_files, digit, fashion, kuzushiji
from evojax.train_mnist_cnn import CNN, linear_layer_name


@dataclass
class State(TaskState):
    obs: jnp.ndarray  # This will be the dataset label for now as this is the input for the
    labels: jnp.ndarray
    image_data: jnp.ndarray


def sample_batch(key: jnp.ndarray,
                 data: jnp.ndarray,
                 class_labels: jnp.ndarray,
                 dataset_labels: jnp.ndarray,
                 batch_size: int) -> Tuple:
    ix = random.choice(
        key=key, a=data.shape[0], shape=(batch_size,), replace=False)
    return (jnp.take(data, indices=ix, axis=0),
            jnp.take(class_labels, indices=ix, axis=0),
            jnp.take(dataset_labels, indices=ix, axis=0))


def loss(prediction: jnp.ndarray, target: jnp.ndarray) -> jnp.float32:
    target = jax.nn.one_hot(target, 10)
    return -jnp.mean(jnp.sum(prediction * target, axis=1))


def accuracy(prediction: jnp.ndarray, target: jnp.ndarray) -> jnp.float32:
    predicted_class = jnp.argmax(prediction, axis=1)
    return jnp.mean(predicted_class == target)


class Masking(VectorizedTask):
    """Masking task for MNIST."""

    def __init__(self,
                 batch_size: int = 1024,
                 test: bool = False,
                 mnist_params=None,
                 mask_size: int = None):

        self.mnist_params = mnist_params
        self.linear_weights_orig = self.mnist_params[linear_layer_name]["kernel"]

        self.max_steps = 1
        self.obs_shape = tuple([1, ])
        self.act_shape = tuple([mask_size, ])

        x_array, y_array = [], []
        for dataset_name in [digit, fashion, kuzushiji]:
            x, y = read_data_files(dataset_name, 'test' if test else 'train')
            x_array.append(x)
            y_array.append(y)

        # TODO remove this once the memory issues have been fixed
        if not test:
            random_sample = np.random.permutation(range(180000))[:2**13]
        else:
            random_sample = np.random.permutation(range(30000))[:2**11]

        image_data = jnp.float32(np.concatenate(x_array)[random_sample]) / 255.
        labels = jnp.int16(np.concatenate(y_array)[random_sample])
        class_labels = labels[:, 0]
        dataset_labels = labels[:, 1]

        def reset_fn(key):
            batch_data, batch_class_labels, batch_dataset_labels = sample_batch(
                key, image_data, class_labels, dataset_labels, batch_size)
            return State(obs=dataset_labels, labels=batch_class_labels, image_data=image_data)
        self._reset_fn = jax.jit(jax.vmap(reset_fn))

        def step_fn(state, action):
            # TODO is this state the dataclass or state of the masking model???

            # Action should be the mask which will be applied to the linear weights
            # TODO Should the weights be masked or just the input to the linear layer
            # params = unfreeze(self.mnist_params)
            # masked_weights = self.linear_weights_orig * action.reshape(self.linear_weights_orig.shape)
            # params[linear_layer_name]["kernel"] = masked_weights
            # params = freeze(params)

            output_logits = CNN().apply({'params': self.mnist_params}, state.image_data, action)

            if test:
                reward = accuracy(output_logits, state.labels)
            else:
                reward = -loss(output_logits, state.labels)
            return state, reward, jnp.ones(())
        self._step_fn = jax.jit(jax.vmap(step_fn))

    def reset(self, key: jnp.ndarray) -> State:
        return self._reset_fn(key)

    def step(self,
             state: TaskState,
             action: jnp.ndarray) -> Tuple[TaskState, jnp.ndarray, jnp.ndarray]:
        return self._step_fn(state, action)
