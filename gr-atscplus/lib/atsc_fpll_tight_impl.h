/* -*- c++ -*- */
/*
 * Copyright 2014 Free Software Foundation, Inc.
 *
 * This file is part of GNU Radio
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 */

#ifndef INCLUDED_ATSCPLUS_ATSC_FPLL_TIGHT_IMPL_H
#define INCLUDED_ATSCPLUS_ATSC_FPLL_TIGHT_IMPL_H

#include <gnuradio/analog/agc.h>
#include <gnuradio/atscplus/atsc_fpll_tight.h>
#include <gnuradio/filter/single_pole_iir.h>
#include <gnuradio/nco.h>
#include <cstdio>

namespace gr {
namespace atscplus {

class atsc_fpll_tight_impl : public atsc_fpll_tight
{
private:
    gr::nco<float, float> d_nco;
    gr::filter::single_pole_iir<gr_complex, gr_complex, float> d_afc;

public:
    atsc_fpll_tight_impl(float rate, float alpha, float afc_tau_us); float d_alpha; float d_beta;
    ~atsc_fpll_tight_impl() override;

    int work(int noutput_items,
             gr_vector_const_void_star& input_items,
             gr_vector_void_star& output_items) override;
};

} /* namespace atscplus */
} /* namespace gr */

#endif /* INCLUDED_ATSCPLUS_ATSC_FPLL_TIGHT_IMPL_H */
