/*
 * Copyright 2026 gr-atscplus authors
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include <pybind11/complex.h>
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

namespace py = pybind11;

#include <gnuradio/atscplus/atsc_wl_frontend.h>

void bind_atsc_wl_frontend(py::module& m)
{
    using atsc_wl_frontend = ::gr::atscplus::atsc_wl_frontend;

    py::class_<atsc_wl_frontend,
               gr::block,
               gr::basic_block,
               std::shared_ptr<atsc_wl_frontend>>(m, "atsc_wl_frontend")
        .def(py::init(&atsc_wl_frontend::make), py::arg("rate"));
}
