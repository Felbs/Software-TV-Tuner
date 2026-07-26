/* SPDX-License-Identifier: GPL-3.0-or-later */
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>
namespace py = pybind11;
#include <gnuradio/atscplus/atsc_mer_probe.h>

void bind_atsc_mer_probe(py::module& m)
{
    using atsc_mer_probe = ::gr::atscplus::atsc_mer_probe;
    py::class_<atsc_mer_probe,
               gr::sync_block,
               gr::block,
               gr::basic_block,
               std::shared_ptr<atsc_mer_probe>>(m, "atsc_mer_probe")
        .def(py::init(&atsc_mer_probe::make),
             py::arg("tag")    = "eq",
             py::arg("period") = 5000000)
        .def("dd_err_rms", &atsc_mer_probe::dd_err_rms)
        .def("dd_mer",     &atsc_mer_probe::dd_mer);
}
