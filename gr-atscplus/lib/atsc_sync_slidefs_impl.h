/* -*- c++ -*- */
/*
 * Copyright 2026 gr-atscplus authors
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#ifndef INCLUDED_ATSCPLUS_ATSC_SYNC_SLIDEFS_IMPL_H
#define INCLUDED_ATSCPLUS_ATSC_SYNC_SLIDEFS_IMPL_H

#include <gnuradio/atscplus/atsc_sync_slidefs.h>
#include <gnuradio/dtv/atsc_consts.h>
#include <gnuradio/filter/mmse_fir_interpolator_ff.h>
#include <gnuradio/filter/single_pole_iir.h>
#include <cstdint>
#include <vector>

namespace gr {
namespace atscplus {

class atsc_sync_slidefs_impl : public atsc_sync_slidefs
{
private:
    // -------- Soft-base state (mirrors atsc_sync_soft / fieldlock) --------
    gr::filter::single_pole_iir<float, float, float> d_loop;
    gr::filter::mmse_fir_interpolator_ff d_interp;

    double d_rx_clock_to_symbol_freq;
    int d_si;
    double d_w;
    double d_mu;
    int d_incr;

    // d_sample_mem[i] holds the i-th interpolated sample of the CURRENT
    // segment-cycle. Used for the timing-error gradient computation.
    float d_sample_mem[gr::dtv::ATSC_DATA_SEGMENT_LENGTH];
    // d_data_mem[d_symbol_index] receives interpolated symbols and is
    // emitted as a 832-float vector when d_symbol_index hits 831.
    float d_data_mem[gr::dtv::ATSC_DATA_SEGMENT_LENGTH];

    float d_mf_buf[4];
    int d_mf_idx;

    double d_timing_adjust;
    int d_counter;
    int d_symbol_index;
    bool d_seg_locked;
    int d_locked_idx;

    float d_integrator[gr::dtv::ATSC_DATA_SEGMENT_LENGTH];
    int d_output_produced;

    // -------- Soft-base tunables --------
    float d_alpha;
    float d_lock_threshold;
    float d_unlock_threshold;
    bool d_emit_when_unlocked;
    bool d_debug;
    float d_timing_gain_scale;

    // -------- Sliding-FS state --------
    static constexpr int FS_TPL_LEN = gr::dtv::ATSC_DATA_SEGMENT_LENGTH; // 832
    static constexpr int RING_LEN = 2 * FS_TPL_LEN;                     // 1664
    float d_ring[RING_LEN];                  // continuous symbol ring
    uint64_t d_total_symbols;                // monotonic interpolated-symbol counter
    float d_fs_template[FS_TPL_LEN];         // ±1 / 0 template
    std::vector<int> d_fs_active_idx;
    std::vector<float> d_fs_active_sign;

    int d_fs_check_period_locked;            // every-N symbols check after lock
    int d_fs_check_period_bootstrap;         // every-N symbols pre-lock
    int d_fs_drift_w;                        // max drift between adjacent FSes
    float d_fs_threshold;
    int d_fs_hold;                           // emitted-segs to keep FS lock w/o new FS

    bool d_fs_locked;                        // sliding-FS lock established
    uint64_t d_fs_anchor_symbol;             // absolute symbol# where last FS started
    int d_fs_segs_since;                     // segments since last FS hit
    int d_symbols_until_check;               // counter to next FS check

    // -------- Stats --------
    uint64_t d_segs_emitted;
    uint64_t d_segs_aligned;
    uint64_t d_relocks;
    uint64_t d_seg_count;
    uint64_t d_fs_detections;
    uint64_t d_fs_anchor_corrections; // segs where anchor moved by >0
    uint64_t d_fs_anchor_drifts;      // small drifts (|d|<=fs_drift_w)
    uint64_t d_fs_anchor_jumps;       // big jumps (re-acquire)

    void build_fs_template();
    // Sliding correlation across ring; returns peak corr & ring offset of FS start.
    // ring_off is in [0, RING_LEN-FS_TPL_LEN] (note: NOT cyclic — correlations
    // requiring cyclic wrap are skipped, since we have a 2-segment ring).
    float fs_slide_search(int& ring_off, int range_lo, int range_hi);

public:
    atsc_sync_slidefs_impl(float rate);
    ~atsc_sync_slidefs_impl() override;

    void reset();

    void forecast(int noutput_items, gr_vector_int& ninput_items_required) override;

    int general_work(int noutput_items,
                     gr_vector_int& ninput_items,
                     gr_vector_const_void_star& input_items,
                     gr_vector_void_star& output_items) override;
};

} /* namespace atscplus */
} /* namespace gr */

#endif /* INCLUDED_ATSCPLUS_ATSC_SYNC_SLIDEFS_IMPL_H */
