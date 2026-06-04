/*
 * Copyright 2026 gr-atscplus authors
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include <pybind11/complex.h>
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

namespace py = pybind11;

#include <gnuradio/atscplus/atsc_sync_pathA.h>

void bind_atsc_sync_pathA(py::module& m)
{
    using atsc_sync_pathA = ::gr::atscplus::atsc_sync_pathA;

    py::class_<atsc_sync_pathA,
               gr::block,
               gr::basic_block,
               std::shared_ptr<atsc_sync_pathA>>(m, "atsc_sync_pathA")
        .def(py::init(&atsc_sync_pathA::make), py::arg("rate"));
}
