#!/bin/bash


for split in $(seq 0 1 4)
do
  for exp5 in $(seq 12 1 12)
  do
    outname=study12_"$exp5"_"$split".h5
    echo $outname
    geoToH5 study12.geo "$outname" "-setnumber split $split -setnumber exp5 $exp5"
  done
done