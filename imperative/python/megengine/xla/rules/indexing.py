from collections import namedtuple
from enum import IntEnum
from typing import Any, List, NamedTuple, Optional, Sequence, Tuple, Union

import numpy as np

from ...core._imperative_rt import ops as mops
from .. import ir_utils
from ..lib.mlir import ir
from ..lib.mlir.dialects import hlo
from .hlotensor import HLOTensor
from .utils import _parse_var_as_value, register_lower_rule


"""
case1: idx is a int - x[1]
module @jit_index {
  func.func public @main(%arg0: tensor<16x128x224x224xf32> {mhlo.sharding = ""}) -> tensor<128x224x224xf32> {
    %0 = mhlo.constant dense<1> : tensor<i32>
    %1 = mhlo.constant dense<0> : tensor<i32>
    %2 = mhlo.constant dense<0> : tensor<i32>
    %3 = mhlo.constant dense<0> : tensor<i32>
    %4 = "mhlo.dynamic_slice"(%arg0, %0, %1, %2, %3) {slice_sizes = dense<[1, 128, 224, 224]> : tensor<4xi64>} : (tensor<16x128x224x224xf32>, tensor<i32>, tensor<i32>, tensor<i32>, tensor<i32>) -> tensor<1x128x224x224xf32>
    %5 = mhlo.reshape %4 : (tensor<1x128x224x224xf32>) -> tensor<128x224x224xf32>
    return %5 : tensor<128x224x224xf32>
  }
}

case2: idx is a slice with step is 1 - x[1:10:1]
module @jit_index {
  func.func public @main(%arg0: tensor<16x128x224x224xf32> {mhlo.sharding = ""}) -> tensor<9x128x224x224xf32> {
    %0 = mhlo.constant dense<1> : tensor<i32>
    %1 = mhlo.constant dense<0> : tensor<i32>
    %2 = mhlo.constant dense<0> : tensor<i32>
    %3 = mhlo.constant dense<0> : tensor<i32>
    %4 = "mhlo.dynamic_slice"(%arg0, %0, %1, %2, %3) {slice_sizes = dense<[9, 128, 224, 224]> : tensor<4xi64>} : (tensor<16x128x224x224xf32>, tensor<i32>, tensor<i32>, tensor<i32>, tensor<i32>) -> tensor<9x128x224x224xf32>
    return %4 : tensor<9x128x224x224xf32>
  }
}

case3: idx is a slice with step is not 1 - x[1:10:2]
module @jit_index {
  func.func public @main(%arg0: tensor<16x128x224x224xf32> {mhlo.sharding = ""}) -> tensor<5x128x224x224xf32> {
    %0 = "mhlo.slice"(%arg0) {limit_indices = dense<[10, 128, 224, 224]> : tensor<4xi64>, start_indices = dense<[1, 0, 0, 0]> : tensor<4xi64>, strides = dense<[2, 1, 1, 1]> : tensor<4xi64>} : (tensor<16x128x224x224xf32>) -> tensor<5x128x224x224xf32>
    return %0 : tensor<5x128x224x224xf32>
  }
}

case4: do index in multi dims - x[1, 10:20, 20:30, 3]
module @jit_index {
  func.func public @main(%arg0: tensor<16x128x224x224xf32> {mhlo.sharding = ""}) -> tensor<10x10xf32> {
    %0 = mhlo.constant dense<1> : tensor<i32>
    %1 = "mhlo.broadcast_in_dim"(%0) {broadcast_dimensions = dense<> : tensor<0xi64>} : (tensor<i32>) -> tensor<1xi32>
    %2 = mhlo.constant dense<10> : tensor<i32>
    %3 = "mhlo.broadcast_in_dim"(%2) {broadcast_dimensions = dense<> : tensor<0xi64>} : (tensor<i32>) -> tensor<1xi32>
    %4 = mhlo.constant dense<20> : tensor<i32>
    %5 = "mhlo.broadcast_in_dim"(%4) {broadcast_dimensions = dense<> : tensor<0xi64>} : (tensor<i32>) -> tensor<1xi32>
    %6 = mhlo.constant dense<3> : tensor<i32>
    %7 = "mhlo.broadcast_in_dim"(%6) {broadcast_dimensions = dense<> : tensor<0xi64>} : (tensor<i32>) -> tensor<1xi32>
    %8 = "mhlo.concatenate"(%1, %3, %5, %7) {dimension = 0 : i64} : (tensor<1xi32>, tensor<1xi32>, tensor<1xi32>, tensor<1xi32>) -> tensor<4xi32>
    %9 = "mhlo.gather"(%arg0, %8) {dimension_numbers = #mhlo.gather<offset_dims = [0, 1], collapsed_slice_dims = [0, 3], start_index_map = [0, 1, 2, 3]>, indices_are_sorted = true, slice_sizes = dense<[1, 10, 10, 1]> : tensor<4xi64>} : (tensor<16x128x224x224xf32>, tensor<4xi32>) -> tensor<10x10xf32>
    return %9 : tensor<10x10xf32>
  }
}

case5: x[:, 1]
module @jit_index {
  func.func public @main(%arg0: tensor<16x128x224x224xf32> {mhlo.sharding = ""}) -> tensor<16x224x224xf32> {
    %0 = mhlo.constant dense<1> : tensor<i32>
    %1 = "mhlo.broadcast_in_dim"(%0) {broadcast_dimensions = dense<> : tensor<0xi64>} : (tensor<i32>) -> tensor<1xi32>
    %2 = "mhlo.gather"(%arg0, %1) {dimension_numbers = #mhlo.gather<offset_dims = [0, 1, 2], collapsed_slice_dims = [1], start_index_map = [1]>, indices_are_sorted = true, slice_sizes = dense<[16, 1, 224, 224]> : tensor<4xi64>} : (tensor<16x128x224x224xf32>, tensor<1xi32>) -> tensor<16x224x224xf32>
    return %2 : tensor<16x224x224xf32>
  }
}

x[:]
return x directly
"""


def _is_canonicalized_axis(sl: slice, axis_len: int):
    return (
        (0 <= sl.start and sl.start < axis_len)
        and (0 <= sl.stop and sl.stop <= axis_len)
        and (0 < sl.step)
    )


def _canonicalize_slice_with_axis_len(sl: slice, axis_len: int):
    """
    make slice canonicalized: 0 <= sl.start < axis_len and 0 <= sl.stop <= axis_len
    """

    def impl(idx, axis_len):
        if idx < 0:
            idx = idx + axis_len
        assert idx >= 0 and idx <= axis_len, f"{idx}, {axis_len}"
        if idx < 0:
            idx = 0
        if idx > axis_len:
            idx = axis_len
        return idx

    assert isinstance(sl, slice)
    start = impl(sl.start, axis_len)
    stop = impl(sl.stop, axis_len)

    new_sl = slice(start, stop, sl.step)

    assert new_sl.step > 0, "step <= 0 is not supported now"
    assert _is_canonicalized_axis(
        new_sl, axis_len
    ), f"slice {new_sl} is illegal for axis whose length is {axis_len}"
    return new_sl


def _hslice_with_step_is_one(inp, slices):
    """
    if inp_shape is N-dim, slices should contain N slice, slice can not None.
    for shape [12, 15], slices can be [slice(0, 3, 1), slice(12, 15, 1)].
    the step of slice should must be 1
    """
    assert all([sl.step == 1 for sl in slices])
    starts = [int(sl.start) for sl in slices]
    slice_sizes = [int(max(0, sl.stop - sl.start)) for sl in slices]

    starts = [ir_utils.ir_constant(si) for si in starts]
    slice_sizes = ir_utils.dense_int_elements(slice_sizes)

    return hlo.DynamicSliceOp(inp, starts, slice_sizes).results


def _hslice_with_any_step(inp, slices):
    """
    if inp_shape is N-dim, slices should contain N slice, slice can not None
    for shape [12, 15], slices can be [slice(0, 3, 1), slice(12, 15, 1)]
    """
    starts = [int(sl.start) for sl in slices]
    stops = [int(sl.stop) for sl in slices]
    steps = [int(sl.step) for sl in slices]

    return hlo.SliceOp(
        inp,
        ir_utils.dense_int_elements(starts),
        ir_utils.dense_int_elements(stops),
        ir_utils.dense_int_elements(steps),
    ).results


def index_with_slices(inp, slices):
    """
    if inp_shape is N-dim, slices should contain N slice, slice can be None
    for shape [12, 15], slices can be [slice(0, 3, 1), slice(12, 15, 1)] or [None, None]
    """
    assert isinstance(slices, Sequence), f"{slices}"
    assert len(inp.shape) >= len(slices), f"{inp.shape}, {slices}"
    slices = list(slices) + [None,] * (len(inp.shape) - len(slices))

    slices = [
        sl if sl is not None else slice(0, axis_len, 1)
        for (sl, axis_len) in zip(slices, inp.shape)
    ]
    slices = [
        _canonicalize_slice_with_axis_len(sl, axis_len)
        for (sl, axis_len) in zip(slices, inp.shape)
    ]

    all_step_is_one = all(sl.step == 1 for sl in slices)
    if all_step_is_one:
        return HLOTensor(_hslice_with_step_is_one(inp.tensor, slices))
    else:
        return HLOTensor(_hslice_with_any_step(inp.tensor, slices))


class IndexType(IntEnum):
    DEFAULT = (0,)
    INT = (1,)
    SLICE = (2,)
    NONE = (3,)
    ELLIPSIS = (4,)


def _parse_subtensor_items_as_slices(srcitems, inp_shape, idx_vars):
    inp_ndim = len(inp_shape)

    Item = namedtuple("Item", "axis axis_len slice_or_idx")
    items = []
    inp_offset = 0
    for item in srcitems:
        #  items for: axis, start, step, end, is_index
        axis, has_start, has_stop, has_step, is_idx = item
        axis_len = inp_shape[axis]
        is_slice = has_start or has_stop or has_step
        assert is_slice ^ is_idx, f"cannot specify index idx and slice simultaneously"

        if is_slice:
            start, stop, step = 0, axis_len, 1
            if has_start:
                start = _parse_var_as_value(idx_vars[inp_offset])
                inp_offset += 1
            if has_stop:
                stop = _parse_var_as_value(idx_vars[inp_offset])
                inp_offset += 1
            if has_step:
                step = _parse_var_as_value(idx_vars[inp_offset])
                inp_offset += 1
            sl = _canonicalize_slice_with_axis_len(slice(start, stop, step), axis_len)
            items.append(Item(axis, axis_len, sl))
        elif is_idx:
            idx = _parse_var_as_value(idx_vars[inp_offset])
            inp_offset += 1
            if idx < 0:
                idx = idx + axis_len
            assert (
                0 <= idx and idx < axis_len
            ), f"idx {idx} out of range, shape {inp_shape}, axis {axis}"
            items.append(Item(axis, axis_len, idx))
        else:
            assert False

    slices = [None,] * inp_ndim
    indices_type = [IndexType.DEFAULT,] * inp_ndim
    for item in items:
        # if item.slice_or_idx is int, that means it is a index, not a slice, so we need to reshape the result
        if isinstance(item.slice_or_idx, int):
            slices[item.axis] = slice(item.slice_or_idx, item.slice_or_idx + 1, 1)
            indices_type[item.axis] = IndexType.INT
        else:
            slices[item.axis] = item.slice_or_idx
            indices_type[item.axis] = IndexType.SLICE
    return (
        slices,
        indices_type,
        any([isinstance(item.slice_or_idx, int) for item in items]),
    )


@register_lower_rule(mops.Subtensor)
def subtensor_lower(
    ctx, *args: Union[ir.Value, Sequence[ir.Value]], explicit_type=False
):
    assert len(ctx.op.slice_items) == 0 and len(ctx.vars_out) == 1
    opr, inp, inp_shape = ctx.op, args[0], ctx.vars_in[0].shape
    slices, _, any_axis_is_index = _parse_subtensor_items_as_slices(
        opr.items, inp_shape, ctx.vars_in[1:]
    )
    oup = index_with_slices(inp, slices)

    if any_axis_is_index:
        return oup.reshape(ctx.vars_out[0].shape)
    else:
        return oup


"""
# x.shape = (32, 16, 8), y.shape = (8,): x[10, 13] = y
_Indexer(
    slice_shape=[8],
    gather_slice_shape=[1, 1, 8],
    gather_indices=Array([10, 13], dtype=int32),
    dnums=GatherDimensionNumbers(
        offset_dims=(0,), collapsed_slice_dims=(0, 1), start_index_map=(0, 1)
    ),
    unique_indices=True,
    indices_are_sorted=True,
    reversed_y_dims=[],
    newaxis_dims=(),
)

# x.shape = (32, 16, 8), y.shape = (8,): x[10, slice(0, 3, 2)] = y
_Indexer(
    slice_shape=[2, 8],
    gather_slice_shape=[1, 1, 8],
    gather_indices=Array([[10, 0], [10, 2]], dtype=int32),
    dnums=GatherDimensionNumbers(
        offset_dims=(1,), collapsed_slice_dims=(0, 1), start_index_map=(0, 1)
    ),
    unique_indices=True,
    indices_are_sorted=True,
    reversed_y_dims=[],
    newaxis_dims=(),
)

# x.shape = (32, 16, 8), y.shape = (2, 8,): x[10, slice(0, 3, 2)] = y
_Indexer(
    slice_shape=[2, 8],
    gather_slice_shape=[1, 1, 8],
    gather_indices=Array([[10, 0], [10, 2]], dtype=int32),
    dnums=GatherDimensionNumbers(
        offset_dims=(1,), collapsed_slice_dims=(0, 1), start_index_map=(0, 1)
    ),
    unique_indices=True,
    indices_are_sorted=True,
    reversed_y_dims=[],
    newaxis_dims=(),
)


# x.shape = (32, 16, 8), y.shape = (1,): x[10, slice(0, 3, 2)] = y
_Indexer(
    slice_shape=[2, 8],
    gather_slice_shape=[1, 1, 8],
    gather_indices=Array([[10, 0], [10, 2]], dtype=int32),
    dnums=GatherDimensionNumbers(
        offset_dims=(1,), collapsed_slice_dims=(0, 1), start_index_map=(0, 1)
    ),
    unique_indices=True,
    indices_are_sorted=True,
    reversed_y_dims=[],
    newaxis_dims=(),
)

# x.shape = (32, 16, 8), y.shape = (32, 2, 8): x[:, slice(0, 3, 2)] = y
_Indexer(
    slice_shape=[32, 2, 8],
    gather_slice_shape=[32, 1, 8],
    gather_indices=Array([[0], [2]], dtype=int32),
    dnums=GatherDimensionNumbers(
        offset_dims=(0, 2), collapsed_slice_dims=(1,), start_index_map=(1,)
    ),
    unique_indices=True,
    indices_are_sorted=True,
    reversed_y_dims=[],
    newaxis_dims=(),
)
"""


class GatherDimensionNumbers(NamedTuple):
    offset_dims: Tuple[int, ...]
    collapsed_slice_dims: Tuple[int, ...]
    start_index_map: Tuple[int, ...]


class _Indexer(NamedTuple):
    # The expected shape of the slice output.
    slice_shape: Sequence[int]
    # The slice shape to pass to lax.gather().
    gather_slice_shape: Sequence[int]
    # The gather indices to use.
    gather_indices: Any
    # A GatherDimensionNumbers object describing the gather to perform.
    dnums: GatherDimensionNumbers

    # Are the gather_indices known to be non-overlapping and/or sorted?
    # (In practice, these translate to "there no advanced indices", because
    # only advanced indices could lead to index repetition.)
    unique_indices: bool
    indices_are_sorted: bool

    # Slice dimensions that have negative strides, and so must be reversed after
    # the gather.
    reversed_y_dims: Sequence[int]

    # Keep track of any axes created by `newaxis`. These must be inserted for
    # gathers and eliminated for scatters.
    newaxis_dims: Sequence[int]


class ScatterDimensionNumbers(NamedTuple):
    update_window_dims: Sequence[int]
    inserted_window_dims: Sequence[int]
    scatter_dims_to_operand_dims: Sequence[int]


def _static_idx(idx: slice, size):
    if isinstance(size, int):
        start, stop, step = idx.indices(size)
    else:
        raise TypeError(size)

    if (step < 0 and stop >= start) or (step > 0 and start >= stop):
        return 0, 0, 1, False  # sliced to size zero

    if step > 0:
        return start, stop, step, False
    else:
        k = (start - stop - 1) % (-step)
        return stop + k + 1, start + 1, -step, True


def _index_to_gather(
    x_shape, indices, indices_type, normalize_indices: bool = True
) -> _Indexer:
    assert len(indices) == len(indices_type), f"{len(indices)}, {len(indices_type)}"
    assert len(indices) == len(x_shape), f"{len(indices)}, {len(x_shape)}   "

    advanced_indexes: Optional[Sequence[Union[Sequence, np.ndarray]]] = None
    x_axis = 0  # Current axis in x.
    y_axis = 0  # Current axis in y, before collapsing. See below.
    collapsed_y_axis = 0  # Current axis in y, after collapsing.

    # Scatter dimension numbers.
    offset_dims: Sequence[int] = []
    collapsed_slice_dims: Sequence[int] = []
    start_index_map: Sequence[int] = []
    index_dtype = np.int32

    # Gather indices.
    # Pairs of (array, start_dim) values. These will be broadcast into
    # gather_indices_shape, with the array dimensions aligned to start_dim, and
    # then concatenated.
    gather_indices: List[Tuple[Sequence, int]] = []
    gather_indices_shape: List[int] = []

    # We perform three transformations to y before the scatter op, in order:
    # First, y is broadcast to slice_shape. In general `y` only need broadcast to
    # the right shape.
    slice_shape: Sequence[int] = []

    # Next, y is squeezed to remove newaxis_dims. This removes np.newaxis/`None`
    # indices, which the scatter cannot remove itself.
    newaxis_dims: Sequence[int] = []

    # Finally, we reverse reversed_y_dims to handle slices with negative strides.
    reversed_y_dims: Sequence[int] = []

    gather_slice_shape: Sequence[int] = []

    for i, (idx, idx_type) in enumerate(zip(indices, indices_type)):
        if idx is None:
            assert idx_type == IndexType.DEFAULT
            indices_type[i] = IndexType.SLICE
            indices[i] = slice(None, None, None)

    for idx, idx_type in zip(indices, indices_type):
        # Handle basic int indexes.
        if idx_type == IndexType.INT:
            gather_indices.append(
                (np.array(idx.start, index_dtype), len(gather_indices_shape))
            )
            collapsed_slice_dims.append(x_axis)
            gather_slice_shape.append(1)
            start_index_map.append(x_axis)
            x_axis += 1
        # # Handle np.newaxis (None)
        # elif idx_type == IndexType.NONE:
        #     slice_shape.append(1)
        #     newaxis_dims.append(y_axis)
        #     y_axis += 1
        elif idx_type == IndexType.SLICE:
            # Normalize the slice to use None when possible
            start, stop, step = idx.start, idx.stop, idx.step
            # Handle slice(None) and slice(None, None, -1)
            if (
                start is None
                and stop is None
                and (step is None or isinstance(step, int) and step == -1)
            ):
                if step == -1:
                    reversed_y_dims.append(collapsed_y_axis)
                slice_shape.append(x_shape[x_axis])
                gather_slice_shape.append(x_shape[x_axis])
                offset_dims.append(collapsed_y_axis)
                collapsed_y_axis += 1
                y_axis += 1
                x_axis += 1
            # Handle slice index (only static, otherwise an error is raised)
            else:
                start, limit, stride, needs_rev = _static_idx(
                    slice(start, stop, step), x_shape[x_axis]
                )
                if needs_rev:
                    reversed_y_dims.append(collapsed_y_axis)
                if stride == 1:
                    idx = np.array(start, index_dtype)
                    gather_indices.append((idx, len(gather_indices_shape)))
                    slice_shape.append(limit - start)
                    gather_slice_shape.append(limit - start)
                    offset_dims.append(collapsed_y_axis)
                    start_index_map.append(x_axis)
                else:
                    idx = np.arange(start, limit, stride, dtype=index_dtype)
                    size = idx.shape[0]
                    slice_shape.append(size)
                    gather_slice_shape.append(1)
                    gather_indices.append((idx, len(gather_indices_shape)))
                    gather_indices_shape.append(size)

                    start_index_map.append(x_axis)
                    collapsed_slice_dims.append(x_axis)

                collapsed_y_axis += 1
                y_axis += 1
                x_axis += 1
        else:
            msg = "Indexing mode not yet supported. Open a feature request!\n{}"
            raise IndexError(msg.format(indices))

    if len(gather_indices) == 0:
        gather_indices_array = np.zeros((0,), dtype=index_dtype)
    elif len(gather_indices) == 1:
        g, _ = gather_indices[0]
        gather_indices_array = np.expand_dims(g, (g.ndim,))
    else:
        last_dim = len(gather_indices_shape)
        gather_indices_shape.append(1)

        def _broadcast_to(src, tgt_shape, axises):
            src_shape = src.shape
            expanded_src_shape = [1,] * len(tgt_shape)
            for i, ax in enumerate(axises):
                expanded_src_shape[ax] = src_shape[i]
            src = np.reshape(src, expanded_src_shape)
            return np.broadcast_to(src, tgt_shape)

        gather_indices_array = np.concatenate(
            [
                _broadcast_to(g, gather_indices_shape, tuple(range(i, i + g.ndim)))
                for g, i in gather_indices
            ],
            last_dim,
        )

    dnums = GatherDimensionNumbers(
        offset_dims=tuple(offset_dims),
        collapsed_slice_dims=tuple(sorted(collapsed_slice_dims)),
        start_index_map=tuple(start_index_map),
    )
    return _Indexer(
        slice_shape=slice_shape,
        newaxis_dims=tuple(newaxis_dims),
        gather_slice_shape=gather_slice_shape,
        reversed_y_dims=reversed_y_dims,
        dnums=dnums,
        gather_indices=gather_indices_array,
        unique_indices=advanced_indexes is None,
        indices_are_sorted=advanced_indexes is None,
    )


def scatter(
    x,
    indices,
    y,
    dnums,
    oup_var=None,
    indices_are_sorted=False,
    unique_indices=False,
    mode=None,
):
    scatter_dnums = hlo.ScatterDimensionNumbers.get(
        update_window_dims=list(dnums.update_window_dims),
        inserted_window_dims=list(dnums.inserted_window_dims),
        scattered_dims_to_operand_dims=list(dnums.scatter_dims_to_operand_dims),
        index_vector_dim=len(indices.shape) - 1,
    )
    if oup_var is not None:
        oshape, odtype = oup_var.shape, oup_var.dtype
    else:
        oshape, odtype = x.shape, x.dtype

    op = hlo.ScatterOp(
        ir_utils.make_ir_type_according_meta_tuple(oshape, odtype),
        [x.tensor],
        ir_utils.ir_constant(indices),
        [y.tensor],
        scatter_dnums,
        indices_are_sorted=ir.BoolAttr.get(indices_are_sorted),
        unique_indices=ir.BoolAttr.get(unique_indices),
    )

    scalar_type = ir_utils.make_ir_type_according_meta(tuple(), odtype)
    update = op.update_computation.blocks.append(scalar_type, scalar_type)

    with ir.InsertionPoint(update):
        hlo.ReturnOp((update.arguments[1],))
    return HLOTensor(op.results)


@register_lower_rule(mops.SetSubtensor)
def setsubtensor_lower(ctx, *args: Union[HLOTensor, Sequence[HLOTensor]]):
    assert len(ctx.vars_out) == 1
    opr, x, y = ctx.op, args[0], args[1]

    slices, indices_type, _ = _parse_subtensor_items_as_slices(
        opr.items, x.shape, ctx.vars_in[2:]
    )

    indexer = _index_to_gather(x.shape, slices, indices_type)
    if len(indexer.slice_shape) == 0 or np.prod(indexer.slice_shape) == 0:
        return [x]

    y = y.broadcast_to(indexer.slice_shape)
    if len(indexer.newaxis_dims) != 0:
        assert False, "not support"
    if len(indexer.reversed_y_dims) != 0:
        assert False, "not support"

    dnums = ScatterDimensionNumbers(
        update_window_dims=indexer.dnums.offset_dims,
        inserted_window_dims=indexer.dnums.collapsed_slice_dims,
        scatter_dims_to_operand_dims=indexer.dnums.start_index_map,
    )

    out = scatter(
        x,
        indexer.gather_indices,
        y,
        dnums,
        indices_are_sorted=indexer.indices_are_sorted,
        unique_indices=indexer.unique_indices,
        mode=None,
    )
    return out


def _check_tensor_indexing_arg(src, index, axis):
    assert src.ndim - 1 == index.ndim, f"{src.shape} {index.shape}"
    assert axis < src.ndim and 0 <= axis, f"invalid axis {axis} for shape {src.shape}"

    src_shape = list(src.shape)
    del src_shape[axis]
    assert src_shape == list(index.shape), f"{src.shape} {index.shape} {axis}"

    assert str(index.dtype) in [
        "int16",
        "int32",
        "int64",
        "uint16",
        "uint32",
        "uint64",
    ], f"{index.dtype}"


def indexing_with_tensor_index(src, index, axis=-1, keepdims=False):
    """
    indexing select items from src according to index in one dimension.
    src.ndim should equal to index.ndim + 1.
    if the src.shape remove the axis-th element, it should equal to index.shape.
    for example:
        src.shape=(2, 4, 6), index.shape=(2, 4), axis=2, out.shape=(2, 4, 1), out[i, j, 1] = src[i, j, index[i, j]]
        src.shape=(2, 4, 6), index.shape=(2, 6), axis=1, out.shape=(2, 1, 6), out[i, 1, j] = src[i, index[i, j], j]
        src.shape=(3, 9), index.shape=(3,), axis=1, out.shape=(3, 1), out[i, 1] = src[i, index[i]]
        src.shape=(3, 9), index.shape=(9,), axis=0, out.shape=(1, 9), out[1, i] = src[index[i], i]
    """
    axis = (axis + src.ndim) if axis < 0 else axis
    _check_tensor_indexing_arg(src, index, axis)

    arange_array = np.arange(src.shape[axis], dtype=index.dtype)
    arange_array = HLOTensor(arange_array).broadcast_to(
        src.shape, broadcast_dims=[axis]
    )
    broadcast_dims = [i for i in range(src.ndim) if i != axis]
    index_array = index.broadcast_to(src.shape, broadcast_dims=broadcast_dims)

    mask = (arange_array == index_array).astype(src.dtype)
    return (src * mask).sum(axis, keepdims=keepdims)


@register_lower_rule(mops.IndexingOneHot)
def indexing_one_hot_lower(ctx, *args: Union[HLOTensor, Sequence[HLOTensor]]):
    assert (
        len(ctx.vars_out) == 1 and len(ctx.vars_in) == 2 and len(args) == 2
    ), f"{len(ctx.vars_out)}, {len(ctx.vars_in)}, {len(args)}"
    assert ctx.op.ndim == args[0].ndim, f"{ctx.op.ndim}, {args[0].shape}"
    return indexing_with_tensor_index(args[0], args[1], ctx.op.axis, keepdims=True)


def indexing_set_with_tensor_index(src, value, index, axis):
    """
    indexing set value to src according to index in one dimension. 
    value shape should can be broadcast or reshape to index shape.
    if value shape not equal to index shape, it should be broadcast to index shape firstly
    examples:
        src.shape=(2, 4, 6), value.shape=(2, 4), index.shape=(2, 4), axis=2, out.shape=(2, 4, 6)
        out[i, j, k] = value[i, j] if k == index[i, j] else src[i, j, k]

        src.shape=(2, 4, 6), value.shape=(2, 6), index.shape=(2, 6), axis=1, out.shape=(2, 4, 6)
        out[i, j, k] = value[i, k] if j == index[i, k] else src[i, j, k]
    """
    axis = (axis + src.ndim) if axis < 0 else axis
    _check_tensor_indexing_arg(src, index, axis)

    value = value if isinstance(value, HLOTensor) else HLOTensor(value)
    assert src.dtype == value.dtype, f"{src.dtype}, {value.dtype}"

    arange_array = np.arange(src.shape[axis]).astype(index.dtype)
    arange_array = HLOTensor(arange_array).broadcast_to(src.shape, [axis])
    broadcast_dims = [i for i in range(src.ndim) if i != axis]
    index_array = index.broadcast_to(src.shape, broadcast_dims=broadcast_dims)

    mask1 = (arange_array == index_array).astype(src.dtype)
    mask2 = (arange_array != index_array).astype(value.dtype)

    return mask1 * value + mask2 * src


@register_lower_rule(mops.IndexingSetOneHot)
def indexing_set_one_hot_lower(ctx, *args: Union[HLOTensor, Sequence[HLOTensor]]):
    assert (
        len(ctx.vars_out) == 1 and len(ctx.vars_in) == 3 and len(args) == 3
    ), f"{len(ctx.vars_out)}, {len(ctx.vars_in)}, {len(args)}"

    assert ctx.op.ndim == args[0].ndim, f"{ctx.op.ndim}, {args[0].shape}"
    return indexing_set_with_tensor_index(args[0], args[2], args[1], ctx.op.axis)
