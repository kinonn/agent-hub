from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
NGINX_CONFIGS = [
    ROOT / "deploy" / "nginx.conf",
    ROOT / "frontend" / "nginx.conf",
    ROOT / "frontend" / "nginx-tls.conf",
]


@pytest.mark.parametrize("path", NGINX_CONFIGS, ids=lambda p: p.relative_to(ROOT).as_posix())
def test_nginx_overwrites_forwarded_for_with_the_real_client(path):
    # Appending would let a client pre-set the first entry, which Uvicorn trusts.
    forwarded = re.findall(r"proxy_set_header X-Forwarded-For (\S+);", path.read_text())
    assert forwarded
    assert set(forwarded) == {"$remote_addr"}


@pytest.mark.parametrize("path", NGINX_CONFIGS, ids=lambda p: p.relative_to(ROOT).as_posix())
def test_location_regexes_with_braces_are_quoted(path):
    # An unquoted "{8,}" in a location line is parsed as a block and stops nginx from starting.
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("location") and re.search(r"\{\d", stripped):
            assert '"' in stripped, stripped
