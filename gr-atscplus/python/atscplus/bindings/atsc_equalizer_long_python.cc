/*
 * Copyright 2026 gr-atscplus authors
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include <pybind11/complex.h>
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

namespace py = pybind11;

#include <gnuradio/atscplus/atsc_equalizer_long.h>

void bind_atsc_equalizer_long(py::module& m)
{
    using atsc_equalizer_long = ::gr::atscplus::atsc_equalizer_long;

    py::class_<atsc_equalizer_long,
               gr::block,
               gr::basic_block,
               std::shared_ptr<atsc_equalizer_long>>(m, "atsc_equalizer_long")
        .def(py::init(&atsc_equalizer_long::make))
        .def("taps", &atsc_equalizer_long::taps)
        .def("data", &atsc_equalizer_long::data);
}
