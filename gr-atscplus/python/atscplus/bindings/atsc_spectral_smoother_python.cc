/* SPDX-License-Identifier: GPL-3.0-or-later */
#include <pybind11/pybind11.h>
namespace py = pybind11;
#include <gnuradio/atscplus/atsc_spectral_smoother.h>

void bind_atsc_spectral_smoother(py::module& m)
{
    using atsc_spectral_smoother = ::gr::atscplus::atsc_spectral_smoother;
    py::class_<atsc_spectral_smoother,
               gr::sync_block,
               gr::block,
               gr::basic_block,
               std::shared_ptr<atsc_spectral_smoother>>(m, "atsc_spectral_smoother")
        .def(py::init(&atsc_spectral_smoother::make),
             py::arg("sample_rate"),
             py::arg("fft_size")        = 1024,
             py::arg("neighborhood")    = 32,
             py::arg("threshold")       = 3.0,
             py::arg("pilot_offset_hz") = -2.69e6,
             py::arg("pilot_guard_hz")  =  300e3);
}
