/* -*- c++ -*- */
/* SPDX-License-Identifier: GPL-3.0-or-later */
/* atsc_fs_checker with 313-segment spacing validation (see README, Tier 21).
   Class name kept for backward compat with downstream bindings. */

#ifndef INCLUDED_ATSCPLUS_ATSC_FS_CHECKER_INST_IMPL_H
#define INCLUDED_ATSCPLUS_ATSC_FS_CHECKER_INST_IMPL_H

#include "atsc_syminfo_impl.h"
#include <gnuradio/atscplus/atsc_fs_checker_inst.h>
#include <cstdint>

namespace gr {
namespace atscplus {

class atsc_fs_checker_inst_impl : public atsc_fs_checker_inst
{
private:
    static constexpr int SRSIZE = 1024;
    int d_index;
    float d_sample_sr[SRSIZE];
    ::gr::dtv::atsc::syminfo d_tag_sr[SRSIZE];
    unsigned char d_bit_sr[SRSIZE];
    int d_field_num;
    int d_segment_num;

    static constexpr int OFFSET_511 = 4;
    static constexpr int LENGTH_511 = 511;
    static constexpr int OFFSET_2ND_63 = 578;
    static constexpr int LENGTH_2ND_63 = 63;

    // Each ATSC field is exactly 313 segments (1 FS + 312 data). Any candidate
    // FS hit at a gap below d_fs_tol_low is presumed spurious and rejected,
    // which keeps the equalizer downstream from training on misaligned input.
    // The dominant failure mode observed was *early* spurious hits (gap≈210);
    // late hits indicate a missed FS where we do want to re-acquire, so the
    // high bound is effectively disabled by default.
    //   ATSCPLUS_FS_VALIDATE=0/off  -> disable validator entirely
    //   ATSCPLUS_FS_TOL_LOW=<int>   -> override low tolerance (default 280)
    //   ATSCPLUS_FS_TOL_HIGH=<int>  -> override high tolerance (default INT_MAX)
    bool d_fs_validate_enabled;
    bool d_fs_locked;
    uint64_t d_segs_since_accepted_fs;
    uint64_t d_fs_accepted;
    uint64_t d_fs_rejected_early;
    uint64_t d_fs_rejected_late;
    int d_fs_tol_low;
    int d_fs_tol_high;

public:
    atsc_fs_checker_inst_impl();
    ~atsc_fs_checker_inst_impl() override;

    void reset();

    int general_work(int noutput_items,
                     gr_vector_int& ninput_items,
                     gr_vector_const_void_star& input_items,
                     gr_vector_void_star& output_items) override;
};

} /* namespace atscplus */
} /* namespace gr */

#endif
