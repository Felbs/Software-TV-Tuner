/* -*- c++ -*- */
/* SPDX-License-Identifier: GPL-3.0-or-later */

#ifndef INCLUDED_ATSCPLUS_ATSC_NOISE_BLANKER_IMPL_H
#define INCLUDED_ATSCPLUS_ATSC_NOISE_BLANKER_IMPL_H

#include <gnuradio/atscplus/atsc_noise_blanker.h>
#include <chrono>
#include <cstdint>

namespace gr {
namespace atscplus {

class atsc_noise_blanker_impl : public atsc_noise_blanker
{
private:
    // Tunables (set in ctor, overridable via env).
    float    d_threshold;
    int      d_blank_samples;
    float    d_alpha;

    // State.
    float    d_ema;                // running mean of |sample|
    int      d_blank_remaining;    // samples left to blank after trigger
    int      d_warmup_samples;     // skip blanking until EMA settles
    uint64_t d_n_samples;
    uint64_t d_n_blanked;

    // Periodic telemetry.
    std::chrono::steady_clock::time_point d_t0;
    std::chrono::steady_clock::time_point d_last_log;
    uint64_t d_log_samples;
    uint64_t d_log_blanked;

public:
    atsc_noise_blanker_impl(float threshold, int blank_samples, float alpha);
    ~atsc_noise_blanker_impl() override;

    uint64_t num_samples() const override { return d_n_samples; }
    uint64_t num_blanked() const override { return d_n_blanked; }

    int work(int noutput_items,
             gr_vector_const_void_star& input_items,
             gr_vector_void_star& output_items) override;
};

} /* namespace atscplus */
} /* namespace gr */

#endif