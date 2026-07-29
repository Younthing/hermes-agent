"""Surgical edits: manual + scoped AI edits that branch artifact versions."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from scbio_provenance.impact import ImpactEngine, classify_change
from scbio_provenance.model import ArtifactKind, EventType, ImpactTier
from scbio_provenance.store import ProvenanceStore


def manual_edit_artifact(
    store: ProvenanceStore,
    logical_id: str,
    *,
    content: Optional[str] = None,
    source_path: Optional[Path] = None,
    kind: Optional[str] = None,
    note: str = "",
    impact_tier: Optional[str] = None,
    apply_impact: bool = True,
) -> Dict[str, Any]:
    """Create a new artifact version (git-like branch via parent pointer)."""
    prev = store.get_latest_artifact_version(logical_id)
    parent_id = prev["id"] if prev else None
    old_hash = prev["content_hash"] if prev else None
    art_kind = kind or (prev["kind"] if prev else ArtifactKind.DOC.value)

    if content is not None:
        data = content.encode("utf-8")
        digest, blob = store.blobs.put_bytes(data)
        # write into workspace
        rel = prev["path"] if prev else f"{logical_id.split(':')[-1]}.txt"
        dest = store.workspace / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        info = {
            "version_id": store.create_artifact_version(
                logical_id=logical_id,
                path=rel,
                content_hash=digest,
                kind=art_kind,
                parent_version_id=parent_id,
                blob_path=str(blob),
                meta={"edit": "manual", "note": note},
            ),
            "logical_id": logical_id,
            "path": rel,
            "content_hash": digest,
            "kind": art_kind,
            "parent_version_id": parent_id,
        }
    elif source_path is not None:
        info = store.ingest_file(
            Path(source_path),
            logical_id=logical_id,
            kind=art_kind,
            parent_version_id=parent_id,
            meta={"edit": "manual", "note": note},
        )
        info["parent_version_id"] = parent_id
    else:
        raise ValueError("Provide content or source_path")

    store.append_event(
        EventType.MANUAL_EDIT,
        {
            **info,
            "note": note,
            "old_content_hash": old_hash,
        },
    )

    tier = classify_change(
        kind=art_kind,
        explicit_tier=impact_tier,
    )
    plan = None
    if apply_impact:
        engine = ImpactEngine(store)
        plan = engine.plan_from_artifact_change(
            logical_id,
            tier,
            new_content_hash=info["content_hash"],
            old_content_hash=old_hash,
        )
        engine.apply_marks(plan)

    return {"artifact": info, "tier": tier.value, "impact": plan.to_dict() if plan else None}


def ai_edit_artifact(
    store: ProvenanceStore,
    logical_id: str,
    instruction: str,
    *,
    scope: str = "local",  # local | global | agent
    agent_id: Optional[str] = None,
    context_text: Optional[str] = None,
    impact_tier: Optional[str] = None,
    apply_impact: bool = True,
    use_hermes: bool = False,
) -> Dict[str, Any]:
    """Scoped AI edit. Thin slice: deterministic rewrite; optional Hermes chat."""
    prev = store.get_latest_artifact_version(logical_id)
    if not prev:
        raise KeyError(f"No artifact {logical_id}")
    path = store.workspace / prev["path"]
    original = path.read_text(encoding="utf-8") if path.exists() else ""

    if use_hermes:
        new_text = _hermes_rewrite(original, instruction, scope, context_text)
    else:
        new_text = _deterministic_rewrite(original, instruction, scope, context_text)

    art_kind = prev.get("kind") or ArtifactKind.DOC.value
    digest, blob = store.blobs.put_bytes(new_text.encode("utf-8"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new_text, encoding="utf-8")
    vid = store.create_artifact_version(
        logical_id=logical_id,
        path=prev["path"],
        content_hash=digest,
        kind=art_kind,
        parent_version_id=prev["id"],
        blob_path=str(blob),
        meta={
            "edit": "ai",
            "scope": scope,
            "agent_id": agent_id,
            "instruction": instruction,
        },
    )
    info = {
        "version_id": vid,
        "logical_id": logical_id,
        "path": prev["path"],
        "content_hash": digest,
        "kind": art_kind,
        "parent_version_id": prev["id"],
    }
    store.append_event(
        EventType.AI_EDIT,
        {
            **info,
            "scope": scope,
            "agent_id": agent_id,
            "instruction": instruction,
            "old_content_hash": prev["content_hash"],
        },
        agent_id=agent_id,
    )

    tier = classify_change(kind=art_kind, explicit_tier=impact_tier)
    plan = None
    if apply_impact:
        engine = ImpactEngine(store)
        plan = engine.plan_from_artifact_change(
            logical_id,
            tier,
            new_content_hash=digest,
            old_content_hash=prev["content_hash"],
        )
        engine.apply_marks(plan)

    return {
        "artifact": info,
        "tier": tier.value,
        "impact": plan.to_dict() if plan else None,
        "preview": new_text[:2000],
        "scope": scope,
    }


def _deterministic_rewrite(
    original: str,
    instruction: str,
    scope: str,
    context_text: Optional[str],
) -> str:
    header = f"<!-- ai-edit scope={scope} instruction={instruction!r} -->\n"
    ctx = f"\n<!-- context -->\n{context_text}\n" if context_text else ""
    if not original.strip():
        return header + ctx + f"\n(Rewritten per: {instruction})\n"
    return header + original.rstrip() + "\n\n## AI revision note\n\n" + instruction + ctx + "\n"


def _hermes_rewrite(
    original: str,
    instruction: str,
    scope: str,
    context_text: Optional[str],
) -> str:
    try:
        from run_agent import AIAgent  # type: ignore
    except Exception:
        return _deterministic_rewrite(original, instruction, scope, context_text)
    try:
        agent = AIAgent(
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            max_iterations=2,
            enabled_toolsets=[],
        )
        prompt = (
            f"Scope={scope}. Edit the document per instruction. "
            f"Return ONLY the full revised markdown.\n\n"
            f"Instruction: {instruction}\n\n"
            f"Context:\n{context_text or '(none)'}\n\n"
            f"Document:\n{original[:12000]}"
        )
        return agent.chat(prompt) or _deterministic_rewrite(
            original, instruction, scope, context_text
        )
    except Exception:
        return _deterministic_rewrite(original, instruction, scope, context_text)


def update_stage_params(
    store: ProvenanceStore,
    stage_id: str,
    *,
    compute_params: Optional[Dict[str, Any]] = None,
    presentation_params: Optional[Dict[str, Any]] = None,
    apply_impact: bool = True,
) -> Dict[str, Any]:
    """Patch params in pipeline.yaml and emit impact plan for that stage."""
    import yaml

    from scbio_provenance.impact import ImpactEngine

    path = store.workspace / "pipeline.yaml"
    pipeline = yaml.safe_load(path.read_text(encoding="utf-8"))
    stage = next(s for s in pipeline["stages"] if s["id"] == stage_id)
    changed_compute = []
    changed_pres = []
    if compute_params:
        for k, v in compute_params.items():
            if stage.setdefault("compute_params", {}).get(k) != v:
                changed_compute.append(k)
            stage["compute_params"][k] = v
    if presentation_params:
        for k, v in presentation_params.items():
            if stage.setdefault("presentation_params", {}).get(k) != v:
                changed_pres.append(k)
            stage["presentation_params"][k] = v
    path.write_text(yaml.safe_dump(pipeline, sort_keys=False), encoding="utf-8")

    # version pipeline artifact
    data = path.read_bytes()
    digest, blob = store.blobs.put_bytes(data, suffix=".yaml")
    prev = store.get_latest_artifact_version("pipeline:main")
    store.create_artifact_version(
        logical_id="pipeline:main",
        path="pipeline.yaml",
        content_hash=digest,
        kind=ArtifactKind.PIPELINE,
        parent_version_id=prev["id"] if prev else None,
        blob_path=str(blob),
        meta={"patched_stage": stage_id},
    )

    engine = ImpactEngine(store)
    step_node = f"step:{stage_id}"
    if changed_pres and not changed_compute:
        plan = engine.plan_param_change(step_node, "presentation", changed_pres)
    else:
        plan = engine.plan_param_change(
            step_node, "compute", changed_compute or changed_pres
        )
    if apply_impact:
        engine.apply_marks(plan)
    return {"pipeline_hash": digest, "impact": plan.to_dict(), "stage": stage}
