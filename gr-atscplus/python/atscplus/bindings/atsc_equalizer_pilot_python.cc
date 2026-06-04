/*
 * Copyright 2026 gr-atscplus authors
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include <pybind11/complex.h>
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

namespace py = pybind11;

#include <gnuradio/atscplus/atsc_equalizer_pilot.h>

void bind_atsc_equalizer_pilot(py::module& m)
{
    using atsc_equalizer_pilot = ::gr::atscplus::atsc_equalizer_pilot;

    py::class_<atsc_equalizer_pilot,
               gr::block,
               gr::basic_block,
               std::shared_ptr<atsc_equalizer_pilot>>(m, "atsc_equalizer_pilot")
        .def(py::init(&atsc_equalizer_pilot::make))
        .def("taps", &atsc_equalizer_pilot::taps)
        .def("data", &atsc_equalizer_pilot::data)
        .def("last_residual_rms", &atsc_equalizer_pilot::last_residual_rms);
}
