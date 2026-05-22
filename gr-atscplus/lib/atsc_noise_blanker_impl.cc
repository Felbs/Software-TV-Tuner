/* -*- c++ -*- */
/* SPDX-License-Identifier: GPL-3.0-or-later */

#ifdef HAVE_CONFIG_H
#include "config.h"
#endif

#include "atsc_noise_blanker_impl.h"
#include <gnuradio/io_signature.h>

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstdlib>

namespace gr {
namespace atscplus {

atsc_noise_blanker::sptr
atsc_noise_blanker::make(float threshold, int blank_samples, float alpha)
{
    return gnuradio::make_block_sptr<atsc_noise_blanker_impl>(
        threshold, blank_samples, alpha);
}

atsc_noise_blanker_impl::atsc_noise_blanker_impl(float threshold,
                                                 int blank_samples,
                                                 float alpha)
    : sync_block("atscplus_atsc_noise_blanker",
                 io_signature::make(1, 1, sizeof(gr_complex)),
                 io_signature::make(1, 1, sizeof(gr_complex)))
{
    d_threshold     = threshold;
    d_blank_samples = std::max(0, blank_samples);
    d_alpha         = std::clamp(alpha, 1e-8f, 1.0f);

    // Env overrides (lets the user retune without rebuilding).
    if (const char* p = std::getenv("STVT_NB_THRESHOLD")) {
        float v = std::atof(p);
        d_threshold = v;
    }
    if (const char* p = std::getenv("STVT_NB_BLANK_SAMPLES")) {
        int v = std::atoi(p);
        if (v >= 0) d_blank_samples = v;
    }
    if (const char* p = std::getenv("STVT_NB_ALPHA")) {
        float v = std::atof(p);
        if (v > 0.0f && v <= 1.0f) d_alpha = v;
    }

    d_ema             = 0.0f;
    d_blank_remaining = 0;
    d_n_samples       = 0;
    d_n_blanked       = 0;

    d_t0           = std::chrono::steady_clock::now();
    d_last_log     = d_t0;
    d_log_samples  = 0;
    d_log_blanked  = 0;

    std::fprintf(stderr,
                 "[noise_blanker] threshold=%.2f blank_samples=%d alpha=%.6f "
                 "(disabled=%d)\n",
                 d_threshold, d_blank_samples, d_alpha,
                 (int)(d_threshold <= 0.0f));
}

atsc_noise_blanker_impl::~atsc_noise_blanker_impl() = default;

int atsc_noise_blanker_impl::work(int noutput_items,
                                  gr_vector_const_void_star& input_items,
                                  gr_vector_void_star& output_items)
{
    const auto* in  = static_cast<const gr_complex*>(input_items[0]);
    auto*       out = static_cast<gr_complex*>(output_items[0]);

    // Disabled (threshold <= 0): pure passthrough, no EMA tracking.
    if (d_threshold <= 0.0f) {
        std::memcpy(out, in, noutput_items * sizeof(gr_complex));
        d_n_samples += noutput_items;
        d_log_samples += noutput_items;
    } else {
        const float a = d_alpha;
        const float thr = d_threshold;

        for (int i = 0; i < noutput_items; i++) {
            const float mag = std::abs(in[i]);

            if (d_blank_remaining > 0) {
                // Still inside a blanking window — zero out and decrement.
                out[i] = gr_complex(0.0f, 0.0f);
                d_blank_remaining--;
                d_n_blanked++;
                d_log_blanked++;
                // Don't update EMA during blanking (impulse would skew it).
            } else if (mag > thr * d_ema && d_ema > 1e-6f) {
                // Impulse trigger.
                out[i] = gr_complex(0.0f, 0.0f);
                d_blank_remaining = d_blank_samples;
                d_n_blanked++;
                d_log_blanked++;
                // Hold EMA — don't poison it with the impulse.
            } else {
                // Normal sample. Update EMA, passthrough.
                d_ema = (1.0f - a) * d_ema + a * mag;
                out[i] = in[i];
            }
        }
        d_n_samples += noutput_items;
        d_log_samples += noutput_items;
    }

    // Telemetry every 5s.
    auto now = std::chrono::steady_clock::now();
    long dt = std::chrono::duration_cast<std::chrono::seconds>(
                  now - d_last_log).count();
    if (dt >= 5) {
        auto elapsed_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
                              now - d_t0).count();
        double total_pct = d_n_samples > 0
            ? 100.0 * (double)d_n_blanked / (double)d_n_samples : 0.0;
        double log_pct = d_log_samples > 0
            ? 100.0 * (double)d_log_blanked / (double)d_log_samples : 0.0;
        std::fprintf(stderr,
                     "[noise_blanker t=%6.1fs] samples=%llu blanked=%llu "
                     "(%.4f%%)  ema=%.4f  last5s: %.4f%%\n",
                     elapsed_ms / 1000.0,
                     (unsigned long long)d_n_samples,
                     (unsigned long long)d_n_blanked,
                     total_pct, d_ema, log_pct);
        d_last_log = now;
        d_log_samples = 0;
        d_log_blanked = 0;
    }

    return noutput_items;
}

} /* namespace atscplus */
} /* namespace gr */