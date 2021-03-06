# -*- coding: utf-8 -*-
"""Tests for basic CLI function."""
# third-party imports
import pytest
import sh
import sys

# module imports
from . import help_check
from . import print_docstring
from . import working_directory

# global constants
azulejo = sh.Command("azulejo")


def test_cli():
    """Test global help function."""
    help_check("global")


@print_docstring()
def test_version(tmp_path):
    """Test version command."""
    with working_directory(tmp_path):
        try:
            output = azulejo(["--version"])
        except sh.ErrorReturnCode as errors:
            print(errors)
            pytest.fail(errors)
        assert "version" in output


@print_docstring()
def test_taxonomy(tmp_path):
    """Test taxonomy rank check command."""
    with working_directory(tmp_path):
        try:
            azulejo(["taxonomy"], _out=sys.stdout)
        except sh.ErrorReturnCode as errors:
            print(errors)
            pytest.fail(errors)
        assert int(azulejo(["taxonomy", "species"])) == 130
        assert int(azulejo(["taxonomy", "subspecies"])) == 131
        assert int(azulejo(["taxonomy", "superspecies"])) == 128
