/* -*- c++ -*- */
/*
 * Copyright 2026 gr-atscplus authors
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * Soft-decision matched-filter ATSC segment-sync detector. Replaces
 * gr::dtv::atsc_sync's hard-decision (sample > 0) PN511 correlator with
 * a true float matched filter on the 4-tap segment-sync pattern
 * +5,-5,-5,+5. See ~/overnight/DOWNSTREAM_INVESTIGATION.md for the
 * smoking-gun analysis that motivated this block.
 */

#ifdef HAVE_CONFIG_H
#include "config.h"
#endif

#include "atsc_sync_soft_impl.h"
#include <gnuradio/io_signature.h>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>

using namespace gr::dtv;

namespace gr {
namespace atscplus {

static const double LOOP_FILTER_TAP = 0.0005;
static const double ADJUSTMENT_GAIN = 1.0e-5 / (10 * ATSC_DATA_SEGMENT_LENGTH);
static const int SYMBOL_INDEX_OFFSET = 3;

atsc_sync_soft::sptr atsc_sync_soft::make(float rate)
{
    return gnuradio::make_block_sptr<atsc_sync_soft_impl>(rate);
}

atsc_sync_soft_impl::atsc_sync_soft_impl(float rate)
    : gr::block("atscplus_atsc_sync_soft",
                io_signature::make(1, 1, sizeof(float)),
                io_signature::make(1, 1, ATSC_DATA_SEGMENT_LENGTH * sizeof(float))),
      d_rx_clock_to_symbol_freq(rate / ATSC_SYMBOL_RATE),
      d_si(0)
{
    d_loop.set_taps(LOOP_FILTER_TAP);

    // Defaults tuned on sdr_RF36.cf32 (89.66% segment alignment, vs
    // 71.6% from gr-dtv atsc_sync). See ATSC_SYNC_SOFT_* env vars and
    // ~/overnight/SYNC_BUILD_RESULT.md for the tuning sweep.
    d_alpha = 0.40f;
    d_lock_threshold = 4.0f;
    d_unlock_threshold = 2.0f;
    d_sticky_fraction = 0.95f;
    d_emit_when_unlocked = true;
    d_debug = false;
    d_locked_idx = -1;
    d_timing_gain_scale = 1.0f;

    if (const char* p = std::getenv("ATSC_SYNC_SOFT_ALPHA")) {
        float v = std::atof(p);
        if (v > 0.0f && v <= 1.0f) d_alpha = v;
    }
    if (const char* p = std::getenv("ATSC_SYNC_SOFT_LOCK")) {
        float v = std::atof(p);
        if (v > 0.0f) d_lock_threshold = v;
    }
    if (const char* p = std::getenv("ATSC_SYNC_SOFT_UNLOCK")) {
        float v = std::atof(p);
        if (v > 0.0f) d_unlock_threshold = v;
    }
    if (const char* p = std::getenv("ATSC_SYNC_SOFT_STICKY")) {
        float v = std::atof(p);
        if (v >= 0.0f && v <= 1.0f) d_sticky_fraction = v;
    }
    if (const char* p = std::getenv("ATSC_SYNC_SOFT_TIMING_SCALE")) {
        float v = std::atof(p);
        if (v >= 0.0f && v <= 10.0f) d_timing_gain_scale = v;
    }
    if (const char* p = std::getenv("ATSC_SYNC_SOFT_EMIT_UNLOCKED")) {
        d_emit_when_unlocked = (std::atoi(p) != 0);
    }
    if (const char* p = std::getenv("ATSC_SYNC_SOFT_DEBUG")) {
        d_debug = (std::atoi(p) != 0);
    }

    d_segs_emitted = 0;
    d_segs_held = 0;
    d_segs_aligned = 0;
    d_segs_total = 0;
    d_relocks = 0;
    d_seg_count = 0;

    std::fprintf(stderr,
                 "[sync_soft] rate=%.0f alpha=%.4f lock=%.2f unlock=%.2f "
                 "sticky=%.2f timing_scale=%.2f emit_unlocked=%d debug=%d\n",
                 rate, d_alpha, d_lock_threshold, d_unlock_threshold,
                 d_sticky_fraction, d_timing_gain_scale,
                 (int)d_emit_when_unlocked, (int)d_debug);

    reset();
}

void atsc_sync_soft_impl::reset()
{
    d_w = d_rx_clock_to_symbol_freq;
    d_mu = 0.5;
    d_timing_adjust = 0;
    d_counter = 0;
    d_symbol_index = 0;
    d_seg_locked = false;
    d_locked_idx = -1;
    d_mf_idx = 0;
    memset(d_mf_buf, 0, sizeof(d_mf_buf));
    memset(d_sample_mem, 0, sizeof(d_sample_mem));
    memset(d_data_mem, 0, sizeof(d_data_mem));
    memset(d_integrator, 0, sizeof(d_integrator));
}

atsc_sync_soft_impl::~atsc_sync_soft_impl()
{
    double align_pct = (d_segs_emitted > 0)
        ? 100.0 * (double)d_segs_aligned / (double)d_segs_emitted
        : 0.0;
    std::fprintf(stderr,
                 "[sync_soft FINAL] segs_emitted=%llu segs_held=%llu "
                 "segs_aligned=%llu (%.2f%%) relocks=%llu\n",
                 (unsigned long long)d_segs_emitted,
                 (unsigned long long)d_segs_held,
                 (unsigned long long)d_segs_aligned,
                 align_pct,
                 (unsigned long long)d_relocks);
}

void atsc_sync_soft_impl::forecast(int noutput_items,
                                   gr_vector_int& ninput_items_required)
{
    unsigned ninputs = ninput_items_required.size();
    for (unsigned i = 0; i < ninputs; i++)
        ninput_items_required[i] =
            static_cast<int>(noutput_items * d_rx_clock_to_symbol_freq *
                             ATSC_DATA_SEGMENT_LENGTH) + 1500 - 1;
}

int atsc_sync_soft_impl::general_work(int noutput_items,
                                      gr_vector_int& ninput_items,
                                      gr_vector_const_void_star& input_items,
                                      gr_vector_void_star& output_items)
{
    const float* in = static_cast<const float*>(input_items[0]);
    float* out = static_cast<float*>(output_items[0]);

    float interp_sample;
    d_si = 0;
    d_output_produced = 0;

    while (d_output_produced < noutput_items &&
           (d_si + (int)d_interp.ntaps()) < ninput_items[0]) {

        interp_sample = d_interp.interpolate(&in[d_si], d_mu);
        // Reduce timing-adjust influence when locked: each per-segment
        // timing_adjust has noise even at lock; aggressive updates cause
        // d_counter to drift through best_idx bins.
        const double t_gain = d_seg_locked
            ? (double)d_timing_gain_scale : 1.0;
        d_mu += t_gain * ADJUSTMENT_GAIN * 1e3 * d_timing_adjust;

        double s = d_mu + d_w;
        double float_incr = std::floor(s);
        d_mu = s - float_incr;
        d_incr = (int)float_incr;
        if (d_incr < 1) d_incr = 1;
        if (d_incr > 3) d_incr = 3;
        d_si += d_incr;

        d_sample_mem[d_counter] = interp_sample;

        // Update 4-sample matched-filter FIFO. Indexing convention:
        //   x[n-3] = d_mf_buf[(d_mf_idx + 0) & 3]  (oldest, before write)
        //   x[n-2] = d_mf_buf[(d_mf_idx + 1) & 3]
        //   x[n-1] = d_mf_buf[(d_mf_idx + 2) & 3]
        //   x[n]   = d_mf_buf[(d_mf_idx + 3) & 3]  (newest, the one we just wrote)
        // Write current sample, then advance idx.
        d_mf_buf[d_mf_idx] = interp_sample;
        d_mf_idx = (d_mf_idx + 1) & 3;

        // Matched filter output for segment-sync template +5,-5,-5,+5:
        // corr = +x[n-3] - x[n-2] - x[n-1] + x[n]
        // Indexed via the (post-increment) d_mf_idx so x[n-3] is at d_mf_idx.
        const float x_nm3 = d_mf_buf[d_mf_idx];
        const float x_nm2 = d_mf_buf[(d_mf_idx + 1) & 3];
        const float x_nm1 = d_mf_buf[(d_mf_idx + 2) & 3];
        const float x_n   = d_mf_buf[(d_mf_idx + 3) & 3];
        const float corr = x_nm3 - x_nm2 - x_nm1 + x_n;

        // EMA accumulator. Each bin is hit once per segment, so alpha
        // is effectively the per-segment update gain.
        d_integrator[d_counter] =
            (1.0f - d_alpha) * d_integrator[d_counter] + d_alpha * corr;

        d_symbol_index++;
        if (d_symbol_index >= ATSC_DATA_SEGMENT_LENGTH) d_symbol_index = 0;

        d_counter++;
        if (d_counter >= ATSC_DATA_SEGMENT_LENGTH) {
            // Find the bin with the largest matched-filter response.
            float best_val = d_integrator[0];
            int best_idx = 0;
            double sum_sq = 0.0;
            for (int i = 0; i < ATSC_DATA_SEGMENT_LENGTH; i++) {
                const float v = d_integrator[i];
                sum_sq += (double)v * (double)v;
                if (v > best_val) {
                    best_val = v;
                    best_idx = i;
                }
            }
            const double rms = std::sqrt(sum_sq / (double)ATSC_DATA_SEGMENT_LENGTH);
            const double snr_ratio = (rms > 1e-9) ? (double)best_val / rms : 0.0;

            // Sticky-lock + constrained search: when locked, prefer the
            // previously-locked bin and only switch within a small window
            // around it. The matched filter peak typically wanders by 1-2
            // bins/seg from per-sample timing-adjust noise; a narrow
            // window suppresses far-away false peaks while still allowing
            // small corrections.
            if (d_seg_locked && d_locked_idx >= 0 &&
                d_locked_idx < ATSC_DATA_SEGMENT_LENGTH) {
                // Re-search within ±W around d_locked_idx for the local max.
                static constexpr int SEARCH_W = 6;
                int local_best_idx = d_locked_idx;
                float local_best_val = d_integrator[d_locked_idx];
                for (int d = -SEARCH_W; d <= SEARCH_W; d++) {
                    int j = d_locked_idx + d;
                    if (j < 0) j += ATSC_DATA_SEGMENT_LENGTH;
                    if (j >= ATSC_DATA_SEGMENT_LENGTH) j -= ATSC_DATA_SEGMENT_LENGTH;
                    if (d_integrator[j] > local_best_val) {
                        local_best_val = d_integrator[j];
                        local_best_idx = j;
                    }
                }
                const float locked_val = d_integrator[d_locked_idx];
                if (locked_val >= d_sticky_fraction * best_val) {
                    // Locked bin still dominates globally — small adjust.
                    best_idx = local_best_idx;
                    best_val = local_best_val;
                    d_locked_idx = local_best_idx;
                } else {
                    // Locked bin has clearly lost; allow a global switch.
                    d_locked_idx = best_idx;
                    d_relocks++;
                }
            }

            const bool was_locked = d_seg_locked;
            if (d_seg_locked) {
                d_seg_locked = (snr_ratio >= d_unlock_threshold);
                if (!d_seg_locked) d_locked_idx = -1;
            } else {
                d_seg_locked = (snr_ratio >= d_lock_threshold);
                if (d_seg_locked) {
                    d_locked_idx = best_idx;
                    if (!was_locked) d_relocks++;
                }
            }

            // Timing-error gradient: discrete derivative of matched-filter
            // signal at the lock point. Same formula as gr-dtv atsc_sync.
            int corr_count = best_idx;
            d_timing_adjust = -d_sample_mem[corr_count--];
            if (corr_count < 0) corr_count = ATSC_DATA_SEGMENT_LENGTH - 1;
            d_timing_adjust -= d_sample_mem[corr_count--];
            if (corr_count < 0) corr_count = ATSC_DATA_SEGMENT_LENGTH - 1;
            d_timing_adjust += d_sample_mem[corr_count--];
            if (corr_count < 0) corr_count = ATSC_DATA_SEGMENT_LENGTH - 1;
            d_timing_adjust += d_sample_mem[corr_count--];

            d_symbol_index = SYMBOL_INDEX_OFFSET - 1 - best_idx;
            if (d_symbol_index < 0) d_symbol_index += ATSC_DATA_SEGMENT_LENGTH;
            d_counter = 0;

            d_seg_count++;
            if (d_debug && (d_seg_count % 256 == 0)) {
                std::fprintf(stderr,
                             "[sync_soft] seg=%llu peak=%+.2f rms=%.3f "
                             "snr_ratio=%.2f locked=%d best_idx=%d emitted=%llu "
                             "aligned=%llu (%.2f%%)\n",
                             (unsigned long long)d_seg_count, best_val, rms,
                             snr_ratio, (int)d_seg_locked, best_idx,
                             (unsigned long long)d_segs_emitted,
                             (unsigned long long)d_segs_aligned,
                             d_segs_emitted > 0
                                ? 100.0 * (double)d_segs_aligned / (double)d_segs_emitted
                                : 0.0);
            }
            (void)was_locked;
        }

        const bool will_emit = d_seg_locked || d_emit_when_unlocked;
        if (will_emit) {
            d_data_mem[d_symbol_index] = interp_sample;
            if (d_symbol_index >= (ATSC_DATA_SEGMENT_LENGTH - 1)) {
                float* out_seg = &out[d_output_produced * ATSC_DATA_SEGMENT_LENGTH];
                memcpy(out_seg, d_data_mem,
                       ATSC_DATA_SEGMENT_LENGTH * sizeof(float));
                d_output_produced++;
                d_segs_emitted++;

                // Track alignment: a correctly-locked segment has signs
                // +,-,-,+ at positions 0..3.
                if (out_seg[0] > 0 && out_seg[1] < 0 &&
                    out_seg[2] < 0 && out_seg[3] > 0) {
                    d_segs_aligned++;
                }
            }
        } else {
            if (d_symbol_index >= (ATSC_DATA_SEGMENT_LENGTH - 1)) {
                d_segs_held++;
            }
        }
    }

    consume_each(d_si);
    return d_output_produced;
}

} /* namespace atscplus */
} /* namespace gr */
