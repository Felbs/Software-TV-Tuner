/* -*- c++ -*- */
/* SPDX-License-Identifier: GPL-3.0-or-later */

#ifndef INCLUDED_ATSCPLUS_ATSC_ADAPTIVE_NOTCH_IMPL_H
#define INCLUDED_ATSCPLUS_ATSC_ADAPTIVE_NOTCH_IMPL_H

#include <gnuradio/atscplus/atsc_adaptive_notch.h>
#include <gnuradio/fft/fft.h>
#include <chrono>
#include <complex>
#include <cstdint>
#include <memory>
#include <vector>

namespace gr {
namespace atscplus {

class atsc_adaptive_notch_impl : public atsc_adaptive_notch
{
private:
    /* Tunables (constructor + env overrides). */
    double d_sample_rate;
    int    d_fft_size;
    double d_threshold_db;
    double d_pole_radius;
    double d_pilot_offset_hz;
    double d_pilot_guard_hz;

    /* FFT buffer + analyzer. */
    std::unique_ptr<gr::fft::fft_complex_fwd> d_fft;
    std::vector<gr_complex> d_fft_buf;   // size fft_size; rolling input
    int                     d_fft_count; // samples in d_fft_buf

    /* IIR notch state.
     * H(z) = (1 - e^{jω}·z⁻¹) / (1 - r·e^{jω}·z⁻¹)
     *  y[n] = x[n] - e^{jω}·x[n-1] + r·e^{jω}·y[n-1]
     * notch_active=false => pass-through (no filter applied).
     */
    bool      d_notch_active;
    double    d_notch_omega;          // 2π·f_notch / f_sample (rad/sample)
    gr_complex d_notch_coef;          // e^{jω}
    gr_complex d_notch_x_prev;        // x[n-1]
    gr_complex d_notch_y_prev;        // y[n-1]
    int        d_pilot_bin_lo;        // exclusion window (FFT-shifted index)
    int        d_pilot_bin_hi;

    /* Periodic logging + counters. */
    uint64_t d_n_samples;
    uint64_t d_n_notch_active;
    int      d_total_updates;
    int      d_log_updates;
    int      d_log_active_updates;
    std::chrono::steady_clock::time_point d_t0;
    std::chrono::steady_clock::time_point d_last_log;

    /* Run one peak-detection pass on d_fft_buf and update notch state. */
    void analyze_and_update();

public:
    atsc_adaptive_notch_impl(double sample_rate,
                             int    fft_size,
                             double threshold_db,
                             double pole_radius,
                             double pilot_offset_hz,
                             double pilot_guard_hz);
    ~atsc_adaptive_notch_impl() override;

    uint64_t num_samples()       const override { return d_n_samples; }
    uint64_t num_notch_active()  const override { return d_n_notch_active; }
    double   current_notch_hz()  const override;

    int work(int noutput_items,
             gr_vector_const_void_star& input_items,
             gr_vector_void_star&       output_items) override;
};

} /* namespace atscplus */
} /* namespace gr */

#endif
