/* -*- c++ -*- */
/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Tunable atsc_sync fork - parameterized lock/unlock thresholds, optional always-emit. */

#ifdef HAVE_CONFIG_H
#include "config.h"
#endif

#include "atsc_sync_tunable_impl.h"
#include "atsc_types.h"
#include <gnuradio/io_signature.h>
#include <cstdio>

using namespace gr::dtv;

namespace gr {
namespace atscplus {

static const double LOOP_FILTER_TAP = 0.0005;
static const double ADJUSTMENT_GAIN = 1.0e-5 / (10 * ATSC_DATA_SEGMENT_LENGTH);
static const int SYMBOL_INDEX_OFFSET = 3;
static const signed char SSI_MIN = -16;
static const signed char SSI_MAX = 15;

atsc_sync_tunable::sptr atsc_sync_tunable::make(float rate, int min_lock_corr,
                                                int unlock_corr, bool emit_when_unlocked)
{
    return gnuradio::make_block_sptr<atsc_sync_tunable_impl>(rate, min_lock_corr,
                                                              unlock_corr, emit_when_unlocked);
}

atsc_sync_tunable_impl::atsc_sync_tunable_impl(float rate, int min_lock_corr,
                                                int unlock_corr, bool emit_when_unlocked)
    : gr::block("atscplus_atsc_sync_tunable",
                io_signature::make(1, 1, sizeof(float)),
                io_signature::make(1, 1, ATSC_DATA_SEGMENT_LENGTH * sizeof(float))),
      d_rx_clock_to_symbol_freq(rate / ATSC_SYMBOL_RATE),
      d_si(0)
{
    d_loop.set_taps(LOOP_FILTER_TAP);
    d_min_lock_corr = min_lock_corr;
    d_unlock_corr = unlock_corr;
    d_emit_when_unlocked = emit_when_unlocked;
    d_segs_emitted = 0;
    d_segs_held = 0;
    std::fprintf(stderr,
                 "[sync_tunable] rate=%.0f min_lock_corr=%d unlock_corr=%d emit_when_unlocked=%d\n",
                 rate, d_min_lock_corr, d_unlock_corr, (int)d_emit_when_unlocked);
    reset();
}

void atsc_sync_tunable_impl::reset()
{
    d_w = d_rx_clock_to_symbol_freq;
    d_mu = 0.5;
    d_timing_adjust = 0;
    d_counter = 0;
    d_symbol_index = 0;
    d_seg_locked = false;
    d_sr = 0;
    memset(d_sample_mem, 0, ATSC_DATA_SEGMENT_LENGTH * sizeof(*d_sample_mem));
    memset(d_data_mem, 0, ATSC_DATA_SEGMENT_LENGTH * sizeof(*d_data_mem));
    memset(d_integrator, SSI_MIN, ATSC_DATA_SEGMENT_LENGTH * sizeof(*d_integrator));
}

atsc_sync_tunable_impl::~atsc_sync_tunable_impl()
{
    std::fprintf(stderr,
                 "[sync_tunable FINAL] segs_emitted=%llu segs_held=%llu\n",
                 (unsigned long long)d_segs_emitted,
                 (unsigned long long)d_segs_held);
}

void atsc_sync_tunable_impl::forecast(int noutput_items, gr_vector_int& ninput_items_required)
{
    unsigned ninputs = ninput_items_required.size();
    for (unsigned i = 0; i < ninputs; i++)
        ninput_items_required[i] =
            static_cast<int>(noutput_items * d_rx_clock_to_symbol_freq *
                             ATSC_DATA_SEGMENT_LENGTH) + 1500 - 1;
}

int atsc_sync_tunable_impl::general_work(int noutput_items,
                                         gr_vector_int& ninput_items,
                                         gr_vector_const_void_star& input_items,
                                         gr_vector_void_star& output_items)
{
    const float* in = static_cast<const float*>(input_items[0]);
    float* out = static_cast<float*>(output_items[0]);

    float interp_sample;
    d_si = 0;

    for (d_output_produced = 0; d_output_produced < noutput_items &&
                                (d_si + (int)d_interp.ntaps()) < ninput_items[0];) {
        interp_sample = d_interp.interpolate(&in[d_si], d_mu);
        d_mu += ADJUSTMENT_GAIN * 1e3 * d_timing_adjust;

        double s = d_mu + d_w;
        double float_incr = floor(s);
        d_mu = s - float_incr;
        d_incr = (int)float_incr;
        assert(d_incr >= 1 && d_incr <= 3);
        d_si += d_incr;

        d_sample_mem[d_counter] = interp_sample;
        int bit = (interp_sample < 0 ? 0 : 1);
        d_sr = ((bit & 1) << 3) | (d_sr >> 1);

        d_integrator[d_counter] += ((d_sr == 0x9) ? +2 : -1);
        if (d_integrator[d_counter] < SSI_MIN) d_integrator[d_counter] = SSI_MIN;
        if (d_integrator[d_counter] > SSI_MAX) d_integrator[d_counter] = SSI_MAX;

        d_symbol_index++;
        if (d_symbol_index >= ATSC_DATA_SEGMENT_LENGTH) d_symbol_index = 0;

        d_counter++;
        if (d_counter >= ATSC_DATA_SEGMENT_LENGTH) {
            int best_correlation_value = d_integrator[0];
            int best_correlation_index = 0;
            for (int i = 1; i < ATSC_DATA_SEGMENT_LENGTH; i++)
                if (d_integrator[i] > best_correlation_value) {
                    best_correlation_value = d_integrator[i];
                    best_correlation_index = i;
                }

            // Hysteresis: stricter to acquire lock, easier to keep it
            if (d_seg_locked) {
                d_seg_locked = best_correlation_value >= d_unlock_corr;
            } else {
                d_seg_locked = best_correlation_value >= d_min_lock_corr;
            }

            int corr_count = best_correlation_index;
            d_timing_adjust = -d_sample_mem[corr_count--];
            if (corr_count < 0) corr_count = ATSC_DATA_SEGMENT_LENGTH - 1;
            d_timing_adjust -= d_sample_mem[corr_count--];
            if (corr_count < 0) corr_count = ATSC_DATA_SEGMENT_LENGTH - 1;
            d_timing_adjust += d_sample_mem[corr_count--];
            if (corr_count < 0) corr_count = ATSC_DATA_SEGMENT_LENGTH - 1;
            d_timing_adjust += d_sample_mem[corr_count--];

            d_symbol_index = SYMBOL_INDEX_OFFSET - 1 - best_correlation_index;
            if (d_symbol_index < 0) d_symbol_index += ATSC_DATA_SEGMENT_LENGTH;
            d_counter = 0;
        }

        bool will_emit = d_seg_locked || d_emit_when_unlocked;
        if (will_emit) {
            d_data_mem[d_symbol_index] = interp_sample;
            if (d_symbol_index >= (ATSC_DATA_SEGMENT_LENGTH - 1)) {
                memcpy(&out[d_output_produced * ATSC_DATA_SEGMENT_LENGTH],
                       d_data_mem,
                       ATSC_DATA_SEGMENT_LENGTH * sizeof(float));
                d_output_produced++;
                d_segs_emitted++;
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
