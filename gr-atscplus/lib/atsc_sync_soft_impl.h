/* -*- c++ -*- */
/*
 * Copyright 2026 gr-atscplus authors
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#ifndef INCLUDED_ATSCPLUS_ATSC_SYNC_SOFT_IMPL_H
#define INCLUDED_ATSCPLUS_ATSC_SYNC_SOFT_IMPL_H

#include <gnuradio/atscplus/atsc_sync_soft.h>
#include <gnuradio/dtv/atsc_consts.h>
#include <gnuradio/filter/mmse_fir_interpolator_ff.h>
#include <gnuradio/filter/single_pole_iir.h>
#include <cstdint>

namespace gr {
namespace atscplus {

class atsc_sync_soft_impl : public atsc_sync_soft
{
private:
    gr::filter::single_pole_iir<float, float, float> d_loop;
    gr::filter::mmse_fir_interpolator_ff d_interp;

    double d_rx_clock_to_symbol_freq;
    int d_si;
    double d_w;   // Tx/Rx clock period ratio
    double d_mu;  // fractional sample delay [0,1]
    int d_incr;

    float d_sample_mem[gr::dtv::ATSC_DATA_SEGMENT_LENGTH];
    float d_data_mem[gr::dtv::ATSC_DATA_SEGMENT_LENGTH];

    // 4-tap FIFO of recent interpolated symbols for the matched filter.
    float d_mf_buf[4];
    int d_mf_idx;

    double d_timing_adjust;
    int d_counter;
    int d_symbol_index;
    bool d_seg_locked;

    // Float-precision EMA integrator. d_integrator[k] holds the
    // smoothed matched-filter response observed at counter==k.
    float d_integrator[gr::dtv::ATSC_DATA_SEGMENT_LENGTH];

    int d_output_produced;

    // Tunables
    float d_alpha;             // EMA rate per sample
    float d_lock_threshold;    // peak/RMS to acquire lock
    float d_unlock_threshold;  // peak/RMS to hold lock
    float d_sticky_fraction;   // sticky-lock: stay on d_locked_idx while its value >= sticky*max
    bool d_emit_when_unlocked;
    bool d_debug;
    int d_locked_idx;          // currently-locked argmax bin (-1 = unlocked)
    float d_timing_gain_scale; // multiplier on timing_adjust gain when locked

    // Stats
    uint64_t d_segs_emitted;
    uint64_t d_segs_held;
    uint64_t d_segs_aligned;   // sign(out[0..3]) == +,-,-,+
    uint64_t d_segs_total;
    uint64_t d_relocks;
    uint64_t d_seg_count;      // increments every 832 samples (for periodic logging)

public:
    atsc_sync_soft_impl(float rate);
    ~atsc_sync_soft_impl() override;

    void reset();

    void forecast(int noutput_items, gr_vector_int& ninput_items_required) override;

    int general_work(int noutput_items,
                     gr_vector_int& ninput_items,
                     gr_vector_const_void_star& input_items,
                     gr_vector_void_star& output_items) override;
};

} /* namespace atscplus */
} /* namespace gr */

#endif /* INCLUDED_ATSCPLUS_ATSC_SYNC_SOFT_IMPL_H */
