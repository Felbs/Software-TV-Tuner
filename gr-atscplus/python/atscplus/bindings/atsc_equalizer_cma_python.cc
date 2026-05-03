/*
 * Tier 6 (2026-05-02): pybind11 binding for atsc_equalizer_cma.
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include <pybind11/complex.h>
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

namespace py = pybind11;

#include <gnuradio/atscplus/atsc_equalizer_cma.h>

void bind_atsc_equalizer_cma(py::module& m)
{
    using atsc_equalizer_cma = ::gr::atscplus::atsc_equalizer_cma;

    py::class_<atsc_equalizer_cma,
               gr::block,
               gr::basic_block,
               std::shared_ptr<atsc_equalizer_cma>>(m, "atsc_equalizer_cma")
        .def(py::init(&atsc_equalizer_cma::make))
        .def("taps",     &atsc_equalizer_cma::taps)
        .def("dfe_taps", &atsc_equalizer_cma::dfe_taps);
}
