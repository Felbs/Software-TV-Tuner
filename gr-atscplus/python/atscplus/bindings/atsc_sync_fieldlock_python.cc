/*
 * Copyright 2026 gr-atscplus authors
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include <pybind11/complex.h>
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

namespace py = pybind11;

#include <gnuradio/atscplus/atsc_sync_fieldlock.h>

void bind_atsc_sync_fieldlock(py::module& m)
{
    using atsc_sync_fieldlock = ::gr::atscplus::atsc_sync_fieldlock;

    py::class_<atsc_sync_fieldlock,
               gr::block,
               gr::basic_block,
               std::shared_ptr<atsc_sync_fieldlock>>(m, "atsc_sync_fieldlock")
        .def(py::init(&atsc_sync_fieldlock::make), py::arg("rate"));
}
