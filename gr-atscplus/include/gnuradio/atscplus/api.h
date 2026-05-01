/* -*- c++ -*- */
/* SPDX-License-Identifier: GPL-3.0-or-later */
#ifndef INCLUDED_ATSCPLUS_API_H
#define INCLUDED_ATSCPLUS_API_H

#include <gnuradio/attributes.h>

#ifdef gnuradio_atscplus_EXPORTS
#define ATSCPLUS_API __GR_ATTR_EXPORT
#else
#define ATSCPLUS_API __GR_ATTR_IMPORT
#endif

#endif /* INCLUDED_ATSCPLUS_API_H */
