#include <pybind11/complex.h>
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>
namespace py = pybind11;
#include <gnuradio/atscplus/atsc_viterbi_soft.h>
void bind_atsc_viterbi_soft(py::module& m) {
    using atsc_viterbi_soft = ::gr::atscplus::atsc_viterbi_soft;
    py::class_<atsc_viterbi_soft, gr::block, gr::basic_block, std::shared_ptr<atsc_viterbi_soft>>(m, "atsc_viterbi_soft")
        .def(py::init(&atsc_viterbi_soft::make));
}
