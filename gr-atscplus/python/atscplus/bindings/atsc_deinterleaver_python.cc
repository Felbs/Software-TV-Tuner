#include <pybind11/complex.h>
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>
namespace py = pybind11;
#include <gnuradio/atscplus/atsc_deinterleaver.h>
void bind_atsc_deinterleaver(py::module& m) {
    using atsc_deinterleaver = ::gr::atscplus::atsc_deinterleaver;
    py::class_<atsc_deinterleaver, gr::block, gr::basic_block, std::shared_ptr<atsc_deinterleaver>>(m, "atsc_deinterleaver")
        .def(py::init(&atsc_deinterleaver::make));
}
