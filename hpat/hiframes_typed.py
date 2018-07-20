from __future__ import print_function, division, absolute_import

import numpy as np
import warnings
import numba
from numba import ir, ir_utils, types
from numba.ir_utils import (replace_arg_nodes, compile_to_numba_ir,
                            find_topo_order, gen_np_call, get_definition, guard,
                            find_callname, mk_alloc, find_const, is_setitem)

import hpat
from hpat.utils import get_definitions
from hpat.hiframes import include_new_blocks, gen_empty_like
from hpat.str_ext import string_type
from hpat.str_arr_ext import string_array_type, StringArrayType, is_str_arr_typ
from hpat.pd_series_ext import SeriesType, string_series_type, series_to_array_type, BoxedSeriesType

class HiFramesTyped(object):
    """Analyze and transform hiframes calls after typing"""

    def __init__(self, func_ir, typingctx, typemap, calltypes):
        self.func_ir = func_ir
        self.typingctx = typingctx
        self.typemap = typemap
        self.calltypes = calltypes
        self.df_cols = func_ir.df_cols

    def run(self):
        blocks = self.func_ir.blocks
        call_table, _ = ir_utils.get_call_table(blocks)
        topo_order = find_topo_order(blocks)
        for label in topo_order:
            new_body = []
            for inst in blocks[label].body:
                if isinstance(inst, ir.Assign):
                    out_nodes = self._run_assign(inst, call_table)
                    if isinstance(out_nodes, list):
                        new_body.extend(out_nodes)
                    if isinstance(out_nodes, dict):
                        label = include_new_blocks(blocks, out_nodes, label,
                                                   new_body)
                        new_body = []
                    if isinstance(out_nodes, tuple):
                        gen_blocks, post_nodes = out_nodes
                        label = include_new_blocks(blocks, gen_blocks, label,
                                                   new_body)
                        new_body = post_nodes
                else:
                    # replace SetItem series type with array
                    if is_setitem(inst):
                        sig = self.calltypes[inst]
                        arr_typ = series_to_array_type(sig.args[0])
                        sig.args = (arr_typ, *sig.args[1:])

                    new_body.append(inst)
            blocks[label].body = new_body

        replace_series = {}
        for vname, typ in self.typemap.items():
            if isinstance(typ, SeriesType):
                # print("replacing series type", vname)
                new_typ = series_to_array_type(typ)
                replace_series[vname] = new_typ

        for vname, typ in replace_series.items():
            self.typemap.pop(vname)
            self.typemap[vname] = typ

        self.func_ir._definitions = get_definitions(self.func_ir.blocks)
        return

    def _run_assign(self, assign, call_table):
        lhs = assign.target.name
        rhs = assign.value

        if isinstance(rhs, ir.Expr):
            res = self._handle_string_array_expr(lhs, rhs, assign)
            if res is not None:
                return res

            res = self._handle_fix_df_array(lhs, rhs, assign, call_table)
            if res is not None:
                return res

            res = self._handle_empty_like(lhs, rhs, assign, call_table)
            if res is not None:
                return res

            res = self._handle_df_col_filter(lhs, rhs, assign)
            if res is not None:
                return res

            if rhs.op == 'call':
                res = self._handle_df_col_calls(lhs, rhs, assign)
                if res is not None:
                    return res

                fdef = guard(find_callname, self.func_ir, rhs)
                if fdef is None:
                    # could be make_function from list comprehension which is ok
                    func_def = guard(get_definition, self.func_ir, rhs.func)
                    if isinstance(func_def, ir.Expr) and func_def.op == 'make_function':
                        return [assign]
                    warnings.warn(
                        "function call couldn't be found for initial analysis")
                    return [assign]
                else:
                    func_name, func_mod = fdef

                if fdef == ('ts_binop_wrapper', 'hpat.hiframes_api'):
                    return self._handle_ts_binop(lhs, rhs, assign)

            res = self._handle_str_contains(lhs, rhs, assign, call_table)
            if res is not None:
                return res

        return [assign]

    def _handle_ts_binop(self, lhs, rhs, assign):
        op = guard(find_const, self.func_ir, rhs.args[0])
        assert op is not None
        ts_arr = rhs.args[1]
        ts_str = rhs.args[2]

        func_text = 'def f(ts_arr, ts_str):\n'
        func_text += '  l = len(ts_arr)\n'
        func_text += '  other = hpat.pd_timestamp_ext.parse_datetime_str(ts_str)\n'
        func_text += '  S = numba.unsafe.ndarray.empty_inferred((l,))\n'
        func_text += '  for i in numba.parfor.internal_prange(l):\n'
        func_text += '    S[i] = ts_arr[i] {} other\n'.format(op)
        loc_vars = {}
        exec(func_text, {}, loc_vars)
        f = loc_vars['f']
        f_blocks = compile_to_numba_ir(f,
                                        {'numba': numba, 'np': np, 'hpat': hpat},
                                        self.typingctx,
                                        (self.typemap[ts_arr.name],
                                        self.typemap[ts_str.name]),
                                        self.typemap, self.calltypes).blocks
        replace_arg_nodes(f_blocks[min(f_blocks.keys())], [ts_arr, ts_str])
        # replace == expression with result of parfor (S)
        # S is target of last statement in 1st block of f
        assign.value = f_blocks[min(f_blocks.keys())].body[-2].target
        return (f_blocks, [assign])

    def _handle_string_array_expr(self, lhs, rhs, assign):
        # convert str_arr==str into parfor
        if (rhs.op == 'binop'
                and rhs.fn in ['==', '!=', '>=', '>', '<=', '<']
                and (is_str_arr_typ(self.typemap[rhs.lhs.name])
                     or is_str_arr_typ(self.typemap[rhs.rhs.name]))):
            arg1 = rhs.lhs
            arg2 = rhs.rhs
            arg1_access = 'A'
            arg2_access = 'B'
            len_call = 'len(A)'
            if is_str_arr_typ(self.typemap[arg1.name]):
                arg1_access = 'A[i]'
                # replace type now for correct typing of len, etc.
                self.typemap.pop(arg1.name)
                self.typemap[arg1.name] = string_array_type

            if is_str_arr_typ(self.typemap[arg2.name]):
                arg1_access = 'B[i]'
                len_call = 'len(B)'
                self.typemap.pop(arg2.name)
                self.typemap[arg2.name] = string_array_type

            func_text = 'def f(A, B):\n'
            func_text += '  l = {}\n'.format(len_call)
            func_text += '  S = np.empty(l, dtype=np.bool_)\n'
            func_text += '  for i in numba.parfor.internal_prange(l):\n'
            func_text += '    S[i] = {} {} {}\n'.format(arg1_access, rhs.fn,
                                                        arg2_access)

            loc_vars = {}
            exec(func_text, {}, loc_vars)
            f = loc_vars['f']
            f_blocks = compile_to_numba_ir(f,
                                           {'numba': numba, 'np': np}, self.typingctx,
                                           (self.typemap[arg1.name],
                                            self.typemap[arg2.name]),
                                           self.typemap, self.calltypes).blocks
            replace_arg_nodes(f_blocks[min(f_blocks.keys())], [arg1, arg2])
            # replace == expression with result of parfor (S)
            # S is target of last statement in 1st block of f
            assign.value = f_blocks[min(f_blocks.keys())].body[-2].target
            return (f_blocks, [assign])

        return None

    def _handle_fix_df_array(self, lhs, rhs, assign, call_table):
        if (rhs.op == 'call'
                and rhs.func.name in call_table
                and call_table[rhs.func.name] ==
                ['to_series_type', 'hiframes_api', hpat]):
            assign.value = rhs.args[0]
            return [assign]
        # arr = fix_df_array(col) -> arr=col if col is array
        if (rhs.op == 'call'
                and rhs.func.name in call_table
                and call_table[rhs.func.name] ==
            ['fix_df_array', 'hiframes_api', hpat]
                and isinstance(self.typemap[rhs.args[0].name],
                               (types.Array, StringArrayType))):
            assign.value = rhs.args[0]
            return [assign]
        # arr = fix_rolling_array(col) -> arr=col if col is float array
        if (rhs.op == 'call'
                and rhs.func.name in call_table
                and call_table[rhs.func.name] ==
                ['fix_rolling_array', 'hiframes_api', hpat]):
            in_arr = rhs.args[0]
            if isinstance(self.typemap[in_arr.name].dtype, types.Float):
                assign.value = rhs.args[0]
                return [assign]
            else:
                def f(column):  # pragma: no cover
                    a = column.astype(np.float64)
                f_block = compile_to_numba_ir(f,
                                              {'hpat': hpat, 'np': np}, self.typingctx,
                                              (self.typemap[in_arr.name],),
                                              self.typemap, self.calltypes).blocks.popitem()[1]
                replace_arg_nodes(f_block, [in_arr])
                nodes = f_block.body[:-3]
                nodes[-1].target = assign.target
                return nodes
        return None

    def _handle_empty_like(self, lhs, rhs, assign, call_table):
        # B = empty_like(A) -> B = empty(len(A), dtype)
        if (rhs.op == 'call'
                and rhs.func.name in call_table
                and call_table[rhs.func.name] == ['empty_like', np]):
            in_arr = rhs.args[0]

            if self.typemap[in_arr.name].ndim == 1:
                # generate simpler len() for 1D case
                def f(_in_arr):  # pragma: no cover
                    _alloc_size = len(_in_arr)
                    _out_arr = np.empty(_alloc_size, _in_arr.dtype)
            else:
                def f(_in_arr):  # pragma: no cover
                    _alloc_size = _in_arr.shape
                    _out_arr = np.empty(_alloc_size, _in_arr.dtype)

            f_block = compile_to_numba_ir(f, {'np': np}, self.typingctx, (self.typemap[in_arr.name],),
                                          self.typemap, self.calltypes).blocks.popitem()[1]
            replace_arg_nodes(f_block, [in_arr])
            nodes = f_block.body[:-3]  # remove none return
            nodes[-1].target = assign.target
            return nodes
        return None

    def _handle_str_contains(self, lhs, rhs, assign, call_table):
        fname = guard(find_callname, self.func_ir, rhs)
        if fname is None:
            return None

        if fname == ('str_contains_regex', 'hpat.hiframes_api'):
            comp_func = 'hpat.str_ext.contains_regex'
        elif fname == ('str_contains_noregex', 'hpat.hiframes_api'):
            comp_func = 'hpat.str_ext.contains_noregex'
        else:
            return None

        str_arr = rhs.args[0]
        pat = rhs.args[1]
        func_text = 'def f(str_arr, pat):\n'
        func_text += '  l = len(str_arr)\n'
        func_text += '  S = np.empty(l, dtype=np.bool_)\n'
        func_text += '  for i in numba.parfor.internal_prange(l):\n'
        func_text += '    S[i] = {}(str_arr[i], pat)\n'.format(comp_func)
        loc_vars = {}
        exec(func_text, {}, loc_vars)
        f = loc_vars['f']
        f_blocks = compile_to_numba_ir(f,
                                       {'numba': numba, 'np': np,
                                           'hpat': hpat}, self.typingctx,
                                       (self.typemap[str_arr.name],
                                        self.typemap[pat.name]),
                                       self.typemap, self.calltypes).blocks
        replace_arg_nodes(f_blocks[min(f_blocks.keys())], [str_arr, pat])
        # replace call with result of parfor (S)
        # S is target of last statement in 1st block of f
        assign.value = f_blocks[min(f_blocks.keys())].body[-2].target
        return (f_blocks, [assign])

    def _handle_df_col_filter(self, lhs_name, rhs, assign):
        # find df['col2'] = df['col1'][arr]
        # since columns should have the same size, output is filled with NaNs
        # TODO: check for float, make sure col1 and col2 are in the same df
        if (rhs.op == 'getitem'
                and rhs.value.name in self.df_cols
                and lhs_name in self.df_cols
                and self.is_bool_arr(rhs.index.name)):
            lhs = assign.target
            in_arr = rhs.value
            index_var = rhs.index
            f_blocks = compile_to_numba_ir(_column_filter_impl_float,
                                           {'numba': numba, 'np': np}, self.typingctx,
                                           (self.typemap[lhs.name], self.typemap[in_arr.name],
                                               self.typemap[index_var.name]),
                                           self.typemap, self.calltypes).blocks
            first_block = min(f_blocks.keys())
            replace_arg_nodes(f_blocks[first_block], [lhs, in_arr, index_var])
            alloc_nodes = gen_np_call('empty_like', np.empty_like, lhs, [in_arr],
                                      self.typingctx, self.typemap, self.calltypes)
            f_blocks[first_block].body = alloc_nodes + \
                f_blocks[first_block].body
            return f_blocks

    def _handle_df_col_calls(self, lhs_name, rhs, assign):

        func_name = ""
        func_mod = ""
        fdef = guard(find_callname, self.func_ir, rhs)
        if fdef is None:
            # could be make_function from list comprehension which is ok
            func_def = guard(get_definition, self.func_ir, rhs.func)
            if isinstance(func_def, ir.Expr) and func_def.op == 'make_function':
                return [assign]
            # warnings.warn(
            #     "function call couldn't be found for initial analysis")
            return [assign]
        else:
            func_name, func_mod = fdef

        if func_mod != 'hpat.hiframes_api':
            return [assign]

        if func_name == 'ts_series_getitem':
            in_arr = rhs.args[0]
            ind = rhs.args[1]
            def f(_in_arr, _ind):
                dt = _in_arr[_ind]
                s = np.int64(dt)
                res = hpat.pd_timestamp_ext.convert_datetime64_to_timestamp(s)

            f_block = compile_to_numba_ir(f, {'numba': numba, 'np': np,
                                               'hpat': hpat}, self.typingctx,
                                           (self.typemap[in_arr.name], types.intp),
                                           self.typemap, self.calltypes).blocks.popitem()[1]
            replace_arg_nodes(f_block, [in_arr, ind])
            nodes = f_block.body[:-3]  # remove none return
            nodes[-1].target = assign.target
            return nodes

        if func_name == 'count':
            in_arr = rhs.args[0]
            f_blocks = compile_to_numba_ir(_column_count_impl,
                                           {'numba': numba, 'np': np,
                                               'hpat': hpat}, self.typingctx,
                                           (self.typemap[in_arr.name],),
                                           self.typemap, self.calltypes).blocks
            topo_order = find_topo_order(f_blocks)
            first_block = topo_order[0]
            last_block = topo_order[-1]
            replace_arg_nodes(f_blocks[first_block], [in_arr])
            # assign results to lhs output
            f_blocks[last_block].body[-3].target = assign.target
            return f_blocks

        if func_name == 'fillna':
            out_arr = rhs.args[0]
            in_arr = rhs.args[1]
            val = rhs.args[2]
            f_blocks = compile_to_numba_ir(_column_fillna_impl,
                                           {'numba': numba, 'np': np}, self.typingctx,
                                           (self.typemap[out_arr.name], self.typemap[in_arr.name],
                                               self.typemap[val.name]),
                                           self.typemap, self.calltypes).blocks
            first_block = min(f_blocks.keys())
            replace_arg_nodes(f_blocks[first_block], [out_arr, in_arr, val])
            return f_blocks

        if func_name == 'column_sum':
            in_arr = rhs.args[0]
            f_blocks = compile_to_numba_ir(_column_sum_impl,
                                           {'numba': numba, 'np': np,
                                               'hpat': hpat}, self.typingctx,
                                           (self.typemap[in_arr.name],),
                                           self.typemap, self.calltypes).blocks
            topo_order = find_topo_order(f_blocks)
            first_block = topo_order[0]
            last_block = topo_order[-1]
            replace_arg_nodes(f_blocks[first_block], [in_arr])
            # assign results to lhs output
            f_blocks[last_block].body[-3].target = assign.target
            return f_blocks

        if func_name == 'mean':
            in_arr = rhs.args[0]
            f_blocks = compile_to_numba_ir(_column_mean_impl,
                                           {'numba': numba, 'np': np,
                                               'hpat': hpat}, self.typingctx,
                                           (self.typemap[in_arr.name],),
                                           self.typemap, self.calltypes).blocks
            topo_order = find_topo_order(f_blocks)
            first_block = topo_order[0]
            last_block = topo_order[-1]
            replace_arg_nodes(f_blocks[first_block], [in_arr])
            # assign results to lhs output
            f_blocks[last_block].body[-3].target = assign.target
            return f_blocks

        if func_name == 'var':
            in_arr = rhs.args[0]
            f_blocks = compile_to_numba_ir(_column_var_impl,
                                           {'numba': numba, 'np': np,
                                               'hpat': hpat}, self.typingctx,
                                           (self.typemap[in_arr.name],),
                                           self.typemap, self.calltypes).blocks
            topo_order = find_topo_order(f_blocks)
            first_block = topo_order[0]
            last_block = topo_order[-1]
            replace_arg_nodes(f_blocks[first_block], [in_arr])
            # assign results to lhs output
            f_blocks[last_block].body[-3].target = assign.target
            return f_blocks

        return

    def is_bool_arr(self, varname):
        typ = self.typemap[varname]
        return isinstance(typ, types.npytypes.Array) and typ.dtype == types.bool_

# float columns can have regular np.nan


def _column_filter_impl_float(A, B, ind):  # pragma: no cover
    for i in numba.parfor.internal_prange(len(A)):
        s = 0
        if ind[i]:
            s = B[i]
        else:
            s = np.nan
        A[i] = s


def _column_count_impl(A):  # pragma: no cover
    numba.parfor.init_prange()
    count = 0
    for i in numba.parfor.internal_prange(len(A)):
        val = A[i]
        if not np.isnan(val):
            count += 1

    res = count
    return res


def _column_fillna_impl(A, B, fill):  # pragma: no cover
    for i in numba.parfor.internal_prange(len(A)):
        s = B[i]
        if np.isnan(s):
            s = fill
        A[i] = s


@numba.njit
def _sum_handle_nan(s, count):  # pragma: no cover
    if not count:
        s = np.nan
    return s


def _column_sum_impl(A):  # pragma: no cover
    numba.parfor.init_prange()
    count = 0
    s = 0
    for i in numba.parfor.internal_prange(len(A)):
        val = A[i]
        if not np.isnan(val):
            s += val
            count += 1

    res = hpat.hiframes_typed._sum_handle_nan(s, count)
    return res


@numba.njit
def _mean_handle_nan(s, count):  # pragma: no cover
    if not count:
        s = np.nan
    else:
        s = s / count
    return s


def _column_mean_impl(A):  # pragma: no cover
    numba.parfor.init_prange()
    count = 0
    s = 0
    for i in numba.parfor.internal_prange(len(A)):
        val = A[i]
        if not np.isnan(val):
            s += val
            count += 1

    res = hpat.hiframes_typed._mean_handle_nan(s, count)
    return res


@numba.njit
def _var_handle_nan(s, count):  # pragma: no cover
    if count <= 1:
        s = np.nan
    else:
        s = s / (count - 1)
    return s


def _column_var_impl(A):  # pragma: no cover
    count_m = 0
    m = 0
    for i in numba.parfor.internal_prange(len(A)):
        val = A[i]
        if not np.isnan(val):
            m += val
            count_m += 1

    m = hpat.hiframes_typed._mean_handle_nan(m, count_m)
    s = 0
    count = 0
    for i in numba.parfor.internal_prange(len(A)):
        val = A[i]
        if not np.isnan(val):
            s += (val - m)**2
            count += 1

    res = hpat.hiframes_typed._var_handle_nan(s, count)
    return res

def _column_min_impl(in_arr):
    numba.parfor.init_prange()
    count = 0
    s = numba.targets.builtins.get_type_max_value(in_arr.dtype)
    for i in numba.parfor.internal_prange(len(in_arr)):
        val = in_arr[i]
        if not np.isnan(val):
            s = min(s, val)
            count += 1
    res = hpat.hiframes_typed._sum_handle_nan(s, count)
    return res

def _column_max_impl(in_arr):
    numba.parfor.init_prange()
    count = 0
    s = numba.targets.builtins.get_type_min_value(in_arr.dtype)
    for i in numba.parfor.internal_prange(len(in_arr)):
        val = in_arr[i]
        if not np.isnan(val):
            s = max(s, val)
            count += 1
    res = hpat.hiframes_typed._sum_handle_nan(s, count)
    return res
