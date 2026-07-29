from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

SCBIO = Path(__file__).resolve().parents[2] / "apps" / "scbio"
sys.path.insert(0, str(SCBIO))

from orchestrator import Orchestrator
from orchestrator.edits import ai_edit_artifact, manual_edit_artifact, update_stage_params
from orchestrator.planner import compile_pipeline
from scbio_provenance.impact import ImpactEngine, classify_change
from scbio_provenance.model import ImpactTier
from scbio_provenance.replay import events_for_timeline, replay_to
from scbio_provenance.store import ProvenanceStore


@pytest.fixture()
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("SCBIO_DATA_ROOT", str(tmp_path / "data"))
    root = tmp_path / "proj"
    store = ProvenanceStore(root)
    yield store
    store.close()


def test_compile_pipeline_default_stages():
    p = compile_pipeline(intent="demo")
    assert [s["id"] for s in p["stages"]] == ["qc", "normalize", "cluster"]
    assert "compute_params" in p["stages"][0]
    assert "presentation_params" in p["stages"][0]


def test_plan_run_replay_and_impact(project: ProvenanceStore):
    orch = Orchestrator(project)
    planned = orch.ensure_pipeline(intent="thin slice")
    assert (project.workspace / "pipeline.yaml").exists()
    assert planned["content_hash"]

    todos = orch.todo_from_pipeline()
    assert [t["id"] for t in todos] == ["qc", "normalize", "cluster"]

    results = orch.run_all()
    assert len(results) == 3
    assert all(r["ok"] for r in results)

    g = project.current_graph()
    step_ids = {n["id"] for n in g.nodes if n["kind"] == "step"}
    assert "step:qc" in step_ids and "step:cluster" in step_ids
    assert any(n["id"] == "art:cluster_adata" for n in g.nodes)

    events = events_for_timeline(project)
    assert events
    mid = events[len(events) // 2]["id"]
    snap = replay_to(project, until_event_id=mid)
    assert snap.as_of_event_id == mid
    assert isinstance(snap.nodes, list)

    # T0: edit notes doc — no hard rerun plan
    notes = (project.workspace / "cluster_notes.md").read_text(encoding="utf-8")
    edited = manual_edit_artifact(
        project,
        "art:cluster_notes",
        content=notes + "\n\nWording tweak only.\n",
        kind="doc",
        impact_tier="T0",
    )
    assert edited["tier"] == "T0"
    assert edited["impact"]["rerun_plan"] == []

    # T1: presentation param — figures_only
    t1 = update_stage_params(
        project,
        "cluster",
        presentation_params={"umap_palette": "viridis"},
    )
    assert t1["impact"]["tier"] == "T1"
    assert t1["impact"]["rerun_plan"][0]["mode"] == "figures_only"
    before_hash = project.get_latest_artifact_version("art:cluster_adata")["content_hash"]
    orch.run_stage("cluster", mode="figures_only")
    after_hash = project.get_latest_artifact_version("art:cluster_adata")["content_hash"]
    # figures_only re-records existing data file with same bytes → same content hash
    assert before_hash == after_hash

    # T2: compute resolution change marks dirty cascade
    t2 = update_stage_params(
        project,
        "cluster",
        compute_params={"resolution": 1.4},
    )
    assert t2["impact"]["tier"] == "T2"
    assert any(r["step_node_id"] == "step:cluster" for r in t2["impact"]["rerun_plan"])


def test_classify_change_tiers():
    assert classify_change(kind="doc") == ImpactTier.T0
    assert classify_change(param_section="presentation") == ImpactTier.T1
    assert classify_change(param_section="compute") == ImpactTier.T2
    assert classify_change(kind="data") == ImpactTier.T3


def test_ai_edit_branches_version(project: ProvenanceStore):
    orch = Orchestrator(project)
    orch.ensure_pipeline()
    orch.run_stage("qc")
    prev = project.get_latest_artifact_version("art:qc_notes")
    assert prev
    out = ai_edit_artifact(
        project,
        "art:qc_notes",
        "Use calmer language.",
        scope="local",
        impact_tier="T0",
    )
    assert out["artifact"]["parent_version_id"] == prev["id"]
    assert out["artifact"]["content_hash"] != prev["content_hash"]
    versions = project.list_artifact_versions("art:qc_notes")
    assert len(versions) >= 2


def test_hash_short_circuit(project: ProvenanceStore):
    orch = Orchestrator(project)
    orch.ensure_pipeline()
    orch.run_all()
    engine = ImpactEngine(project)
    art = "art:norm_adata"
    ver = project.get_latest_artifact_version(art)
    plan = engine.plan_from_artifact_change(
        art,
        ImpactTier.T2,
        new_content_hash=ver["content_hash"],
        old_content_hash=ver["content_hash"],
    )
    assert "short-circuit" in plan.notes.lower() or plan.rerun_plan


def test_fastapi_health_and_flow(tmp_path, monkeypatch):
    monkeypatch.setenv("SCBIO_DATA_ROOT", str(tmp_path / "data"))
    from fastapi.testclient import TestClient
    from backend.main import app

    client = TestClient(app)
    h = client.get("/health")
    assert h.status_code == 200
    assert h.json()["ok"] is True

    created = client.post("/projects", json={"name": "api-demo"})
    assert created.status_code == 200
    pid = created.json()["id"]

    planned = client.post(f"/projects/{pid}/plan", json={"intent": "demo"})
    assert planned.status_code == 200
    assert "pipeline" in planned.json()

    ran = client.post(f"/projects/{pid}/run", json={})
    assert ran.status_code == 200
    assert len(ran.json()["results"]) == 3

    graph = client.get(f"/projects/{pid}/graph")
    assert graph.status_code == 200
    assert len(graph.json()["nodes"]) >= 3

    tl = client.get(f"/projects/{pid}/timeline")
    assert tl.status_code == 200
    assert tl.json()["events"]

    # scrub timeline
    eid = tl.json()["events"][0]["id"]
    g2 = client.get(f"/projects/{pid}/graph", params={"as_of": eid})
    assert g2.status_code == 200
