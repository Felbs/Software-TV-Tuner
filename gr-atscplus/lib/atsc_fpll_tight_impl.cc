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
#include <cstdlib>
#include <cstring>
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

    // STVT_FPLL_FOLD=1: absorb the downstream dc_blocker_ff + agc_ff here
    // (see header). Parameter envs mirror the python chain's defaults
    // (STVT_DCR_TAPS=32, STVT_AGC_ALPHA=1e-6, STVT_AGC_REFERENCE=4.0) so the
    // folded path computes exactly what the separate blocks would.
    if (const char* p = std::getenv("STVT_FPLL_FOLD")) {
        d_fold = (std::atoi(p) == 1);
    }
    if (d_fold) {
        int D = 32;
        if (const char* p = std::getenv("STVT_DCR_TAPS")) {
            int v = std::atoi(p);
            if (v > 1) D = v;
        }
        if (const char* p = std::getenv("STVT_AGC_ALPHA")) {
            char* e = nullptr; float v = std::strtof(p, &e);
            if (e != p) d_agc_rate = v;
        }
        if (const char* p = std::getenv("STVT_AGC_REFERENCE")) {
            char* e = nullptr; float v = std::strtof(p, &e);
            if (e != p) d_agc_reference = v;
        }
        d_fma0 = std::make_unique<fold_moving_averager_f>(D);
        d_fma1 = std::make_unique<fold_moving_averager_f>(D);
        d_fma2 = std::make_unique<fold_moving_averager_f>(D);
        d_fma3 = std::make_unique<fold_moving_averager_f>(D);
        d_fold_delay = std::make_unique<fold_ring_f>(D - 1);
        std::fprintf(stderr,
                     "[fpll_tight] FOLD active: dc_blocker(D=%d) + agc(rate=%g ref=%g) in-loop\n",
                     D, d_agc_rate, d_agc_reference);
    }

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

        if (d_fold) {
            // Exact replica of dc_blocker_ff_impl::work() long-form branch
            // followed by kernel::agc_ff::scale() — see header note.
            float y1 = d_fma0->filter(out[k]);
            float y2 = d_fma1->filter(y1);
            float y3 = d_fma2->filter(y2);
            float y4 = d_fma3->filter(y3);
            float dly = d_fold_delay->rotate(d_fma0->delayed_sig());
            float dcb = dly - y4;
            float o = dcb * d_agc_gain;
            d_agc_gain += (d_agc_reference - fabsf(o)) * d_agc_rate;
            if (d_agc_max_gain > 0.0 && d_agc_gain > d_agc_max_gain)
                d_agc_gain = d_agc_max_gain;
            out[k] = o;
        }

        filtered = d_afc.filter(result);
        x = gr::fast_atan2f(filtered.imag(), filtered.real());

        // FIX #1 (2026-05-23): saturation freeze.
        // Degenerate samples (low |filtered| OR x at ±π/2) carry no info
        // about the carrier offset. Legacy code fed ±π/2 to the NCO — a
        // max-magnitude correction from a zero-info sample. New behavior:
        // freeze the NCO on degenerate samples. STVT_FPLL_SAT_MODE=clip
        // restores legacy. STVT_FPLL_MAG_MIN=<rms> (default 4) sets the
        // magnitude threshold.
        static const int SAT_MODE = []() -> int {
            if (const char* p = std::getenv("STVT_FPLL_SAT_MODE")) {
                if (std::strcmp(p, "clip") == 0) return 1;
            }
            return 0;
        }();
        static const float MAG_MIN_RMS = []() -> float {
            if (const char* p = std::getenv("STVT_FPLL_MAG_MIN")) {
                char* e = nullptr; float v = std::strtof(p, &e);
                if (e != p) return v;
            }
            return 4.0f;
        }();
        // 2026-05-30 phase-error gate. Drift-localization showed carrier-slip
        // onsets coincide with large phase errors (|x|→π/2) that aren't caught
        // by the old razor-thin [π/2-1e-4, π/2] freeze band, so they kick the
        // NCO at near-full strength. STVT_FPLL_XGATE=<rad> widens the freeze:
        // any |x| > XGATE is treated as a degenerate/noise sample and frozen.
        // Default = π/2-1e-4 (EXACTLY the old behavior — zero change when unset).
        static const float XGATE = []() -> float {
            if (const char* p = std::getenv("STVT_FPLL_XGATE")) {
                char* e = nullptr; float v = std::strtof(p, &e);
                if (e != p && v > 0.0f && v <= (float)M_PI_2) return v;
            }
            return (float)M_PI_2 - 1e-4f;
        }();
        const float mag2_min = MAG_MIN_RMS * MAG_MIN_RMS;
        float mag2 = filtered.real() * filtered.real()
                   + filtered.imag() * filtered.imag();
        bool degenerate = (mag2 < mag2_min) || (x >= XGATE) || (x <= -XGATE);
        if (degenerate) {
            if (SAT_MODE == 1) {
                if (x > M_PI_2) x = M_PI_2;
                else if (x < -M_PI_2) x = -M_PI_2;
            } else {
                x = 0.0f;
            }
        }

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
