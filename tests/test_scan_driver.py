"""End-to-end scan() behaviour on real files.

The per-rule tests cover the matchers; these cover the driver — that a hit in
content actually increments the counter and returns non-zero, and that a
clean tree returns zero. That distinction is the whole point: the original
leak came from a file type the driver skipped.

Run: python3 -m pytest tests/test_scan_driver.py -q
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

_spec = importlib.util.spec_from_file_location(
    "private_scan", ROOT / "tools" / "private_scan.py")
ps = importlib.util.module_from_spec(_spec)
assert _spec and _spec.loader
_spec.loader.exec_module(ps)


def _join(*p: str) -> str:
    return "".join(p)


# Synthetic shapes, never real values (see test_private_scan.py for why).
PUB_IP = _join("203", ".", "0", ".", "114", ".", "77")
HEX32 = _join("0123456789abcdef", "0123456789abcdef")


# --------------------------------------------------------------------------
# the driver returns non-zero when anything is found
# --------------------------------------------------------------------------

def test_clean_file_scans_ok(tmp_path):
    f = tmp_path / "clean.md"
    f.write_text("nothing private here, see src/osched/pool.py\n",
                 encoding="utf-8")
    assert ps.scan([str(f)]) == 0


def test_public_ip_in_markdown_blocks(tmp_path):
    """The exact leak class: an IP in a Markdown table."""
    f = tmp_path / "endpoints.md"
    f.write_text(f"| ingress | {PUB_IP} |\n", encoding="utf-8")
    assert ps.scan([str(f)]) == 1


def test_public_ip_in_generated_html_blocks(tmp_path):
    """Regression: docs/site/*.html was never scanned."""
    f = tmp_path / "page.html"
    f.write_text(f"<td><code>{PUB_IP}</code></td>\n", encoding="utf-8")
    assert ps.scan([str(f)]) == 1


def test_zone_id_shape_blocks(tmp_path):
    f = tmp_path / "zone.md"
    f.write_text(f"zone id `{HEX32}`\n", encoding="utf-8")
    assert ps.scan([str(f)]) == 1


def test_loopback_in_html_is_fine(tmp_path):
    f = tmp_path / "local.html"
    f.write_text("<p>bind 127.0.0.1:8080</p>\n", encoding="utf-8")
    assert ps.scan([str(f)]) == 0


def test_count_accumulates_across_files(tmp_path, capsys):
    """Both files are reported; the gate still fails once for the batch."""
    a = tmp_path / "a.md"
    a.write_text(f"ip {PUB_IP}\n", encoding="utf-8")
    b = tmp_path / "b.md"
    b.write_text(f"ip {PUB_IP}\n", encoding="utf-8")
    assert ps.scan([str(a), str(b)]) == 1
    out = capsys.readouterr().out
    assert "a.md" in out and "b.md" in out


# --------------------------------------------------------------------------
# a real tree stays clean
# --------------------------------------------------------------------------

def test_this_repo_is_clean():
    """The gate has to hold on the actual working tree, not just fixtures."""
    assert ps.scan([]) == 0