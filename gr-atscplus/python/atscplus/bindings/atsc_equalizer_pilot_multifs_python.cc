/*
 * Copyright 2026 gr-atscplus authors
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include <pybind11/complex.h>
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

namespace py = pybind11;

#include <gnuradio/atscplus/atsc_equalizer_pilot_multifs.h>

void bind_atsc_equalizer_pilot_multifs(py::module& m)
{
    using atsc_equalizer_pilot_multifs = ::gr::atscplus::atsc_equalizer_pilot_multifs;

    py::class_<atsc_equalizer_pilot_multifs,
               gr::block,
               gr::basic_block,
               std::shared_ptr<atsc_equalizer_pilot_multifs>>(m, "atsc_equalizer_pilot_multifs")
        .def(py::init(&atsc_equalizer_pilot_multifs::make))
        .def("taps", &atsc_equalizer_pilot_multifs::taps)
        .def("data", &atsc_equalizer_pilot_multifs::data)
        .def("last_residual_rms", &atsc_equalizer_pilot_multifs::last_residual_rms);
}
