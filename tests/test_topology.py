"""WDC discovery topology manifest: export, validation, read-only rendering.

Run: python3 -m pytest tests/test_topology.py -q
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "import"))

import prepare_discovery_topology as exporter  # noqa: E402
from osched import topology  # noqa: E402


@pytest.fixture()
def workset(tmp_path):
    ws = tmp_path / "workset"
    ws.mkdir()
    gr = tmp_path / "graph"
    (gr / "raw").mkdir(parents=True)
    (gr / "edges2026").mkdir(parents=True)

    (gr / "raw" / "cc-main-2026-aug-sep-oct-domain-ranks.txt.gz").write_bytes(b"ranks")
    (gr / "edges2026" / "cc-main-2026-aug-sep-oct-domain-edges.txt.gz").write_bytes(b"edges")
    with open(gr / "wdc_domains.db", "wb") as fh:       # sparse, no real 16 GB write
        fh.truncate(exporter._HASH_LIMIT + 1)

    docs = {
        "seeds.json": {"total": 5},
        "seed_vertex_ids.json": {"host.example": 1},
        "votes_b.meta.json": {"targets": 100},
        "colink_final_balanced.json": {"results": [1, 2, 3]},
        "directory_scores.json": {"scores": {"a": 1}},
        "colink_final_filtered.json": {"results": [1, 2]},
        "outlink_results.json": {"candidates_scored": 9, "edges_scanned": 77},
        "colink_cross_validated.json": {"candidates": 2},
    }
    for name, doc in docs.items():
        (ws / name).write_text(json.dumps(doc), encoding="utf-8")
    (ws / "data-graph-closed-loop.md").write_text("nodes=123 arcs=77", encoding="utf-8")
    with open(ws / "votes_b.bin", "wb") as fh:
        fh.truncate(exporter._HASH_LIMIT + 1)
    (ws / "taxonomy.md").write_text("# taxonomy", encoding="utf-8")
    (ws / "review_shortlist_v2.csv").write_text(
        "industry,domain\nx,one.example\n", encoding="utf-8")
    return ws, gr


def test_export_records_stages_counts_and_honest_gaps(workset, tmp_path):
    ws, gr = workset
    info = exporter.export(str(ws), str(gr), "0.3", repo_root=str(tmp_path))
    doc = topology.load_topology(info["output"])
    assert info["stages"] == len(doc["stages"]) == 9
    assert info["edges"] == len(doc["edges"]) == 12
    assert doc["snapshot_version"] == "wdc-v0.3"
    assert doc["graph_stats"]["nodes_documented"] == 123
    assert doc["graph_stats"]["arcs_scanned"] == 77

    stages = {s["id"]: s for s in doc["stages"]}
    assert stages["review-loop"]["status"] == "human-gate-pending"
    assert stages["deliverable"]["outputs"][0]["records"] == 2
    assert stages["votes"]["outputs"][0]["hash_policy"].startswith("size-only")
    assert stages["votes"]["outputs"][0]["sha256"] is None
    assert stages["seeds"]["outputs"][1]["sha256"]          # small file hashed
    assert stages["review-loop"]["outputs"][0]["records"] == 1
    assert str(ws) not in pathlib.Path(info["output"]).read_text(encoding="utf-8")

    summary = topology.summarize(doc)
    assert summary["deliverable_rows"] == 2
    assert summary["review_status"] == "human-gate-pending"
    assert summary["status_counts"]["human-gate-pending"] == 1
    text = topology.render_text(doc)
    assert "discovery layer only" in text and "review-loop ->" not in text
    assert "deliverable -> review-loop" in text


def test_export_stays_inside_data_root(workset, tmp_path):
    ws, gr = workset
    with pytest.raises(ValueError):
        exporter.export(str(ws), str(gr), "0.3", repo_root=str(tmp_path),
                        out=str(tmp_path / "elsewhere.json"))
    with pytest.raises(ValueError):
        exporter.export(str(ws), str(gr), "bad", repo_root=str(tmp_path))


def test_load_topology_rejects_bad_structure(tmp_path):
    base = {
        "schema_version": topology.SCHEMA_VERSION,
        "snapshot_version": "wdc-v0.3",
        "stages": [
            {"id": "a", "label": "A", "status": "complete", "outputs": []},
            {"id": "b", "label": "B", "status": "complete", "outputs": []},
        ],
        "edges": [{"from": "a", "to": "b"}],
    }
    good = tmp_path / "good.json"
    good.write_text(json.dumps(base), encoding="utf-8")
    assert topology.load_topology(good) == base

    for mutate in (
        lambda d: d.update(schema_version="9.9"),
        lambda d: d["stages"][1].update(id="a"),
        lambda d: d["edges"].append({"from": "a", "to": "ghost"}),
        lambda d: d["stages"][0].pop("outputs"),
    ):
        bad = json.loads(json.dumps(base))
        mutate(bad)
        path = tmp_path / "bad.json"
        path.write_text(json.dumps(bad), encoding="utf-8")
        with pytest.raises(topology.TopologyError):
            topology.load_topology(path)
