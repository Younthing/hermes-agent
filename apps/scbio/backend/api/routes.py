"""API routes for projects, plan, run, graph, timeline, edits, impact."""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from backend.config import get_data_root, project_dir
from backend.hermes_bridge import ping_planner
from orchestrator import Orchestrator
from orchestrator.edits import ai_edit_artifact, manual_edit_artifact, update_stage_params
from orchestrator.planner import compile_pipeline, load_pipeline
from scbio_provenance.impact import ImpactEngine, classify_change
from scbio_provenance.replay import events_for_timeline, replay_to
from scbio_provenance.store import ProvenanceStore

router = APIRouter()


def _slug(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_-]+", "-", name.strip().lower()).strip("-")
    return s or uuid.uuid4().hex[:8]


def _open_store(project_id: str) -> ProvenanceStore:
    path = project_dir(project_id)
    if not path.exists():
        raise HTTPException(404, f"Project not found: {project_id}")
    return ProvenanceStore(path)


class CreateProject(BaseModel):
    name: str = "demo"
    description: str = ""


class PlanRequest(BaseModel):
    intent: str = ""
    stages: Optional[List[str]] = None
    overrides: Optional[Dict[str, Dict[str, Any]]] = None
    ping_hermes: bool = False


class RunRequest(BaseModel):
    from_stage: Optional[str] = None
    only_dirty: bool = False
    mode: str = "full"


class ParamPatch(BaseModel):
    compute_params: Optional[Dict[str, Any]] = None
    presentation_params: Optional[Dict[str, Any]] = None
    rerun: bool = False


class ManualEditRequest(BaseModel):
    logical_id: str
    content: str
    kind: Optional[str] = None
    note: str = ""
    impact_tier: Optional[str] = None
    apply_impact: bool = True


class AiEditRequest(BaseModel):
    logical_id: str
    instruction: str
    scope: str = Field(default="local", pattern="^(local|global|agent)$")
    agent_id: Optional[str] = None
    context_text: Optional[str] = None
    impact_tier: Optional[str] = None
    apply_impact: bool = True
    use_hermes: bool = False


class ImpactRequest(BaseModel):
    logical_id: Optional[str] = None
    step_id: Optional[str] = None
    tier: Optional[str] = None
    param_section: Optional[str] = None
    changed_keys: Optional[List[str]] = None
    apply: bool = False
    rerun: bool = False


@router.get("/projects")
def list_projects():
    root = get_data_root() / "projects"
    root.mkdir(parents=True, exist_ok=True)
    items = []
    for p in sorted(root.iterdir()):
        if not p.is_dir():
            continue
        meta = {}
        mp = p / "project.json"
        if mp.exists():
            meta = json.loads(mp.read_text(encoding="utf-8"))
        items.append({"id": p.name, **meta})
    return {"projects": items}


@router.post("/projects")
def create_project(body: CreateProject):
    pid = _slug(body.name)
    path = project_dir(pid)
    if path.exists():
        pid = f"{pid}-{uuid.uuid4().hex[:4]}"
        path = project_dir(pid)
    store = ProvenanceStore(path)
    meta = {"id": pid, "name": body.name, "description": body.description}
    (path / "project.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    store.close()
    return meta


@router.get("/projects/{project_id}")
def get_project(project_id: str):
    path = project_dir(project_id)
    if not path.exists():
        raise HTTPException(404, "not found")
    meta = json.loads((path / "project.json").read_text(encoding="utf-8"))
    store = ProvenanceStore(path)
    g = store.current_graph()
    store.close()
    return {
        **meta,
        "graph_summary": {
            "n_nodes": len(g.nodes),
            "n_edges": len(g.edges),
            "n_versions": len(g.artifact_versions),
            "as_of_event_id": g.as_of_event_id,
        },
    }


@router.post("/projects/{project_id}/plan")
def plan_project(project_id: str, body: PlanRequest):
    store = _open_store(project_id)
    orch = Orchestrator(store)
    result = orch.ensure_pipeline(
        intent=body.intent, stages=body.stages, overrides=body.overrides
    )
    todos = orch.todo_from_pipeline(result["pipeline"])
    hermes = ping_planner(body.intent) if body.ping_hermes else None
    store.close()
    return {"pipeline": result, "todos": todos, "hermes": hermes}


@router.get("/projects/{project_id}/pipeline")
def get_pipeline(project_id: str):
    store = _open_store(project_id)
    path = store.workspace / "pipeline.yaml"
    if not path.exists():
        store.close()
        raise HTTPException(404, "pipeline.yaml not found — call /plan first")
    data = load_pipeline(path)
    store.close()
    return data


@router.post("/projects/{project_id}/run")
def run_all(project_id: str, body: RunRequest = RunRequest()):
    store = _open_store(project_id)
    orch = Orchestrator(store)
    try:
        results = orch.run_all(from_stage=body.from_stage, only_dirty=body.only_dirty)
    except Exception as e:  # noqa: BLE001
        store.close()
        raise HTTPException(400, str(e)) from e
    store.close()
    return {"results": results}


@router.post("/projects/{project_id}/run/{stage_id}")
def run_stage(project_id: str, stage_id: str, body: RunRequest = RunRequest()):
    store = _open_store(project_id)
    orch = Orchestrator(store)
    try:
        result = orch.run_stage(stage_id, mode=body.mode)
    except Exception as e:  # noqa: BLE001
        store.close()
        raise HTTPException(400, str(e)) from e
    store.close()
    return result


@router.get("/projects/{project_id}/graph")
def get_graph(project_id: str, as_of: Optional[int] = None, as_of_ts: Optional[str] = None):
    store = _open_store(project_id)
    if as_of is not None or as_of_ts is not None:
        snap = replay_to(store, until_event_id=as_of, until_ts=as_of_ts)
    else:
        snap = store.current_graph()
    store.close()
    return {
        "nodes": snap.nodes,
        "edges": snap.edges,
        "artifact_versions": snap.artifact_versions,
        "as_of_event_id": snap.as_of_event_id,
        "as_of_ts": snap.as_of_ts,
    }


@router.get("/projects/{project_id}/timeline")
def get_timeline(project_id: str):
    store = _open_store(project_id)
    events = events_for_timeline(store)
    store.close()
    return {"events": events}


@router.get("/projects/{project_id}/artifacts/{logical_id}")
def get_artifact(project_id: str, logical_id: str):
    store = _open_store(project_id)
    versions = store.list_artifact_versions(logical_id)
    latest = store.get_latest_artifact_version(logical_id)
    content = None
    if latest:
        p = store.workspace / latest["path"]
        if p.exists() and p.stat().st_size < 200_000 and latest.get("kind") in (
            "doc",
            "metrics",
            "pipeline",
        ):
            try:
                content = p.read_text(encoding="utf-8")
            except Exception:
                content = None
    store.close()
    return {"logical_id": logical_id, "latest": latest, "versions": versions, "content": content}


@router.post("/projects/{project_id}/edits/manual")
def manual_edit(project_id: str, body: ManualEditRequest):
    store = _open_store(project_id)
    try:
        result = manual_edit_artifact(
            store,
            body.logical_id,
            content=body.content,
            kind=body.kind,
            note=body.note,
            impact_tier=body.impact_tier,
            apply_impact=body.apply_impact,
        )
    except Exception as e:  # noqa: BLE001
        store.close()
        raise HTTPException(400, str(e)) from e
    store.close()
    return result


@router.post("/projects/{project_id}/edits/ai")
def ai_edit(project_id: str, body: AiEditRequest):
    store = _open_store(project_id)
    try:
        result = ai_edit_artifact(
            store,
            body.logical_id,
            body.instruction,
            scope=body.scope,
            agent_id=body.agent_id,
            context_text=body.context_text,
            impact_tier=body.impact_tier,
            apply_impact=body.apply_impact,
            use_hermes=body.use_hermes,
        )
    except Exception as e:  # noqa: BLE001
        store.close()
        raise HTTPException(400, str(e)) from e
    store.close()
    return result


@router.patch("/projects/{project_id}/stages/{stage_id}/params")
def patch_params(project_id: str, stage_id: str, body: ParamPatch):
    store = _open_store(project_id)
    try:
        result = update_stage_params(
            store,
            stage_id,
            compute_params=body.compute_params,
            presentation_params=body.presentation_params,
            apply_impact=True,
        )
        if body.rerun:
            orch = Orchestrator(store)
            plan = result["impact"]
            from scbio_provenance.impact import ImpactPlan, RerunStep

            rp = ImpactPlan(
                tier=plan["tier"],
                dirty_nodes=plan["dirty_nodes"],
                rerun_plan=[RerunStep(**r) for r in plan["rerun_plan"]],
                notes=plan.get("notes", ""),
            )
            # only rerun the seed stage modes appropriately
            for step in rp.rerun_plan:
                sid = step.step_node_id.split(":", 1)[-1].replace("sc-", "")
                if sid == stage_id:
                    result["rerun"] = orch.run_stage(stage_id, mode=step.mode)
                    break
    except Exception as e:  # noqa: BLE001
        store.close()
        raise HTTPException(400, str(e)) from e
    store.close()
    return result


@router.post("/projects/{project_id}/impact")
def impact(project_id: str, body: ImpactRequest):
    store = _open_store(project_id)
    engine = ImpactEngine(store)
    try:
        if body.step_id and body.param_section:
            plan = engine.plan_param_change(
                f"step:{body.step_id}",
                body.param_section,
                body.changed_keys,
            )
        elif body.logical_id:
            tier = classify_change(
                explicit_tier=body.tier,
                param_section=body.param_section,
            )
            plan = engine.plan_from_artifact_change(body.logical_id, tier)
        else:
            raise HTTPException(400, "Provide logical_id or step_id+param_section")
        out: Dict[str, Any] = {"plan": plan.to_dict()}
        if body.apply:
            engine.apply_marks(plan)
        if body.rerun:
            orch = Orchestrator(store)
            out["results"] = orch.apply_impact_and_rerun(plan)
    except HTTPException:
        store.close()
        raise
    except Exception as e:  # noqa: BLE001
        store.close()
        raise HTTPException(400, str(e)) from e
    store.close()
    return out


@router.post("/projects/{project_id}/hermes/ping")
def hermes_ping(project_id: str, intent: str = "thin-slice scRNA-seq"):
    _ = _open_store(project_id)  # ensure project exists
    return ping_planner(intent)
