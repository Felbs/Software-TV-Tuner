/* SPDX-License-Identifier: GPL-3.0-or-later */
#include <pybind11/pybind11.h>
namespace py = pybind11;
#include <gnuradio/atscplus/atsc_sync_tunable.h>

void bind_atsc_sync_tunable(py::module& m)
{
    using atsc_sync_tunable = ::gr::atscplus::atsc_sync_tunable;
    py::class_<atsc_sync_tunable,
               gr::block,
               gr::basic_block,
               std::shared_ptr<atsc_sync_tunable>>(m, "atsc_sync_tunable")
        .def(py::init(&atsc_sync_tunable::make),
             py::arg("rate"),
             py::arg("min_lock_corr") = 3,
             py::arg("unlock_corr") = 1,
             py::arg("emit_when_unlocked") = true);
}
