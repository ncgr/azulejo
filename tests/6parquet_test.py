# -*- coding: utf-8 -*-
"""Tests for data ingestion."""
# standard library imports
import sys
from pathlib import Path

# third-party imports
import pytest
import sh

# module imports
from . import TSV_OUTPUT_FILE
from . import TSV_TEST_FILE
from . import help_check
from . import print_docstring

# global constants
azulejo = sh.Command("azulejo")
SUBCOMMAND = "parquet-to-tsv"


def test_subcommand_help():
    """Test subcommand help message."""
    help_check(SUBCOMMAND)


@print_docstring()
def test_parquet_conversion(datadir_mgr):
    """Test parquet-to-TSV conversion."""
    with datadir_mgr.in_tmp_dir(
        inpathlist=[TSV_TEST_FILE],
        save_outputs=False,
    ):
        args = ["-q", "-e", SUBCOMMAND, "-w", TSV_TEST_FILE]
        print(f"azulejo {' '.join(args)}")
        try:
            azulejo(
                args,
                _out=sys.stdout,
            )
        except sh.ErrorReturnCode as errors:
            print(errors)
        try:
            azulejo(["-q", "-e", SUBCOMMAND, "-w", TSV_TEST_FILE])
        except sh.ErrorReturnCode as errors:
            print(errors)
            pytest.fail("Parquet-to-TSV conversion failed")
        assert Path(TSV_OUTPUT_FILE).exists()
