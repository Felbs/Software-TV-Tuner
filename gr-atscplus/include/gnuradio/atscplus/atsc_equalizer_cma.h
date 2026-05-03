/* -*- c++ -*- */
/*
 * Tier 6 (2026-05-02): CMA-trained equalizer + DFE for gr-atscplus.
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#ifndef INCLUDED_ATSCPLUS_ATSC_EQUALIZER_CMA_H
#define INCLUDED_ATSCPLUS_ATSC_EQUALIZER_CMA_H

#include <gnuradio/block.h>
#include <gnuradio/atscplus/api.h>

namespace gr {
namespace atscplus {

/*!
 * \brief ATSC Receiver Equalizer — CMA + DFE (Tier 6).
 *
 * Drop-in replacement for atsc_equalizer_long. Uses the Constant-Modulus
 * Algorithm to adapt the feedforward filter on every output symbol
 * (data segments AND field-sync segments are both used for tracking),
 * and a small (32-tap) decision-feedback filter to cancel post-cursor
 * ISI. Field-sync segments still drive a hard supervised LMS pass for
 * fast initial convergence, but unlike Tier 3 the FF filter is not
 * idle between FSes — CMA tracks channel changes continuously.
 *
 * I/O signature matches atsc_equalizer_long: two ports each, port 0 is
 * a float vector of ATSC_DATA_SEGMENT_LENGTH samples per item, port 1
 * is a plinfo struct.
 *
 * \ingroup dtv_atsc
 */
class ATSCPLUS_API atsc_equalizer_cma : virtual public gr::block
{
public:
    typedef std::shared_ptr<atsc_equalizer_cma> sptr;

    static sptr make();

    virtual std::vector<float> taps() const = 0;
    virtual std::vector<float> dfe_taps() const = 0;
};

} /* namespace atscplus */
} /* namespace gr */

#endif /* INCLUDED_ATSCPLUS_ATSC_EQUALIZER_CMA_H */
