"""Fold event log into a graph snapshot at time T (timeline scrubbing).

Contract: the snapshot at event id T is the *exclusive* fold of all events
with ``id <= T``. Nodes / edges / versions that only appear in later events
MUST NOT be present. Scrubbing backward therefore removes (deletes from the
frame) anything that had not been created yet — each timeline frame is a
clean historical graph, not a dirty overlay on the live graph.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .model import EventType, GraphSnapshot, NodeKind, NodeStatus
from .store import ProvenanceStore


def replay_to(
    store: ProvenanceStore,
    until_event_id: Optional[int] = None,
    until_ts: Optional[str] = None,
) -> GraphSnapshot:
    """Rebuild graph state by folding events up to T (clean snapshot)."""
    events = store.list_events(until_event_id=until_event_id, until_ts=until_ts)
    nodes: Dict[str, Dict[str, Any]] = {}
    edges: Dict[str, Dict[str, Any]] = {}
    versions: Dict[str, Dict[str, Any]] = {}
    edge_seq = 0
    # Track which artifact versions are "current" as of T (latest created ≤ T)
    latest_by_logical: Dict[str, str] = {}

    def ensure_node(nid: str, kind: str, label: str, **extra: Any) -> None:
        if nid not in nodes:
            nodes[nid] = {
                "id": nid,
                "kind": kind,
                "label": label,
                "status": NodeStatus.PENDING.value,
                "meta": {},
            }
        nodes[nid].update({k: v for k, v in extra.items() if k != "meta"})
        if "meta" in extra:
            nodes[nid]["meta"] = {**nodes[nid].get("meta", {}), **extra["meta"]}

    def add_edge(kind: str, src: str, dst: str) -> None:
        nonlocal edge_seq
        edge_seq += 1
        eid = f"replay_e_{edge_seq}"
        edges[eid] = {"id": eid, "kind": kind, "src_id": src, "dst_id": dst}

    for ev in events:
        t = ev["type"]
        p = ev["payload"] or {}

        if t == EventType.PIPELINE_PLANNED.value:
            ensure_node(
                "pipeline:main",
                NodeKind.ARTIFACT.value,
                "pipeline.yaml",
                status=NodeStatus.CLEAN.value,
                meta={"kind": "pipeline"},
            )
            if p.get("version_id"):
                versions[p["version_id"]] = {
                    "id": p["version_id"],
                    "logical_id": "pipeline:main",
                    "path": p.get("path", "pipeline.yaml"),
                    "content_hash": p.get("content_hash", ""),
                    "kind": "pipeline",
                    "parent_version_id": p.get("parent_version_id"),
                }
                latest_by_logical["pipeline:main"] = p["version_id"]

        elif t == EventType.STEP_STARTED.value:
            sid = f"step:{p['step_id']}"
            ensure_node(
                sid,
                NodeKind.STEP.value,
                p.get("skill", p["step_id"]),
                skill=p.get("skill"),
                status=NodeStatus.RUNNING.value,
            )

        elif t in (EventType.STEP_FINISHED.value, EventType.STEP_FAILED.value):
            sid = f"step:{p['step_id']}"
            st = (
                NodeStatus.CLEAN.value
                if t == EventType.STEP_FINISHED.value
                else NodeStatus.FAILED.value
            )
            ensure_node(
                sid,
                NodeKind.STEP.value,
                p.get("skill", p["step_id"]),
                skill=p.get("skill"),
                status=st,
                meta={
                    "compute_params": p.get("compute_params", {}),
                    "presentation_params": p.get("presentation_params", {}),
                },
            )
            # consumes: inputs known at finish time
            for inp in p.get("inputs") or []:
                logical = inp.get("logical_id")
                if not logical:
                    continue
                ensure_node(
                    logical,
                    NodeKind.ARTIFACT.value,
                    logical.split(":")[-1],
                    status=NodeStatus.CLEAN.value,
                    meta={"kind": inp.get("kind"), "path": inp.get("path")},
                )
                add_edge("consumes", logical, sid)

            for out in p.get("outputs") or []:
                logical = out.get("logical_id")
                if not logical:
                    continue
                ensure_node(
                    logical,
                    NodeKind.ARTIFACT.value,
                    logical.split(":")[-1],
                    status=NodeStatus.CLEAN.value,
                    meta={
                        "kind": out.get("kind"),
                        "path": out.get("path"),
                        "latest_version_id": out.get("version_id"),
                    },
                )
                add_edge("produces", sid, logical)
                if out.get("version_id"):
                    versions[out["version_id"]] = {
                        "id": out["version_id"],
                        "logical_id": logical,
                        "path": out.get("path", ""),
                        "content_hash": out.get("content_hash", ""),
                        "kind": out.get("kind", "other"),
                        "parent_version_id": out.get("parent_version_id"),
                    }
                    latest_by_logical[logical] = out["version_id"]

        elif t == EventType.ARTIFACT_VERSION_CREATED.value:
            logical = p["logical_id"]
            ensure_node(
                logical,
                NodeKind.ARTIFACT.value,
                logical.split(":")[-1],
                status=NodeStatus.CLEAN.value,
                meta={
                    "kind": p.get("kind"),
                    "path": p.get("path"),
                    "latest_version_id": p.get("version_id"),
                },
            )
            versions[p["version_id"]] = {
                "id": p["version_id"],
                "logical_id": logical,
                "path": p.get("path", ""),
                "content_hash": p.get("content_hash", ""),
                "kind": p.get("kind", "other"),
                "parent_version_id": p.get("parent_version_id"),
            }
            latest_by_logical[logical] = p["version_id"]

        elif t == EventType.STATUS_SET.value:
            nid = p.get("node_id")
            if nid and nid in nodes:
                nodes[nid]["status"] = p.get("status", nodes[nid]["status"])

        elif t == EventType.INVALIDATED.value:
            for nid in p.get("dirty_nodes") or []:
                if nid in nodes:
                    nodes[nid]["status"] = NodeStatus.DIRTY.value
                # Do NOT create nodes that only appear as dirty targets if they
                # never existed yet — a dirty mark on a not-yet-created node is
                # a no-op for historical frames.

        elif t in (EventType.MANUAL_EDIT.value, EventType.AI_EDIT.value):
            logical = p.get("logical_id")
            if logical:
                ensure_node(
                    logical,
                    NodeKind.ARTIFACT.value,
                    logical.split(":")[-1],
                    status=NodeStatus.CLEAN.value,
                    meta={
                        "edit": t,
                        "note": p.get("note", ""),
                        "kind": p.get("kind"),
                        "latest_version_id": p.get("version_id"),
                    },
                )
                if p.get("version_id"):
                    versions[p["version_id"]] = {
                        "id": p["version_id"],
                        "logical_id": logical,
                        "path": p.get("path", ""),
                        "content_hash": p.get("content_hash", ""),
                        "kind": p.get("kind", "doc"),
                        "parent_version_id": p.get("parent_version_id"),
                    }
                    latest_by_logical[logical] = p["version_id"]

    # Annotate nodes with as-of latest version when known
    for logical, vid in latest_by_logical.items():
        if logical in nodes:
            nodes[logical].setdefault("meta", {})["latest_version_id"] = vid

    last = events[-1] if events else None
    return GraphSnapshot(
        nodes=list(nodes.values()),
        edges=list(edges.values()),
        # Only versions created at or before T (already the case — we only
        # inserted from folded events).
        artifact_versions=list(versions.values()),
        as_of_event_id=last["id"] if last else None,
        as_of_ts=last["ts"] if last else None,
    )


def events_for_timeline(store: ProvenanceStore) -> List[Dict[str, Any]]:
    """Compact event list for the UI timeline scrubber."""
    events = store.list_events()
    out = []
    for ev in events:
        out.append(
            {
                "id": ev["id"],
                "ts": ev["ts"],
                "type": ev["type"],
                "summary": _summarize(ev),
                "payload": ev["payload"],
            }
        )
    return out


def _summarize(ev: Dict[str, Any]) -> str:
    t = ev["type"]
    p = ev.get("payload") or {}
    if t == EventType.STEP_FINISHED.value:
        return f"Finished {p.get('skill', p.get('step_id'))}"
    if t == EventType.STEP_STARTED.value:
        return f"Started {p.get('skill', p.get('step_id'))}"
    if t == EventType.STEP_FAILED.value:
        return f"Failed {p.get('skill', p.get('step_id'))}"
    if t == EventType.ARTIFACT_VERSION_CREATED.value:
        return f"Artifact {p.get('logical_id')} @ {str(p.get('content_hash', ''))[:8]}"
    if t == EventType.INVALIDATED.value:
        return f"Invalidated {len(p.get('dirty_nodes') or [])} nodes ({p.get('tier')})"
    if t == EventType.MANUAL_EDIT.value:
        return f"Manual edit {p.get('logical_id')}"
    if t == EventType.AI_EDIT.value:
        return f"AI edit {p.get('logical_id')}"
    if t == EventType.PIPELINE_PLANNED.value:
        return "Pipeline planned"
    return t
