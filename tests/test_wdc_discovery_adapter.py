"""WDC discovery-graph snapshot import.

The graph snapshot is a discovery layer, not competitor backlinks. These
tests pin the no-fabrication mapping and the existing import-pipeline
contract (lazy adapter -> SourceRecord -> pool).

Run: python3 -m pytest tests/test_wdc_discovery_adapter.py -q
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "import"))

from osched.adapters import (  # noqa: E402
    WdcDiscoveryAdapter, build_adapters,
)
from osched import matching  # noqa: E402
from osched.db import connect  # noqa: E402
from osched.pool import import_source  # noqa: E402

import prepare_discovery_snapshot as prep  # noqa: E402


ROW = {
    "domain": "topaitoolsreview.com",
    "vertex_id": 71799825,
    "industry": "ai-data",
    "dir_llr": 62.818,
    "dir_prob": 1.0,
    "outlink_l1": "ai-data",
    "outlink_similarity": 0.0508,
    "outlink_shared_targets": 279,
    "outlink_degree": 490,
    "dual_signal_agree": True,
    "outlink_used": True,
    "composite": 1.0,
    "inlink_support": 94,
}


def _doc(rows=None):
    return {
        "sources": ["colink_final_filtered.json", "outlink_results.json"],
        "formula": "composite = dir_prob + 0.12*min(sim*8,1)",
        "candidates": len(rows or [ROW]),
        "all_scored": rows if rows is not None else [ROW],
    }


def _write(tmp_path, doc, name="wdc-discovery-v0.3.json"):
    path = tmp_path / name
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.fixture()
def conn(tmp_path):
    return connect(tmp_path)


def test_parse_maps_graph_signals_without_fabricating_competitors(tmp_path):
    path = _write(tmp_path, _doc())
    adapter = WdcDiscoveryAdapter(str(path), snapshot="wdc-v0.3")
    files = adapter.discover()
    assert len(files) == 1
    rec = next(adapter.parse(files[0]))

    assert rec.url == "https://topaitoolsreview.com/"
    assert rec.domain == "topaitoolsreview.com"
    assert rec.source.kind == "wdc_discovery"
    assert rec.source.row_no == 0
    assert rec.evidence.via_competitors == []   # no invented competitor signal
    assert rec.evidence.inbound == 94           # seed co-link support
    assert rec.evidence.corpus == "wdc-v0.3"
    assert rec.hints["industry"] == "ai-data"
    assert rec.hints["layer"] == "discovery"
    assert rec.hints["feedback_layer"] == "not-implemented"
    assert rec.hints["discovery"]["dir_llr"] == 62.818
    assert rec.hints["lineage"]["sources"][0] == "colink_final_filtered.json"
    assert rec.raw == ROW                        # original row never lost


def test_snapshot_version_falls_back_to_filename(tmp_path):
    path = _write(tmp_path, _doc(), name="wdc-discovery-v0.4.json")
    adapter = WdcDiscoveryAdapter(str(path))
    rec = next(adapter.parse(adapter.discover()[0]))
    assert rec.evidence.corpus == "wdc-v0.4"
    assert rec.hints["snapshot"] == "wdc-v0.4"


def test_bad_domain_is_rejected_and_unknown_industry_only_warns(tmp_path):
    bad = dict(ROW, domain="not a domain", industry="not-a-real-industry")
    path = _write(tmp_path, _doc([bad]))
    adapter = WdcDiscoveryAdapter(str(path))
    rec = next(adapter.parse(adapter.discover()[0]))
    issues = adapter.validate(rec)
    assert any(i.severity == "error" and "url" in i.message for i in issues)
    assert any(i.severity == "warning" and "industry" in i.message for i in issues)


def test_import_is_idempotent_and_evidence_reaches_pool(conn, tmp_path):
    path = _write(tmp_path, _doc())
    adapter = WdcDiscoveryAdapter(str(path), snapshot="wdc-v0.3")
    first = import_source(conn, adapter)
    second = import_source(conn, adapter)
    assert (first["rows"], first["inserted"]) == (1, 1)
    assert (second["rows"], second["inserted"], second["updated"]) == (1, 0, 1)
    row = conn.execute("SELECT * FROM pool").fetchone()
    assert row["domain"] == "topaitoolsreview.com"
    evidence = json.loads(row["evidence_json"])
    assert evidence["inbound"] == 94 and evidence["via_competitors"] == []
    assert conn.execute("SELECT COUNT(*) FROM pool").fetchone()[0] == 1


def test_registry_builds_adapter_from_yaml_block(tmp_path):
    path = _write(tmp_path, _doc())
    adapters = build_adapters({
        "wdc_discovery_snapshot": {
            "glob": str(path.parent / "wdc-discovery-*.json"),
            "snapshot": "wdc-v0.3",
        }
    })
    assert len(adapters) == 1 and isinstance(adapters[0], WdcDiscoveryAdapter)
    assert len(adapters[0].discover()) == 1


def test_prepare_helper_stamps_and_scopes_output(tmp_path):
    src = tmp_path / "product.json"
    src.write_text(json.dumps(_doc()), encoding="utf-8")
    info = prep.prepare(str(src), "0.3", repo_root=str(tmp_path))
    assert info["rows"] == 1
    assert info["snapshot_version"] == "wdc-v0.3"
    assert info["industries"] == {"ai-data": 1}
    out = pathlib.Path(info["output"])
    assert out.exists() and out.name == "wdc-discovery-v0.3.json"
    assert json.loads(out.read_text(encoding="utf-8"))["snapshot_version"] == "wdc-v0.3"

    with pytest.raises(ValueError):
        prep.prepare(str(src), "0.3", repo_root=str(tmp_path),
                     out=str(tmp_path / "outside.json"))
    with pytest.raises(ValueError):
        prep.prepare(str(src), "bad version", repo_root=str(tmp_path))


def test_prepare_helper_rejects_malformed_product(tmp_path):
    src = tmp_path / "bad.json"
    src.write_text(json.dumps({"candidates": 0}), encoding="utf-8")
    with pytest.raises(ValueError, match="all_scored"):
        prep.prepare(str(src), "0.3", repo_root=str(tmp_path))


def _score_inputs(hints, evidence=None):
    return {
        "category": "gaming", "geo": "", "wave": 1, "dr": 0.0,
        "evidence_json": json.dumps(evidence or {}),
        "hints_json": json.dumps(hints),
    }


PROJECT = {
    "tags": ["gaming"],
    "scoring": {"weights": matching.DEFAULT_WEIGHTS, "min_score": 0.5},
}


def test_discovery_prior_ignores_every_other_source(tmp_path):
    assert matching.discovery_prior("not json") == 0.0
    assert matching.discovery_prior("{}") == 0.0
    curated = {"workbench_id": "t_1", "fit_score": 90}
    assert matching.discovery_prior(json.dumps(curated)) == 0.0


def test_discovery_prior_is_bounded_and_signal_ordered(tmp_path):
    path = _write(tmp_path, _doc())
    rec = next(WdcDiscoveryAdapter(str(path)).parse(
        WdcDiscoveryAdapter(str(path)).discover()[0]))
    strong = matching.discovery_prior(json.dumps(rec.hints))
    weak_hints = dict(rec.hints, discovery={
        "dir_prob": 0.0, "dir_llr": 0.0,
        "outlink_similarity": 0.0, "dual_signal_agree": False,
    })
    weak = matching.discovery_prior(json.dumps(weak_hints))
    assert 0.0 < strong <= 0.75
    assert strong > weak == 0.0
    # Corrupt numeric fields must not raise or inflate the prior.
    bad_hints = dict(rec.hints, discovery={"dir_prob": "oops", "dir_llr": None})
    assert matching.discovery_prior(json.dumps(bad_hints)) == 0.0


def test_scoring_uses_max_not_sum_and_no_fake_boost(tmp_path):
    path = _write(tmp_path, _doc())
    adapter = WdcDiscoveryAdapter(str(path), snapshot="wdc-v0.3")
    rec = next(adapter.parse(adapter.discover()[0]))
    evidence = {"via_competitors": [], "inbound": 94}
    assert matching.competitor_prior(json.dumps(evidence)) == 0.3

    score, detail = matching.score_row(_score_inputs(rec.hints, evidence), PROJECT)
    disc = matching.discovery_prior(json.dumps(rec.hints))
    assert detail["prior"] == round(disc, 2)   # max(), not 0.3 + disc
    assert detail["boost"] == 0.0              # graph evidence never hits 1.0
    assert score < 1.0

    # Non-discovery rows keep their exact previous score.
    plain, plain_detail = matching.score_row(
        _score_inputs({}, evidence), PROJECT)
    assert plain_detail["prior"] == 0.3
    assert plain_detail["discovery_prior"] == 0.0
    assert plain < score
