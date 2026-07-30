/* -*- c++ -*- */
/*
 * gr-atscplus — widely-linear ATSC equalizer impl (2026-07-26)
 * SPDX-License-Identifier: GPL-3.0-or-later
 */
#ifndef INCLUDED_ATSCPLUS_ATSC_EQUALIZER_WL_IMPL_H
#define INCLUDED_ATSCPLUS_ATSC_EQUALIZER_WL_IMPL_H

#include "atsc_syminfo_impl.h"
#include <gnuradio/atscplus/atsc_equalizer_wl.h>
#include <gnuradio/dtv/atsc_consts.h>
#include <gnuradio/gr_complex.h>
#include <string>
#include <vector>

namespace gr {
namespace atscplus {

class atsc_equalizer_wl_impl : public atsc_equalizer_wl
{
private:
    // 128 taps (was 256): the widely-linear filter runs TWO complex dot products
    // per symbol (main + conjugate), so it is ~2x a normal equalizer. 128 keeps it
    // within the real-time budget at 10.76 Msym/s (12 us echo reach — plenty for
    // the fading-limited channels WL targets). Bump back to 256 if CPU allows.
    static constexpr int NTAPS = 128;
    static constexpr int NPRETAPS = (int)(NTAPS * 0.2);
    static constexpr int KNOWN_FIELD_SYNC_LENGTH = 4 + 511 + 3 * 63;

    float training_sequence1[KNOWN_FIELD_SYNC_LENGTH];
    float training_sequence2[KNOWN_FIELD_SYNC_LENGTH];

    // Widely-linear taps: y[k] = Re( sum_j w1[j]*x[k+j] + w2[j]*conj(x[k+j]) )
    std::vector<gr_complex> d_w1;   // main branch  (x)
    std::vector<gr_complex> d_w2;   // conjugate branch (x*)

    // FUSED-FILTER FORM (2026-07-27): for a REAL output the widely-linear
    // filter folds algebraically into TWO REAL dot products —
    //   y[k] = sum_j xr[k+j]*(Re w1[j] + Re w2[j])
    //        + sum_j xi[k+j]*(Im w2[j] - Im w1[j])
    // (exactly Re(w1·x) + Re(w2·conj x), a 4x MAC cut vs two complex dots).
    // d_a/d_b are refreshed from d_w1/d_w2 after every field-sync adaptation.
    std::vector<float> d_a;         // Re(w1) + Re(w2)
    std::vector<float> d_b;         // Im(w2) - Im(w1)

    // sliding PLANE windows: [NPRETAPS pre | segment | post] — real and imag
    // kept separate (the upstream front end already delivers planes; the
    // folded filter wants contiguous floats; complex is only materialized for
    // the field-sync adaptation, 1 segment in 313).
    float d_win_r[gr::dtv::ATSC_DATA_SEGMENT_LENGTH + NTAPS];
    float d_win_i[gr::dtv::ATSC_DATA_SEGMENT_LENGTH + NTAPS];
    // complex scratch for adaptN (field-sync segments only)
    gr_complex d_cwin[gr::dtv::ATSC_DATA_SEGMENT_LENGTH + NTAPS];
    float data_mem2[gr::dtv::ATSC_DATA_SEGMENT_LENGTH];

    unsigned short d_flags = 0;
    short d_segno = 0;
    bool d_buff_not_filled = true;
    float d_conj_frac = 0.0f;

    // ── v3 ADAPTIVE CONJUGATE SHRINKAGE (2026-07-29) ──────────────────────
    // Physics: at high SNR the conjugate branch adds estimation variance
    // (excess MSE) without adding signal — that is WL's ~1-2% deficit on
    // strong channels. At the cliff the impropriety is real and WL wins big.
    // So make the conjugate branch EARN its weight.
    //
    // WHERE the conjugate branch actually lives in this folded architecture:
    // the NLMS updates are  Re w1 += s*xr, Re w2 += s*xr,
    //                       Im w1 -= s*xi, Im w2 += s*xi
    // so  (Re w1 - Re w2)  and  (Im w1 + Im w2)  are NEVER updated — they are
    // frozen at their init values (delta, 0). Therefore, always and exactly:
    //     a = Re w1 + Re w2                (coefficient on the REAL plane)
    //     b = Im w2 - Im w1 = 2*Im w2      (coefficient on the IMAG plane)
    // and the block is an NLMS over the doubled real regressor [xr; xi].
    // CONSEQUENCE: shrinking w2 alone is the WRONG operation here — it only
    // halves b (b_new = (1 - k/2) b_old, never reaching 0) while CORRUPTING a
    // (a_new = a - k*Re w2), i.e. it damages the linear part that the block
    // shares with atsc_equalizer_long. The correct shrinkage target is the
    // IMAG-plane vector b — the entire extra degree of freedom WL has over the
    // strictly-linear production equalizer. We shrink it by scaling Im w1 and
    // Im w2 together (exact: b scales by the same factor, a is untouched, and
    // the Im w1 = -Im w2 invariant is preserved).
    //
    // Shrinkage strength is measured, not assumed: once per field sync we
    // recompute the field-sync error WITH and WITHOUT b,
    //     B = max(0, (e_lin - e_wl) / e_lin)     ("imag benefit")
    // and set   kappa = kappa_max * exp(-B / B0).
    // B ~ 0 (strong channel, conjugate branch contributes nothing) -> kappa ->
    // kappa_max -> b decays toward 0 -> WL degenerates to the strictly-linear
    // real equalizer. B >> B0 (cliff, impropriety real) -> kappa -> 0 ->
    // shrinkage releases and the full WL filter runs.
    // With kappa == 1 the leak zeroes b after EVERY training symbol, so b is
    // identically 0 at every output segment AND at every error evaluation:
    // the block is then EXACTLY the strictly-linear real FFE y = dot(xr, a).
    bool d_shrink = false;      // STVT_WL_SHRINK
    bool d_shrink_force = false; // STVT_WL_SHRINK_FORCE (kappa := gain, no measurement)
    float d_shrink_gain = 0.5f; // STVT_WL_SHRINK_GAIN  (kappa_max)
    float d_shrink_b0 = 0.02f;  // STVT_WL_SHRINK_B0    (benefit scale)
    int d_shrink_warmup = 16;   // STVT_WL_SHRINK_WARMUP (field syncs held off)
    float d_kappa = 0.0f;       // current per-field shrinkage
    float d_leak = 1.0f;        // per-symbol imag leak = (1-kappa)^(1/nsym)
    float d_imag_benefit = 0.0f;    // OUT-of-sample (steers kappa)
    float d_imag_benefit_in = 0.0f; // in-sample (diagnostic only — overfits)
    // COUNTERFACTUAL shadow equalizer — a complete, never-shrunk widely-linear
    // filter in the folded domain, adapted only on field syncs. Its benefit
    // answers "what would the conjugate branch be worth if we left it alone?",
    // which is a channel property, so shrinkage cannot suppress its own
    // evidence. (A shadow of the imag plane ALONE was tried first and still
    // locked out: once the live b is suppressed the live a absorbs the
    // residual, and a shadow driven by the live error stops seeing anything.)
    std::vector<float> d_as;        // shadow real-plane coefficients
    std::vector<float> d_bs;        // shadow imag-plane coefficients
    float d_imag_frac = 0.0f;   // |b|^2 / (|a|^2 + |b|^2) — the HONEST conj metric
    float d_fs_err_rms = 0.0f;  // field-sync error rms => MER = 20 log10(5/err)
    unsigned long long d_fs_count = 0;

    // ── warm-start tap cache (2026-07-30, ported from atsc_equalizer_long) ──
    // Own magic ('TAPW') and own file (long's path + ".wl") so the two
    // equalizers keep INDEPENDENT warm starts and can never adopt each other's
    // taps. Inert unless STVT_EQ_TAP_CACHE_FILE is set.
    std::vector<gr_complex> d_w1_lkg;   // last-known-good main branch
    std::vector<gr_complex> d_w2_lkg;   // last-known-good conjugate branch
    bool d_lkg_valid = false;
    unsigned long long d_last_cache_save_fs = 0;

    static std::string cache_path();
    bool cache_load();
    bool cache_save();
    void reset_to_delta();

    void fold_taps();
    void filterN(const float* inr, const float* ini, float* out, int nsamples);
    void adaptN(const gr_complex* in, const float* training, float* out, int nsamples);

public:
    atsc_equalizer_wl_impl();
    ~atsc_equalizer_wl_impl() override;

    // Persist the warm-start cache on a clean shutdown.
    bool stop() override;

    std::vector<float> taps() const override;
    std::vector<float> data() const override;
    float conj_energy_fraction() const override;
    float imag_energy_fraction() const { return d_imag_frac; }
    float imag_benefit() const { return d_imag_benefit; }
    float shrinkage() const { return d_kappa; }
    float fs_err_rms() const { return d_fs_err_rms; }

    int general_work(int noutput_items,
                     gr_vector_int& ninput_items,
                     gr_vector_const_void_star& input_items,
                     gr_vector_void_star& output_items) override;
};

} /* namespace atscplus */
} /* namespace gr */

#endif /* INCLUDED_ATSCPLUS_ATSC_EQUALIZER_WL_IMPL_H */
