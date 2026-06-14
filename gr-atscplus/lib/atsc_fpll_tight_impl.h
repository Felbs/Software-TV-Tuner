/* -*- c++ -*- */
/*
 * Copyright 2014 Free Software Foundation, Inc.
 *
 * This file is part of GNU Radio
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 */

#ifndef INCLUDED_ATSCPLUS_ATSC_FPLL_TIGHT_IMPL_H
#define INCLUDED_ATSCPLUS_ATSC_FPLL_TIGHT_IMPL_H

#include <gnuradio/analog/agc.h>
#include <gnuradio/atscplus/atsc_fpll_tight.h>
#include <gnuradio/filter/single_pole_iir.h>
#include <gnuradio/nco.h>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <vector>
#include <memory>

namespace gr {
namespace atscplus {

// STVT_FPLL_FOLD=1: run the downstream dc_blocker_ff(long form) + agc_ff
// INSIDE this block's output loop, eliminating two scheduler threads and two
// inter-block buffer crossings (the Pi 5 chain spends ~40% of a core on
// them). This struct is an EXACT arithmetic replica of GNU Radio 3.10.12's
// moving_averager_f (gr-filter/lib/dc_blocker_ff_impl.{h,cc}) — same float
// ops in the same order, so the folded output is bit-identical to the
// separate-blocks chain. GR's std::deque is replaced by a flat ring buffer
// (same VALUES, same order — only the container differs; deque indexing in
// the fpll critical path measured -4.5% chain throughput).
struct fold_ring_f {
    std::vector<float> buf;
    size_t head = 0; // oldest element
    explicit fold_ring_f(int n) : buf(n, 0.0f) {}
    // pop the oldest, push x — returns the popped value (== deque
    // push_back + [0] + pop_front sequence).
    float rotate(float x)
    {
        float oldest = buf[head];
        buf[head] = x;
        head = (head + 1) % buf.size();
        return oldest;
    }
};
struct fold_moving_averager_f {
    int d_length;
    float d_out = 0, d_out_d1 = 0, d_out_d2 = 0;
    fold_ring_f d_delay_line;
    explicit fold_moving_averager_f(int D) : d_length(D), d_delay_line(D - 1) {}
    float filter(float x)
    {
        d_out_d1 = d_out;
        d_out = d_delay_line.rotate(x);
        float y = x - d_out_d1 + d_out_d2;
        d_out_d2 = y;
        return (y / (float)(d_length));
    }
    float delayed_sig() { return d_out; }
};

class atsc_fpll_tight_impl : public atsc_fpll_tight
{
private:
    gr::nco<float, float> d_nco;
    gr::filter::single_pole_iir<gr_complex, gr_complex, float> d_afc;

    // Folded dc_blocker(long form) + agc_ff state (STVT_FPLL_FOLD=1).
    bool d_fold = false;
    std::unique_ptr<fold_moving_averager_f> d_fma0, d_fma1, d_fma2, d_fma3;
    std::unique_ptr<fold_ring_f> d_fold_delay;
    float d_agc_rate = 1e-6f, d_agc_reference = 4.0f;
    float d_agc_gain = 1.0f, d_agc_max_gain = 0.0f;

    // Tier-3 telemetry state.
    std::chrono::steady_clock::time_point d_t0;
    uint64_t d_total_samples;
    uint64_t d_window_count;
    double   d_window_sum_abs_x;
    double   d_window_sum_x2;
    float    d_window_max_abs_x;
    double   d_window_in_pwr;
    double   d_window_out_pwr;
    float    d_rate;

public:
    atsc_fpll_tight_impl(float rate, float alpha, float afc_tau_us); float d_alpha; float d_beta;
    ~atsc_fpll_tight_impl() override;

    int work(int noutput_items,
             gr_vector_const_void_star& input_items,
             gr_vector_void_star& output_items) override;
};

} /* namespace atscplus */
} /* namespace gr */

#endif /* INCLUDED_ATSCPLUS_ATSC_FPLL_TIGHT_IMPL_H */
