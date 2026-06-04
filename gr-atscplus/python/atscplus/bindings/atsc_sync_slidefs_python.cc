/*
 * Copyright 2026 gr-atscplus authors
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include <pybind11/complex.h>
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

namespace py = pybind11;

#include <gnuradio/atscplus/atsc_sync_slidefs.h>

void bind_atsc_sync_slidefs(py::module& m)
{
    using atsc_sync_slidefs = ::gr::atscplus::atsc_sync_slidefs;

    py::class_<atsc_sync_slidefs,
               gr::block,
               gr::basic_block,
               std::shared_ptr<atsc_sync_slidefs>>(m, "atsc_sync_slidefs")
        .def(py::init(&atsc_sync_slidefs::make), py::arg("rate"));
}
