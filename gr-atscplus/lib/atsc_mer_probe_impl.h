/* -*- c++ -*- */
/* SPDX-License-Identifier: GPL-3.0-or-later */

#ifndef INCLUDED_ATSCPLUS_ATSC_MER_PROBE_IMPL_H
#define INCLUDED_ATSCPLUS_ATSC_MER_PROBE_IMPL_H

#include <gnuradio/atscplus/atsc_mer_probe.h>
#include <string>

namespace gr {
namespace atscplus {

class atsc_mer_probe_impl : public atsc_mer_probe
{
private:
    std::string d_tag;
    uint64_t    d_period;      // symbols between emissions

    // Slow self-calibration state (see work()).
    bool   d_init;
    double d_dc;               // EWMA of the symbol-stream DC / pilot offset
    double d_g;                // EWMA gain that normalizes to IDEAL_RMS

    // Accumulator for the current window.
    double   d_acc;            // sum of squared decision error
    uint64_t d_n;              // symbols accumulated

    // Latest results.
    double d_dd_err_rms;
    double d_dd_mer;

public:
    atsc_mer_probe_impl(const std::string& tag, uint64_t period);
    ~atsc_mer_probe_impl() override;

    double dd_err_rms() const override { return d_dd_err_rms; }
    double dd_mer()     const override { return d_dd_mer; }

    int work(int noutput_items,
             gr_vector_const_void_star& input_items,
             gr_vector_void_star& output_items) override;
};

} /* namespace atscplus */
} /* namespace gr */

#endif /* INCLUDED_ATSCPLUS_ATSC_MER_PROBE_IMPL_H */
