/*
 * Copyright 2026 gr-atscplus authors
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include <pybind11/complex.h>
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

namespace py = pybind11;

#include <gnuradio/atscplus/atsc_equalizer_pilot_dd_soft.h>

void bind_atsc_equalizer_pilot_dd_soft(py::module& m)
{
    using atsc_equalizer_pilot_dd_soft = ::gr::atscplus::atsc_equalizer_pilot_dd_soft;

    py::class_<atsc_equalizer_pilot_dd_soft,
               gr::block,
               gr::basic_block,
               std::shared_ptr<atsc_equalizer_pilot_dd_soft>>(m, "atsc_equalizer_pilot_dd_soft")
        .def(py::init(&atsc_equalizer_pilot_dd_soft::make))
        .def("taps", &atsc_equalizer_pilot_dd_soft::taps)
        .def("data", &atsc_equalizer_pilot_dd_soft::data)
        .def("last_residual_rms", &atsc_equalizer_pilot_dd_soft::last_residual_rms);
}
