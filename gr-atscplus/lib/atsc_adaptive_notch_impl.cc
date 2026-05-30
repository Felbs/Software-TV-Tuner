/* -*- c++ -*- */
/* SPDX-License-Identifier: GPL-3.0-or-later */
/*
 * 2026-05-29 atsc_adaptive_notch — narrowband interferer suppression.
 *
 * Math:
 *   Every UPDATE_INTERVAL_SAMPLES samples (or each work() call past a
 *   threshold), we accumulate FFT_SIZE complex samples and run a forward
 *   FFT. We compute |X[k]|² per bin, take the median across all non-
 *   pilot bins, and find the maximum bin. If max > threshold·median
 *   (in dB), we set the notch frequency to that bin's center and
 *   activate the IIR notch:
 *
 *     y[n] = x[n] - c·x[n-1] + r·c·y[n-1]      where c = exp(jω)
 *
 *   Steady-state: |H(e^{jω})| = 0 at the notch freq, → 1 elsewhere
 *   as r → 1. r=0.985 gives a -3dB notch width of ~30 kHz at 6.25 MS/s.
 *
 *   When no peak above threshold, the notch is disabled (pass-through).
 *
 * Pilot exclusion:
 *   The ATSC pilot tone at the SDR baseband (~-2.69 MHz on RF35) IS a
 *   narrowband peak. We exclude a ±200 kHz window around it from the
 *   peak-detection scan so we don't notch the carrier-recovery signal.
 */

#ifdef HAVE_CONFIG_H
#include "config.h"
#endif

#include "atsc_adaptive_notch_impl.h"
#include <gnuradio/io_signature.h>

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <vector>

namespace gr {
namespace atscplus {

atsc_adaptive_notch::sptr
atsc_adaptive_notch::make(double sample_rate,
                          int    fft_size,
                          double threshold_db,
                          double pole_radius,
                          double pilot_offset_hz,
                          double pilot_guard_hz)
{
    return gnuradio::make_block_sptr<atsc_adaptive_notch_impl>(
        sample_rate, fft_size, threshold_db, pole_radius,
        pilot_offset_hz, pilot_guard_hz);
}

atsc_adaptive_notch_impl::atsc_adaptive_notch_impl(double sample_rate,
                                                   int    fft_size,
                                                   double threshold_db,
                                                   double pole_radius,
                                                   double pilot_offset_hz,
                                                   double pilot_guard_hz)
    : sync_block("atscplus_atsc_adaptive_notch",
                 io_signature::make(1, 1, sizeof(gr_complex)),
                 io_signature::make(1, 1, sizeof(gr_complex)))
{
    d_sample_rate     = sample_rate;
    d_fft_size        = std::max(256, std::min(8192, fft_size));
    d_threshold_db    = threshold_db;
    d_pole_radius     = std::clamp(pole_radius, 0.5, 0.999);
    d_pilot_offset_hz = pilot_offset_hz;
    d_pilot_guard_hz  = std::max(0.0, pilot_guard_hz);

    /* Env overrides — let the user retune without rebuilding. */
    if (const char* p = std::getenv("STVT_NOTCH_FFT")) {
        int v = std::atoi(p);
        if (v >= 256 && v <= 8192) d_fft_size = v;
    }
    if (const char* p = std::getenv("STVT_NOTCH_THRESH_DB")) {
        double v = std::strtod(p, nullptr);
        if (v > 0.0) d_threshold_db = v;
    }
    if (const char* p = std::getenv("STVT_NOTCH_R")) {
        double v = std::strtod(p, nullptr);
        if (v > 0.5 && v < 0.999) d_pole_radius = v;
    }
    if (const char* p = std::getenv("STVT_NOTCH_PILOT_HZ")) {
        d_pilot_offset_hz = std::strtod(p, nullptr);
    }
    if (const char* p = std::getenv("STVT_NOTCH_GUARD_HZ")) {
        double v = std::strtod(p, nullptr);
        if (v >= 0.0) d_pilot_guard_hz = v;
    }

    /* Pilot exclusion window in FFT-shifted bin indices.
     * FFT bin k maps to frequency: f[k] = k·fs/N for k<N/2,
     *                              f[k] = (k-N)·fs/N for k>=N/2.
     * We work in the shifted index space [0, N) where N/2 is DC.
     * shifted_idx = (raw_idx + N/2) mod N.
     */
    auto hz_to_shifted = [&](double hz) -> int {
        double bin = (hz / d_sample_rate) * d_fft_size;
        int    raw = (int)std::lround(bin);
        // Wrap into [-N/2, N/2)
        while (raw <= -d_fft_size/2) raw += d_fft_size;
        while (raw >   d_fft_size/2) raw -= d_fft_size;
        return raw + d_fft_size/2;
    };
    d_pilot_bin_lo = hz_to_shifted(d_pilot_offset_hz - d_pilot_guard_hz);
    d_pilot_bin_hi = hz_to_shifted(d_pilot_offset_hz + d_pilot_guard_hz);
    if (d_pilot_bin_lo > d_pilot_bin_hi)
        std::swap(d_pilot_bin_lo, d_pilot_bin_hi);

    d_fft = std::make_unique<gr::fft::fft_complex_fwd>(d_fft_size);
    d_fft_buf.assign(d_fft_size, gr_complex(0.0f, 0.0f));
    d_fft_count = 0;

    d_notch_active = false;
    d_notch_omega  = 0.0;
    d_notch_coef   = gr_complex(1.0f, 0.0f);
    d_notch_x_prev = gr_complex(0.0f, 0.0f);
    d_notch_y_prev = gr_complex(0.0f, 0.0f);

    d_n_samples          = 0;
    d_n_notch_active     = 0;
    d_total_updates      = 0;
    d_log_updates        = 0;
    d_log_active_updates = 0;

    d_t0       = std::chrono::steady_clock::now();
    d_last_log = d_t0;

    std::fprintf(stderr,
                 "[atsc_adaptive_notch] init: fs=%.0f Hz fft=%d "
                 "thresh=%.1f dB r=%.3f pilot_offset=%.0f Hz "
                 "guard=%.0f Hz exclude_bins=[%d,%d]\n",
                 d_sample_rate, d_fft_size, d_threshold_db,
                 d_pole_radius, d_pilot_offset_hz, d_pilot_guard_hz,
                 d_pilot_bin_lo, d_pilot_bin_hi);
}

atsc_adaptive_notch_impl::~atsc_adaptive_notch_impl() = default;

double atsc_adaptive_notch_impl::current_notch_hz() const
{
    if (!d_notch_active) return 0.0;
    /* Convert d_notch_omega ∈ [-π, π) back to absolute Hz. */
    return d_notch_omega / (2.0 * M_PI) * d_sample_rate;
}

void atsc_adaptive_notch_impl::analyze_and_update()
{
    /* Copy d_fft_buf into the FFT input. */
    gr_complex* in = d_fft->get_inbuf();
    std::copy(d_fft_buf.begin(), d_fft_buf.end(), in);
    d_fft->execute();
    const gr_complex* out = d_fft->get_outbuf();

    /* Magnitude squared per bin, shifted so DC is at index N/2. */
    std::vector<float> mag(d_fft_size);
    const int N = d_fft_size;
    for (int k = 0; k < N; k++) {
        int shifted = (k + N/2) % N;
        mag[shifted] = out[k].real()*out[k].real()
                     + out[k].imag()*out[k].imag();
    }

    /* Median of non-pilot bins (we use Nth-element for O(N) median). */
    std::vector<float> bg;
    bg.reserve(N);
    for (int s = 0; s < N; s++) {
        if (s >= d_pilot_bin_lo && s <= d_pilot_bin_hi) continue;
        bg.push_back(mag[s]);
    }
    if (bg.empty()) return;  // pathological — pilot window covers whole band
    std::nth_element(bg.begin(),
                     bg.begin() + bg.size()/2,
                     bg.end());
    float median = bg[bg.size()/2];
    if (median <= 0.0f) median = 1e-30f;  // avoid log(0)

    /* Find max bin OUTSIDE pilot window. */
    int   max_bin  = -1;
    float max_mag  = 0.0f;
    for (int s = 0; s < N; s++) {
        if (s >= d_pilot_bin_lo && s <= d_pilot_bin_hi) continue;
        if (mag[s] > max_mag) { max_mag = mag[s]; max_bin = s; }
    }
    if (max_bin < 0) return;

    /* dB above median. */
    float peak_db = 10.0f * std::log10(max_mag / median);

    d_total_updates++;
    d_log_updates++;

    if (peak_db >= (float)d_threshold_db) {
        /* Bin index (shifted) → frequency in cycles/sample. */
        int    raw_bin = max_bin - N/2;
        double f_hz    = (double)raw_bin / (double)N * d_sample_rate;
        double omega   = 2.0 * M_PI * f_hz / d_sample_rate;
        d_notch_omega  = omega;
        d_notch_coef   = std::exp(gr_complex(0.0f, (float)omega));
        if (!d_notch_active) {
            d_notch_x_prev = gr_complex(0.0f, 0.0f);
            d_notch_y_prev = gr_complex(0.0f, 0.0f);
        }
        d_notch_active = true;
        d_log_active_updates++;
    } else {
        d_notch_active = false;
    }

    /* Periodic stderr log every ~5 sec. */
    auto now = std::chrono::steady_clock::now();
    if (std::chrono::duration_cast<std::chrono::seconds>(now - d_last_log)
            .count() >= 5) {
        double elapsed_sec = std::chrono::duration_cast<std::chrono::milliseconds>(
                                 now - d_t0).count() / 1000.0;
        std::fprintf(stderr,
                     "[atsc_adaptive_notch t=%6.1fs] updates=%d "
                     "active=%d (last5s: upd=%d active=%d) "
                     "peak=%.1f dB notch=%.0f kHz%s\n",
                     elapsed_sec, d_total_updates,
                     (int)d_n_notch_active,
                     d_log_updates, d_log_active_updates,
                     peak_db, current_notch_hz() / 1e3,
                     d_notch_active ? "" : " (INACTIVE)");
        d_log_updates        = 0;
        d_log_active_updates = 0;
        d_last_log = now;
    }
}

int atsc_adaptive_notch_impl::work(int noutput_items,
                                   gr_vector_const_void_star& input_items,
                                   gr_vector_void_star&       output_items)
{
    const gr_complex* in  = static_cast<const gr_complex*>(input_items[0]);
    gr_complex*       out = static_cast<gr_complex*>(output_items[0]);

    /* Capture local copies of state to avoid memory-ordering concerns
     * (this block isn't multithreaded but the compiler can still help). */
    const bool       notch_active = d_notch_active;
    const gr_complex c            = d_notch_coef;
    const float      r            = (float)d_pole_radius;
    gr_complex       x_prev       = d_notch_x_prev;
    gr_complex       y_prev       = d_notch_y_prev;

    for (int i = 0; i < noutput_items; i++) {
        gr_complex x = in[i];
        gr_complex y;
        if (notch_active) {
            /* IIR notch: y[n] = x[n] - c·x[n-1] + r·c·y[n-1] */
            y = x - c * x_prev + (r * c) * y_prev;
        } else {
            y = x;
        }
        out[i]  = y;
        x_prev  = x;
        y_prev  = y;

        /* Feed FFT analyzer. We use a non-overlapping window. */
        d_fft_buf[d_fft_count++] = x;
        if (d_fft_count >= d_fft_size) {
            d_fft_count = 0;
            analyze_and_update();
        }
    }

    d_notch_x_prev = x_prev;
    d_notch_y_prev = y_prev;
    d_n_samples   += noutput_items;
    if (d_notch_active) d_n_notch_active++;

    return noutput_items;
}

} /* namespace atscplus */
} /* namespace gr */
