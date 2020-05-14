# -*- coding: utf-8 -*-

# standard library imports
import os
import statistics
import sys
from os.path import commonprefix as prefix

# third-party imports
import click
import gffpandas.gffpandas as gffpd
import numpy as np
import pandas as pd
from Bio import SeqIO
from loguru import logger

# module imports
from . import cli
from . import click_loguru
from .common import *
from .core import cluster_set_name
from .core import usearch_cluster

# global constants


HOMOLOGY_ENDING = "-homology.tsv"
FILES_ENDING = "-files.tsv"
SYNTENY_ENDING = "-synteny.tsv"
PROXY_ENDING = "-proxy.tsv"


def synteny_block_func(k, rmer, frame, name_only=False):
    """Return a synteny block closure and its name."""
    if name_only:
        if rmer:
            return f"rmer{k}"
        else:
            return f"kmer{k}"
    frame_len = len(frame)
    cluster_size_col = frame.columns.get_loc("cluster_size")
    cluster_col = frame.columns.get_loc("cluster_id")

    def kmer_block(first_index):
        """Calculate a reversible hash of cluster values.."""
        cluster_list = []
        for idx in range(first_index, first_index + k):
            if idx + 1 > frame_len or frame.iloc[idx, cluster_size_col] == 1:
                return (
                    0,
                    0,
                    0,
                )
            cluster_list.append(frame.iloc[idx, cluster_col])
        fwd_hash = hash(tuple(cluster_list))
        rev_hash = hash(tuple(reversed(cluster_list)))
        if fwd_hash > rev_hash:
            return k, 1, fwd_hash
        else:
            return k, -1, rev_hash

    def rmer_block(first_index):
        """Calculate a reversible cluster hash, ignoring repeats."""
        cluster_list = []
        idx = first_index
        last_cluster = None
        while len(cluster_list) < k:
            if idx + 1 > frame_len or frame.iloc[idx, cluster_size_col] == 1:
                return (
                    0,
                    0,
                    0,
                )
            current_cluster = frame.iloc[idx, cluster_col]
            if current_cluster == last_cluster:
                idx += 1
            else:
                last_cluster = current_cluster
                cluster_list.append(current_cluster)
                idx += 1
        fwd_hash = hash(tuple(cluster_list))
        rev_hash = hash(tuple(reversed(cluster_list)))
        if fwd_hash > rev_hash:
            return idx - first_index, 1, fwd_hash
        else:
            return idx - first_index, -1, rev_hash

    if rmer:
        return rmer_block
    else:
        return kmer_block


def read_files(setname, synteny=None):
    """Read previously-calculated homology/synteny files and file frame."""
    set_path = Path(setname)
    files_frame_path = set_path / f"{setname}{FILES_ENDING}"
    try:
        file_frame = pd.read_csv(files_frame_path, index_col=0, sep="\t")
    except FileNotFoundError:
        logger.error(f"Unable to read files frome from {files_frame_path}")
        sys.exit(1)
    if synteny is None:
        ending = HOMOLOGY_ENDING
        file_type = "homology"
    else:
        ending = f"-{synteny}{SYNTENY_ENDING}"
        file_type = "synteny"
    paths = [p for p in set_path.glob("*" + ending)]
    stems = [p.name[: -len(ending)] for p in paths]
    if len(stems) != len(file_frame):
        logger.error(
            f"Number of {file_type} files ({len(stems)})is not the same as length of file frame({len(file_frame)})."
        )
        sys.exit(1)
    frame_dict = {}
    for i, path in enumerate(paths):
        logger.debug(f"Reading homology file {path}.")
        frame_dict[stems[i]] = pd.read_csv(path, index_col=0, sep="\t")
    return file_frame, frame_dict


def pair_matching_file_types(mixedlist, extA, extB):
    """Matches pairs of file types with differing extensions."""
    file_dict = {}
    typeA_stems = [str(Path(n).stem) for n in mixedlist if n.find(extA) > -1]

    typeA_stems.sort(key=len)
    typeB_stems = [str(Path(n).stem) for n in mixedlist if n.find(extB) > -1]
    typeB_stems.sort(key=len)
    if len(typeA_stems) != len(typeB_stems):
        logger.error(
            f"Differing number of {extA} ({len(typeB_stems)}) and {extB} files ({len(typeA_stems)})."
        )
        sys.exit(1)
    for typeB in typeB_stems:
        prefix_len = max(
            [len(prefix([typeB, typeA])) for typeA in typeA_stems]
        )
        match_typeA_idx = [
            i
            for i, typeA in enumerate(typeA_stems)
            if len(prefix([typeB, typeA])) == prefix_len
        ][0]
        match_typeA = typeA_stems.pop(match_typeA_idx)
        typeB_path = [
            Path(p) for p in mixedlist if p.endswith(typeB + "." + extB)
        ][0]
        typeA_path = [
            Path(p) for p in mixedlist if p.endswith(match_typeA + "." + extA)
        ][0]
        stem = prefix([typeB, match_typeA])
        file_dict[stem] = {extA: typeA_path, extB: typeB_path}
    return file_dict


@cli.command()
@click_loguru.init_logger()
@click.option(
    "--identity",
    "-i",
    default=0.0,
    help="Minimum sequence ID (0-1). [default: lowest]",
)
@click.option(
    "--clust/--no-clust",
    "-c/-x",
    is_flag=True,
    default=True,
    help="Do cluster calc.",
    show_default=True,
)
@click.option(
    "-s",
    "--shorten_source",
    default=False,
    is_flag=True,
    show_default=True,
    help="Remove invariant dotpaths in source IDs.",
)
@click.argument("setname")
@click.argument("gff_faa_path_list", nargs=-1)
def annotate_homology(
    identity, clust, shorten_source, setname, gff_faa_path_list
):
    """Marshal homology and sequence information.

    Corresponding GFF and FASTA files must have a corresponding prefix to their
    file names, but theu may occur in any order in the list.  Paths to files
    need not be the same.  Files must be uncompressed. FASTA files must be
    protein files with extension ".faa".  GFF files must have extension ".gff3".

    IDs must correspond between GFF and FASTA files and must be unique across
    the entire set.
    """
    if not len(gff_faa_path_list):
        logger.error("No files in list, exiting.")
        sys.exit(0)
    file_dict = pair_matching_file_types(gff_faa_path_list, GFF_EXT, FAA_EXT)
    frame_dict = {}
    set_path = Path(setname)
    set_path.mkdir(parents=True, exist_ok=True)
    # TODO-all combinations of file sets
    fasta_records = []
    for stem in file_dict.keys():
        logger.debug(f"Reading GFF file {file_dict[stem][GFF_EXT]}.")
        annotation = gffpd.read_gff3(file_dict[stem][GFF_EXT])
        mRNAs = annotation.filter_feature_of_type(
            ["mRNA"]
        ).attributes_to_columns()
        mRNAs.drop(
            mRNAs.columns.drop(["seq_id", "start", "strand", "ID"]),
            axis=1,
            inplace=True,
        )  # drop non-essential columns
        if shorten_source:
            # drop identical sub-fields in seq_id to keep them visually short (for development)
            split_sources = mRNAs["seq_id"].str.split(".", expand=True)
            split_sources = split_sources.drop(
                [
                    i
                    for i in split_sources.columns
                    if len(set(split_sources[i])) == 1
                ],
                axis=1,
            )
            sources = split_sources.agg(".".join, axis=1)
            mRNAs["seq_id"] = sources
        # TODO-calculated subfragments from repeated
        # TODO-sort GFFs in order of longest fragments
        # TODO-add gene order
        file_dict[stem]["fragments"] = len(set(mRNAs["seq_id"]))
        logger.debug(f"Reading FASTA file {file_dict[stem][FAA_EXT]}.")
        fasta_dict = SeqIO.to_dict(
            SeqIO.parse(file_dict[stem][FAA_EXT], "fasta")
        )
        # TODO-filter out crap and calculate ambiguous
        file_dict[stem]["n_seqs"] = len(fasta_dict)
        file_dict[stem]["residues"] = sum(
            [len(fasta_dict[k].seq) for k in fasta_dict.keys()]
        )
        mRNAs = mRNAs[mRNAs["ID"].isin(fasta_dict.keys())]
        mRNAs["protein_len"] = mRNAs["ID"].map(
            lambda k: len(fasta_dict[k].seq)
        )
        frame_dict[stem] = mRNAs.set_index("ID")
        del annotation
        for key in fasta_dict.keys():
            fasta_records.append(fasta_dict[key])
    file_frame = pd.DataFrame.from_dict(file_dict).transpose()
    file_frame = file_frame.sort_values(by=["n_seqs"])
    file_frame["n"] = range(len(file_frame))
    file_frame["stem"] = file_frame.index
    file_frame = file_frame.set_index("n")
    logger.debug("Writing files frame.")
    file_frame.to_csv(set_path / f"{setname}{FILES_ENDING}", sep="\t")
    del file_dict
    set_keys = list(file_frame["stem"])
    concatenated_fasta_name = f"{setname}.faa"
    if clust:
        logger.debug(
            f"Writing concatenated FASTA file {concatenated_fasta_name}."
        )
        with (set_path / concatenated_fasta_name).open("w") as concat_fh:
            SeqIO.write(fasta_records, concat_fh, "fasta")
        logger.debug("Doing cluster calculation.")
        cwd = Path.cwd()
        os.chdir(set_path)
        stats, graph, hist, any_, all_ = usearch_cluster.callback(
            concatenated_fasta_name, identity, write_ids=True, delete=False
        )
        os.chdir(cwd)
        del stats, graph, hist, any_, all_
    del fasta_records
    cluster_frame = pd.read_csv(
        set_path / (cluster_set_name(setname, identity) + "-ids.tsv"), sep="\t"
    )
    cluster_frame = cluster_frame.set_index("id")
    logger.debug("Mapping FASTA IDs to cluster properties.")

    def id_to_cluster_property(ident, column):
        try:
            return int(cluster_frame.loc[ident, column])
        except KeyError:
            raise KeyError(f"ID {id} not found in clusters")

    for stem in set_keys:
        frame = frame_dict[stem]
        frame["cluster_id"] = frame.index.map(
            lambda i: id_to_cluster_property(i, "cluster")
        )
        frame["cluster_size"] = frame.index.map(
            lambda i: id_to_cluster_property(i, "siz")
        )
        homology_filename = f"{stem}{HOMOLOGY_ENDING}"
        logger.debug(f"Writing homology file {homology_filename}")
        frame.to_csv(set_path / homology_filename, sep="\t")


@cli.command()
@click_loguru.init_logger()
@click.option("-k", default=6, help="Synteny block length.", show_default=True)
@click.option(
    "-r",
    "--rmer",
    default=False,
    is_flag=True,
    show_default=True,
    help="Allow repeats in block.",
)
@click.argument("setname")
@click.argument("gff_fna_path_list", nargs=-1)
def synteny_anchors(k, rmer, setname, gff_fna_path_list):
    """Calculate synteny anchors.
    """
    if not len(gff_fna_path_list):
        logger.error("No files in list, exiting.")
        sys.exit(0)
    set_path = Path(setname)
    files_frame, frame_dict = read_files(setname)
    set_keys = list(files_frame["stem"])
    logger.debug(f"Calculating k-mer of length {k} synteny blocks.")
    merge_frame_columns = ["hash", "source"]
    merge_frame = pd.DataFrame(columns=merge_frame_columns)
    for stem in set_keys:
        frame = frame_dict[stem]
        synteny_func_name = synteny_block_func(k, rmer, None, name_only=True)
        frame_len = frame.shape[0]
        map_results = []
        for seq_id, subframe in frame.groupby(by=["seq_id"]):
            hash_closure = synteny_block_func(k, rmer, subframe)
            for i in range(len(subframe)):
                map_results.append(hash_closure(i))
        frame["footprint"] = [map_results[i][0] for i in range(len(frame))]
        frame["hashdir"] = [map_results[i][1] for i in range(len(frame))]
        frame[synteny_func_name] = [
            map_results[i][2] for i in range(len(frame))
        ]
        del map_results
        # TODO:E values
        hash_series = frame[synteny_func_name]
        assigned_hashes = hash_series[hash_series != 0]
        del hash_series
        n_assigned = len(assigned_hashes)
        logger.info(
            f"{stem} has {frame_len} proteins, {n_assigned} of which have {synteny_func_name} hashes,"
        )
        hash_counts = assigned_hashes.value_counts()
        assigned_hash_frame = pd.DataFrame(columns=merge_frame_columns)
        assigned_hash_frame["hash"] = assigned_hashes.unique()
        assigned_hash_frame["source"] = stem
        n_non_unique = n_assigned - len(assigned_hash_frame)
        percent_non_unique = n_non_unique / n_assigned * 100.0
        logger.info(
            f"  of which {n_non_unique} ({percent_non_unique:0.1f})% are non-unique."
        )
        merge_frame.append(assigned_hash_frame)
        del assigned_hash_frame
        # create self_count column in frame
        frame["self_count"] = 0
        for idx, row in frame[frame[synteny_func_name] != 0].iterrows():
            frame.loc[idx, "self_count"] = hash_counts.loc[
                row[synteny_func_name]
            ]
        del hash_counts
    logger.debug(f"Calculating overlap of {len(merge_frame)} hash terms.")
    hash_counts = merge_frame["hash"].value_counts()
    merged_hash_frame = pd.DataFrame(
        index=merge_frame["hash"].unique(), columns=["count"]
    )
    for idx, row in merged_hash_frame.iterrows():
        merged_hash_frame.loc[idx, "count"] = hash_counts.loc[
            row[synteny_func_name]
        ]
    print(f"Merged_hash_frame={merged_hash_frame}")
    merged_hash_frame = merged_hash_frame[merged_hash_frame["count"] > 1]
    print(
        f"after dropping non-matching hashes, len = {len(merged_hash_frame)}"
    )
    print(f"merged hash counts={hash_counts}")
    for stem in set_keys:
        synteny_name = f"{stem}-{synteny_func_name}{SYNTENY_ENDING}"
        logger.debug(
            f"Writing {synteny_func_name} synteny frame {synteny_name}."
        )
        frame_dict[stem].to_csv(set_path / synteny_name, sep="\t")


def dagchainer_id_to_int(ident):
    """Accepts DAGchainer ids such as "cl1" and returns an integer."""
    if not ident.startswith("cl"):
        raise ValueError(f"Invalid ID {ident}.")
    id_val = ident[2:]
    if not id_val.isnumeric():
        raise ValueError(f"Non-numeric ID value in {ident}.")
    return int(id_val)


@cli.command()
@click_loguru.init_logger()
@click.argument("setname")
def dagchainer_synteny(setname):
    """Read DAGchainer synteny into homology frames.

    IDs must correspond between DAGchainer files and homology blocks.
    Currently does not calculate DAGchainer synteny.
    """

    cluster_path = Path.cwd() / "out_azulejo" / "clusters.tsv"
    if not cluster_path.exists():
        logger.debug("Running azulejo_tool clean")
        from sh import azulejo_tool

        output = azulejo_tool(["clean"])
        print(output)
        logger.debug("Running azulejo_tool run")
        try:
            output = azulejo_tool(["run"])
            print(output)
        except:
            logger.error(
                "Something went wrong in azulejo_tool, check installation."
            )
            sys.exit(1)
        if not cluster_path.exists():
            logger.error(
                "Something went wrong with DAGchainer run.  Please run it manually."
            )
            sys.exit(1)
    synteny_func_name = "dagchainer"
    set_path = Path(setname)
    logger.debug(f"Reading {synteny_func_name} synteny file.")
    synteny_frame = pd.read_csv(
        cluster_path, sep="\t", header=None, names=["cluster", "id"]
    )
    synteny_frame["synteny_id"] = synteny_frame["cluster"].map(
        dagchainer_id_to_int
    )
    synteny_frame = synteny_frame.drop(["cluster"], axis=1)
    cluster_counts = synteny_frame["synteny_id"].value_counts()
    synteny_frame["synteny_count"] = synteny_frame["synteny_id"].map(
        cluster_counts
    )
    synteny_frame = synteny_frame.sort_values(
        by=["synteny_count", "synteny_id"]
    )
    synteny_frame = synteny_frame.set_index(["id"])
    files_frame, frame_dict = read_files(setname)
    set_keys = list(files_frame["stem"])

    def id_to_synteny_property(ident, column):
        try:
            return int(synteny_frame.loc[ident, column])
        except KeyError:
            return 0

    for stem in set_keys:
        homology_frame = frame_dict[stem]
        homology_frame["synteny_id"] = homology_frame.index.map(
            lambda x: id_to_synteny_property(x, "synteny_id")
        )
        homology_frame["synteny_count"] = homology_frame.index.map(
            lambda x: id_to_synteny_property(x, "synteny_count")
        )
        synteny_name = f"{stem}-{synteny_func_name}{SYNTENY_ENDING}"
        logger.debug(
            f"Writing {synteny_func_name} synteny frame {synteny_name}."
        )
        homology_frame.to_csv(set_path / synteny_name, sep="\t")


class ProxySelector(object):

    """Provide methods for downselection of proxy genes."""

    def __init__(self, frame, prefs):
        """Calculate any joint statistics from frame."""
        self.frame = frame
        self.prefs = prefs
        self.reasons = []
        self.drop_ids = []
        self.first_choice = prefs[0]
        self.first_choice_hits = 0
        self.first_choice_unavailable = 0
        self.cluster_count = 0

    def choose(self, chosen_one, cluster, reason, drop_non_chosen=True):
        """Make the choice, recording stats."""
        self.frame.loc[chosen_one, "reason"] = reason
        self.first_choice_unavailable += int(
            self.first_choice not in set(cluster["stem"])
        )
        self.first_choice_hits += int(
            cluster.loc[chosen_one, "stem"] == self.first_choice
        )
        non_chosen_ones = list(cluster.index)
        non_chosen_ones.remove(chosen_one)
        if drop_non_chosen:
            self.drop_ids += non_chosen_ones
        else:
            self.cluster_count += len(non_chosen_ones)

    def choose_by_preference(
        self, subcluster, cluster, reason, drop_non_chosen=True
    ):
        """Choose in order of preference."""
        stems = subcluster["stem"]
        pref_idxs = [subcluster[stems == pref].index for pref in self.prefs]
        pref_lens = np.array([int(len(idx) > 0) for idx in pref_idxs])
        best_choice = np.argmax(pref_lens)  # first occurrance
        if pref_lens[best_choice] > 1:
            raise ValueError(
                f"subcluster {subcluster} is not unique w.r.t. genome {list(stems)[best_choice]}."
            )
        self.choose(
            pref_idxs[best_choice][0], cluster, reason, drop_non_chosen
        )

    def choose_by_length(self, subcluster, cluster, drop_non_chosen=True):
        """Return an index corresponding to the selected modal/median length."""
        counts = subcluster["protein_len"].value_counts()
        max_count = max(counts)
        if max_count > 1:  # repeated values exist
            max_vals = list(counts[counts == max(counts)].index)
            modal_cluster = subcluster[
                subcluster["protein_len"].isin(max_vals)
            ]
            self.choose_by_preference(
                modal_cluster,
                cluster,
                f"mode{len(modal_cluster)}",
                drop_non_chosen=drop_non_chosen,
            )
        else:
            lengths = list(subcluster["protein_len"])
            median_vals = [
                statistics.median_low(lengths),
                statistics.median_high(lengths),
            ]
            median_pair = subcluster[
                subcluster["protein_len"].isin(median_vals)
            ]
            self.choose_by_preference(
                median_pair, cluster, "median", drop_non_chosen=drop_non_chosen
            )

    def cluster_selector(self, cluster):
        "Calculate which gene in a homology cluster should be left and why."
        self.cluster_count += 1
        if len(cluster) == 1:
            self.choose(cluster.index[0], cluster, "singleton")
        else:
            for synteny_id, subcluster in cluster.groupby(by=["synteny_id"]):
                if len(subcluster) > 1:
                    self.choose_by_length(
                        subcluster, cluster, drop_non_chosen=(not synteny_id)
                    )
                else:
                    if subcluster["synteny_id"][0] != 0:
                        self.choose(
                            subcluster.index[0],
                            cluster,
                            "bad_synteny",
                            drop_non_chosen=(not synteny_id),
                        )
                    else:
                        self.choose(
                            subcluster.index[0],
                            cluster,
                            "single",
                            drop_non_chosen=(not synteny_id),
                        )

    def downselect_frame(self):
        """Return a frame with reasons for keeping and non-chosen-ones dropped."""
        drop_pct = len(self.drop_ids) * 100.0 / len(self.frame)
        logger.info(
            f"Dropping {len(self.drop_ids)} ({drop_pct:0.1f}%) of {len(self.frame)} genes."
        )
        return self.frame.drop(self.drop_ids)

    def selection_stats(self):
        """Return selection stats."""
        return (
            self.cluster_count,
            self.first_choice_unavailable,
            self.first_choice_hits,
        )


@cli.command()
@click_loguru.init_logger()
@click.argument("setname")
@click.argument("synteny_type")
@click.argument("prefs", nargs=-1)
def proxy_genes(setname, synteny_type, prefs):
    """Calculate a set of proxy genes from synteny files.

    prefs is an optional list of genome stems in order of preference in the proxy calc.
    """
    set_path = Path(setname)
    files_frame, frame_dict = read_files(setname, synteny=synteny_type)
    set_keys = list(files_frame["stem"])
    default_prefs = set_keys.copy()
    default_prefs.reverse()
    if prefs != ():
        for stem in prefs:
            if stem not in default_prefs:
                logger.error(f"Preference {stem} not in {default_prefs}")
                sys.exit(1)
            else:
                default_prefs.remove(stem)
        prefs = list(prefs) + default_prefs
        order = "non-default"
    else:
        prefs = default_prefs
        order = "default"
    logger.debug(
        f"Genome preference for proxy selection in {order} order: {prefs}"
    )
    proxy_frame = None
    for stem in set_keys:
        logger.debug(f"Reading {stem}")
        frame_dict[stem]["stem"] = stem
        if proxy_frame is None:
            proxy_frame = frame_dict[stem]
        else:
            proxy_frame = proxy_frame.append(frame_dict[stem])
    del files_frame
    proxy_frame = proxy_frame.sort_values(
        by=["cluster_size", "cluster_id", "synteny_count", "synteny_id"]
    )
    proxy_filename = f"{setname}-{synteny_type}{PROXY_ENDING}"
    logger.debug(f"Writing initial proxy file {proxy_filename}.")
    proxy_frame.to_csv(set_path / proxy_filename, sep="\t")
    proxy_frame["reason"] = ""
    logger.debug(f"Downselecting homology clusters.")
    downselector = ProxySelector(proxy_frame, prefs)
    for unused_cluster_id, homology_cluster in proxy_frame.groupby(
        by=["cluster_id"]
    ):  # pylint: disable=unused-variable
        downselector.cluster_selector(homology_cluster)
    downselected = downselector.downselect_frame()
    downselected_filename = (
        f"{setname}-{synteny_type}-downselected{PROXY_ENDING}"
    )
    logger.debug(f"Writing downselected proxy file {downselected_filename}.")
    downselected.to_csv(set_path / downselected_filename, sep="\t")
    # print out stats
    (
        cluster_count,
        first_choice_unavailable,
        first_choice_hits,
    ) = downselector.selection_stats()
    first_choice_percent = (
        first_choice_hits * 100.0 / (cluster_count - first_choice_unavailable)
    )
    first_choice_unavailable_percent = (
        first_choice_unavailable * 100.0 / cluster_count
    )
    logger.info(
        f"First-choice ({prefs[0]}) selections from {cluster_count} homology clusters:"
    )
    logger.info(
        f"   not in cluster: {first_choice_unavailable} ({first_choice_unavailable_percent:.1f}%)"
    )
    logger.info(
        f"   chosen as proxy: {first_choice_hits} ({first_choice_percent:.1f}%)"
    )
