/* SPDX-License-Identifier: GPL-3.0-or-later */
#include <pybind11/pybind11.h>
namespace py = pybind11;
#include <gnuradio/atscplus/atsc_rs_decoder_erasure.h>

void bind_atsc_rs_decoder_erasure(py::module& m)
{
    using atsc_rs_decoder_erasure = ::gr::atscplus::atsc_rs_decoder_erasure;
    py::class_<atsc_rs_decoder_erasure,
               gr::sync_block,
               gr::block,
               gr::basic_block,
               std::shared_ptr<atsc_rs_decoder_erasure>>(m, "atsc_rs_decoder_erasure")
        .def(py::init(&atsc_rs_decoder_erasure::make),
             py::arg("max_erasures") = 14);
}
