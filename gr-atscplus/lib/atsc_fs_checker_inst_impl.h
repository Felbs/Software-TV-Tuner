/* -*- c++ -*- */
/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Instrumented atsc_fs_checker fork - adds diagnostic counters. */

#ifndef INCLUDED_ATSCPLUS_ATSC_FS_CHECKER_INST_IMPL_H
#define INCLUDED_ATSCPLUS_ATSC_FS_CHECKER_INST_IMPL_H

#include "atsc_syminfo_impl.h"
#include <gnuradio/atscplus/atsc_fs_checker_inst.h>
#include <chrono>
#include <cstdint>

namespace gr {
namespace atscplus {

class atsc_fs_checker_inst_impl : public atsc_fs_checker_inst
{
private:
    static constexpr int SRSIZE = 1024;
    int d_index;
    float d_sample_sr[SRSIZE];
    ::gr::dtv::atsc::syminfo d_tag_sr[SRSIZE];
    unsigned char d_bit_sr[SRSIZE];
    int d_field_num;
    int d_segment_num;

    static constexpr int OFFSET_511 = 4;
    static constexpr int LENGTH_511 = 511;
    static constexpr int OFFSET_2ND_63 = 578;
    static constexpr int LENGTH_2ND_63 = 63;

    uint64_t d_total_segments;
    uint64_t d_pn511_hits;
    uint64_t d_field1_hits;
    uint64_t d_field2_hits;
    uint64_t d_pn63_uncertain;
    int d_min_pn511_errors_window;
    int d_min_pn63_errors_window;
    uint64_t d_pn63_hist[64];
    uint64_t d_pn511_hist[32];
    static constexpr uint64_t LOG_EVERY = 50000;

    // Tier-3 telemetry. Track post-AGC, post-sync signal level (mean abs and
    // peak) per window — this is the cleanest available proxy for AGC drift
    // because AGC is the immediately upstream block. Also track inter-field-
    // sync segment cadence: if sync timing drifts, segments-per-field-sync
    // wanders away from 313.
    std::chrono::steady_clock::time_point d_t0;
    double  d_window_sum_abs;
    double  d_window_sum_sq;
    float   d_window_max_abs;
    uint64_t d_window_sample_count;
    // Per-window field-sync metrics (the "FS511 errors" already tracked are
    // global mins; these are per-window means).
    uint64_t d_window_pn511_hits_start;
    uint64_t d_window_field1_start;
    uint64_t d_window_field2_start;
    uint64_t d_window_uncertain_start;
    uint64_t d_segs_at_last_fs;
    uint64_t d_last_fs_gap;          // segments since previous FS hit
    uint64_t d_window_fs_gap_sum;    // sum of FS gaps in window
    uint64_t d_window_fs_gap_count;  // count of FS gaps in window
    uint64_t d_window_fs_gap_min;
    uint64_t d_window_fs_gap_max;

    inline static int wrap(int index) { return index & (SRSIZE - 1); }
    inline static int incr(int index) { return wrap(index + 1); }
    inline static int decr(int index) { return wrap(index - 1); }

public:
    atsc_fs_checker_inst_impl();
    ~atsc_fs_checker_inst_impl() override;

    void reset();

    int general_work(int noutput_items,
                     gr_vector_int& ninput_items,
                     gr_vector_const_void_star& input_items,
                     gr_vector_void_star& output_items) override;
};

} /* namespace atscplus */
} /* namespace gr */

#endif
