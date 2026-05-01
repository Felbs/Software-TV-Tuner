#!/bin/bash
set -e
SRC="${GR_DTV_SRC:-/path/to/gnuradio/gr-dtv}"
DST=$HOME/gr-atscplus

cd $DST

# Copy viterbi source files
cp $SRC/lib/atsc/atsc_single_viterbi.h          lib/
cp $SRC/lib/atsc/atsc_single_viterbi.cc         lib/
cp $SRC/lib/atsc/atsc_basic_trellis_encoder.h   lib/
cp $SRC/lib/atsc/atsc_viterbi_decoder_impl.h    lib/
cp $SRC/lib/atsc/atsc_viterbi_decoder_impl.cc   lib/
cp $SRC/lib/atsc/atsc_viterbi_mux.h             lib/
cp $SRC/include/gnuradio/dtv/atsc_viterbi_decoder.h include/gnuradio/atscplus/atsc_viterbi_soft.h

cd lib

# Public header rename
sed -i 's/INCLUDED_DTV_ATSC_VITERBI_DECODER_H/INCLUDED_ATSCPLUS_ATSC_VITERBI_SOFT_H/g; s|<gnuradio/dtv/api.h>|<gnuradio/atscplus/api.h>|g; s/namespace dtv/namespace atscplus/g; s/atsc_viterbi_decoder\b/atsc_viterbi_soft/g; s/DTV_API/ATSCPLUS_API/g' $DST/include/gnuradio/atscplus/atsc_viterbi_soft.h

# atsc_single_viterbi -> _soft, with L1->L2 metric
sed -i 's/INCLUDED_ATSC_SINGLE_VITERBI_H/INCLUDED_ATSCPLUS_ATSC_SINGLE_VITERBI_SOFT_H/g; s/namespace dtv/namespace atscplus/g; s/atsc_single_viterbi\b/atsc_single_viterbi_soft/g' atsc_single_viterbi.h
mv atsc_single_viterbi.h atsc_single_viterbi_soft.h
sed -i 's/atsc_single_viterbi.h/atsc_single_viterbi_soft.h/g; s/namespace dtv/namespace atscplus/g; s/atsc_single_viterbi\b/atsc_single_viterbi_soft/g' atsc_single_viterbi.cc
# THE METRIC CHANGE: L1 fabsf -> L2 squared
sed -i 's|fabsf(input + 7)|(input + 7) * (input + 7)|g; s|fabsf(input + 5)|(input + 5) * (input + 5)|g; s|fabsf(input + 3)|(input + 3) * (input + 3)|g; s|fabsf(input + 1)|(input + 1) * (input + 1)|g; s|fabsf(input - 1)|(input - 1) * (input - 1)|g; s|fabsf(input - 3)|(input - 3) * (input - 3)|g; s|fabsf(input - 5)|(input - 5) * (input - 5)|g; s|fabsf(input - 7)|(input - 7) * (input - 7)|g' atsc_single_viterbi.cc
mv atsc_single_viterbi.cc atsc_single_viterbi_soft.cc

# atsc_viterbi_decoder_impl -> atsc_viterbi_soft_impl
sed -i 's/INCLUDED_DTV_ATSC_VITERBI_DECODER_IMPL_H/INCLUDED_ATSCPLUS_ATSC_VITERBI_SOFT_IMPL_H/g; s|<gnuradio/dtv/atsc_viterbi_decoder.h>|<gnuradio/atscplus/atsc_viterbi_soft.h>|g; s|atsc_single_viterbi.h|atsc_single_viterbi_soft.h|g; s/namespace dtv/namespace atscplus/g; s/atsc_viterbi_decoder_impl\b/atsc_viterbi_soft_impl/g; s/atsc_viterbi_decoder\b/atsc_viterbi_soft/g; s/atsc_single_viterbi\b/atsc_single_viterbi_soft/g' atsc_viterbi_decoder_impl.h
mv atsc_viterbi_decoder_impl.h atsc_viterbi_soft_impl.h
sed -i 's|atsc_viterbi_decoder_impl.h|atsc_viterbi_soft_impl.h|g; s/namespace dtv/namespace atscplus/g; s/atsc_viterbi_decoder_impl\b/atsc_viterbi_soft_impl/g; s/atsc_viterbi_decoder\b/atsc_viterbi_soft/g; s/atsc_single_viterbi\b/atsc_single_viterbi_soft/g' atsc_viterbi_decoder_impl.cc
mv atsc_viterbi_decoder_impl.cc atsc_viterbi_soft_impl.cc

echo "=== L2 metric verified in atsc_single_viterbi_soft.cc ==="
grep -nE "\(input \+ 7\)|\(input - 7\)" atsc_single_viterbi_soft.cc | head -4
echo "=== files renamed ==="
ls atsc_viterbi_soft* atsc_single_viterbi_soft* atsc_viterbi_mux.h atsc_basic_trellis_encoder.h 2>&1
