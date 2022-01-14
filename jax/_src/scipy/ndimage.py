# Copyright 2019 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


import functools
import itertools
import operator
import textwrap

import scipy.ndimage

from jax._src import api
from jax import lax
from jax._src.numpy import lax_numpy as jnp
from jax._src.numpy.util import _wraps
from jax._src.util import safe_zip as zip


_nonempty_prod = functools.partial(functools.reduce, operator.mul)
_nonempty_sum = functools.partial(functools.reduce, operator.add)


def _mirror_index_fixer(index, size):
    s = size - 1 # Half-wavelength of triangular wave
    # Scaled, integer-valued version of the triangular wave |x - round(x)|
    return jnp.abs((index + s) % (2 * s) - s)


def _reflect_index_fixer(index, size):
    return jnp.floor_divide(_mirror_index_fixer(2*index+1, 2*size+1) - 1, 2)

_INDEX_FIXERS = {
    'constant': lambda index, size: index,
    'nearest': lambda index, size: jnp.clip(index, 0, size - 1),
    'wrap': lambda index, size: index % size,
    'mirror': _mirror_index_fixer,
    'reflect': _reflect_index_fixer,
}


def _round_half_away_from_zero(a):
  return a if jnp.issubdtype(a.dtype, jnp.integer) else lax.round(a)


def _nearest_indices_and_weights(coordinate):
  index = _round_half_away_from_zero(coordinate).astype(jnp.int32)
  weight = coordinate.dtype.type(1)
  return [(index, weight)]


def _linear_indices_and_weights(coordinate):
  lower = jnp.floor(coordinate)
  upper_weight = coordinate - lower
  lower_weight = 1 - upper_weight
  index = lower.astype(jnp.int32)
  return [(index, lower_weight), (index + 1, upper_weight)]


@functools.partial(api.jit, static_argnums=(2, 3, 4))
def _map_coordinates(input, coordinates, order, mode, cval):
    input = jnp.asarray(input)
    coordinates = [jnp.asarray(c) for c in coordinates]
    cval = jnp.asarray(cval, input.dtype)

    if len(coordinates) != input.ndim:
      raise ValueError('coordinates must be a sequence of length input.ndim, but '
                       '{} != {}'.format(len(coordinates), input.ndim))

    index_fixer = _INDEX_FIXERS.get(mode)
    if index_fixer is None:
      raise NotImplementedError(
          'jax.scipy.ndimage.map_coordinates does not yet support mode {}. '
          'Currently supported modes are {}.'.format(mode, set(_INDEX_FIXERS)))

    if mode == 'constant':
        is_valid = lambda index, size: (index >= 0) & (index < size)
    else:
        is_valid = lambda index, size: True

    if order == 0:
      interp_fun = _nearest_indices_and_weights
    elif order == 1:
      interp_fun = _linear_indices_and_weights
    else:
      raise NotImplementedError(
          'jax.scipy.ndimage.map_coordinates currently requires order<=1')

    valid_1d_interpolations = []
    for coordinate, size in zip(coordinates, input.shape):
      interp_nodes = interp_fun(coordinate)
      valid_interp = []
      for index, weight in interp_nodes:
        fixed_index = index_fixer(index, size)
        valid = is_valid(index, size)
        valid_interp.append((fixed_index, valid, weight))
      valid_1d_interpolations.append(valid_interp)

    outputs = []
    for items in itertools.product(*valid_1d_interpolations):
      indices, validities, weights = zip(*items)
      if all(valid is True for valid in validities):
        # fast path
        contribution = input[indices]
      else:
        all_valid = functools.reduce(operator.and_, validities)
        contribution = jnp.where(all_valid, input[indices], cval)
      outputs.append(_nonempty_prod(weights) * contribution)
    result = _nonempty_sum(outputs)
    if jnp.issubdtype(input.dtype, jnp.integer):
      result = _round_half_away_from_zero(result)
    return result.astype(input.dtype)


@_wraps(scipy.ndimage.map_coordinates, lax_description=textwrap.dedent("""\
    Only nearest neighbor (``order=0``), linear interpolation (``order=1``) and
    modes ``'constant'``, ``'nearest'``, ``'wrap'`` ``'mirror'`` and ``'reflect'`` are currently supported.
    Note that interpolation near boundaries differs from the scipy function,
    because we fixed an outstanding bug (https://github.com/scipy/scipy/issues/2640);
    this function interprets the ``mode`` argument as documented by SciPy, but
    not as implemented by SciPy.
    """))
def map_coordinates(
    input, coordinates, order, mode='constant', cval=0.0,
):
  return _map_coordinates(input, coordinates, order, mode, cval)
