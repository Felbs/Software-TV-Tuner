/* -*- c++ -*- */
/*
 * Copyright 2026 gr-atscplus authors
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#ifndef INCLUDED_ATSCPLUS_ATSC_SYNC_PATHA_IMPL_H
#define INCLUDED_ATSCPLUS_ATSC_SYNC_PATHA_IMPL_H

#include <gnuradio/atscplus/atsc_sync_pathA.h>
#include <gnuradio/dtv/atsc_consts.h>
#include <gnuradio/filter/mmse_fir_interpolator_ff.h>
#include <gnuradio/filter/single_pole_iir.h>
#include <cstdint>
#include <vector>

namespace gr {
namespace atscplus {

class atsc_sync_pathA_impl : public atsc_sync_pathA
{
private:
    // -------- Soft-base state --------
    gr::filter::single_pole_iir<float, float, float> d_loop;
    gr::filter::mmse_fir_interpolator_ff d_interp;

    double d_rx_clock_to_symbol_freq;
    int d_si;
    double d_w;
    double d_mu;
    int d_incr;

    float d_sample_mem[gr::dtv::ATSC_DATA_SEGMENT_LENGTH];
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

    // -------- Tunables --------
    float d_alpha;
    float d_lock_threshold;
    float d_unlock_threshold;
    float d_sticky_fraction;
    float d_freeze_snr;            // PA: freeze locked_idx below this snr_ratio
    bool  d_timing_freeze;         // PA: also freeze timing-adjust update under low SNR
    bool  d_fs_reset_enabled;      // PA: FS observation overrides d_symbol_index
    bool  d_emit_when_unlocked;
    bool  d_debug;
    float d_timing_gain_scale;

    // -------- Sliding-FS state --------
    static constexpr int FS_TPL_LEN = gr::dtv::ATSC_DATA_SEGMENT_LENGTH; // 832
    static constexpr int RING_LEN = 2 * FS_TPL_LEN;                     // 1664
    float d_ring[RING_LEN];
    uint64_t d_total_symbols;
    float d_fs_template[FS_TPL_LEN];
    std::vector<int> d_fs_active_idx;
    std::vector<float> d_fs_active_sign;

    int d_fs_check_period_locked;
    int d_fs_check_period_bootstrap;
    float d_fs_threshold;
    int d_fs_hold;

    bool d_fs_locked;
    uint64_t d_fs_anchor_symbol;
    int d_fs_segs_since;
    int d_symbols_until_check;

    // -------- Stats --------
    uint64_t d_segs_emitted;
    uint64_t d_segs_aligned;
    uint64_t d_relocks;
    uint64_t d_seg_count;
    uint64_t d_fs_detections;
    uint64_t d_fs_resets;
    uint64_t d_locked_idx_freezes;

    void build_fs_template();
    float fs_slide_search(int& ring_off, int range_lo, int range_hi);

public:
    atsc_sync_pathA_impl(float rate);
    ~atsc_sync_pathA_impl() override;

    void reset();

    void forecast(int noutput_items, gr_vector_int& ninput_items_required) override;

    int general_work(int noutput_items,
                     gr_vector_int& ninput_items,
                     gr_vector_const_void_star& input_items,
                     gr_vector_void_star& output_items) override;
};

} /* namespace atscplus */
} /* namespace gr */

#endif /* INCLUDED_ATSCPLUS_ATSC_SYNC_PATHA_IMPL_H */
