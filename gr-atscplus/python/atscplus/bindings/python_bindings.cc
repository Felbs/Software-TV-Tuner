/*
 * Copyright 2020 Free Software Foundation, Inc.
 *
 * This file is part of GNU Radio
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 */

#include <pybind11/pybind11.h>

#define NPY_NO_DEPRECATED_API NPY_1_7_API_VERSION
#include <numpy/arrayobject.h>

namespace py = pybind11;

// Headers for binding functions
/**************************************/
// The following comment block is used for
// gr_modtool to insert function prototypes
// Please do not delete
/**************************************/
// BINDING_FUNCTION_PROTOTYPES(
void bind_atsc_equalizer_long(py::module& m);
void bind_atsc_equalizer_pilot(py::module& m);
void bind_atsc_equalizer_pilot_dd(py::module& m);
void bind_atsc_equalizer_pilot_dd_soft(py::module& m);
void bind_atsc_equalizer_pilot_multifs(py::module& m);
void bind_atsc_equalizer_pilot_multifs_dd(py::module& m);
void bind_atsc_equalizer_cma(py::module& m);
void bind_atsc_viterbi_soft(py::module& m);
void bind_atsc_fs_checker_inst(py::module& m);
void bind_atsc_fpll_tight(py::module& m);
void bind_atsc_sync_tunable(py::module& m);
void bind_atsc_sync_soft(py::module& m);
void bind_atsc_sync_kalman(py::module& m);
void bind_atsc_sync_fieldlock(py::module& m);
void bind_atsc_sync_slidefs(py::module& m);
void bind_atsc_sync_pathA(py::module& m);
// ) END BINDING_FUNCTION_PROTOTYPES


// We need this hack because import_array() returns NULL
// for newer Python versions.
// This function is also necessary because it ensures access to the C API
// and removes a warning.
void* init_numpy()
{
    import_array();
    return NULL;
}

PYBIND11_MODULE(atscplus_python, m)
{
    // Initialize the numpy C API
    // (otherwise we will see segmentation faults)
    init_numpy();

    // Allow access to base block methods
    py::module::import("gnuradio.gr");

    /**************************************/
    // The following comment block is used for
    // gr_modtool to insert binding function calls
    // Please do not delete
    /**************************************/
    // BINDING_FUNCTION_CALLS(
    bind_atsc_equalizer_long(m);
    bind_atsc_equalizer_pilot(m);
    bind_atsc_equalizer_pilot_dd(m);
    bind_atsc_equalizer_pilot_dd_soft(m);
    bind_atsc_equalizer_pilot_multifs(m);
    bind_atsc_equalizer_pilot_multifs_dd(m);
    bind_atsc_equalizer_cma(m);
    bind_atsc_viterbi_soft(m);
    bind_atsc_fs_checker_inst(m);
    bind_atsc_fpll_tight(m);
    bind_atsc_sync_tunable(m);
    bind_atsc_sync_soft(m);
    bind_atsc_sync_kalman(m);
    bind_atsc_sync_fieldlock(m);
    bind_atsc_sync_slidefs(m);
    bind_atsc_sync_pathA(m);
    // ) END BINDING_FUNCTION_CALLS
}
