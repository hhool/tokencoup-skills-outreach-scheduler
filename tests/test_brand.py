"""Public-facing identity resolution (brand name, privacy URL).

Before this existed, `linkattest` was hard-coded in verify.py, portal.py and
bin/osched.py. A rename meant grepping the source; worse, the literal was
dragged into a public repo where it did not belong next to infra handles.

Run: python3 -m pytest tests/test_brand.py -q
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from osched import report  # noqa: E402


# --------------------------------------------------------------------------
# brand_name — resolution order
# --------------------------------------------------------------------------

def test_default_brand_when_nothing_configured(monkeypatch):
    monkeypatch.delenv("PORTAL_BRAND", raising=False)
    assert report.brand_name({}) == "outreach-scheduler"
    assert report.brand_name(None) == "outreach-scheduler"


def test_portal_brand_wins_over_report(monkeypatch):
    monkeypatch.delenv("PORTAL_BRAND", raising=False)
    cfg = {"portal": {"brand": "Acme"}, "report": {"brand": "Other"}}
    assert report.brand_name(cfg) == "Acme"


def test_report_brand_used_when_portal_missing(monkeypatch):
    monkeypatch.delenv("PORTAL_BRAND", raising=False)
    assert report.brand_name({"report": {"brand": "Acme"}}) == "Acme"


def test_env_brand_used_when_config_absent(monkeypatch):
    monkeypatch.setenv("PORTAL_BRAND", "EnvBrand")
    assert report.brand_name({}) == "EnvBrand"


def test_config_brand_beats_env(monkeypatch):
    monkeypatch.setenv("PORTAL_BRAND", "EnvBrand")
    assert report.brand_name({"portal": {"brand": "Cfg"}}) == "Cfg"


def test_blank_brand_falls_through(monkeypatch):
    monkeypatch.delenv("PORTAL_BRAND", raising=False)
    assert report.brand_name({"portal": {"brand": "   "}}) == "outreach-scheduler"


# --------------------------------------------------------------------------
# privacy_url
# --------------------------------------------------------------------------

def test_privacy_url_from_config(monkeypatch):
    monkeypatch.delenv("PORTAL_BASE_URL", raising=False)
    assert report.privacy_url({"portal": {"base_url": "https://x.com"}}) == \
        "https://x.com/privacy"


def test_privacy_url_strips_trailing_slash(monkeypatch):
    monkeypatch.delenv("PORTAL_BASE_URL", raising=False)
    assert report.privacy_url({"portal": {"base_url": "https://x.com/"}}) == \
        "https://x.com/privacy"


def test_privacy_url_env_wins(monkeypatch):
    monkeypatch.setenv("PORTAL_BASE_URL", "https://env.example")
    assert report.privacy_url({"portal": {"base_url": "https://cfg.example"}}) == \
        "https://env.example/privacy"


def test_privacy_url_relative_when_unset(monkeypatch):
    monkeypatch.delenv("PORTAL_BASE_URL", raising=False)
    assert report.privacy_url({}) == "/privacy"


# --------------------------------------------------------------------------
# no brand literal left in the source
# --------------------------------------------------------------------------

def test_no_brand_literal_hardcoded_in_src():
    """The point of the change: a rename is config-only."""
    offenders = []
    for path in sorted((ROOT / "src").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for needle in ("linkattest", "LinkAttest"):
            if needle in text:
                offenders.append(f"{path.name}:{needle}")
    assert offenders == [], offenders