/* -*- c++ -*- */
/*
 * gr-atscplus — fused widely-linear front end impl (2026-07-27)
 * SPDX-License-Identifier: GPL-3.0-or-later
 */
#ifndef INCLUDED_ATSCPLUS_ATSC_WL_FRONTEND_IMPL_H
#define INCLUDED_ATSCPLUS_ATSC_WL_FRONTEND_IMPL_H

#include "atsc_syminfo_impl.h"
#include "atsc_types.h"
#include <gnuradio/atscplus/atsc_wl_frontend.h>
#include <gnuradio/dtv/atsc_consts.h>
#include <cstdint>
#include <vector>

namespace gr {
namespace atscplus {

class atsc_wl_frontend_impl : public atsc_wl_frontend
{
private:
    // ── inlined MMSE interpolator ─────────────────────────────────────────
    // Taps table extracted at construction from GR's own
    // mmse_fir_interpolator_ff (impulse-probing each mu step), so the values
    // are IDENTICAL to what atsc_sync_soft computes — but applied inline to
    // both the real and imaginary plane in one pass, with zero library-call
    // overhead (the measured overhead was ~64 ns/call at 10.76 Msym/s).
    //
    // 2026-07-29: the probe must be run through a PADDED, zero-filled buffer —
    // see build_interp_table() for the NaN-poisoning bug this cost us.
    int d_int_ntaps  = 0; // 8 for GR 3.10
    int d_int_nsteps = 0; // 128 for GR 3.10
    std::vector<float> d_itaps; // (nsteps+1) x ntaps, row-major

    // Rebuild d_itaps by impulse-probing GR's interpolator. Returns false if
    // the resulting table is not entirely finite (which must never happen with
    // the padded probe, but is checked because an all-NaN table is invisible
    // until the decode has already failed).
    bool build_interp_table();

    // ── timing-recovery state (verbatim port of atsc_sync_soft) ───────────
    double d_rx_clock_to_symbol_freq;
    int d_si;
    double d_w;
    double d_mu;
    int d_incr;

    float d_sample_mem[gr::dtv::ATSC_DATA_SEGMENT_LENGTH];
    float d_data_mem[gr::dtv::ATSC_DATA_SEGMENT_LENGTH];
    float d_data_mem_imag[gr::dtv::ATSC_DATA_SEGMENT_LENGTH];

    float d_mf_buf[4];
    int d_mf_idx;

    double d_timing_adjust;
    int d_counter;
    int d_symbol_index;
    bool d_seg_locked;
    float d_integrator[gr::dtv::ATSC_DATA_SEGMENT_LENGTH];
    int d_output_produced;

    float d_alpha;
    float d_lock_threshold;
    float d_unlock_threshold;
    float d_sticky_fraction;
    bool d_emit_when_unlocked;
    bool d_debug;
    int d_locked_idx;
    float d_timing_gain_scale;

    bool d_adaptive;
    float d_acquire_lock_thresh;
    float d_acquire_unlock_thresh;
    float d_steady_lock_thresh;
    float d_steady_unlock_thresh;
    int d_steady_after_segs;
    int d_consec_locked_segs;
    bool d_in_steady_state;

    uint64_t d_segs_emitted;
    uint64_t d_segs_held;
    uint64_t d_segs_aligned;
    uint64_t d_relocks;
    uint64_t d_seg_count;

    // ── framing state (verbatim port of atsc_fs_checker_inst) ─────────────
    static constexpr int OFFSET_511 = 4;
    static constexpr int LENGTH_511 = 511;
    static constexpr int OFFSET_2ND_63 = 578;
    static constexpr int LENGTH_2ND_63 = 63;

    int d_field_num;
    int d_segment_num;
    bool d_fs_validate_enabled;
    bool d_fs_locked;
    uint64_t d_segs_since_accepted_fs;
    uint64_t d_fs_accepted;
    uint64_t d_fs_rejected_early;
    uint64_t d_fs_rejected_late;
    int d_fs_tol_low;
    int d_fs_tol_high;
    int d_coast_run;
    uint64_t d_coast_total;
    int d_clean_fs_streak;

    // ── ACQUISITION WATCHDOG (2026-07-29) — additive, bounded ─────────────
    // Defence in depth for "the front end never locks at all": if no field
    // sync has been accepted within a bounded window, reset the timing and
    // framing state (and re-probe the interpolator table) and try again — with
    // a HARD retry cap and an explicit success condition, per the respawn-
    // safety law. Never touches a healthy stream: every branch is gated on
    // d_fs_accepted == 0.
    bool d_wd_enabled;
    uint64_t d_wd_window;     // segments per attempt
    int d_wd_max;             // max resets
    int d_wd_resets;
    uint64_t d_wd_origin;     // d_seg_count at the start of this attempt
    bool d_wd_gave_up;
    bool d_wd_recovered;
    uint64_t d_first_align_seg; // d_seg_count of the first aligned segment
    uint64_t d_first_fs_seg;    // d_seg_count of the first accepted field sync

    void watchdog_reset();

    // process one completed, emit-gated segment (d_data_mem/_imag) through
    // the field-sync framing; writes to out arrays and bumps output_produced
    // when the segment survives. Returns via reference-style members.
    void handle_segment(float* out_r,
                        ::gr::dtv::plinfo* out_pl,
                        float* out_i,
                        int& output_produced);

public:
    atsc_wl_frontend_impl(float rate);
    ~atsc_wl_frontend_impl() override;

    void reset();

    void forecast(int noutput_items, gr_vector_int& ninput_items_required) override;

    int general_work(int noutput_items,
                     gr_vector_int& ninput_items,
                     gr_vector_const_void_star& input_items,
                     gr_vector_void_star& output_items) override;
};

} /* namespace atscplus */
} /* namespace gr */

#endif /* INCLUDED_ATSCPLUS_ATSC_WL_FRONTEND_IMPL_H */
