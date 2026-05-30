/* SPDX-License-Identifier: GPL-3.0-or-later */
#include <pybind11/pybind11.h>
namespace py = pybind11;
#include <gnuradio/atscplus/atsc_adaptive_notch.h>

void bind_atsc_adaptive_notch(py::module& m)
{
    using atsc_adaptive_notch = ::gr::atscplus::atsc_adaptive_notch;
    py::class_<atsc_adaptive_notch,
               gr::sync_block,
               gr::block,
               gr::basic_block,
               std::shared_ptr<atsc_adaptive_notch>>(m, "atsc_adaptive_notch")
        .def(py::init(&atsc_adaptive_notch::make),
             py::arg("sample_rate"),
             py::arg("fft_size")        = 1024,
             py::arg("threshold_db")    = 12.0,
             py::arg("pole_radius")     = 0.985,
             py::arg("pilot_offset_hz") = -2.69e6,
             py::arg("pilot_guard_hz")  =  200e3);
}
