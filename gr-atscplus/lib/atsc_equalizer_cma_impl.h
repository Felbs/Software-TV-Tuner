/* -*- c++ -*- */
/*
 * Tier 6 (2026-05-02): CMA + DFE equalizer.
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#ifndef INCLUDED_ATSCPLUS_ATSC_EQUALIZER_CMA_IMPL_H
#define INCLUDED_ATSCPLUS_ATSC_EQUALIZER_CMA_IMPL_H

#include "atsc_syminfo_impl.h"
#include <gnuradio/dtv/atsc_consts.h>
#include <gnuradio/atscplus/atsc_equalizer_cma.h>
#include <chrono>

namespace gr {
namespace atscplus {

class atsc_equalizer_cma_impl : public atsc_equalizer_cma
{
private:
    // Phase-0 channel-IR estimate for RF36 showed:
    //   90% energy in 289 symbols (96 pre, 192 post)
    //   95% energy in 350 symbols (96 pre, 253 post)
    // Existing Tier-3 EQ: NTAPS=256, pre=51, post=204 — undershoots both
    // pre-cursor and (slightly) post-cursor. Tier 5 went to 1024 taps and
    // it didn't help — too many degrees of freedom for the LMS to misuse.
    //
    // Tier 6: keep NTAPS=256 (matches data_mem geometry of Tier 3 so we
    // don't disturb general_work boundary handling), but rebalance to
    // pre=96 / post=159, then add a 32-tap DFE for post-cursor ISI in
    // the +160..+192 region. Effective channel coverage:
    //   FF:  -96 ... +159   (256 taps)
    //   DFE: +160 ... +191  (32 taps, post-cursor only)
    static constexpr int NTAPS = 256;
    static constexpr int NPRETAPS = 96;             // matches Phase-0 90%-pre
    static constexpr int NDFE = 32;

    // Field sync trainable region (data sync + PN511 + 3*PN63).
    static constexpr int KNOWN_FIELD_SYNC_LENGTH = 4 + 511 + 3 * 63;

    float training_sequence1[KNOWN_FIELD_SYNC_LENGTH];
    float training_sequence2[KNOWN_FIELD_SYNC_LENGTH];

    // CMA "constant-modulus" radius for 8-VSB:
    //   R = E[|s|^4] / E[|s|^2]
    //   For ±{1,3,5,7}: R = 777 / 21 = 37.0
    static constexpr float CMA_R = 37.0f;

    // Adaptation rates. Tunable via env var override at construction time.
    float d_mu_cma;          // CMA step (default 1e-5)
    float d_mu_lms_fs;       // LMS step on field-sync segments (default 5e-5)
    float d_mu_dfe;          // DFE step (default 1e-4, gated by slicer
                             //          confidence to avoid error
                             //          propagation)

    float d_leak;            // tap leakage per FS pass (default 5e-4)
    float d_diverge_norm;    // bail to delta if ||taps||_2 > this

    // Decision confidence gate for DFE updates: skip if |y - decision|
    // exceeds this. Half a symbol-spacing (1.0) is the safe value.
    float d_dfe_gate;

    // Slicer levels (8-VSB).
    static constexpr int N_LEVELS = 8;
    static constexpr float LEVELS[N_LEVELS] = {-7.f, -5.f, -3.f, -1.f, 1.f, 3.f, 5.f, 7.f};

    // FF taps + DFE taps + DFE decision history.
    std::vector<float> d_taps;
    float d_dfe_taps[NDFE];
    float d_dec_hist[NDFE];

    // Equalizer working buffer for general_work — same shape as the
    // Tier-3 block so the FS-detection scaffolding is unchanged.
    float data_mem[gr::dtv::ATSC_DATA_SEGMENT_LENGTH + NTAPS];
    float data_mem2[gr::dtv::ATSC_DATA_SEGMENT_LENGTH];
    unsigned short d_flags;
    short d_segno;
    bool  d_buff_not_filled;

    // Telemetry state (matches Tier 3 style).
    int   d_field_sync_count;
    float d_last_fs_mse;
    int   d_diverge_resets;
    long long d_cma_updates;
    long long d_dfe_updates_applied;
    long long d_dfe_updates_skipped;
    std::chrono::steady_clock::time_point d_t0;
    int   d_telem_every_n_fs;

    // Helpers.
    void filterN(const float* input_samples, float* output_samples, int nsamples);
    // Field-sync supervised LMS — same idea as Tier-3 adaptN, plus
    // anti-windup + leakage. Used only on the 4+511+189-sample FS region.
    void adaptN_fs(const float* input_samples,
                   const float* training_pattern,
                   float* output_samples,
                   int nsamples);
    // CMA + DFE adaptation on data segments. Updates FF taps with CMA
    // gradient, runs the DFE filter, gates DFE tap updates by slicer
    // confidence.
    void adaptN_cma_dfe(const float* input_samples,
                         float* output_samples,
                         int nsamples);

    // Anti-windup helper. Returns true if a reset was triggered.
    bool antiwindup_check();

    // Periodic telemetry dump.
    void maybe_print_telem();

public:
    atsc_equalizer_cma_impl();
    ~atsc_equalizer_cma_impl() override;

    std::vector<float> taps() const override;
    std::vector<float> dfe_taps() const override;

    int general_work(int noutput_items,
                     gr_vector_int& ninput_items,
                     gr_vector_const_void_star& input_items,
                     gr_vector_void_star& output_items) override;
};

} /* namespace atscplus */
} /* namespace gr */

#endif /* INCLUDED_ATSCPLUS_ATSC_EQUALIZER_CMA_IMPL_H */
