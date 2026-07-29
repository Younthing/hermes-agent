"""Orchestrator: pipeline.yaml → ordered stages → run scripts → gate → provenance."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from scbio_provenance.client import load_manifest
from scbio_provenance.impact import ImpactEngine, ImpactPlan
from scbio_provenance.model import NodeStatus
from scbio_provenance.store import ProvenanceStore

from .gates import GateError, assert_gate
from .planner import compile_pipeline, load_pipeline, write_pipeline

# Resolve skill scripts relative to hermes-agent repo root
REPO_ROOT = Path(__file__).resolve().parents[3]
SKILL_SCRIPTS = {
    "qc": REPO_ROOT / "optional-skills/single-cell/qc/scripts/run.py",
    "normalize": REPO_ROOT / "optional-skills/single-cell/normalize/scripts/run.py",
    "cluster": REPO_ROOT / "optional-skills/single-cell/cluster/scripts/run.py",
    "sc-qc": REPO_ROOT / "optional-skills/single-cell/qc/scripts/run.py",
    "sc-normalize": REPO_ROOT / "optional-skills/single-cell/normalize/scripts/run.py",
    "sc-cluster": REPO_ROOT / "optional-skills/single-cell/cluster/scripts/run.py",
}


class Orchestrator:
    def __init__(self, store: ProvenanceStore):
        self.store = store
        self.workspace = store.workspace
        self.impact = ImpactEngine(store)

    def ensure_pipeline(
        self,
        intent: str = "",
        stages: Optional[List[str]] = None,
        overrides: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        pipeline = compile_pipeline(intent=intent, stages=stages, overrides=overrides)
        return write_pipeline(self.store, pipeline)

    def todo_from_pipeline(self, pipeline: Optional[Dict[str, Any]] = None) -> List[Dict[str, str]]:
        if pipeline is None:
            path = self.workspace / "pipeline.yaml"
            if not path.exists():
                raise FileNotFoundError("pipeline.yaml missing — plan first")
            pipeline = load_pipeline(path)
        todos = []
        for s in pipeline.get("stages", []):
            todos.append(
                {
                    "id": s["id"],
                    "content": s.get("description") or s["id"],
                    "status": "pending",
                    "skill": s.get("skill", s["id"]),
                }
            )
        return todos

    def run_stage(
        self,
        stage_id: str,
        *,
        mode: str = "full",
        extra_env: Optional[Dict[str, str]] = None,
        pipeline: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if pipeline is None:
            pipeline = load_pipeline(self.workspace / "pipeline.yaml")
        stage = next((s for s in pipeline["stages"] if s["id"] == stage_id), None)
        if not stage:
            raise KeyError(f"Unknown stage: {stage_id}")

        script = SKILL_SCRIPTS.get(stage.get("skill") or stage_id) or SKILL_SCRIPTS.get(stage_id)
        if not script or not script.exists():
            raise FileNotFoundError(f"No script for stage {stage_id}: {script}")

        step_node = f"step:{stage_id}"
        self.store.upsert_node(
            step_node,
            "step",
            label=stage.get("skill", stage_id),
            skill=stage.get("skill", stage_id),
            status=NodeStatus.RUNNING,
            meta={
                "compute_params": stage.get("compute_params", {}),
                "presentation_params": stage.get("presentation_params", {}),
            },
        )

        params_path = self.workspace / f"{stage_id}.params.json"
        params_path.write_text(
            json.dumps(
                {
                    "compute": stage.get("compute_params", {}),
                    "presentation": stage.get("presentation_params", {}),
                    "mode": mode,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

        env = os.environ.copy()
        env["SCBIO_PROJECT"] = str(self.store.project_dir)
        env["SCBIO_WORKSPACE"] = str(self.workspace)
        env["SCBIO_STAGE"] = stage_id
        env["SCBIO_PARAMS"] = str(params_path)
        env["PYTHONPATH"] = os.pathsep.join(
            [
                str(REPO_ROOT / "apps" / "scbio"),
                env.get("PYTHONPATH", ""),
            ]
        )
        if extra_env:
            env.update(extra_env)

        # Capture previous data hashes for short-circuit
        prev_hashes = {}
        for out in stage.get("outputs") or []:
            if out.get("kind") == "data":
                ver = self.store.get_latest_artifact_version(out["logical_id"])
                if ver:
                    prev_hashes[out["logical_id"]] = ver["content_hash"]

        proc = subprocess.run(
            [sys.executable, str(script), "--workspace", str(self.workspace)],
            cwd=str(self.workspace),
            env=env,
            capture_output=True,
            text=True,
        )
        log_path = self.workspace / f"{stage_id}.log"
        log_path.write_text(
            f"STDOUT:\n{proc.stdout}\n\nSTDERR:\n{proc.stderr}\n\nreturncode={proc.returncode}\n",
            encoding="utf-8",
        )
        if proc.returncode != 0:
            self.store.set_node_status(step_node, NodeStatus.FAILED)
            raise RuntimeError(
                f"Stage {stage_id} failed (rc={proc.returncode}): {proc.stderr[-2000:]}"
            )

        # Prefer stage-specific manifest; fall back to step.manifest.json
        manifest_path = self.workspace / f"{stage_id}.manifest.json"
        if not manifest_path.exists():
            generic = self.workspace / "step.manifest.json"
            if generic.exists():
                shutil.copy2(generic, manifest_path)

        if not manifest_path.exists():
            self.store.set_node_status(step_node, NodeStatus.FAILED)
            raise RuntimeError(f"Stage {stage_id} produced no step.manifest.json")

        self.store.record_step_manifest(load_manifest(manifest_path))
        gate_report = assert_gate(stage, self.workspace, pipeline.get("gates"))

        new_hashes = {}
        for out in stage.get("outputs") or []:
            if out.get("kind") == "data":
                ver = self.store.get_latest_artifact_version(out["logical_id"])
                if ver:
                    new_hashes[out["logical_id"]] = ver["content_hash"]

        short = None
        if prev_hashes:
            short = self.impact.apply_after_rerun(
                step_node,
                previous_data_hashes=prev_hashes,
                new_data_hashes=new_hashes,
            )

        self.store.set_node_status(step_node, NodeStatus.CLEAN)
        return {
            "stage": stage_id,
            "ok": True,
            "gate": gate_report,
            "log": str(log_path),
            "stdout": proc.stdout[-4000:],
            "hash_short_circuit": short.to_dict() if short else None,
            "prev_hashes": prev_hashes,
            "new_hashes": new_hashes,
        }

    def run_all(
        self,
        *,
        from_stage: Optional[str] = None,
        only_dirty: bool = False,
        pipeline: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        if pipeline is None:
            pipeline = load_pipeline(self.workspace / "pipeline.yaml")
        results = []
        started = from_stage is None
        graph = self.store.current_graph()
        dirty = {n["id"] for n in graph.nodes if n.get("status") == NodeStatus.DIRTY.value}
        for stage in pipeline.get("stages", []):
            sid = stage["id"]
            if not started:
                if sid == from_stage:
                    started = True
                else:
                    continue
            step_node = f"step:{sid}"
            if only_dirty and dirty and step_node not in dirty:
                # also run if any produced artifact dirty
                produced_dirty = False
                # check outputs
                for out in stage.get("outputs") or []:
                    if out["logical_id"] in dirty:
                        produced_dirty = True
                if not produced_dirty:
                    continue
            results.append(self.run_stage(sid, pipeline=pipeline))
        return results

    def apply_impact_and_rerun(self, plan: ImpactPlan) -> Dict[str, Any]:
        self.impact.apply_marks(plan)
        results = []
        for step in plan.rerun_plan:
            stage_id = step.step_node_id.split(":", 1)[-1]
            # map skill names like sc-qc → qc
            for key in (stage_id, step.skill):
                if key in ("qc", "normalize", "cluster", "sc-qc", "sc-normalize", "sc-cluster"):
                    stage_id = key.replace("sc-", "")
                    break
            results.append(self.run_stage(stage_id, mode=step.mode))
        return {"plan": plan.to_dict(), "results": results}

    def delegate_stage_via_hermes(self, stage_id: str, goal: str) -> Optional[str]:
        """Optional: spawn a Hermes leaf subagent for a stage (when credentials exist).

        Thin slice primary path is direct script execution; this is the multi-agent hook.
        """
        try:
            from run_agent import AIAgent  # type: ignore
            from tools.delegate_tool import delegate_task  # type: ignore
        except Exception:
            return None
        try:
            parent = AIAgent(
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
                max_iterations=5,
                platform="scbio",
            )
            # Attach parent for delegate internals
            result = delegate_task(
                goal=goal,
                context=f"Run single-cell stage {stage_id} using its skill script.",
                role="leaf",
                parent_agent=parent,
            )
            return result if isinstance(result, str) else json.dumps(result)
        except Exception as e:  # noqa: BLE001
            return f"[delegate unavailable: {e}]"
