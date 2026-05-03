/* -*- c++ -*- */
/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Instrumented atsc_fs_checker fork - prints diagnostics to stderr. */

#ifdef HAVE_CONFIG_H
#include "config.h"
#endif

#include "atsc_fs_checker_inst_impl.h"
#include "atsc_pnXXX_impl.h"
#include "atsc_syminfo_impl.h"
#include "atsc_types.h"
#include <gnuradio/dtv/atsc_consts.h>
#include <gnuradio/io_signature.h>
#include <cstdio>
#include <cmath>
#include <algorithm>

#define ATSC_SEGMENTS_PER_DATA_FIELD 313

using gr::dtv::ATSC_DATA_SEGMENT_LENGTH;
using gr::dtv::plinfo;

// Limits raised from upstream gr-dtv values (20/5) to (50/15) to tolerate
// the residual SRRC ISI in our synthetic IQ. Empirically: at SNR=25 dB AWGN
// the synthetic signal lands ~24 PN511 errors after the receive chain
// (vs 0 expected with no ISI). Below the relaxed threshold the equalizer
// never trains, RS sees garbage, and rs_clean_frac collapses to 0%. With
// 50/15 the agent's first run hit 99.1% RS-clean — this was the
// uncommitted change behind that baseline.
static const int PN511_ERROR_LIMIT = 50;
static const int PN63_ERROR_LIMIT = 15;

namespace gr {
namespace atscplus {

atsc_fs_checker_inst::sptr atsc_fs_checker_inst::make()
{
    return gnuradio::make_block_sptr<atsc_fs_checker_inst_impl>();
}

atsc_fs_checker_inst_impl::atsc_fs_checker_inst_impl()
    : gr::block("atscplus_atsc_fs_checker_inst",
                gr::io_signature::make(1, 1, ATSC_DATA_SEGMENT_LENGTH * sizeof(float)),
                gr::io_signature::make2(
                    2, 2, ATSC_DATA_SEGMENT_LENGTH * sizeof(float), sizeof(plinfo)))
{
    reset();
}

void atsc_fs_checker_inst_impl::reset()
{
    d_index = 0;
    std::memset(d_sample_sr, 0, sizeof(d_sample_sr));
    std::memset(d_tag_sr, 0, sizeof(d_tag_sr));
    std::memset(d_bit_sr, 0, sizeof(d_bit_sr));
    d_field_num = 0;
    d_segment_num = 0;

    d_total_segments = 0;
    d_pn511_hits = 0;
    d_field1_hits = 0;
    d_field2_hits = 0;
    d_pn63_uncertain = 0;
    d_min_pn511_errors_window = 511;
    d_min_pn63_errors_window = 63;
    std::memset(d_pn63_hist, 0, sizeof(d_pn63_hist));
    std::memset(d_pn511_hist, 0, sizeof(d_pn511_hist));

    // Tier-3 telemetry init.
    d_t0 = std::chrono::steady_clock::now();
    d_window_sum_abs = 0.0;
    d_window_sum_sq = 0.0;
    d_window_max_abs = 0.0f;
    d_window_sample_count = 0;
    d_window_pn511_hits_start = 0;
    d_window_field1_start = 0;
    d_window_field2_start = 0;
    d_window_uncertain_start = 0;
    d_segs_at_last_fs = 0;
    d_last_fs_gap = 0;
    d_window_fs_gap_sum = 0;
    d_window_fs_gap_count = 0;
    d_window_fs_gap_min = ~0ull;
    d_window_fs_gap_max = 0;
}

atsc_fs_checker_inst_impl::~atsc_fs_checker_inst_impl()
{
    std::fprintf(stderr,
                 "[fs_checker_inst FINAL] segments=%llu pn511_hits=%llu "
                 "field1=%llu field2=%llu uncertain=%llu min_pn511_err=%d min_pn63_err=%d\n",
                 (unsigned long long)d_total_segments,
                 (unsigned long long)d_pn511_hits,
                 (unsigned long long)d_field1_hits,
                 (unsigned long long)d_field2_hits,
                 (unsigned long long)d_pn63_uncertain,
                 d_min_pn511_errors_window,
                 d_min_pn63_errors_window);
    std::fprintf(stderr, "[fs_checker_inst FINAL] PN63 error histogram (errors -> count):\n");
    for (int e = 0; e <= 63; e++) {
        if (d_pn63_hist[e]) {
            std::fprintf(stderr, "  pn63_err=%2d : %llu\n", e,
                         (unsigned long long)d_pn63_hist[e]);
        }
    }
    std::fprintf(stderr, "[fs_checker_inst FINAL] PN511 error histogram (binned by 16):\n");
    for (int b = 0; b < 32; b++) {
        if (d_pn511_hist[b]) {
            std::fprintf(stderr, "  pn511_err [%3d-%3d): %llu\n", b * 16, (b + 1) * 16,
                         (unsigned long long)d_pn511_hist[b]);
        }
    }
}

int atsc_fs_checker_inst_impl::general_work(int noutput_items,
                                            gr_vector_int& ninput_items,
                                            gr_vector_const_void_star& input_items,
                                            gr_vector_void_star& output_items)
{
    auto in = static_cast<const float*>(input_items[0]);
    auto out = static_cast<float*>(output_items[0]);
    auto out_pl = static_cast<plinfo*>(output_items[1]);

    int output_produced = 0;

    for (int i = 0; i < noutput_items; i++) {
        d_total_segments++;

        // Tier-3: accumulate per-segment input level (post-AGC, post-sync)
        // for AGC drift telemetry. Sum |x| across the whole segment.
        const float* seg = &in[i * ATSC_DATA_SEGMENT_LENGTH];
        for (int j = 0; j < ATSC_DATA_SEGMENT_LENGTH; j++) {
            float a = std::fabs(seg[j]);
            d_window_sum_abs += a;
            d_window_sum_sq  += (double)seg[j] * (double)seg[j];
            if (a > d_window_max_abs) d_window_max_abs = a;
        }
        d_window_sample_count += ATSC_DATA_SEGMENT_LENGTH;

        int errors_511 = 0;
        for (int j = 0; j < LENGTH_511; j++) {
            errors_511 +=
                (in[i * ATSC_DATA_SEGMENT_LENGTH + j + OFFSET_511] >= 0) ^ atsc_pn511[j];
        }
        int bin = std::min(errors_511 / 16, 31);
        d_pn511_hist[bin]++;
        if (errors_511 < d_min_pn511_errors_window) d_min_pn511_errors_window = errors_511;

        int errors_63 = 0;

        if (errors_511 < PN511_ERROR_LIMIT) {
            d_pn511_hits++;
            errors_63 = 0;
            for (int j = 0; j < LENGTH_2ND_63; j++)
                errors_63 += (in[i * ATSC_DATA_SEGMENT_LENGTH + j + OFFSET_2ND_63] >= 0) ^
                             atsc_pn63[j];
            if (errors_63 >= 0 && errors_63 <= 63) d_pn63_hist[errors_63]++;
            int dist_to_field1 = errors_63;
            int dist_to_field2 = LENGTH_2ND_63 - errors_63;
            int min_dist = std::min(dist_to_field1, dist_to_field2);
            if (min_dist < d_min_pn63_errors_window) d_min_pn63_errors_window = min_dist;

            if (errors_63 <= PN63_ERROR_LIMIT) {
                d_field1_hits++;
                d_field_num = 1;
                d_segment_num = -1;
                // Tier-3: record FS gap.
                uint64_t gap = d_total_segments - d_segs_at_last_fs;
                d_segs_at_last_fs = d_total_segments;
                d_last_fs_gap = gap;
                if (d_window_fs_gap_count > 0 || gap < 1000) {
                    d_window_fs_gap_sum += gap;
                    d_window_fs_gap_count++;
                    if (gap < d_window_fs_gap_min) d_window_fs_gap_min = gap;
                    if (gap > d_window_fs_gap_max) d_window_fs_gap_max = gap;
                }
            } else if (errors_63 >= (LENGTH_2ND_63 - PN63_ERROR_LIMIT)) {
                d_field2_hits++;
                d_field_num = 2;
                d_segment_num = -1;
                uint64_t gap = d_total_segments - d_segs_at_last_fs;
                d_segs_at_last_fs = d_total_segments;
                d_last_fs_gap = gap;
                if (d_window_fs_gap_count > 0 || gap < 1000) {
                    d_window_fs_gap_sum += gap;
                    d_window_fs_gap_count++;
                    if (gap < d_window_fs_gap_min) d_window_fs_gap_min = gap;
                    if (gap > d_window_fs_gap_max) d_window_fs_gap_max = gap;
                }
            } else {
                d_pn63_uncertain++;
            }
        }

        if (d_field_num == 1 || d_field_num == 2) {
            std::memcpy(&out[output_produced * ATSC_DATA_SEGMENT_LENGTH],
                        &in[i * ATSC_DATA_SEGMENT_LENGTH],
                        ATSC_DATA_SEGMENT_LENGTH * sizeof(float));

            plinfo pli_out;
            // Always use set_regular_seg(field2, segno) — the GR stock
            // equalizer keys on segno==-1 to detect sync segments and
            // train its taps. Calling set_field_sync1/2 (theoretically
            // more correct) leaves segno=0 and the equalizer never
            // trains, regressing stock from 99.1% to 0.3% RS-clean.
            pli_out.set_regular_seg((d_field_num == 2), d_segment_num);

            d_segment_num++;
            if (d_segment_num > (ATSC_SEGMENTS_PER_DATA_FIELD - 1)) {
                d_field_num = 0;
                d_segment_num = 0;
            } else {
                out_pl[output_produced++] = pli_out;
            }
        }

        if (d_total_segments % LOG_EVERY == 0) {
            auto now = std::chrono::steady_clock::now();
            double t = std::chrono::duration<double>(now - d_t0).count();

            // Tier-3 window stats.
            double mean_abs = (d_window_sample_count > 0)
                ? d_window_sum_abs / (double)d_window_sample_count : 0.0;
            double rms = (d_window_sample_count > 0)
                ? std::sqrt(d_window_sum_sq / (double)d_window_sample_count) : 0.0;
            uint64_t fs_in_window = (d_pn511_hits - d_window_pn511_hits_start);
            uint64_t f1_in_window = (d_field1_hits - d_window_field1_start);
            uint64_t f2_in_window = (d_field2_hits - d_window_field2_start);
            double mean_fs_gap = (d_window_fs_gap_count > 0)
                ? (double)d_window_fs_gap_sum / (double)d_window_fs_gap_count : 0.0;

            std::fprintf(stderr,
                         "[fs_check t=%6.2fs @%llu segs] pn511_hits=%llu "
                         "f1=%llu f2=%llu uncertain=%llu min_pn511_e=%d "
                         "min_pn63_e=%d | win: f1=%llu f2=%llu mean|x|=%.3f "
                         "rms=%.3f maxabs=%.2f fs_gap[min/mean/max]=%llu/%.1f/%llu n=%llu\n",
                         t,
                         (unsigned long long)d_total_segments,
                         (unsigned long long)d_pn511_hits,
                         (unsigned long long)d_field1_hits,
                         (unsigned long long)d_field2_hits,
                         (unsigned long long)d_pn63_uncertain,
                         d_min_pn511_errors_window,
                         d_min_pn63_errors_window,
                         (unsigned long long)f1_in_window,
                         (unsigned long long)f2_in_window,
                         mean_abs, rms, d_window_max_abs,
                         (unsigned long long)d_window_fs_gap_min,
                         mean_fs_gap,
                         (unsigned long long)d_window_fs_gap_max,
                         (unsigned long long)d_window_fs_gap_count);
            std::fflush(stderr);

            d_min_pn511_errors_window = 511;
            d_min_pn63_errors_window = 63;
            d_window_sum_abs = 0.0;
            d_window_sum_sq = 0.0;
            d_window_max_abs = 0.0f;
            d_window_sample_count = 0;
            d_window_pn511_hits_start = d_pn511_hits;
            d_window_field1_start = d_field1_hits;
            d_window_field2_start = d_field2_hits;
            d_window_uncertain_start = d_pn63_uncertain;
            d_window_fs_gap_sum = 0;
            d_window_fs_gap_count = 0;
            d_window_fs_gap_min = ~0ull;
            d_window_fs_gap_max = 0;
        }
    }

    consume_each(noutput_items);
    return output_produced;
}

} /* namespace atscplus */
} /* namespace gr */
