/* SPDX-License-Identifier: GPL-3.0-or-later */
#include <pybind11/pybind11.h>
namespace py = pybind11;
#include <gnuradio/atscplus/atsc_fpll_tight.h>

void bind_atsc_fpll_tight(py::module& m)
{
    using atsc_fpll_tight = ::gr::atscplus::atsc_fpll_tight;
    py::class_<atsc_fpll_tight,
               gr::sync_block,
               gr::block,
               gr::basic_block,
               std::shared_ptr<atsc_fpll_tight>>(m, "atsc_fpll_tight")
        .def(py::init(&atsc_fpll_tight::make),
             py::arg("rate"),
             py::arg("alpha") = 0.003f,
             py::arg("afc_tau_us") = 20.0f);
}
