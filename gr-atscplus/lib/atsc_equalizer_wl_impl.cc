/* -*- c++ -*- */
/*
 * gr-atscplus — widely-linear ATSC equalizer impl (2026-07-26)
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * Minimal first pass: complex-in / real-out widely-linear FFE, LMS-trained on the
 * ATSC field sync. Two complex tap sets (main x + conjugate x*). Same segment /
 * plinfo interface as atsc_equalizer_long so it is a drop-in ONCE the chain feeds
 * it a carrier-corrected, symbol-timed COMPLEX stream (the next architectural step
 * — see lab/equalizer/README.md). Deliberately omits the long equalizer's FFT/DFE/
 * RLS/LKG/sheriff/mod12 machinery; those can be ported once WL is validated live.
 */
#ifdef HAVE_CONFIG_H
#include "config.h"
#endif

#include "atsc_equalizer_wl_impl.h"
#include "atsc_pnXXX_impl.h"
#include "atsc_types.h"
#include <gnuradio/io_signature.h>
#include <volk/volk.h>
#include <chrono>
#include <cmath>
#include <complex>
#include <cstdio>
#include <cstdlib>
#include <cstring>

namespace gr {
namespace atscplus {
using gr::dtv::plinfo;
using gr::dtv::ATSC_DATA_SEGMENT_LENGTH;

atsc_equalizer_wl::sptr atsc_equalizer_wl::make()
{
    return gnuradio::make_block_sptr<atsc_equalizer_wl_impl>();
}

static float bin_map(int bit) { return bit ? +5 : -5; }

static void init_field_sync_common(float* p, int mask)
{
    int i = 0;
    p[i++] = bin_map(1); // data segment sync pulse
    p[i++] = bin_map(0);
    p[i++] = bin_map(0);
    p[i++] = bin_map(1);
    for (int j = 0; j < 511; j++) // PN511
        p[i++] = bin_map(atsc_pn511[j]);
    for (int j = 0; j < 63; j++) // PN63
        p[i++] = bin_map(atsc_pn63[j]);
    for (int j = 0; j < 63; j++) // PN63, toggled on field 2
        p[i++] = bin_map(atsc_pn63[j] ^ mask);
    for (int j = 0; j < 63; j++) // PN63
        p[i++] = bin_map(atsc_pn63[j]);
}

atsc_equalizer_wl_impl::atsc_equalizer_wl_impl()
    : gr::block("atscplus_atsc_equalizer_wl",
                // in0 = REAL 8-VSB segments, in1 = plinfo, in2 = IMAGINARY segments
                // (float). We interleave in0/in2 into complex internally (v2: half
                // the upstream data flow vs carrying full complex).
                io_signature::make3(3,
                                    3,
                                    ATSC_DATA_SEGMENT_LENGTH * sizeof(float),
                                    sizeof(plinfo),
                                    ATSC_DATA_SEGMENT_LENGTH * sizeof(float)),
                io_signature::make2(2,
                                    2,
                                    ATSC_DATA_SEGMENT_LENGTH * sizeof(float),
                                    sizeof(plinfo)))
{
    init_field_sync_common(training_sequence1, 0);
    init_field_sync_common(training_sequence2, 1);

    d_w1.assign(NTAPS, gr_complex(0.0f, 0.0f));
    d_w2.assign(NTAPS, gr_complex(0.0f, 0.0f));
    d_w1[NPRETAPS] = gr_complex(1.0f, 0.0f); // delta init — start as pass-through
    d_a.assign(NTAPS, 0.0f);
    d_b.assign(NTAPS, 0.0f);
    d_as.assign(NTAPS, 0.0f);
    d_bs.assign(NTAPS, 0.0f);
    d_as[NPRETAPS] = 1.0f;   // same delta init as the live filter's a
    fold_taps();

    // ── v3 adaptive conjugate shrinkage knobs (opt-in; v2 behaviour default) ──
    if (const char* p = std::getenv("STVT_WL_SHRINK"))
        d_shrink = std::atoi(p) != 0;
    if (const char* p = std::getenv("STVT_WL_SHRINK_GAIN"))
        d_shrink_gain = (float)std::atof(p);
    if (const char* p = std::getenv("STVT_WL_SHRINK_B0"))
        d_shrink_b0 = (float)std::atof(p);
    if (const char* p = std::getenv("STVT_WL_SHRINK_FORCE"))
        d_shrink_force = std::atoi(p) != 0;
    if (const char* p = std::getenv("STVT_WL_SHRINK_WARMUP"))
        d_shrink_warmup = std::atoi(p);
    if (d_shrink_gain < 0.0f) d_shrink_gain = 0.0f;
    if (d_shrink_gain > 1.0f) d_shrink_gain = 1.0f;
    if (d_shrink_b0 <= 0.0f) d_shrink_b0 = 1e-6f;
    if (d_shrink && d_shrink_force) {
        d_kappa = d_shrink_gain;
        d_leak = (d_kappa >= 1.0f)
                     ? 0.0f
                     : (float)std::pow(1.0 - (double)d_kappa,
                                       1.0 / (double)KNOWN_FIELD_SYNC_LENGTH);
    }
    if (d_shrink)
        std::fprintf(stderr,
                     "[eq-wl] v3 SHRINK on: gain=%.3f B0=%.4f force=%d "
                     "warmup=%d\n",
                     d_shrink_gain, d_shrink_b0, (int)d_shrink_force,
                     d_shrink_warmup);

    std::memset(d_win_r, 0, sizeof(d_win_r));
    std::memset(d_win_i, 0, sizeof(d_win_i));
    std::memset(d_cwin, 0, sizeof(d_cwin));
    std::memset(data_mem2, 0, sizeof(data_mem2));
}

atsc_equalizer_wl_impl::~atsc_equalizer_wl_impl() {}

// Fold the complex widely-linear taps into the two REAL vectors of the fused
// filter form (see header). Exact algebra, refreshed after every adaptation.
void atsc_equalizer_wl_impl::fold_taps()
{
    for (int j = 0; j < NTAPS; j++) {
        d_a[j] = d_w1[j].real() + d_w2[j].real();
        d_b[j] = d_w2[j].imag() - d_w1[j].imag();
    }
}

// y[k] = Re( sum_j w1[j] x[k+j] + w2[j] conj(x[k+j]) ), reference tap at NPRETAPS.
// FUSED FORM (2026-07-27): two REAL volk dot products on the plane windows —
// 4x fewer MACs than the two complex dots, no interleave step.
void atsc_equalizer_wl_impl::filterN(const float* inr, const float* ini,
                                     float* out, int nsamples)
{
    for (int k = 0; k < nsamples; k++) {
        float s1, s2;
        volk_32f_x2_dot_prod_32f(&s1, inr + k, d_a.data(), NTAPS);
        volk_32f_x2_dot_prod_32f(&s2, ini + k, d_b.data(), NTAPS);
        out[k] = s1 + s2;
    }
}

// Widely-linear NLMS on the known field-sync symbols. For a REAL target d the
// augmented update is w1 += mu e conj(x), w2 += mu e x  (e real).
void atsc_equalizer_wl_impl::adaptN(const gr_complex* in,
                                    const float* training,
                                    float* out,
                                    int nsamples)
{
    static const bool TELEM_ON = []() {
        const char* p = std::getenv("STVT_EQ_TELEM"); return p && std::atoi(p) != 0;
    }();

    // ── v3 OUT-OF-SAMPLE PROBE — does the imag plane EARN its weight? ──────
    // Run BEFORE any update this field, so the coefficients being judged were
    // fitted on the PREVIOUS field: a genuine generalization test.
    // It is scored on the COUNTERFACTUAL shadow equalizer (as, bs) — a
    // complete, never-shrunk WL filter running alongside the live one — so the
    // answer is a property of the CHANNEL and the controller cannot suppress
    // its own evidence:
    //   e_lin   = sum (d - dot(xr, as))^2            shadow's linear part only
    //   e_probe = sum (d - dot(xr, as) - dot(xi, bs))^2
    //   B = max(0, (e_lin - e_probe)/e_lin)
    // Scoring the LIVE taps instead is a lock-out: shrinkage drives b to 0, the
    // live a absorbs the residual, B reads 0 forever and the branch can never
    // come back. Measured cost of that bug at the cliff knee: WL 228 -> 0
    // frames. A shadow of the imag plane alone (driven by the live error) was
    // tried second and still locked out on rf9 (350 -> 290 frames).
    if ((d_shrink && !d_shrink_force) || TELEM_ON) {
        double e_lin = 0.0, e_probe = 0.0;
        for (int k = 0; k < nsamples; k++) {
            float s1, s2;
            volk_32f_x2_dot_prod_32f(&s1, d_win_r + k, d_as.data(), NTAPS);
            volk_32f_x2_dot_prod_32f(&s2, d_win_i + k, d_bs.data(), NTAPS);
            double dl = (double)training[k] - s1;
            double dp = dl - s2;
            e_lin += dl * dl;
            e_probe += dp * dp;
        }
        double ben = (e_lin > 0.0) ? (e_lin - e_probe) / e_lin : 0.0;
        if (ben < 0.0) ben = 0.0;
        d_imag_benefit = static_cast<float>(ben);
        // WARM-UP HOLD: the imag plane needs several field syncs to converge
        // from the zero init. Shrinking before then judges an untrained branch,
        // suppresses it, and the suppression is self-confirming — the exact
        // lock-out this hold exists to prevent. (Same discipline as the long
        // equalizer's d_fs_trained>=3 hold before DD/warm-start.)
        if (d_shrink && !d_shrink_force && d_fs_count >= (unsigned long long)d_shrink_warmup) {
            d_kappa = d_shrink_gain * (float)std::exp(-ben / (double)d_shrink_b0);
            if (d_kappa < 0.0f) d_kappa = 0.0f;
            if (d_kappa > 1.0f) d_kappa = 1.0f;
            d_leak = (d_kappa >= 1.0f)
                         ? 0.0f
                         : (float)std::pow(1.0 - (double)d_kappa,
                                           1.0 / (double)nsamples);
        }
    }

    const float mu = 0.5f;
    double err2 = 0.0;
    for (int k = 0; k < nsamples; k++) {
        const gr_complex* x = in + k;
        gr_complex a1, a2, ec;
        volk_32fc_x2_dot_prod_32fc(&a1, x, d_w1.data(), NTAPS);            // w1·x
        volk_32fc_x2_conjugate_dot_prod_32fc(&a2, d_w2.data(), x, NTAPS);  // w2·conj(x)
        volk_32fc_x2_conjugate_dot_prod_32fc(&ec, x, x, NTAPS);            // sum|x|^2
        float y = a1.real() + a2.real();
        out[k] = y;
        float energy = 2.0f * ec.real() + 1e-6f;         // augmented regressor energy
        float e = training[k] - y;
        err2 += (double)e * (double)e;
        float step = mu * e / energy;
        // LMS update runs only on field-sync segments (~1/313) — scalar is fine
        for (int j = 0; j < NTAPS; j++) {
            d_w1[j] += step * std::conj(x[j]);   // += mu*e*conj(x)/E
            d_w2[j] += step * x[j];              // += mu*e*x/E
        }
        // ── the COUNTERFACTUAL shadow equalizer (as, bs) ──────────────────
        // A complete, NEVER-shrunk widely-linear equalizer running in the
        // folded domain alongside the live one, adapted on the same training
        // symbols with the same NLMS. Its benefit is a property of the CHANNEL,
        // not of the live filter's shrinkage state, so the controller cannot
        // lock itself out. It only runs on field-sync symbols (704 of every
        // 313*832), so the cost is ~0.02% of the filter load.
        if (d_shrink || TELEM_ON) {
            float p1, p2;
            volk_32f_x2_dot_prod_32f(&p1, d_win_r + k, d_as.data(), NTAPS);
            volk_32f_x2_dot_prod_32f(&p2, d_win_i + k, d_bs.data(), NTAPS);
            const float gs = 2.0f * (mu * (training[k] - p1 - p2) / energy);
            for (int j = 0; j < NTAPS; j++) {
                d_as[j] += gs * d_win_r[k + j];
                d_bs[j] += gs * d_win_i[k + j];
            }
        }
        // v3: leaky shrinkage of the IMAG-plane coefficient b = Im w2 - Im w1.
        // Scaling BOTH imaginary parts by the same factor scales b exactly and
        // leaves a = Re w1 + Re w2 untouched (and preserves Im w1 = -Im w2).
        // Applied per training symbol so that with kappa == 1 (leak == 0) the
        // imag plane is identically zero at every y evaluation => the block is
        // EXACTLY the strictly-linear real FFE (the degenerate-to-linear proof).
        if (d_shrink && d_leak != 1.0f) {
            for (int j = 0; j < NTAPS; j++) {
                d_w1[j] = gr_complex(d_w1[j].real(), d_w1[j].imag() * d_leak);
                d_w2[j] = gr_complex(d_w2[j].real(), d_w2[j].imag() * d_leak);
            }
        }
    }
    d_fs_err_rms = (nsamples > 0)
                       ? static_cast<float>(std::sqrt(err2 / nsamples))
                       : 0.0f;
    d_fs_count++;

    double e1 = 0.0, e2 = 0.0;
    for (int j = 0; j < NTAPS; j++) {
        e1 += std::norm(d_w1[j]);
        e2 += std::norm(d_w2[j]);
    }
    d_conj_frac = static_cast<float>(e2 / (e1 + e2 + 1e-12));

    // ── v3: measure what the imaginary (conjugate) plane is actually WORTH ──
    // Post-adaptation, re-run the field sync through the folded filter twice:
    // with b (the full WL filter) and with b suppressed (the strictly-linear
    // reduction). The fractional MSE reduction is the shrinkage input.
    fold_taps();
    double ea = 0.0, eb = 0.0;
    for (int j = 0; j < NTAPS; j++) {
        ea += (double)d_a[j] * d_a[j];
        eb += (double)d_b[j] * d_b[j];
    }
    d_imag_frac = static_cast<float>(eb / (ea + eb + 1e-12));

    // IN-SAMPLE benefit — diagnostic ONLY, never steers anything. Re-scores the
    // field sync the taps were just fitted to. It reads ~0.9 even on a clean
    // strong channel, which is exactly the trap this v3 exists to avoid: 256
    // real coefficients fitted to 704 training symbols will always "explain"
    // the training field. The out-of-sample probe above is the honest number;
    // the gap between the two IS the estimation variance the theory predicts.
    if (TELEM_ON) {
        double e_wl = 0.0, e_lin = 0.0;
        for (int k = 0; k < nsamples; k++) {
            float s1, s2;
            volk_32f_x2_dot_prod_32f(&s1, d_win_r + k, d_a.data(), NTAPS);
            volk_32f_x2_dot_prod_32f(&s2, d_win_i + k, d_b.data(), NTAPS);
            double dl = (double)training[k] - s1;
            double dw = dl - s2;
            e_lin += dl * dl;
            e_wl += dw * dw;
        }
        double bi = (e_lin > 0.0) ? (e_lin - e_wl) / e_lin : 0.0;
        d_imag_benefit_in = static_cast<float>(bi < 0.0 ? 0.0 : bi);
    }

    static const int TELEM_EVERY = []() {
        const char* t = std::getenv("STVT_EQ_TELEM");
        if (!(t && std::atoi(t) != 0)) return 0;
        const char* p = std::getenv("STVT_EQ_TELEM_EVERY");
        int n = p ? std::atoi(p) : 8;
        return n > 0 ? n : 8;
    }();
    if (TELEM_EVERY && (d_fs_count % (unsigned long long)TELEM_EVERY) == 0) {
        static auto telem_t0 = std::chrono::steady_clock::now();
        double t = std::chrono::duration<double>(
                       std::chrono::steady_clock::now() - telem_t0).count();
        std::fprintf(stderr,
                     "[eq-wl t=%6.2fs] fs=%llu fs_err_rms=%.4f conj=%.4f "
                     "imag=%.4f ben=%.5f kap=%.4f beni=%.5f\n",
                     t, (unsigned long long)d_fs_count, d_fs_err_rms,
                     d_conj_frac, d_imag_frac, d_imag_benefit, d_kappa,
                     d_imag_benefit_in);
    }
}

std::vector<float> atsc_equalizer_wl_impl::taps() const
{
    std::vector<float> t(2 * NTAPS);
    for (int j = 0; j < NTAPS; j++) {
        t[j] = std::abs(d_w1[j]);
        t[NTAPS + j] = std::abs(d_w2[j]);
    }
    return t;
}

std::vector<float> atsc_equalizer_wl_impl::data() const
{
    return std::vector<float>(data_mem2, data_mem2 + ATSC_DATA_SEGMENT_LENGTH);
}

float atsc_equalizer_wl_impl::conj_energy_fraction() const { return d_conj_frac; }

int atsc_equalizer_wl_impl::general_work(int noutput_items,
                                         gr_vector_int& ninput_items,
                                         gr_vector_const_void_star& input_items,
                                         gr_vector_void_star& output_items)
{
    auto in_real = static_cast<const float*>(input_items[0]);
    auto in_pl = static_cast<const plinfo*>(input_items[1]);
    auto in_imag = static_cast<const float*>(input_items[2]);
    auto out = static_cast<float*>(output_items[0]);
    auto out_pl = static_cast<plinfo*>(output_items[1]);

    int output_produced = 0;
    int i = 0;

    if (d_buff_not_filled) {
        std::memset(&d_win_r[0], 0, NPRETAPS * sizeof(float));
        std::memset(&d_win_i[0], 0, NPRETAPS * sizeof(float));
        std::memcpy(&d_win_r[NPRETAPS],
                    in_real + i * ATSC_DATA_SEGMENT_LENGTH,
                    ATSC_DATA_SEGMENT_LENGTH * sizeof(float));
        std::memcpy(&d_win_i[NPRETAPS],
                    in_imag + i * ATSC_DATA_SEGMENT_LENGTH,
                    ATSC_DATA_SEGMENT_LENGTH * sizeof(float));
        d_flags = in_pl[i].flags();
        d_segno = in_pl[i].segno();
        d_buff_not_filled = false;
        i++;
    }

    for (; i < noutput_items; i++) {
        // post-cursor taps come from the NEXT segment's leading samples
        std::memcpy(&d_win_r[ATSC_DATA_SEGMENT_LENGTH + NPRETAPS],
                    in_real + i * ATSC_DATA_SEGMENT_LENGTH,
                    (NTAPS - NPRETAPS) * sizeof(float));
        std::memcpy(&d_win_i[ATSC_DATA_SEGMENT_LENGTH + NPRETAPS],
                    in_imag + i * ATSC_DATA_SEGMENT_LENGTH,
                    (NTAPS - NPRETAPS) * sizeof(float));

        if (d_segno == -1) {
            // materialize the complex window only here (1 segment in 313)
            volk_32f_x2_interleave_32fc(d_cwin, d_win_r, d_win_i,
                                        ATSC_DATA_SEGMENT_LENGTH + NTAPS);
            const float* trn =
                (d_flags & 0x0010) ? training_sequence2 : training_sequence1;
            adaptN(d_cwin, trn, data_mem2, KNOWN_FIELD_SYNC_LENGTH);
            fold_taps();
            // field-sync segment trains only — produces no output
        } else {
            filterN(d_win_r, d_win_i, data_mem2, ATSC_DATA_SEGMENT_LENGTH);
            std::memcpy(&out[output_produced * ATSC_DATA_SEGMENT_LENGTH],
                        data_mem2,
                        ATSC_DATA_SEGMENT_LENGTH * sizeof(float));
            out_pl[output_produced++] = plinfo(d_flags, d_segno);
        }

        // slide the window: keep NPRETAPS tail as new pre-cursor, load segment i
        std::memmove(d_win_r,
                     &d_win_r[ATSC_DATA_SEGMENT_LENGTH],
                     NPRETAPS * sizeof(float));
        std::memmove(d_win_i,
                     &d_win_i[ATSC_DATA_SEGMENT_LENGTH],
                     NPRETAPS * sizeof(float));
        std::memcpy(&d_win_r[NPRETAPS],
                    in_real + i * ATSC_DATA_SEGMENT_LENGTH,
                    ATSC_DATA_SEGMENT_LENGTH * sizeof(float));
        std::memcpy(&d_win_i[NPRETAPS],
                    in_imag + i * ATSC_DATA_SEGMENT_LENGTH,
                    ATSC_DATA_SEGMENT_LENGTH * sizeof(float));
        d_flags = in_pl[i].flags();
        d_segno = in_pl[i].segno();
    }

    consume_each(noutput_items);
    return output_produced;
}

} /* namespace atscplus */
} /* namespace gr */
