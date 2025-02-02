# SPDX-FileCopyrightText: 2023 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0

import operator
import sys

import dpnp
from numba.core import compiler

import numba_dpex as dpex

from .kernel_template_iface import KernelTemplateInterface


class TreeReduceIntermediateKernelTemplate(KernelTemplateInterface):
    """The class to build reduction main kernel_txt template and
    compiled Numba functionIR."""

    def __init__(
        self,
        kernel_name,
        kernel_params,
        ivar_names,
        sentinel_name,
        loop_ranges,
        param_dict,
        parfor_dim,
        redvars,
        parfor_args,
        parfor_reddict,
        redvars_dict,
        typemap,
        work_group_size,
    ) -> None:
        self._kernel_name = kernel_name
        self._kernel_params = kernel_params
        self._ivar_names = ivar_names
        self._sentinel_name = sentinel_name
        self._loop_ranges = loop_ranges
        self._param_dict = param_dict
        self._parfor_dim = parfor_dim
        self._redvars = redvars
        self._parfor_args = parfor_args
        self._parfor_reddict = parfor_reddict
        self._redvars_dict = redvars_dict
        self._typemap = typemap
        self._work_group_size = work_group_size

        self._kernel_txt = self._generate_kernel_stub_as_string()
        self._kernel_ir = self._generate_kernel_ir()

    def _generate_kernel_stub_as_string(self):
        """Generate reduction main kernel template"""

        gufunc_txt = ""
        gufunc_txt += "def " + self._kernel_name
        gufunc_txt += "(" + (", ".join(self._kernel_params)) + "):\n"
        global_id_dim = 0
        for_loop_dim = self._parfor_dim

        if self._parfor_dim > 3:
            raise NotImplementedError
        else:
            global_id_dim = self._parfor_dim

        for dim in range(global_id_dim):
            dstr = str(dim)
            gufunc_txt += (
                f"    {self._ivar_names[dim]} = dpex.get_global_id({dstr})\n"
            )
            gufunc_txt += f"    local_id{dim} = dpex.get_local_id({dstr})\n"
            gufunc_txt += f"    local_size{dim} = dpex.get_local_size({dstr})\n"
            gufunc_txt += f"    group_id{dim} = dpex.get_group_id({dstr})\n"

        # Allocate local_sums arrays for each reduction variable.
        for redvar in self._redvars:
            rtyp = str(self._typemap[redvar])
            redvar = self._redvars_dict[redvar]
            gufunc_txt += f"    local_sums_{redvar} = \
                dpex.local.array({self._work_group_size}, dpnp.{rtyp})\n"

        for dim in range(global_id_dim, for_loop_dim):
            for indent in range(1 + (dim - global_id_dim)):
                gufunc_txt += "    "

            start, stop, step = self._loop_ranges[dim]
            st = str(self._param_dict.get(str(start), start))
            en = str(self._param_dict.get(str(stop), stop))
            gufunc_txt += (
                f"for {self._ivar_names[dim]} in range({st}, {en} + 1):\n"
            )

        for dim in range(global_id_dim, for_loop_dim):
            for indent in range(1 + (dim - global_id_dim)):
                gufunc_txt += "    "
        # Add the sentinel assignment so that we can find the loop body position
        # in the IR.
        for redvar in self._redvars:
            legal_redvar = self._redvars_dict[redvar]
            gufunc_txt += "    "
            gufunc_txt += legal_redvar + " = "
            gufunc_txt += f"{self._parfor_reddict[redvar].init_val} \n"

        gufunc_txt += "    "
        gufunc_txt += self._sentinel_name + " = 0\n"

        # Generate local_sum[local_id0] = redvar, for each reduction variable
        for redvar in self._redvars:
            legal_redvar = self._redvars_dict[redvar]
            gufunc_txt += (
                "    "
                + f"local_sums_{legal_redvar}[local_id0] = {legal_redvar}\n"
            )

        gufunc_txt += (
            "    stride0 = local_size0 // 2\n"
            + "    while stride0 > 0:\n"
            + "        dpex.barrier(dpex.LOCAL_MEM_FENCE)\n"
            + "        if local_id0 < stride0:\n"
        )

        for redvar in self._redvars:
            redop = self._parfor_reddict[redvar].redop
            redvar_legal = self._redvars_dict[redvar]
            if redop == operator.iadd:
                gufunc_txt += (
                    "            "
                    f"local_sums_{redvar_legal}[local_id0] "
                    f"+= local_sums_{redvar_legal}[local_id0 + stride0]\n"
                )
            elif redop == operator.imul:
                gufunc_txt += (
                    "            "
                    f"local_sums_{redvar_legal}[local_id0] "
                    f"*= local_sums_{redvar_legal}[local_id0 + stride0]\n"
                )
            else:
                raise NotImplementedError

        gufunc_txt += "        stride0 >>= 1\n"
        gufunc_txt += "    if local_id0 == 0:\n"
        for redvar in self._redvars:
            for i, arg in enumerate(self._parfor_args):
                if arg == redvar:
                    partial_sum_var = self._kernel_params[i]
                    redvar_legal = self._redvars_dict[redvar]
                    gufunc_txt += (
                        "        "
                        f"{partial_sum_var}[group_id0] = "
                        f"local_sums_{redvar_legal}[0]\n"
                    )

        gufunc_txt += "    return None\n"

        return gufunc_txt

    def _generate_kernel_ir(self):
        """Exec the kernel_txt string into a Python function object and then
        compile it using Numba's compiler front end.

        Returns: The Numba functionIR object for the compiled kernel_txt string.

        """
        globls = {"dpnp": dpnp, "dpex": dpex}
        locls = {}
        exec(self._kernel_txt, globls, locls)
        kernel_fn = locls[self._kernel_name]

        return compiler.run_frontend(kernel_fn)

    @property
    def kernel_ir(self):
        """Returns the Numba IR generated for a
            TreeReduceIntermediateKernelTemplate.

        Returns: The Numba functionIR object for the compiled kernel_txt string.
        """
        return self._kernel_ir

    @property
    def kernel_string(self):
        """Returns the function string generated for a
            TreeReduceIntermediateKernelTemplate.

        Returns:
            str: A string representing a stub reduction kernel function
            for the parfor.
        """
        return self._kernel_txt

    def dump_kernel_string(self):
        """Helper to print the kernel function string."""
        print(self._kernel_txt)
        sys.stdout.flush()

    def dump_kernel_ir(self):
        """Helper to dump the Numba IR for a
        TreeReduceIntermediateKernelTemplate."""
        self._kernel_ir.dump()


class RemainderReduceIntermediateKernelTemplate(KernelTemplateInterface):
    """The class to build reduction remainder kernel_txt template and
    compiled Numba functionIR.
    """

    def __init__(
        self,
        kernel_name,
        kernel_params,
        sentinel_name,
        redvars,
        parfor_reddict,
        redvars_dict,
        typemap,
        legal_loop_indices,
        global_size_var_name,
        global_size_mod_var_name,
        partial_sum_size_var_name,
        partial_sum_var_name,
        final_sum_var_name,
        reductionKernelVar,
    ) -> None:
        self._kernel_name = kernel_name
        self._kernel_params = kernel_params
        self._sentinel_name = sentinel_name
        self._redvars = redvars
        self._parfor_reddict = parfor_reddict
        self._redvars_dict = redvars_dict
        self._typemap = typemap
        self._legal_loop_indices = legal_loop_indices
        self._global_size_var_name = global_size_var_name
        self._global_size_mod_var_name = global_size_mod_var_name
        self._partial_sum_size_var_name = partial_sum_size_var_name
        self._partial_sum_var_name = partial_sum_var_name
        self._final_sum_var_name = final_sum_var_name
        self._reductionKernelVar = reductionKernelVar

        self._kernel_txt = self._generate_kernel_stub_as_string()
        self._kernel_ir = self._generate_kernel_ir()

    def _generate_kernel_stub_as_string(self):
        """Generate reduction remainder kernel template"""

        gufunc_txt = ""
        gufunc_txt += "def " + self._kernel_name
        gufunc_txt += "(" + (", ".join(self._kernel_params))

        for i in range(len(self._redvars)):
            gufunc_txt += (
                ", "
                + f"{self._global_size_var_name[i]}, "
                + f"{self._global_size_mod_var_name[i]}, "
            )
            gufunc_txt += (
                f"{self._partial_sum_size_var_name[i]}, "
                + f"{self._final_sum_var_name[i]}"
            )

        gufunc_txt += "):\n"

        gufunc_txt += "    "

        gufunc_txt += (
            "for j" + f" in range({self._partial_sum_size_var_name[0]}):\n"
        )

        for i, redvar in enumerate(self._redvars):
            redop = self._parfor_reddict[redvar].redop
            if redop == operator.iadd:
                gufunc_txt += f"        {self._final_sum_var_name[i]}[0] += \
                    {self._partial_sum_var_name[i]}[j]\n"
            elif redop == operator.imul:
                gufunc_txt += f"        {self._final_sum_var_name[i]}[0] *= \
                    {self._partial_sum_var_name[i]}[j]\n"
            else:
                raise NotImplementedError

        gufunc_txt += (
            f"    for j in range ({self._global_size_mod_var_name[0]}) :\n"
        )

        for redvar in self._redvars:
            legal_redvar = self._redvars_dict[redvar]
            gufunc_txt += "        "
            gufunc_txt += legal_redvar + " = "
            gufunc_txt += f"{self._parfor_reddict[redvar].init_val}\n"

        gufunc_txt += (
            "        "
            + self._legal_loop_indices[0]
            + " = "
            + f"{self._global_size_var_name[0]} + j\n"
        )

        for redvar in self._redvars:
            rtyp = str(self._typemap[redvar])
            redvar = self._redvars_dict[redvar]
            gufunc_txt += (
                "        "
                + f"local_sums_{redvar} = "
                + f"dpex.local.array(1, dpnp.{rtyp})\n"
            )

        gufunc_txt += "        " + self._sentinel_name + " = 0\n"

        for i, redvar in enumerate(self._redvars):
            legal_redvar = self._redvars_dict[redvar]
            gufunc_txt += (
                "        " + f"local_sums_{legal_redvar}[0] = {legal_redvar}\n"
            )

        for i, redvar in enumerate(self._redvars):
            legal_redvar = self._redvars_dict[redvar]
            redop = self._parfor_reddict[redvar].redop
            if redop == operator.iadd:
                gufunc_txt += f"        {self._final_sum_var_name[i]}[0] +=  \
                    local_sums_{legal_redvar}[0]\n"
            elif redop == operator.imul:
                gufunc_txt += f"        {self._final_sum_var_name[i]}[0] *=  \
                    local_sums_{legal_redvar}[0]\n"
            else:
                raise NotImplementedError

        return gufunc_txt

    def _generate_kernel_ir(self):
        """Exec the kernel_txt string into a Python function object and then
        compile it using Numba's compiler front end.

        Returns: The Numba functionIR object for the compiled kernel_txt string.

        """

        globls = {"dpnp": dpnp, "dpex": dpex}
        locls = {}
        exec(self._kernel_txt, globls, locls)
        kernel_fn = locls[self._kernel_name]

        return compiler.run_frontend(kernel_fn)

    @property
    def kernel_ir(self):
        """Returns the Numba IR generated for a
            RemainderReduceIntermediateKernelTemplate.

        Returns: The Numba functionIR object for the compiled kernel_txt string.
        """
        return self._kernel_ir

    @property
    def kernel_string(self):
        """Returns the function string generated for a
            RemainderReduceIntermediateKernelTemplate.

        Returns:
            str: A string representing a stub reduction kernel function
            for the parfor.
        """
        return self._kernel_txt

    def dump_kernel_string(self):
        """Helper to print the kernel function string."""

        print(self._kernel_txt)
        sys.stdout.flush()

    def dump_kernel_ir(self):
        """Helper to dump the Numba IR for the
        RemainderReduceIntermediateKernelTemplate."""

        self._kernel_ir.dump()
