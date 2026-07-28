/* -*- c++ -*- */
/*
 * gr-atscplus — fused widely-linear front end (2026-07-27)
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * See atsc_wl_frontend.h for the why. Structure of general_work:
 *
 *   per symbol:  ONE inlined 8-tap MMSE interpolation over BOTH float planes
 *                (real drives timing/lock exactly as atsc_sync_soft;
 *                 imaginary is carried at the same (d_si, d_mu))
 *   per segment: verbatim atsc_fs_checker_inst framing (PN511/PN63, 313-
 *                spacing validation, drought re-acquire, coast flywheel),
 *                real+imag emitted in lockstep with the plinfo
 *
 * The timing/framing code is deliberately a line-for-line port of
 * atsc_sync_soft_impl.cc / atsc_fs_checker_inst_impl.cc (2026-07-26 state) —
 * same env knobs, same defaults, same telemetry meanings. Only the
 * interpolation call structure is new.
 */
#ifdef HAVE_CONFIG_H
#include "config.h"
#endif

#include "atsc_wl_frontend_impl.h"
#include "atsc_pnXXX_impl.h"
#include <gnuradio/filter/mmse_fir_interpolator_ff.h>
#include <gnuradio/io_signature.h>
#include <climits>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <chrono>

#define ATSC_SEGMENTS_PER_DATA_FIELD 313

using namespace gr::dtv;

namespace gr {
namespace atscplus {

static const double ADJUSTMENT_GAIN = 1.0e-5 / (10 * ATSC_DATA_SEGMENT_LENGTH);
static const int SYMBOL_INDEX_OFFSET = 3;

// PN511/PN63 error tolerances — same env overrides + defaults as
// atsc_fs_checker_inst_impl.cc.
static const int WLF_PN511_ERROR_LIMIT = []() -> int {
    if (const char* p = std::getenv("ATSCPLUS_PN511_LIMIT")) {
        int v = std::atoi(p);
        if (v >= 10 && v <= 220) return v;
    }
    return 50;
}();
static const int WLF_PN63_ERROR_LIMIT = []() -> int {
    if (const char* p = std::getenv("ATSCPLUS_PN63_LIMIT")) {
        int v = std::atoi(p);
        if (v >= 3 && v <= 30) return v;
    }
    return 15;
}();

atsc_wl_frontend::sptr atsc_wl_frontend::make(float rate)
{
    return gnuradio::make_block_sptr<atsc_wl_frontend_impl>(rate);
}

atsc_wl_frontend_impl::atsc_wl_frontend_impl(float rate)
    : gr::block("atscplus_atsc_wl_frontend",
                io_signature::make2(2, 2, sizeof(float), sizeof(float)),
                io_signature::make3(3,
                                    3,
                                    ATSC_DATA_SEGMENT_LENGTH * sizeof(float),
                                    sizeof(plinfo),
                                    ATSC_DATA_SEGMENT_LENGTH * sizeof(float))),
      d_rx_clock_to_symbol_freq(rate / ATSC_SYMBOL_RATE),
      d_si(0)
{
    // ── extract the MMSE interpolator taps table from GR's own block ──────
    // Impulse-probe gr::filter::mmse_fir_interpolator_ff at every mu step:
    // interpolate(e_j, imu/nsteps) returns exactly taps[imu][j]. This keeps
    // the interpolation VALUES identical to atsc_sync_soft without paying the
    // per-call library overhead at 10.76 Msym/s.
    {
        gr::filter::mmse_fir_interpolator_ff probe;
        d_int_ntaps = (int)probe.ntaps();
        d_int_nsteps = (int)probe.nsteps();
        d_itaps.assign((size_t)(d_int_nsteps + 1) * d_int_ntaps, 0.0f);
        std::vector<float> unit(d_int_ntaps, 0.0f);
        for (int imu = 0; imu <= d_int_nsteps; imu++) {
            const float mu = (float)imu / (float)d_int_nsteps;
            for (int j = 0; j < d_int_ntaps; j++) {
                unit[j] = 1.0f;
                d_itaps[(size_t)imu * d_int_ntaps + j] =
                    probe.interpolate(unit.data(), mu);
                unit[j] = 0.0f;
            }
        }
    }

    // ── timing knobs: verbatim atsc_sync_soft defaults + env overrides ────
    d_alpha = 0.40f;
    d_lock_threshold = 4.0f;
    d_unlock_threshold = 2.0f;
    d_sticky_fraction = 0.95f;
    d_emit_when_unlocked = true;
    d_debug = false;
    d_locked_idx = -1;
    d_timing_gain_scale = 1.0f;

    if (const char* p = std::getenv("ATSC_SYNC_SOFT_ALPHA")) {
        float v = std::atof(p);
        if (v > 0.0f && v <= 1.0f) d_alpha = v;
    }
    if (const char* p = std::getenv("ATSC_SYNC_SOFT_LOCK")) {
        float v = std::atof(p);
        if (v > 0.0f) d_lock_threshold = v;
    }
    if (const char* p = std::getenv("ATSC_SYNC_SOFT_UNLOCK")) {
        float v = std::atof(p);
        if (v > 0.0f) d_unlock_threshold = v;
    }
    if (const char* p = std::getenv("ATSC_SYNC_SOFT_STICKY")) {
        float v = std::atof(p);
        if (v >= 0.0f && v <= 1.0f) d_sticky_fraction = v;
    }
    if (const char* p = std::getenv("ATSC_SYNC_SOFT_TIMING_SCALE")) {
        float v = std::atof(p);
        if (v >= 0.0f && v <= 10.0f) d_timing_gain_scale = v;
    }
    if (const char* p = std::getenv("ATSC_SYNC_SOFT_EMIT_UNLOCKED")) {
        d_emit_when_unlocked = (std::atoi(p) != 0);
    }
    if (const char* p = std::getenv("ATSC_SYNC_SOFT_DEBUG")) {
        d_debug = (std::atoi(p) != 0);
    }

    d_adaptive = false;
    d_acquire_lock_thresh = 3.0f;
    d_acquire_unlock_thresh = 1.5f;
    d_steady_lock_thresh = d_lock_threshold;
    d_steady_unlock_thresh = d_unlock_threshold;
    d_steady_after_segs = 313;
    d_consec_locked_segs = 0;
    d_in_steady_state = false;

    if (const char* p = std::getenv("ATSC_SYNC_SOFT_ADAPTIVE")) {
        d_adaptive = (std::atoi(p) != 0);
    }
    if (const char* p = std::getenv("ATSC_SYNC_SOFT_ACQUIRE_LOCK")) {
        float v = std::atof(p);
        if (v > 0.0f) d_acquire_lock_thresh = v;
    }
    if (const char* p = std::getenv("ATSC_SYNC_SOFT_ACQUIRE_UNLOCK")) {
        float v = std::atof(p);
        if (v > 0.0f) d_acquire_unlock_thresh = v;
    }
    if (const char* p = std::getenv("ATSC_SYNC_SOFT_STEADY_LOCK")) {
        float v = std::atof(p);
        if (v > 0.0f) d_steady_lock_thresh = v;
    }
    if (const char* p = std::getenv("ATSC_SYNC_SOFT_STEADY_UNLOCK")) {
        float v = std::atof(p);
        if (v > 0.0f) d_steady_unlock_thresh = v;
    }
    if (const char* p = std::getenv("ATSC_SYNC_SOFT_STEADY_AFTER")) {
        int v = std::atoi(p);
        if (v > 0) d_steady_after_segs = v;
    }
    if (d_adaptive) {
        d_lock_threshold = d_acquire_lock_thresh;
        d_unlock_threshold = d_acquire_unlock_thresh;
    }

    d_segs_emitted = 0;
    d_segs_held = 0;
    d_segs_aligned = 0;
    d_relocks = 0;
    d_seg_count = 0;

    // ── framing knobs: verbatim atsc_fs_checker_inst reset() ──────────────
    d_field_num = 0;
    d_segment_num = 0;
    d_fs_validate_enabled = true;
    if (const char* s = std::getenv("ATSCPLUS_FS_VALIDATE")) {
        if (*s && (std::strcmp(s, "0") == 0 || std::strcmp(s, "off") == 0 ||
                   std::strcmp(s, "OFF") == 0 || std::strcmp(s, "false") == 0)) {
            d_fs_validate_enabled = false;
        }
    }
    d_fs_tol_low = 280;
    d_fs_tol_high = INT_MAX;
    if (const char* s = std::getenv("ATSCPLUS_FS_TOL_LOW")) {
        int v = std::atoi(s);
        if (v > 0) d_fs_tol_low = v;
    }
    if (const char* s = std::getenv("ATSCPLUS_FS_TOL_HIGH")) {
        int v = std::atoi(s);
        if (v > 0) d_fs_tol_high = v;
    }
    d_fs_locked = false;
    d_segs_since_accepted_fs = 0;
    d_fs_accepted = 0;
    d_fs_rejected_early = 0;
    d_fs_rejected_late = 0;
    d_coast_run = 0;
    d_coast_total = 0;
    d_clean_fs_streak = 0;

    std::fprintf(stderr,
                 "[wl_front] FUSED v1 (2026-07-27) rate=%.0f interp=%dx%d "
                 "alpha=%.4f lock=%.2f unlock=%.2f sticky=%.2f "
                 "timing_scale=%.2f emit_unlocked=%d adaptive=%d "
                 "fs_validate=%s fs_tol=[%d,%d]\n",
                 rate, d_int_nsteps + 1, d_int_ntaps,
                 d_alpha, d_lock_threshold, d_unlock_threshold,
                 d_sticky_fraction, d_timing_gain_scale,
                 (int)d_emit_when_unlocked, (int)d_adaptive,
                 d_fs_validate_enabled ? "ON" : "OFF",
                 d_fs_tol_low, d_fs_tol_high);

    reset();
}

void atsc_wl_frontend_impl::reset()
{
    d_w = d_rx_clock_to_symbol_freq;
    d_mu = 0.5;
    d_timing_adjust = 0;
    d_counter = 0;
    d_symbol_index = 0;
    d_seg_locked = false;
    d_locked_idx = -1;
    d_mf_idx = 0;
    d_consec_locked_segs = 0;
    d_in_steady_state = false;
    if (d_adaptive) {
        d_lock_threshold = d_acquire_lock_thresh;
        d_unlock_threshold = d_acquire_unlock_thresh;
    }
    memset(d_mf_buf, 0, sizeof(d_mf_buf));
    memset(d_sample_mem, 0, sizeof(d_sample_mem));
    memset(d_data_mem, 0, sizeof(d_data_mem));
    memset(d_data_mem_imag, 0, sizeof(d_data_mem_imag));
    memset(d_integrator, 0, sizeof(d_integrator));
}

atsc_wl_frontend_impl::~atsc_wl_frontend_impl()
{
    double align_pct = (d_segs_emitted > 0)
        ? 100.0 * (double)d_segs_aligned / (double)d_segs_emitted
        : 0.0;
    std::fprintf(stderr,
                 "[wl_front FINAL] segs_emitted=%llu segs_held=%llu "
                 "segs_aligned=%llu (%.2f%%) relocks=%llu | fs accepted=%llu "
                 "rejected_early=%llu rejected_late=%llu coasted=%llu\n",
                 (unsigned long long)d_segs_emitted,
                 (unsigned long long)d_segs_held,
                 (unsigned long long)d_segs_aligned,
                 align_pct,
                 (unsigned long long)d_relocks,
                 (unsigned long long)d_fs_accepted,
                 (unsigned long long)d_fs_rejected_early,
                 (unsigned long long)d_fs_rejected_late,
                 (unsigned long long)d_coast_total);
}

void atsc_wl_frontend_impl::forecast(int noutput_items,
                                     gr_vector_int& ninput_items_required)
{
    unsigned ninputs = ninput_items_required.size();
    for (unsigned i = 0; i < ninputs; i++)
        ninput_items_required[i] =
            static_cast<int>(noutput_items * d_rx_clock_to_symbol_freq *
                             ATSC_DATA_SEGMENT_LENGTH) + 1500 - 1;
}

// Verbatim port of the atsc_fs_checker_inst per-segment body operating on the
// just-completed segment in d_data_mem / d_data_mem_imag.
void atsc_wl_frontend_impl::handle_segment(float* out_r,
                                           plinfo* out_pl,
                                           float* out_i,
                                           int& output_produced)
{
    static const bool FS_TELEM = []() {
        const char* p = std::getenv("ATSCPLUS_FS_TELEM");
        return p && std::atoi(p) != 0;
    }();
    static const auto fs_t0 = std::chrono::steady_clock::now();
    auto fs_now = [&]() {
        return std::chrono::duration<double>(
                   std::chrono::steady_clock::now() - fs_t0).count();
    };
    static const int FS_RELOCK_SEGS = []() -> int {
        if (const char* p = std::getenv("ATSCPLUS_FS_RELOCK_SEGS")) {
            int v = std::atoi(p);
            if (v >= 0) return v;
        }
        return 939;
    }();
    static const int FS_COAST = []() -> int {
        if (const char* p = std::getenv("ATSCPLUS_FS_COAST")) {
            int v = std::atoi(p);
            if (v >= 0 && v <= 100) return v;
        }
        return 0;
    }();
    static const int FS_COAST_WARMUP = []() -> int {
        if (const char* p = std::getenv("ATSCPLUS_FS_COAST_WARMUP")) {
            int v = std::atoi(p);
            if (v >= 1 && v <= 10000) return v;
        }
        return 80;
    }();

    const float* in = d_data_mem;

    d_segs_since_accepted_fs++;

    if (FS_RELOCK_SEGS > 0 && d_fs_locked &&
        (int)d_segs_since_accepted_fs > FS_RELOCK_SEGS) {
        d_fs_locked = false;
        if (FS_TELEM)
            std::fprintf(stderr,
                "[wl_front fs t=%8.2fs] RE-ACQUIRE (no FS for %llu segs > %d) — "
                "dropping lock to recover\n",
                fs_now(), (unsigned long long)d_segs_since_accepted_fs,
                FS_RELOCK_SEGS);
    }

    int errors_511 = 0;
    for (int j = 0; j < LENGTH_511; j++) {
        errors_511 += (in[j + OFFSET_511] >= 0) ^ atsc_pn511[j];
    }

    if (errors_511 < WLF_PN511_ERROR_LIMIT) {
        int errors_63 = 0;
        for (int j = 0; j < LENGTH_2ND_63; j++)
            errors_63 += (in[j + OFFSET_2ND_63] >= 0) ^ atsc_pn63[j];

        int candidate_field = 0;
        if (errors_63 <= WLF_PN63_ERROR_LIMIT) {
            candidate_field = 1;
        } else if (errors_63 >= (LENGTH_2ND_63 - WLF_PN63_ERROR_LIMIT)) {
            candidate_field = 2;
        }

        if (candidate_field != 0) {
            bool accept = true;
            uint64_t gap = d_segs_since_accepted_fs;
            if (d_fs_validate_enabled && d_fs_locked) {
                if ((int)gap < d_fs_tol_low) {
                    accept = false;
                    d_fs_rejected_early++;
                    if (FS_TELEM)
                        std::fprintf(stderr,
                            "[wl_front fs t=%8.2fs] REJECT-EARLY field=%d gap=%llu (tol_low=%d)\n",
                            fs_now(), candidate_field,
                            (unsigned long long)gap, d_fs_tol_low);
                } else if ((int)gap > d_fs_tol_high) {
                    accept = false;
                    d_fs_rejected_late++;
                    if (FS_TELEM)
                        std::fprintf(stderr,
                            "[wl_front fs t=%8.2fs] REJECT-LATE field=%d gap=%llu (tol_high=%d)\n",
                            fs_now(), candidate_field,
                            (unsigned long long)gap, d_fs_tol_high);
                }
            }

            if (accept) {
                if (FS_TELEM && d_fs_locked && gap != ATSC_SEGMENTS_PER_DATA_FIELD)
                    std::fprintf(stderr,
                        "[wl_front fs t=%8.2fs] GAP-ANOMALY field=%d gap=%llu (expected %d)\n",
                        fs_now(), candidate_field,
                        (unsigned long long)gap, ATSC_SEGMENTS_PER_DATA_FIELD);
                d_field_num = candidate_field;
                d_segment_num = -1;
                d_fs_accepted++;
                d_fs_locked = true;
                if (gap == ATSC_SEGMENTS_PER_DATA_FIELD)
                    d_clean_fs_streak++;
                else
                    d_clean_fs_streak = 0;
                d_segs_since_accepted_fs = 0;
                d_coast_run = 0;
            }
        }
    }

    if (d_field_num == 1 || d_field_num == 2) {
        std::memcpy(&out_r[output_produced * ATSC_DATA_SEGMENT_LENGTH],
                    d_data_mem,
                    ATSC_DATA_SEGMENT_LENGTH * sizeof(float));
        std::memcpy(&out_i[output_produced * ATSC_DATA_SEGMENT_LENGTH],
                    d_data_mem_imag,
                    ATSC_DATA_SEGMENT_LENGTH * sizeof(float));

        plinfo pli_out;
        pli_out.set_regular_seg((d_field_num == 2), d_segment_num);

        d_segment_num++;
        if (d_segment_num > (ATSC_SEGMENTS_PER_DATA_FIELD - 1)) {
            if (FS_COAST > 0 && d_coast_run < FS_COAST
                    && d_clean_fs_streak >= FS_COAST_WARMUP) {
                d_coast_run++;
                d_coast_total++;
                d_field_num = (d_field_num == 1) ? 2 : 1;
                d_segment_num = -1;
                if (FS_TELEM)
                    std::fprintf(stderr,
                        "[wl_front fs t=%8.2fs] COAST #%d (synthetic FS, "
                        "field=%d, lifetime=%llu)\n",
                        fs_now(), d_coast_run, d_field_num,
                        (unsigned long long)d_coast_total);
                plinfo pli_fs;
                pli_fs.set_regular_seg((d_field_num == 2), d_segment_num);
                d_segment_num++;
                out_pl[output_produced++] = pli_fs;
            } else {
                if (FS_TELEM)
                    std::fprintf(stderr,
                        "[wl_front fs t=%8.2fs] FIELD-LOSS (313 segs, no new FS "
                        "accepted%s)\n", fs_now(),
                        FS_COAST > 0 ? ", coast budget spent" : "");
                d_field_num = 0;
                d_segment_num = 0;
            }
        } else {
            out_pl[output_produced++] = pli_out;
        }
    }
}

int atsc_wl_frontend_impl::general_work(int noutput_items,
                                        gr_vector_int& ninput_items,
                                        gr_vector_const_void_star& input_items,
                                        gr_vector_void_star& output_items)
{
    const float* in_r = static_cast<const float*>(input_items[0]);
    const float* in_i = static_cast<const float*>(input_items[1]);
    float* out_r = static_cast<float*>(output_items[0]);
    plinfo* out_pl = static_cast<plinfo*>(output_items[1]);
    float* out_i = static_cast<float*>(output_items[2]);

    const int NT = d_int_ntaps;
    const int NSTEPS = d_int_nsteps;
    const int navail = (ninput_items[0] < ninput_items[1]) ? ninput_items[0]
                                                           : ninput_items[1];

    d_si = 0;
    d_output_produced = 0;

    while (d_output_produced < noutput_items && (d_si + NT) < navail) {

        // ── fused dual-plane MMSE interpolation (the whole point) ─────────
        // One taps-row fetch; real + imaginary computed inline in one pass.
        int imu = (int)std::rint(d_mu * NSTEPS);
        if (imu < 0) imu = 0;
        if (imu > NSTEPS) imu = NSTEPS;
        const float* tp = &d_itaps[(size_t)imu * NT];
        const float* pr = &in_r[d_si];
        const float* pi = &in_i[d_si];
        float interp_sample = 0.0f;
        float interp_imag = 0.0f;
        for (int j = 0; j < NT; j++) {
            interp_sample += tp[j] * pr[j];
            interp_imag += tp[j] * pi[j];
        }

        // ── timing / lock logic: verbatim atsc_sync_soft ──────────────────
        const double t_gain = d_seg_locked
            ? (double)d_timing_gain_scale : 1.0;
        d_mu += t_gain * ADJUSTMENT_GAIN * 1e3 * d_timing_adjust;

        double s = d_mu + d_w;
        double float_incr = std::floor(s);
        d_mu = s - float_incr;
        d_incr = (int)float_incr;
        if (d_incr < 1) d_incr = 1;
        if (d_incr > 3) d_incr = 3;
        d_si += d_incr;

        d_sample_mem[d_counter] = interp_sample;

        d_mf_buf[d_mf_idx] = interp_sample;
        d_mf_idx = (d_mf_idx + 1) & 3;

        const float x_nm3 = d_mf_buf[d_mf_idx];
        const float x_nm2 = d_mf_buf[(d_mf_idx + 1) & 3];
        const float x_nm1 = d_mf_buf[(d_mf_idx + 2) & 3];
        const float x_n   = d_mf_buf[(d_mf_idx + 3) & 3];
        const float corr = x_nm3 - x_nm2 - x_nm1 + x_n;

        d_integrator[d_counter] =
            (1.0f - d_alpha) * d_integrator[d_counter] + d_alpha * corr;

        d_symbol_index++;
        if (d_symbol_index >= ATSC_DATA_SEGMENT_LENGTH) d_symbol_index = 0;

        d_counter++;
        if (d_counter >= ATSC_DATA_SEGMENT_LENGTH) {
            float best_val = d_integrator[0];
            int best_idx = 0;
            double sum_sq = 0.0;
            for (int i = 0; i < ATSC_DATA_SEGMENT_LENGTH; i++) {
                const float v = d_integrator[i];
                sum_sq += (double)v * (double)v;
                if (v > best_val) {
                    best_val = v;
                    best_idx = i;
                }
            }
            const double rms = std::sqrt(sum_sq / (double)ATSC_DATA_SEGMENT_LENGTH);
            const double snr_ratio = (rms > 1e-9) ? (double)best_val / rms : 0.0;

            if (d_seg_locked && d_locked_idx >= 0 &&
                d_locked_idx < ATSC_DATA_SEGMENT_LENGTH) {
                static constexpr int SEARCH_W = 6;
                int local_best_idx = d_locked_idx;
                float local_best_val = d_integrator[d_locked_idx];
                for (int d = -SEARCH_W; d <= SEARCH_W; d++) {
                    int j = d_locked_idx + d;
                    if (j < 0) j += ATSC_DATA_SEGMENT_LENGTH;
                    if (j >= ATSC_DATA_SEGMENT_LENGTH) j -= ATSC_DATA_SEGMENT_LENGTH;
                    if (d_integrator[j] > local_best_val) {
                        local_best_val = d_integrator[j];
                        local_best_idx = j;
                    }
                }
                const float locked_val = d_integrator[d_locked_idx];
                if (locked_val >= d_sticky_fraction * best_val) {
                    best_idx = local_best_idx;
                    best_val = local_best_val;
                    d_locked_idx = local_best_idx;
                } else {
                    d_locked_idx = best_idx;
                    d_relocks++;
                }
            }

            const bool was_locked = d_seg_locked;
            if (d_seg_locked) {
                d_seg_locked = (snr_ratio >= d_unlock_threshold);
                if (!d_seg_locked) d_locked_idx = -1;
            } else {
                d_seg_locked = (snr_ratio >= d_lock_threshold);
                if (d_seg_locked) {
                    d_locked_idx = best_idx;
                    if (!was_locked) d_relocks++;
                }
            }

            if (d_adaptive) {
                if (d_seg_locked) {
                    d_consec_locked_segs++;
                    if (!d_in_steady_state &&
                        d_consec_locked_segs >= d_steady_after_segs) {
                        d_in_steady_state = true;
                        d_lock_threshold = d_steady_lock_thresh;
                        d_unlock_threshold = d_steady_unlock_thresh;
                        if (d_debug) {
                            std::fprintf(stderr,
                                "[wl_front] -> STEADY (lock=%.2f unlock=%.2f) after %d segs\n",
                                d_lock_threshold, d_unlock_threshold,
                                d_consec_locked_segs);
                        }
                    }
                } else {
                    if (d_in_steady_state && d_debug) {
                        std::fprintf(stderr,
                            "[wl_front] -> ACQUIRE (lock=%.2f unlock=%.2f) after %d steady segs\n",
                            d_acquire_lock_thresh, d_acquire_unlock_thresh,
                            d_consec_locked_segs);
                    }
                    d_in_steady_state = false;
                    d_consec_locked_segs = 0;
                    d_lock_threshold = d_acquire_lock_thresh;
                    d_unlock_threshold = d_acquire_unlock_thresh;
                }
            }

            int corr_count = best_idx;
            d_timing_adjust = -d_sample_mem[corr_count--];
            if (corr_count < 0) corr_count = ATSC_DATA_SEGMENT_LENGTH - 1;
            d_timing_adjust -= d_sample_mem[corr_count--];
            if (corr_count < 0) corr_count = ATSC_DATA_SEGMENT_LENGTH - 1;
            d_timing_adjust += d_sample_mem[corr_count--];
            if (corr_count < 0) corr_count = ATSC_DATA_SEGMENT_LENGTH - 1;
            d_timing_adjust += d_sample_mem[corr_count--];

            d_symbol_index = SYMBOL_INDEX_OFFSET - 1 - best_idx;
            if (d_symbol_index < 0) d_symbol_index += ATSC_DATA_SEGMENT_LENGTH;
            d_counter = 0;

            d_seg_count++;
            if (d_debug && (d_seg_count % 256 == 0)) {
                std::fprintf(stderr,
                             "[wl_front] seg=%llu peak=%+.2f rms=%.3f "
                             "snr_ratio=%.2f locked=%d best_idx=%d emitted=%llu "
                             "aligned=%llu (%.2f%%)\n",
                             (unsigned long long)d_seg_count, best_val, rms,
                             snr_ratio, (int)d_seg_locked, best_idx,
                             (unsigned long long)d_segs_emitted,
                             (unsigned long long)d_segs_aligned,
                             d_segs_emitted > 0
                                ? 100.0 * (double)d_segs_aligned / (double)d_segs_emitted
                                : 0.0);
            }
            (void)was_locked;
        }

        const bool will_emit = d_seg_locked || d_emit_when_unlocked;
        if (will_emit) {
            d_data_mem[d_symbol_index] = interp_sample;
            d_data_mem_imag[d_symbol_index] = interp_imag;
            if (d_symbol_index >= (ATSC_DATA_SEGMENT_LENGTH - 1)) {
                d_segs_emitted++;
                if (d_data_mem[0] > 0 && d_data_mem[1] < 0 &&
                    d_data_mem[2] < 0 && d_data_mem[3] > 0) {
                    d_segs_aligned++;
                }
                // ── fused framing: the fs_check stage runs right here ─────
                handle_segment(out_r, out_pl, out_i, d_output_produced);
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
