/* SPDX-License-Identifier: GPL-3.0-or-later */
#include <pybind11/pybind11.h>
namespace py = pybind11;
#include <gnuradio/atscplus/atsc_fs_checker_inst.h>

void bind_atsc_fs_checker_inst(py::module& m)
{
    using atsc_fs_checker_inst = ::gr::atscplus::atsc_fs_checker_inst;
    py::class_<atsc_fs_checker_inst,
               gr::block,
               gr::basic_block,
               std::shared_ptr<atsc_fs_checker_inst>>(m, "atsc_fs_checker_inst")
        .def(py::init(&atsc_fs_checker_inst::make));
}
