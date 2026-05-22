/* SPDX-License-Identifier: GPL-3.0-or-later */
#include <pybind11/pybind11.h>
namespace py = pybind11;
#include <gnuradio/atscplus/atsc_noise_blanker.h>

void bind_atsc_noise_blanker(py::module& m)
{
    using atsc_noise_blanker = ::gr::atscplus::atsc_noise_blanker;
    py::class_<atsc_noise_blanker,
               gr::sync_block,
               gr::block,
               gr::basic_block,
               std::shared_ptr<atsc_noise_blanker>>(m, "atsc_noise_blanker")
        .def(py::init(&atsc_noise_blanker::make),
             py::arg("threshold")     = 3.0f,
             py::arg("blank_samples") = 8,
             py::arg("alpha")         = 1e-4f);
}