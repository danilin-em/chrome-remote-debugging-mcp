"""Shared pytest fixtures: loading captured accessibility trees."""

import gzip
import json
import pathlib

import pytest

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def load_ax(name: str) -> list[dict]:
    """Load a captured tree as ``[{"frameId": str, "nodes": [...]}, ...]``."""
    gz = FIXTURES / f"{name}.json.gz"
    if gz.exists():
        with gzip.open(gz, "rt", encoding="utf-8") as fh:
            return json.load(fh)
    plain = FIXTURES / f"{name}.json"
    return json.loads(plain.read_text(encoding="utf-8"))


@pytest.fixture
def ax_fixture():
    """Return the loader, so a test can pull several fixtures."""
    return load_ax


@pytest.fixture
def probe_nodes():
    """Nodes of the probe page's main frame — the smallest useful tree."""
    return load_ax("probe")[0]["nodes"]
