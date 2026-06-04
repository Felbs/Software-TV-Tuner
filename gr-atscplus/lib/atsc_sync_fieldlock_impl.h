/* -*- c++ -*- */
/*
 * Copyright 2026 gr-atscplus authors
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#ifndef INCLUDED_ATSCPLUS_ATSC_SYNC_FIELDLOCK_IMPL_H
#define INCLUDED_ATSCPLUS_ATSC_SYNC_FIELDLOCK_IMPL_H

#include <gnuradio/atscplus/atsc_sync_fieldlock.h>
#include <gnuradio/dtv/atsc_consts.h>
#include <gnuradio/filter/mmse_fir_interpolator_ff.h>
#include <gnuradio/filter/single_pole_iir.h>
#include <cstdint>
#include <vector>

namespace gr {
namespace atscplus {

class atsc_sync_fieldlock_impl : public atsc_sync_fieldlock
{
private:
    // -------- 4-tap soft sync state (mirrors atsc_sync_soft) --------
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

    float d_integrator[gr::dtv::ATSC_DATA_SEGMENT_LENGTH];

    int d_output_produced;

    // -------- Soft-detector tunables --------
    float d_alpha;
    float d_lock_threshold;
    float d_unlock_threshold;
    float d_sticky_fraction;
    bool d_emit_when_unlocked;
    bool d_debug;
    int d_locked_idx;
    float d_timing_gain_scale;
    float d_local_move_factor;
    int d_search_w;

    // -------- Field-Sync template (the new bit) --------
    static constexpr int FS_TPL_LEN = gr::dtv::ATSC_DATA_SEGMENT_LENGTH; // 832
    float d_fs_template[FS_TPL_LEN];
    // (idx, sign) pairs for nonzero template positions, ~641 entries
    std::vector<int> d_fs_active_idx;
    std::vector<float> d_fs_active_sign;

    int d_fs_window_w;          // half-width in samples
    float d_fs_threshold;       // correlation threshold for FS detection
    int d_fs_hold;              // segs to hold FS lock without seeing FS
    bool d_fs_drive;            // when FS-locked, override 4-tap bin search
    bool d_fs_hold_lock;        // when FS-locked, inhibit 4-tap unlock
    bool d_fs_correct;          // apply argmax offset correction

    bool d_fs_locked;           // FS lock established
    int  d_fs_anchor_idx;       // d_locked_idx as confirmed by last FS
    int  d_segs_since_fs;       // counter since last FS detection

    // -------- Stats --------
    uint64_t d_segs_emitted;
    uint64_t d_segs_held;
    uint64_t d_segs_aligned;
    uint64_t d_segs_total;
    uint64_t d_relocks;
    uint64_t d_seg_count;
    uint64_t d_fs_detections;
    uint64_t d_fs_corrections;     // segs where argmax offset was non-zero
    uint64_t d_fs_held_locks;      // segs where FS lock prevented 4-tap unlock
    uint64_t d_fs_drive_overrides; // segs where FS-driven bin overrode EMA pick

    void build_fs_template();
    // Returns true if FS detected (corr > thr); writes peak corr & offset.
    bool fs_check(const float* seg, float& peak_corr, int& peak_offset);

public:
    atsc_sync_fieldlock_impl(float rate);
    ~atsc_sync_fieldlock_impl() override;

    void reset();

    void forecast(int noutput_items, gr_vector_int& ninput_items_required) override;

    int general_work(int noutput_items,
                     gr_vector_int& ninput_items,
                     gr_vector_const_void_star& input_items,
                     gr_vector_void_star& output_items) override;
};

} /* namespace atscplus */
} /* namespace gr */

#endif /* INCLUDED_ATSCPLUS_ATSC_SYNC_FIELDLOCK_IMPL_H */
