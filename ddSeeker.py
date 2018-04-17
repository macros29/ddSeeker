#!/usr/bin/env python3

import time
import argparse
from sys import argv, exit
from os.path import abspath, dirname
from os.path import join as pjoin
from numpy import cumsum
from multiprocessing import Pool
from itertools import islice
import pysam
from Bio import pairwise2

local_alignment = pairwise2.align.localxs
global_alignment = pairwise2.align.globalxs
_linkers = ["TAGCCATCGCATTGC", "TACCTCTGAGCTGAA"]

def info(*args, **kwargs):
    print(time.strftime("[%Y-%m-%d %H:%M:%S]"), *args, **kwargs)

def hamming_dist(s1, s2):
    """Return the Hamming distance between equal-length sequences

    >>> hamming_distance("ACG", "ACT")
    1
    """
    if (len(s1) != len(s2)):
        raise ValueError("Undefined for sequences of unequal length")
    return sum(el1 != el2 for el1, el2 in zip(s1, s2))

def fix_block(block):
    """Set barcode to most similar (up to 1 mismatch)

    >>> fix_block("TTTGGG")
    'TTTGGG'

    >>> fix_block("TTTGAG")
    'TTTGGG'

    >>> fix_block("TTTGAA")
    ''
    """
    for bc in _barcodes:
        score = global_alignment(bc, block, -1, -1, score_only=True,
                one_alignment_only=True)
        if len(block) == 5 and score >= 4 or len(block) >= 6 and score >= 5:
            return(bc)
    else:
        return(None)

def make_tags(read):
    """Extract barcodes from R1 and return a tuple of SAM-format TAGs.
    XB = barcode
    XU = UMI
    XE = error

    Errors:
    LX = both linkers not aligned correctly
    L1 = linker 1 not aligned correctly
    L2 = linker 2 not aligned correctly
    I = indel in BC2
    D = deletion in Phase Block or BC1
    J = indel in BC3 or ACG trinucleotide
    K = indel in UMI or GAC trinucleotide
    B = one BC with more than 1 mismatch"""

    read = read.upper()
    starts = []
    k = []
    for linker in _linkers:  # align the two linkers
        alignment = local_alignment(read, linker, -2, -1,
                one_alignment_only=True)[0]
        seqA, seqB, score, begin, end = alignment
        length = end - begin
        if (score == 14 or score == 15) and length == 15:
            # 0-1 mismatch
            starts.append(begin)
            k.append(0)
        elif score == 14 and length == 14:
            # 1 mismatch at starting position
            starts.append(begin - 1)
            k.append(0)
        elif "-" in seqA[begin:end] and score == 12 and length == 15:
            # 1 deletion
            starts.append(begin)
            k.append(-1)
        elif "-" in seqB and score == 13 and length == 16:
            # 1 insertion
            starts.append(begin)
            k.append(1)
        else:
            starts.append(None)
            k.append(None)

    if not starts[0] and not starts[1]:
        return([("XE", "LX", "Z")]) # no linker aligned
    elif not starts[0]:
        return([("XE", "L1", "Z")]) # linker 1 not aligned
    elif not starts[1]:
        return([("XE", "L2", "Z")]) # linker 2 not aligned

    if starts[1]-starts[0] == 21+k[0]:
        bc2 = read[starts[1]-6: starts[1]]
    elif starts[1]-starts[0] == 20+k[0]: # 1 deletion in bc2
        bc2 = read[starts[1]-5: starts[1]]
    elif starts[1]-starts[0] == 22+k[0]: # 1 insertion in bc2
        bc2 = read[starts[1]-7: starts[1]]
    else:
        return([("XE", "I", "Z")])

    if starts[0] < 5:
        return([("XE", "D", "Z")])
    elif starts[0] == 5:
        bc1 = read[: starts[0]]
    else:
        bc1 = read[starts[0]-6: starts[0]]

    acg = read[starts[1]+21+k[1]: starts[1]+24+k[1]]
    try:
        dist_acg = hamming_dist(acg, "ACG")
    except ValueError:
        dist_acg = float("inf")
    if dist_acg > 1:
        return([("XE", "J", "Z")])

    gac = read[starts[1]+32+k[1]: starts[1]+35+k[1]]
    try:
        dist_gac = hamming_dist(gac, "GAC")
    except ValueError:
        dist_gac = float("inf")
    if dist_gac > 1:
        return([("XE", "K", "Z")])

    bc3 = read[starts[1]+15+k[1]: starts[1]+21+k[1]]

    barcode = []
    for block in (bc1, bc2, bc3):
        fixed = fix_block(block)
        if fixed:
            barcode.append(fixed)
        else:
            return([("XE", "B", "Z")])

    umi = read[starts[1]+24+k[1]: starts[1]+32+k[1]]

    return([("XB", "".join(barcode), "Z"), ("XU", umi, "Z")])

def main(args):
    args = parse_args(args)
    in_file = args.input_bam
    out_file = args.output_bam
    summary = args.summary
    cores = args.ncores
    barcodes_file = args.barcodes_file

    global _barcodes
    try:
        global _barcodes
        _barcodes = [_.rstrip().split()[0] for _ in open(barcodes_file).readlines()[:96]]
    except FileNotFoundError:
        print("Error: '{}' file not found.".format(barcodes_file),
            "Specify file path with -b flag or run 'ddSeeker_barcodes.py' to create one.")
        exit(1)

    info("Start analysis:", in_file, "->", out_file)
    in_bam = pysam.AlignmentFile(in_file, "rb", check_sq=False)
    out_bam = pysam.AlignmentFile(out_file, "wb", template = in_bam)
    info("Count total reads:", end=" ")
    n_reads = in_bam.count(until_eof=True)//2
    in_bam.reset()
    print(n_reads)

    info("Get identifiers from R1")
    in_bam_iter = islice(in_bam.fetch(until_eof=True), None, None, 2)
    reads = (_.query_sequence for _ in in_bam_iter)
    with Pool(cores) as pool:
        tags = pool.map(make_tags, reads)

    info("Add tags to R2")
    in_bam.reset()
    out_bam_iter = islice(in_bam.fetch(until_eof=True), 1, None, 2)
    cell_count = {}
    error_count = {}
    for (i, read) in enumerate(out_bam_iter):
        read.set_tags(tags[i])
        read.flag = 4
        out_bam.write(read)

        # summary statistics
        if summary and tags[i][0][0] == "XE":
            error_count[tags[i][0][1]] = error_count.get(tags[i][0][1], 0) + 1
        elif summary and tags[i][0][0] == "XB":
            cell_count[tags[i][0][1]] = cell_count.get(tags[i][0][1], 0) + 1
            error_count["PASS"] = error_count.get("PASS", 0) + 1

    in_bam.close()
    out_bam.close()

    if summary:
        info("Write summary files")
        file_name = summary + ".errors"
        ordered_tags = ["LX", "L1", "L2", "I", "D", "J", "K", "B", "PASS"]
        out = open(file_name, "w")
        out.write("Error\tCount\tFraction\n")
        for tag in ordered_tags:
            count = error_count.get(tag, 0)
            fraction = error_count.get(tag, 0)/sum(error_count.values())
            out.write("{}\t{}\t{}\n".format(tag, count, fraction))
        out.close()

        file_name = summary + ".cell_barcodes"
        sorted_barcodes = sorted(cell_count, key=lambda x: cell_count[x],
                reverse=True)
        cell_cumsum = cumsum([cell_count[b] for b in sorted_barcodes]) / \
                sum(cell_count.values())
        out = open(file_name, "w")
        out.write("Cell_Barcode\tCount\tCumulative_Sum\n")
        for i, barcode in enumerate(sorted_barcodes):
            out.write("{}\t{}\t{}\n".format(barcode, cell_count[barcode],
                cell_cumsum[i]))
        out.close()
    info("Done")

def parse_args(args):
    """Parse argv"""
    description = "A tool to extract cellular and molecular identifiers " +\
        "from single cell RNA sequencing experiments"
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("input_bam", help="Merged paired-end uBAM file")
    parser.add_argument("output_bam", help="Tagged uBAM file")
    parser.add_argument("-b", "--barcodes-file",
        default=pjoin(dirname(abspath(argv[0])), "barcodes.txt"),
        help="Barcode blocks file")
    parser.add_argument("-s", "--summary",
        help="Summary files name prefix (including absolute or relative path)")
    parser.add_argument("-n","--ncores", type=int, default=1,
        help="Number of processing units (CPUs) to use (default=1)")
    parser.add_argument("-v", '--version', action='version', version='%(prog)s 1.0')
    args = parser.parse_args()
    return(args)

if __name__ == "__main__":
    main(argv[1:])