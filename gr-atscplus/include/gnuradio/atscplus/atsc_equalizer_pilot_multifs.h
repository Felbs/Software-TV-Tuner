/* -*- c++ -*- */
/*
 * Copyright 2026 gr-atscplus authors
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#ifndef INCLUDED_ATSCPLUS_ATSC_EQUALIZER_PILOT_MULTIFS_H
#define INCLUDED_ATSCPLUS_ATSC_EQUALIZER_PILOT_MULTIFS_H

#include <gnuradio/block.h>
#include <gnuradio/atscplus/api.h>

namespace gr {
namespace atscplus {

/*!
 * \brief ATSC pilot-LS equalizer with multi-FS-coherent ridge LS.
 *
 * Sibling of atsc_equalizer_pilot. Instead of solving a separate LS at
 * every Field Sync (1093 FS / 30 s on RF36), this block accumulates the
 * normal equations (A^T A, A^T b) across K consecutive FSes and solves
 * once every K FSes. This trades latency (one tap update per K * 24.2 ms
 * = K*24.2 ms) for variance: the LS estimate variance scales as 1/K when
 * the channel is approximately static across the K FSes.
 *
 * On a static UHF link the channel coherence time is >> 100 ms, so K=4
 * is safe. K=1 is identical to atsc_equalizer_pilot.
 *
 * Tunable env vars:
 *   PILOT_MULTIFS_K       number of FSes to coherently combine (default 4)
 *   PILOT_MULTIFS_RIDGE   LS ridge (default 1e-2)
 *   PILOT_MULTIFS_DEBUG   verbose stderr at every solve (default 0)
 *
 * \ingroup dtv_atsc
 */
class ATSCPLUS_API atsc_equalizer_pilot_multifs : virtual public gr::block
{
public:
    typedef std::shared_ptr<atsc_equalizer_pilot_multifs> sptr;

    static sptr make();

    virtual std::vector<float> taps() const = 0;
    virtual std::vector<float> data() const = 0;
    virtual float last_residual_rms() const = 0;
};

} /* namespace atscplus */
} /* namespace gr */

#endif /* INCLUDED_ATSCPLUS_ATSC_EQUALIZER_PILOT_MULTIFS_H */
