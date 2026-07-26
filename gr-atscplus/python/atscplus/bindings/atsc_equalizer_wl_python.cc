/*
 * Copyright 2026 gr-atscplus authors
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include <pybind11/complex.h>
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

namespace py = pybind11;

#include <gnuradio/atscplus/atsc_equalizer_wl.h>

void bind_atsc_equalizer_wl(py::module& m)
{
    using atsc_equalizer_wl = ::gr::atscplus::atsc_equalizer_wl;

    py::class_<atsc_equalizer_wl,
               gr::block,
               gr::basic_block,
               std::shared_ptr<atsc_equalizer_wl>>(m, "atsc_equalizer_wl")
        .def(py::init(&atsc_equalizer_wl::make))
        .def("taps", &atsc_equalizer_wl::taps)
        .def("data", &atsc_equalizer_wl::data)
        .def("conj_energy_fraction", &atsc_equalizer_wl::conj_energy_fraction);
}
