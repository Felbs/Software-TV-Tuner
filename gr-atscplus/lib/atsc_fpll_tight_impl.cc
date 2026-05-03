/* -*- c++ -*- */
/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Tight FPLL fork - tunable loop alpha/beta + AFC time constant. */
/* Tier-3 instrumented (2026-05-02): periodic stderr telemetry. */

#ifdef HAVE_CONFIG_H
#include "config.h"
#endif

#include "atsc_fpll_tight_impl.h"
#include <gnuradio/io_signature.h>
#include <gnuradio/math.h>
#include <gnuradio/sincos.h>
#include <cstdio>
#include <cmath>
#include <chrono>

namespace gr {
namespace atscplus {

atsc_fpll_tight::sptr atsc_fpll_tight::make(float rate, float alpha, float afc_tau_us)
{
    return gnuradio::make_block_sptr<atsc_fpll_tight_impl>(rate, alpha, afc_tau_us);
}

atsc_fpll_tight_impl::atsc_fpll_tight_impl(float rate, float alpha, float afc_tau_us)
    : sync_block("atscplus_atsc_fpll_tight",
                 io_signature::make(1, 1, sizeof(gr_complex)),
                 io_signature::make(1, 1, sizeof(float)))
{
    d_alpha = alpha;
    d_beta = alpha * alpha / 4.0f;
    d_rate = rate;
    d_afc.set_taps(1.0 - exp(-1.0 / rate / (afc_tau_us * 1e-6)));
    d_nco.set_freq((-3e6 + 0.309e6) / rate * 2 * GR_M_PI);
    d_nco.set_phase(0.0);
    d_t0 = std::chrono::steady_clock::now();
    d_total_samples = 0;
    d_window_sum_abs_x = 0.0;
    d_window_sum_x2 = 0.0;
    d_window_max_abs_x = 0.0f;
    d_window_count = 0;
    d_window_in_pwr = 0.0;
    d_window_out_pwr = 0.0;
    std::fprintf(stderr,
                 "[fpll_tight] rate=%.0f alpha=%.5f beta=%.3e afc_tau_us=%.1f\n",
                 rate, d_alpha, d_beta, afc_tau_us);
}

atsc_fpll_tight_impl::~atsc_fpll_tight_impl() {}

int atsc_fpll_tight_impl::work(int noutput_items,
                               gr_vector_const_void_star& input_items,
                               gr_vector_void_star& output_items)
{
    auto in = static_cast<const gr_complex*>(input_items[0]);
    auto out = static_cast<float*>(output_items[0]);

    float a_cos, a_sin;
    float x;
    gr_complex result, filtered;

    for (int k = 0; k < noutput_items; k++) {
        d_nco.step();
        d_nco.sincos(&a_sin, &a_cos);

        gr::fast_cc_multiply(result, in[k], gr_complex(a_sin, a_cos));

        out[k] = result.real();

        filtered = d_afc.filter(result);
        x = gr::fast_atan2f(filtered.imag(), filtered.real());

        if (x > M_PI_2) x = M_PI_2;
        else if (x < -M_PI_2) x = -M_PI_2;

        d_nco.adjust_phase(d_alpha * x);
        d_nco.adjust_freq(d_beta * x);

        // Tier-3 telemetry accumulation. Track:
        //  * mean and max |x| (phase-detector error magnitude)
        //  * input complex power and post-mix real power (proxies for AGC drift,
        //    even though AGC itself is downstream — change in upstream level
        //    leaks through here)
        float ax = std::fabs(x);
        d_window_sum_abs_x += ax;
        d_window_sum_x2 += (double)x * (double)x;
        if (ax > d_window_max_abs_x) d_window_max_abs_x = ax;
        d_window_in_pwr += (double)in[k].real() * in[k].real()
                        + (double)in[k].imag() * in[k].imag();
        d_window_out_pwr += (double)out[k] * out[k];
        d_window_count++;
    }

    d_total_samples += noutput_items;

    // Print every ~LOG_EVERY samples (~1.5M @ 16.14MS/s -> ~93ms).  Choose a
    // value that yields ~5 prints/sec to keep correlation with t=30-45s easy.
    constexpr uint64_t LOG_EVERY = 1u << 21; // 2,097,152 samples ≈ 130 ms
    if (d_window_count >= LOG_EVERY) {
        auto now = std::chrono::steady_clock::now();
        double t = std::chrono::duration<double>(now - d_t0).count();
        // Convert NCO phase increment (rad/sample) back to Hz offset.
        double freq_rad = d_nco.get_freq();
        double freq_hz  = freq_rad * d_rate / (2.0 * GR_M_PI);
        double mean_abs_x = d_window_sum_abs_x / (double)d_window_count;
        double rms_x      = std::sqrt(d_window_sum_x2 / (double)d_window_count);
        double in_rms     = std::sqrt(d_window_in_pwr  / (double)d_window_count);
        double out_rms    = std::sqrt(d_window_out_pwr / (double)d_window_count);
        std::fprintf(stderr,
                     "[fpll t=%6.2fs] nco_freq_hz=%+.1f mean|x|=%.5f rms_x=%.5f "
                     "max|x|=%.4f in_rms=%.1f out_rms=%.1f\n",
                     t, freq_hz, mean_abs_x, rms_x, d_window_max_abs_x,
                     in_rms, out_rms);
        std::fflush(stderr);
        d_window_sum_abs_x = 0.0;
        d_window_sum_x2 = 0.0;
        d_window_max_abs_x = 0.0f;
        d_window_in_pwr = 0.0;
        d_window_out_pwr = 0.0;
        d_window_count = 0;
    }

    return noutput_items;
}

} /* namespace atscplus */
} /* namespace gr */
