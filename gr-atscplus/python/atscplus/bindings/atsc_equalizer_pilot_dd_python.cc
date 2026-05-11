/*
 * Copyright 2026 gr-atscplus authors
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include <pybind11/complex.h>
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

namespace py = pybind11;

#include <gnuradio/atscplus/atsc_equalizer_pilot_dd.h>

void bind_atsc_equalizer_pilot_dd(py::module& m)
{
    using atsc_equalizer_pilot_dd = ::gr::atscplus::atsc_equalizer_pilot_dd;

    py::class_<atsc_equalizer_pilot_dd,
               gr::block,
               gr::basic_block,
               std::shared_ptr<atsc_equalizer_pilot_dd>>(m, "atsc_equalizer_pilot_dd")
        .def(py::init(&atsc_equalizer_pilot_dd::make))
        .def("taps", &atsc_equalizer_pilot_dd::taps)
        .def("data", &atsc_equalizer_pilot_dd::data)
        .def("last_residual_rms", &atsc_equalizer_pilot_dd::last_residual_rms);
}
