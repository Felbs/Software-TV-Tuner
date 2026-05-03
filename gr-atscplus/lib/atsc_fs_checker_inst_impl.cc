/* -*- c++ -*- */
/* SPDX-License-Identifier: GPL-3.0-or-later */
/* atsc_fs_checker with 313-segment spacing validation (see README, Tier 21). */

#ifdef HAVE_CONFIG_H
#include "config.h"
#endif

#include "atsc_fs_checker_inst_impl.h"
#include "atsc_pnXXX_impl.h"
#include "atsc_syminfo_impl.h"
#include "atsc_types.h"
#include <gnuradio/dtv/atsc_consts.h>
#include <gnuradio/io_signature.h>
#include <climits>
#include <cstdio>
#include <cstdlib>
#include <cstring>

#define ATSC_SEGMENTS_PER_DATA_FIELD 313

using gr::dtv::ATSC_DATA_SEGMENT_LENGTH;
using gr::dtv::plinfo;

// PN511/PN63 error tolerances. Raised from upstream gr-dtv (20/5) to (50/15)
// so synthetic IQ at SNR=25 dB AWGN with residual SRRC ISI still trains the
// equalizer (otherwise rs_clean_frac collapses to 0%). Real RF lock at 99.1%.
static const int PN511_ERROR_LIMIT = 50;
static const int PN63_ERROR_LIMIT = 15;

namespace gr {
namespace atscplus {

atsc_fs_checker_inst::sptr atsc_fs_checker_inst::make()
{
    return gnuradio::make_block_sptr<atsc_fs_checker_inst_impl>();
}

atsc_fs_checker_inst_impl::atsc_fs_checker_inst_impl()
    : gr::block("atscplus_atsc_fs_checker_inst",
                gr::io_signature::make(1, 1, ATSC_DATA_SEGMENT_LENGTH * sizeof(float)),
                gr::io_signature::make2(
                    2, 2, ATSC_DATA_SEGMENT_LENGTH * sizeof(float), sizeof(plinfo)))
{
    reset();
}

void atsc_fs_checker_inst_impl::reset()
{
    d_index = 0;
    std::memset(d_sample_sr, 0, sizeof(d_sample_sr));
    std::memset(d_tag_sr, 0, sizeof(d_tag_sr));
    std::memset(d_bit_sr, 0, sizeof(d_bit_sr));
    d_field_num = 0;
    d_segment_num = 0;

    d_fs_validate_enabled = true;
    if (const char* s = std::getenv("ATSCPLUS_FS_VALIDATE")) {
        if (*s && (std::strcmp(s, "0") == 0 || std::strcmp(s, "off") == 0 ||
                   std::strcmp(s, "OFF") == 0 || std::strcmp(s, "false") == 0)) {
            d_fs_validate_enabled = false;
        }
    }
    d_fs_tol_low  = 280;
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
}

atsc_fs_checker_inst_impl::~atsc_fs_checker_inst_impl()
{
    std::fprintf(stderr,
                 "[fs_check] accepted=%llu rejected_early=%llu rejected_late=%llu "
                 "validate=%s tol=[%d,%d]\n",
                 (unsigned long long)d_fs_accepted,
                 (unsigned long long)d_fs_rejected_early,
                 (unsigned long long)d_fs_rejected_late,
                 d_fs_validate_enabled ? "ON" : "OFF",
                 d_fs_tol_low, d_fs_tol_high);
}

int atsc_fs_checker_inst_impl::general_work(int noutput_items,
                                            gr_vector_int& ninput_items,
                                            gr_vector_const_void_star& input_items,
                                            gr_vector_void_star& output_items)
{
    auto in = static_cast<const float*>(input_items[0]);
    auto out = static_cast<float*>(output_items[0]);
    auto out_pl = static_cast<plinfo*>(output_items[1]);

    int output_produced = 0;

    for (int i = 0; i < noutput_items; i++) {
        d_segs_since_accepted_fs++;

        int errors_511 = 0;
        for (int j = 0; j < LENGTH_511; j++) {
            errors_511 +=
                (in[i * ATSC_DATA_SEGMENT_LENGTH + j + OFFSET_511] >= 0) ^ atsc_pn511[j];
        }

        if (errors_511 < PN511_ERROR_LIMIT) {
            int errors_63 = 0;
            for (int j = 0; j < LENGTH_2ND_63; j++)
                errors_63 += (in[i * ATSC_DATA_SEGMENT_LENGTH + j + OFFSET_2ND_63] >= 0) ^
                             atsc_pn63[j];

            int candidate_field = 0;
            if (errors_63 <= PN63_ERROR_LIMIT) {
                candidate_field = 1;
            } else if (errors_63 >= (LENGTH_2ND_63 - PN63_ERROR_LIMIT)) {
                candidate_field = 2;
            }

            if (candidate_field != 0) {
                bool accept = true;
                uint64_t gap = d_segs_since_accepted_fs;
                if (d_fs_validate_enabled && d_fs_locked) {
                    if ((int)gap < d_fs_tol_low) {
                        accept = false;
                        d_fs_rejected_early++;
                    } else if ((int)gap > d_fs_tol_high) {
                        accept = false;
                        d_fs_rejected_late++;
                    }
                }

                if (accept) {
                    d_field_num = candidate_field;
                    d_segment_num = -1;
                    d_fs_accepted++;
                    d_fs_locked = true;
                    d_segs_since_accepted_fs = 0;
                }
            }
        }

        if (d_field_num == 1 || d_field_num == 2) {
            std::memcpy(&out[output_produced * ATSC_DATA_SEGMENT_LENGTH],
                        &in[i * ATSC_DATA_SEGMENT_LENGTH],
                        ATSC_DATA_SEGMENT_LENGTH * sizeof(float));

            plinfo pli_out;
            // set_regular_seg(field2, segno) — the GR stock equalizer keys on
            // segno==-1 to detect sync segments and train its taps. Calling
            // set_field_sync1/2 leaves segno=0, the equalizer never trains,
            // and stock regresses from 99.1% to 0.3% RS-clean.
            pli_out.set_regular_seg((d_field_num == 2), d_segment_num);

            d_segment_num++;
            if (d_segment_num > (ATSC_SEGMENTS_PER_DATA_FIELD - 1)) {
                d_field_num = 0;
                d_segment_num = 0;
            } else {
                out_pl[output_produced++] = pli_out;
            }
        }
    }

    consume_each(noutput_items);
    return output_produced;
}

} /* namespace atscplus */
} /* namespace gr */
