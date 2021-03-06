# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
# pylint: disable=invalid-name, no-member, too-many-locals, too-many-arguments, too-many-statements, singleton-comparison, unused-argument, no-else-return
"""Sort related operators """
import tvm
from tvm import te
from tvm._ffi import get_global_func

from .injective import schedule_injective_from_existing
from ..math import identity
from ..transform import strided_slice, transpose
from .. import tag


def swap(arr, axis):
    """ swap arr[axis] and arr[-1] """
    return arr[:axis] + [arr[-1]] + arr[axis + 1 : -1] + [arr[axis]]


def _schedule_sort(outs):
    """Schedule for argsort operator.

    Parameters
    ----------
    outs: Array of Tensor
        The computation graph description of argsort
        in the format of an array of tensors.

    Returns
    -------
    s: Schedule
      The computation schedule for the op.
    """
    outs = [outs] if isinstance(outs, te.tensor.Tensor) else outs
    s = te.create_schedule([x.op for x in outs])
    scheduled_ops = []

    def traverse(op):
        if tag.is_injective(op.tag):
            schedule_injective_from_existing(s, op.output(0))
        for tensor in op.input_tensors:
            if tensor.op.input_tensors and tensor.op not in scheduled_ops:
                traverse(tensor.op)
        scheduled_ops.append(op)

    for out in outs:
        traverse(out.op)
    return s


def sort_ir(
    data, values_out, values_out_swap, axis, is_ascend, indices_out=None, indices_out_swap=None
):
    """Low level IR to do nms sorting on the GPU, same usage as tvm.contrib.sort.argsort on the CPU.

    Parameters
    ----------
    data: Buffer
        Buffer of input data. Data will be sorted in place.

    values_out : Buffer
        Output buffer of values of sorted tensor with same shape as data.

    values_out_swap : Buffer
        Output buffer of values with same shape as data to use as swap.

    axis : Int
        Axis long which to sort the input tensor.

    is_ascend : Boolean
        Whether to sort in ascending or descending order.

    indicess_out : Buffer
        Output buffer of indices of sorted tensor with same shape as data.

    indices_out_swap : Buffer
        Output buffer of indices with same shape as data to use as swap.

    Returns
    -------
    stmt : Stmt
        The result IR statement.
    """

    def ceil_div(a, b):
        return tvm.tir.indexdiv(a + b - 1, b)

    axis_mul_before = 1
    axis_mul_after = 1
    shape = data.shape
    if axis < 0:
        axis = len(shape) + axis
    for i, value in enumerate(shape, 0):
        if i < axis:
            axis_mul_before *= value
        elif i > axis:
            axis_mul_after *= value

    ib = tvm.tir.ir_builder.create()

    data = ib.buffer_ptr(data)
    values_out = ib.buffer_ptr(values_out)
    values_out_swap = ib.buffer_ptr(values_out_swap)
    if indices_out is not None:
        indices_out = ib.buffer_ptr(indices_out)
        assert indices_out_swap is not None
        indices_out_swap = ib.buffer_ptr(indices_out_swap)

    # Set up threading
    max_threads = int(tvm.target.Target.current(allow_none=False).max_num_threads)
    nthread_tx = max_threads
    nthread_bx = ceil_div(shape[axis], max_threads)
    nthread_by = axis_mul_before
    nthread_bz = axis_mul_after

    # Copy the data to initial output
    with ib.new_scope():
        tx = te.thread_axis("threadIdx.x")
        bx = te.thread_axis("blockIdx.x")
        ib.scope_attr(tx, "thread_extent", nthread_tx)
        ib.scope_attr(bx, "thread_extent", nthread_bx)
        tid = bx * nthread_tx + tx

        by = te.thread_axis("blockIdx.y")
        bz = te.thread_axis("blockIdx.z")
        ib.scope_attr(by, "thread_extent", nthread_by)
        ib.scope_attr(bz, "thread_extent", nthread_bz)
        idx = (by * shape[axis] + tid) * axis_mul_after + bz
        with ib.if_scope(tid < shape[axis]):
            values_out[idx] = data[idx]
            if indices_out is not None:
                indices_out[idx] = tvm.tir.generic.cast(tid, indices_out.dtype)

    ## we are looping over the array doing mergesort from the bottom up.
    ## The outer loop runs on the host and launches a cuda kernel for each iteration
    ## of the algorithm.
    ## The basic idea is that at iteration 0, each thread does sort on 2 elements.
    ## On iteration 1, each thread merges 2 sorted arrays of 2 elements,
    ## to deal with 4 total elements.
    ## On iteration 2, each thread merges 2 sorted arrays of 4 elements,
    ## to deal with 8 total elements. On iteration 3, each thread deals with 16 elements, etc
    ## On the final iteration of the algorithm, one thread will merge two sorted lists
    ## to sort the entire array
    lim = tvm.tir.generic.cast(
        tvm.tir.ceil(tvm.tir.log2(tvm.tir.generic.cast(shape[axis], "float64"))), "int64"
    )
    with ib.for_range(0, lim, dtype="int64") as l2_width:
        width = 2 << l2_width
        # Define and launch the cuda kernel
        with ib.new_scope():
            i = ib.allocate("int64", (1,), name="i", scope="local")
            j = ib.allocate("int64", (1,), name="j", scope="local")
            start = ib.allocate("int64", (1,), name="start", scope="local")
            middle = ib.allocate("int64", (1,), name="middle", scope="local")
            end = ib.allocate("int64", (1,), name="end", scope="local")
            tx = te.thread_axis("threadIdx.x")
            bx = te.thread_axis("blockIdx.x")
            ib.scope_attr(tx, "thread_extent", nthread_tx)
            # Reduce the number of blocks as the work per thread grows
            ib.scope_attr(
                bx,
                "thread_extent",
                tvm.tir.generic.cast(ceil_div(shape[axis], width * max_threads), "int32"),
            )
            tid = bx * nthread_tx + tx

            by = te.thread_axis("blockIdx.y")
            bz = te.thread_axis("blockIdx.z")
            ib.scope_attr(by, "thread_extent", nthread_by)
            ib.scope_attr(bz, "thread_extent", nthread_bz)

            def compare(a, b):
                """
                Compare a and b in proper ascending or descending order
                """
                if is_ascend:
                    out = a <= b
                else:
                    out = b <= a
                return out

            def bottom_up_merge(source, dest, source_idx, dest_idx, start, middle, end, even):
                """
                Merge the two sections of the array assigned to this thread
                """
                # pylint: disable=arguments-out-of-order
                # initialize iterators
                i[0] = start
                j[0] = middle
                # set up indexes
                base_idx = by * shape[axis] * axis_mul_after + bz
                # iterate over the output loop
                with ib.for_range(0, end - start) as k:
                    i_idx = base_idx + i[0] * axis_mul_after
                    j_idx = base_idx + j[0] * axis_mul_after
                    k_idx = base_idx + (k + start) * axis_mul_after

                    def swap_values(source, dest, source_idx, dest_idx):
                        def assign_i():
                            """assign i value to current output"""
                            dest[k_idx] = source[i_idx]
                            if indices_out is not None:
                                dest_idx[k_idx] = source_idx[i_idx]
                            i[0] += 1

                        def assign_j():
                            """assign j value to current output"""
                            dest[k_idx] = source[j_idx]
                            if indices_out is not None:
                                dest_idx[k_idx] = source_idx[j_idx]
                            j[0] += 1

                        ## if both of the iterators are in range
                        with ib.if_scope(tvm.tir.all(i[0] < middle, j[0] < end)):
                            # compare them and insert whichever is next into the output
                            with ib.if_scope(compare(source[i_idx], source[j_idx])):
                                assign_i()
                            with ib.else_scope():
                                assign_j()
                        # otherwise, simply copy the remainder of the valid iterator to the output
                        with ib.else_scope():
                            with ib.if_scope(i[0] < middle):
                                assign_i()
                            with ib.else_scope():
                                assign_j()

                    # Switch which input is the source and which is the destination each iteration
                    with ib.if_scope(even):
                        swap_values(source, dest, source_idx, dest_idx)
                    with ib.else_scope():
                        swap_values(dest, source, dest_idx, source_idx)

            def mergesort(source, dest, source_idx, dest_idx, size, width, even):
                # calculate the start, mid, and end points of this section
                start[0] = width * tid
                with ib.if_scope(start[0] < size):
                    middle[0] = tvm.te.min(start[0] + tvm.tir.indexdiv(width, 2), size)
                    end[0] = tvm.te.min(start[0] + width, size)
                    ## merge the start->middle and middle->end arrays
                    bottom_up_merge(
                        source, dest, source_idx, dest_idx, start[0], middle[0], end[0], even
                    )

            # Call the kernel
            mergesort(
                values_out,
                values_out_swap,
                indices_out,
                indices_out_swap,
                shape[axis],
                width,
                tvm.tir.indexmod(l2_width, 2) == 0,
            )

    ## if the final sorted data ended up in the swap, copy it to the real output
    with ib.if_scope(tvm.tir.indexmod(lim, 2) == 1):
        with ib.new_scope():
            tx = te.thread_axis("threadIdx.x")
            bx = te.thread_axis("blockIdx.x")
            ib.scope_attr(tx, "thread_extent", nthread_tx)
            ib.scope_attr(bx, "thread_extent", nthread_bx)
            tid = bx * nthread_tx + tx

            by = te.thread_axis("blockIdx.y")
            bz = te.thread_axis("blockIdx.z")
            ib.scope_attr(by, "thread_extent", nthread_by)
            ib.scope_attr(bz, "thread_extent", nthread_bz)
            idx = (by * shape[axis] + tid) * axis_mul_after + bz
            with ib.if_scope(tid < shape[axis]):
                idx = (by * shape[axis] + tid) * axis_mul_after + bz
                values_out[idx] = values_out_swap[idx]
                if indices_out is not None:
                    indices_out[idx] = indices_out_swap[idx]

    return ib.get()


def sort_nms_ir(data, valid_count, output, axis, is_ascend):
    """Low level IR to do nms sorting on the GPU, same usage as tvm.contrib.sort.argsort on the CPU.

    Parameters
    ----------
    data: Buffer
        Buffer of input data.

    valid_count : Buffer
        1D Buffer of number of valid number of boxes.

    output : Buffer
        Output buffer of indicies of sorted tensor with same shape as data.

    axis : Int
        Axis long which to sort the input tensor.

    is_ascend : Boolean
        Whether to sort in ascending or descending order.

    Returns
    -------
    stmt : Stmt
        The result IR statement.
    """

    size = 1
    axis_mul_before = 1
    axis_mul_after = 1
    shape = data.shape
    if axis < 0:
        axis = len(shape) + axis
    for i, value in enumerate(shape, 0):
        size *= value
        if i < axis:
            axis_mul_before *= value
        elif i > axis:
            axis_mul_after *= value
    max_threads = int(tvm.target.Target.current(allow_none=False).max_num_threads)
    ib = tvm.tir.ir_builder.create()
    data = ib.buffer_ptr(data)
    valid_count = ib.buffer_ptr(valid_count)
    output = ib.buffer_ptr(output)
    nthread_tx = max_threads
    nthread_bx = size // max_threads + 1
    tx = te.thread_axis("threadIdx.x")
    bx = te.thread_axis("blockIdx.x")
    ib.scope_attr(tx, "thread_extent", nthread_tx)
    ib.scope_attr(bx, "thread_extent", nthread_bx)
    tid = bx * nthread_tx + tx
    temp_data = ib.allocate("float32", (1,), name="temp_data", scope="local")
    temp_index = ib.allocate("int32", (1,), name="temp_index", scope="local")
    is_ascend = tvm.tir.IntImm("int32", is_ascend)

    idxd = tvm.tir.indexdiv
    idxm = tvm.tir.indexmod

    with ib.for_range(0, axis_mul_before) as i:
        with ib.for_range(0, axis_mul_after) as j:
            current_sort_num = valid_count[i * axis_mul_after + j]
            base_idx = i * shape[axis] * axis_mul_after + j
            with ib.if_scope(tid < shape[axis]):
                output[base_idx + tid * axis_mul_after] = tid
            # OddEvenTransposeSort
            with ib.for_range(0, current_sort_num) as k:
                with ib.if_scope(tid < idxd(current_sort_num + 1, 2)):
                    offset = base_idx + (2 * tid + idxm(k, 2)) * axis_mul_after
                    with ib.if_scope(
                        tvm.tir.all(
                            is_ascend == 1,
                            2 * tid + idxm(k, 2) + 1 < current_sort_num,
                            data[offset] > data[offset + axis_mul_after],
                        )
                    ):
                        temp_data[0] = data[offset]
                        data[offset] = data[offset + axis_mul_after]
                        data[offset + axis_mul_after] = temp_data[0]
                        temp_index[0] = output[offset]
                        output[offset] = output[offset + axis_mul_after]
                        output[offset + axis_mul_after] = temp_index[0]
                    with ib.if_scope(
                        tvm.tir.all(
                            is_ascend == 0,
                            2 * tid + idxm(k, 2) + 1 < current_sort_num,
                            data[offset] < data[offset + axis_mul_after],
                        )
                    ):
                        temp_data[0] = data[offset]
                        data[offset] = data[offset + axis_mul_after]
                        data[offset + axis_mul_after] = temp_data[0]
                        temp_index[0] = output[offset]
                        output[offset] = output[offset + axis_mul_after]
                        output[offset + axis_mul_after] = temp_index[0]
                ib.emit(tvm.tir.Call(None, "tir.tvm_storage_sync", tvm.runtime.convert(["shared"])))

    return ib.get()


def argsort_nms_thrust(data, valid_count, axis=-1, is_ascend=1, dtype="float32"):
    """Performs sorting along the given axis and returns an array of indicies
    having same shape as an input array that index data in sorted order.

    Parameters
    ----------
    data: tvm.te.Tensor
        The input array.

    valid_count : tvm.te.Tensor, optional
        The number of valid elements to be sorted.

    axis : int, optional
        Axis long which to sort the input tensor.

    is_ascend : boolean, optional
        Whether to sort in ascending or descending order.

    dtype : string, optional
        DType of the output indices.

    Returns
    -------
    out : tvm.te.Tensor
        The output of this function.
    """
    ndim = len(data.shape)
    if axis < 0:
        axis = ndim + axis
    if axis != ndim - 1:
        # Prepare for sorting along axis -1.
        axes = swap(list(range(ndim)), axis)
        data = transpose(data, axes)

    data_buf = tvm.tir.decl_buffer(data.shape, data.dtype, "data_buf", data_alignment=8)
    valid_count_buf = tvm.tir.decl_buffer(
        valid_count.shape, valid_count.dtype, "valid_count_buf", data_alignment=4
    )
    out_bufs = [
        tvm.tir.decl_buffer(data.shape, data.dtype, "value_buf", data_alignment=8),
        tvm.tir.decl_buffer(data.shape, "int32", "indices_buf", data_alignment=8),
    ]
    out = te.extern(
        [data.shape, data.shape],
        [data, valid_count],
        lambda ins, outs: tvm.tir.call_packed(
            "tvm.contrib.thrust.sort_nms", ins[0], ins[1], outs[0], outs[1], is_ascend
        ),
        in_buffers=[data_buf, valid_count_buf],
        out_buffers=out_bufs,
        dtype=[data.dtype, "int32"],
        name="nms_argsort_gpu",
        tag="nms_argsort_gpu",
    )

    if axis != ndim - 1:
        axes = swap(list(range(ndim)), axis)
        out = [transpose(o, axes) for o in out]

    return out[1]


def sort(data, axis=-1, is_ascend=1):
    """Performs sorting along the given axis and returns an array of
    sorted values with the same shape as the input data.

    Parameters
    ----------
    data: tvm.te.Tensor
        The input array.

    axis : int, optional
        Axis long which to sort the input tensor.

    is_ascend : boolean, optional
        Whether to sort in ascending or descending order.

    Returns
    -------
    out : tvm.te.Tensor
        The output of this function.
    """
    value_buf = tvm.tir.decl_buffer(data.shape, data.dtype, "value_buf", data_alignment=8)
    value_buf_swap = tvm.tir.decl_buffer(data.shape, data.dtype, "value_buf_swap", data_alignment=8)
    out = te.extern(
        [data.shape, data.shape],
        [data],
        lambda ins, outs: sort_ir(ins[0], outs[0], outs[1], axis, is_ascend),
        out_buffers=[value_buf, value_buf_swap],
        name="sort_gpu",
        tag="sort_gpu",
    )[0]
    return out


def sort_thrust(data, axis=-1, is_ascend=1):
    """Performs sorting along the given axis and returns an array of
    sorted values with the same shape as the input data.

    Parameters
    ----------
    data: tvm.te.Tensor
        The input array.

    axis : int, optional
        Axis long which to sort the input tensor.

    is_ascend : boolean, optional
        Whether to sort in ascending or descending order.

    Returns
    -------
    out : tvm.te.Tensor
        The output of this function.
    """
    dtype = "float32"

    ndim = len(data.shape)
    axis = ndim + axis if axis < 0 else axis

    if axis != ndim - 1:
        # Prepare for sorting along axis -1.
        axes = swap(list(range(ndim)), axis)
        data = transpose(data, axes)

    value_buf = tvm.tir.decl_buffer(data.shape, data.dtype, "value_buf", data_alignment=8)
    indices_buf = tvm.tir.decl_buffer(data.shape, dtype, "out_buf", data_alignment=8)
    out = te.extern(
        [data.shape, data.shape],
        [data],
        ## TODO(mbrookhart): This thrust function is actually doing argsort, not sort
        ## For performance, we should probably rename the contrib function and add
        ## a pure sort
        lambda ins, outs: tvm.tir.call_packed(
            "tvm.contrib.thrust.sort", ins[0], outs[0], outs[1], is_ascend
        ),
        out_buffers=[value_buf, indices_buf],
        name="sort_gpu",
        tag="sort_gpu",
    )[0]

    if axis != ndim - 1:
        axes = swap(list(range(ndim)), axis)
        out = transpose(out, axes)
    return out


def argsort(data, valid_count=None, axis=-1, is_ascend=1, dtype="float32"):
    """Performs sorting along the given axis and returns an array of indicies
    having same shape as an input array that index data in sorted order.

    Parameters
    ----------
    data: tvm.te.Tensor
        The input array.

    valid_count : tvm.te.Tensor, optional
        The number of valid elements to be sorted.

    axis : int, optional
        Axis long which to sort the input tensor.

    is_ascend : boolean, optional
        Whether to sort in ascending or descending order.

    dtype : string, optional
        DType of the output indices.

    Returns
    -------
    out : tvm.te.Tensor
        The output of this function.
    """
    if valid_count is not None:
        sorted_data = identity(data)
        sorted_data_buf = tvm.tir.decl_buffer(
            data.shape, data.dtype, "sorted_data_buf", data_alignment=8
        )
        valid_count_buf = tvm.tir.decl_buffer(
            valid_count.shape, valid_count.dtype, "valid_count_buf", data_alignment=4
        )
        out_buf = tvm.tir.decl_buffer(data.shape, "int32", "out_buf", data_alignment=4)
        out = te.extern(
            [data.shape],
            [sorted_data, valid_count],
            lambda ins, outs: sort_nms_ir(ins[0], ins[1], outs[0], axis, is_ascend),
            dtype="int32",
            in_buffers=[sorted_data_buf, valid_count_buf],
            out_buffers=[out_buf],
            name="argsort_nms_gpu",
            tag="argsort_nms_gpu",
        )
    else:
        value_buf = tvm.tir.decl_buffer(data.shape, data.dtype, "value_buf", data_alignment=8)
        value_swap_buf = tvm.tir.decl_buffer(
            data.shape, data.dtype, "value_swap_buf", data_alignment=8
        )
        indices_buf = tvm.tir.decl_buffer(data.shape, dtype, "out_buf", data_alignment=8)
        indices_swap_buf = tvm.tir.decl_buffer(data.shape, dtype, "out_swap_buf", data_alignment=8)
        out = te.extern(
            [data.shape, data.shape, data.shape, data.shape],
            [data],
            lambda ins, outs: sort_ir(
                ins[0],
                outs[0],
                outs[2],
                axis,
                is_ascend,
                indices_out=outs[1],
                indices_out_swap=outs[3],
            ),
            out_buffers=[value_buf, indices_buf, value_swap_buf, indices_swap_buf],
            name="argsort_gpu",
            tag="argsort_gpu",
        )[1]
    return out


def argsort_thrust(data, valid_count=None, axis=-1, is_ascend=1, dtype="float32"):
    """Performs sorting along the given axis and returns an array of indicies
    having same shape as an input array that index data in sorted order.

    Parameters
    ----------
    data: tvm.te.Tensor
        The input array.

    valid_count : tvm.te.Tensor, optional
        The number of valid elements to be sorted.

    axis : int, optional
        Axis long which to sort the input tensor.

    is_ascend : boolean, optional
        Whether to sort in ascending or descending order.

    dtype : string, optional
        DType of the output indices.

    Returns
    -------
    out : tvm.te.Tensor
        The output of this function.
    """
    if valid_count is not None:
        out = argsort_nms_thrust(data, valid_count, axis, is_ascend, dtype)
    else:
        out = topk_thrust(data, 0, axis, "indices", is_ascend, dtype)
    return out


def schedule_sort(outs):
    """Schedule for sort operator.

    Parameters
    ----------
    outs: Array of Tensor
        The computation graph description of argsort
        in the format of an array of tensors.

    Returns
    -------
    s: Schedule
      The computation schedule for the op.
    """
    return _schedule_sort(outs)


def schedule_argsort(outs):
    """Schedule for argsort operator.

    Parameters
    ----------
    outs: Array of Tensor
        The computation graph description of argsort
        in the format of an array of tensors.

    Returns
    -------
    s: Schedule
      The computation schedule for the op.
    """
    return _schedule_sort(outs)


def topk(data, k=1, axis=-1, ret_type="both", is_ascend=False, dtype="int64"):
    """Get the top k elements in an input tensor along the given axis.

    Parameters
    ----------
    data : tvm.te.Tensor
        The input tensor.

    k : int, optional
        Number of top elements to select. Return all elements if k < 1.

    axis : int, optional
        Axis long which to sort the input tensor.

    ret_type: str, optional
        The return type [both, values, indices].
        "both": return both top k data and indices.
        "values": return top k data only.
        "indices": return top k indices only.

    is_ascend : boolean, optional
        Whether to sort in ascending or descending order.

    dtype : string, optional
        The data type of the indices output.

    Returns
    -------
    out : tvm.te.Tensor or List[tvm.te.Tensor]
        The computed result.
    """
    assert ret_type in ["both", "values", "indices"]
    ndim = len(data.shape)
    axis = axis + ndim if axis < 0 else axis
    assert 0 <= axis < ndim
    values_buf = tvm.tir.decl_buffer(data.shape, data.dtype, "values_buf", data_alignment=8)
    values_swap_buf = tvm.tir.decl_buffer(
        data.shape, data.dtype, "values_swap_buf", data_alignment=8
    )
    indices_buf = tvm.tir.decl_buffer(data.shape, dtype, "indices_buf", data_alignment=8)
    indices_swap_buf = tvm.tir.decl_buffer(data.shape, dtype, "indies_swap_buf", data_alignment=8)
    if ret_type == "values":
        output = te.extern(
            [data.shape, data.shape],
            [data],
            lambda ins, outs: sort_ir(ins[0], outs[0], outs[1], axis, is_ascend),
            out_buffers=[values_buf, values_swap_buf],
            name="topk_gpu",
            tag="topk_gpu",
        )[0]
    else:
        output = te.extern(
            [data.shape, data.shape, data.shape, data.shape],
            [data],
            lambda ins, outs: sort_ir(
                ins[0],
                outs[0],
                outs[2],
                axis,
                is_ascend,
                indices_out=outs[1],
                indices_out_swap=outs[3],
            ),
            out_buffers=[values_buf, indices_buf, values_swap_buf, indices_swap_buf],
            name="topk_gpu",
            tag="topk_gpu",
        )[0:2]
    if isinstance(k, int) and k < 1:
        if ret_type == "indices":
            return output[1]
        return output
    beg = [0] * ndim
    end = []
    strides = [1] * ndim
    for i in range(ndim):
        if i == axis:
            end.append(k if isinstance(k, int) else tvm.te.size_var("dim"))
        else:
            end.append(data.shape[i])
    if ret_type == "both":
        values_out, indices_out = output
        values_out = strided_slice(values_out, beg, end, strides)
        indices_out = strided_slice(indices_out, beg, end, strides)
        output = [values_out, indices_out]
    elif ret_type == "values":
        output = [strided_slice(output, beg, end, strides)]
    else:  # ret_type == "indices"
        indices_out = output[1]
        output = [strided_slice(indices_out, beg, end, strides)]
    return output


def topk_thrust(data, k=1, axis=-1, ret_type="both", is_ascend=False, dtype="int64"):
    """Get the top k elements in an input tensor along the given axis.

    Parameters
    ----------
    data : tvm.te.Tensor
        The input tensor.

    k : int, optional
        Number of top elements to select. Return all elements if k < 1.

    axis : int, optional
        Axis long which to sort the input tensor.

    ret_type: str, optional
        The return type [both, values, indices].
        "both": return both top k data and indices.
        "values": return top k data only.
        "indices": return top k indices only.

    is_ascend : boolean, optional
        Whether to sort in ascending or descending order.

    dtype : string, optional
        The data type of the indices output.

    Returns
    -------
    out : tvm.te.Tensor or List[tvm.te.Tensor]
        The computed result.
    """
    assert ret_type in ["both", "values", "indices"]
    ndim = len(data.shape)
    axis = ndim + axis if axis < 0 else axis

    if axis != ndim - 1:
        # Prepare for sorting along axis -1.
        axes = swap(list(range(ndim)), axis)
        data = transpose(data, axes)

    data_buf = tvm.tir.decl_buffer(data.shape, data.dtype, "data_buf", data_alignment=8)
    out_bufs = [
        tvm.tir.decl_buffer(data.shape, data.dtype, "value_buf", data_alignment=8),
        tvm.tir.decl_buffer(data.shape, dtype, "indices_buf", data_alignment=8),
    ]

    is_ascend = 1 if is_ascend else 0

    out = te.extern(
        [data.shape, data.shape],
        [data],
        lambda ins, outs: tvm.tir.call_packed(
            "tvm.contrib.thrust.sort", ins[0], outs[0], outs[1], is_ascend
        ),
        in_buffers=[data_buf],
        out_buffers=out_bufs,
        name="topk_gpu",
        tag="topk_gpu",
    )

    if isinstance(k, tvm.tir.IntImm):
        k = k.value

    if not isinstance(k, int) or k > 0:
        beg = [0] * ndim
        end = data.shape[:-1] + [k if isinstance(k, int) else tvm.te.size_var("dim")]
        strides = [1] * ndim
        out = [strided_slice(o, beg, end, strides) for o in out]

    if axis != ndim - 1:
        axes = swap(list(range(ndim)), axis)
        out = [transpose(o, axes) for o in out]

    if ret_type == "values":
        out = out[0]
    elif ret_type == "indices":
        out = out[1]

    return out


def schedule_topk(outs):
    """Schedule for argsort operator.

    Parameters
    ----------
    outs: Array of Tensor
        The computation graph description of argsort
        in the format of an array of tensors.

    Returns
    -------
    s: Schedule
      The computation schedule for the op.
    """
    return _schedule_sort(outs)


def stable_sort_by_key_thrust(keys, values, for_scatter=False):
    """Sort values with respect to keys using thrust.
    Both keys and values will be sorted and returned.
    Sorting is done via stable sort, so relative ordering among
    ties are preserved.

    Parameters
    ----------
    keys: tvm.te.Tensor
        The 1D input keys.

    values : tvm.te.Tensor,
        The 1D input values.

    for_scatter: bool, optional
        If True, negative keys are interpreted as negative indices.
        Before sorting, negative indices are converted to corresponding positive indices.
        The output keys (indices) are all positive.
        This option is introduced to optimize the scatter implementation.

    Returns
    -------
    keys_sorted : tvm.te.Tensor
        The sorted keys

    values_sorted : tvm.te.Tensor
        The values sorted with respect to the keys
    """
    keys_buf = tvm.tir.decl_buffer(keys.shape, keys.dtype, "keys_buf", data_alignment=8)
    values_buf = tvm.tir.decl_buffer(values.shape, values.dtype, "values_buf", data_alignment=8)
    out_bufs = [
        tvm.tir.decl_buffer(keys.shape, keys.dtype, "keys_buf", data_alignment=8),
        tvm.tir.decl_buffer(keys.shape, values.dtype, "values_buf", data_alignment=8),
    ]
    out = te.extern(
        [keys.shape, values.shape],
        [keys, values],
        lambda ins, outs: tvm.tir.call_packed(
            "tvm.contrib.thrust.stable_sort_by_key", ins[0], ins[1], outs[0], outs[1], for_scatter
        ),
        in_buffers=[keys_buf, values_buf],
        out_buffers=out_bufs,
        dtype=[keys.dtype, values.dtype],
        name="stable_sort_by_key",
        tag="stable_sort_by_key",
    )
    return out[0], out[1]


def is_thrust_available():
    """
    Test if thrust based sorting ops are available.
    """
    return get_global_func("tvm.contrib.thrust.sort", allow_missing=True) is not None
