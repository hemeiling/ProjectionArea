"""Shared pytest fixtures.

The synthetic drawings are regenerated into a temporary directory for each test
session rather than committed as binaries, so the fixtures and the code that
reads them can never drift apart.
"""

from __future__ import annotations

import os
import sys
from typing import Dict

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.fixtures import build_all  # noqa: E402


@pytest.fixture(scope="session")
def drawings(tmp_path_factory) -> Dict[str, Dict[str, object]]:
    """Every synthetic drawing plus its analytically known ground truth."""
    directory = tmp_path_factory.mktemp("drawings")
    return build_all(str(directory))
