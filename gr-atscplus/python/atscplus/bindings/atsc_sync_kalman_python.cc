/*
 * Copyright 2026 gr-atscplus authors
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include <pybind11/complex.h>
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

namespace py = pybind11;

#include <gnuradio/atscplus/atsc_sync_kalman.h>

void bind_atsc_sync_kalman(py::module& m)
{
    using atsc_sync_kalman = ::gr::atscplus::atsc_sync_kalman;

    py::class_<atsc_sync_kalman,
               gr::block,
               gr::basic_block,
               std::shared_ptr<atsc_sync_kalman>>(m, "atsc_sync_kalman")
        .def(py::init(&atsc_sync_kalman::make), py::arg("rate"));
}
