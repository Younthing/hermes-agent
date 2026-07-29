"""Content-addressed blob store + event-sourced SQLite provenance DB."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .model import (
    ArtifactKind,
    EdgeKind,
    EventType,
    GraphSnapshot,
    NodeKind,
    NodeStatus,
    StepManifest,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS nodes (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  label TEXT NOT NULL,
  skill TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  created_at TEXT NOT NULL,
  meta_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS artifact_versions (
  id TEXT PRIMARY KEY,
  logical_id TEXT NOT NULL,
  path TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  kind TEXT NOT NULL,
  parent_version_id TEXT,
  blob_path TEXT,
  created_at TEXT NOT NULL,
  meta_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS edges (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  src_id TEXT NOT NULL,
  dst_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  meta_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  type TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  session_id TEXT,
  agent_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_artifact_logical ON artifact_versions(logical_id);
CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src_id);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst_id);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path, chunk: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class BlobStore:
    """Content-addressed file store (sha256). No size cap (unlike Hermes checkpoints)."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _dest(self, digest: str) -> Path:
        return self.root / digest[:2] / digest[2:4] / digest

    def put_file(self, src: Path) -> Tuple[str, Path]:
        digest = sha256_file(src)
        dest = self._dest(digest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            shutil.copy2(src, dest)
        return digest, dest

    def put_bytes(self, data: bytes, suffix: str = "") -> Tuple[str, Path]:
        digest = sha256_bytes(data)
        dest = self._dest(digest)
        if suffix:
            dest = dest.with_name(dest.name + suffix)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            dest.write_bytes(data)
        return digest, dest

    def get(self, digest: str) -> Optional[Path]:
        dest = self._dest(digest)
        return dest if dest.exists() else None


class ProvenanceStore:
    """Event-sourced provenance store for one project."""

    def __init__(self, project_dir: Path):
        self.project_dir = Path(project_dir)
        self.project_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.project_dir / "provenance.db"
        self.blobs = BlobStore(self.project_dir / "blobs")
        self.workspace = self.project_dir / "workspace"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ events
    def append_event(
        self,
        event_type: EventType | str,
        payload: Dict[str, Any],
        session_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        ts: Optional[str] = None,
    ) -> int:
        et = event_type.value if isinstance(event_type, EventType) else str(event_type)
        stamp = ts or utcnow()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO events(ts, type, payload_json, session_id, agent_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (stamp, et, json.dumps(payload), session_id, agent_id),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def list_events(
        self,
        until_event_id: Optional[int] = None,
        until_ts: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        q = "SELECT id, ts, type, payload_json, session_id, agent_id FROM events"
        args: List[Any] = []
        clauses = []
        if until_event_id is not None:
            clauses.append("id <= ?")
            args.append(until_event_id)
        if until_ts is not None:
            clauses.append("ts <= ?")
            args.append(until_ts)
        if clauses:
            q += " WHERE " + " AND ".join(clauses)
        q += " ORDER BY id ASC"
        with self._lock:
            rows = self._conn.execute(q, args).fetchall()
        out = []
        for r in rows:
            out.append(
                {
                    "id": r["id"],
                    "ts": r["ts"],
                    "type": r["type"],
                    "payload": json.loads(r["payload_json"]),
                    "session_id": r["session_id"],
                    "agent_id": r["agent_id"],
                }
            )
        return out

    # --------------------------------------------------------------- mutations
    def upsert_node(
        self,
        node_id: str,
        kind: NodeKind | str,
        label: str,
        status: NodeStatus | str = NodeStatus.PENDING,
        skill: Optional[str] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        k = kind.value if isinstance(kind, NodeKind) else kind
        st = status.value if isinstance(status, NodeStatus) else status
        with self._lock:
            existing = self._conn.execute(
                "SELECT id FROM nodes WHERE id = ?", (node_id,)
            ).fetchone()
            if existing:
                self._conn.execute(
                    "UPDATE nodes SET kind=?, label=?, skill=?, status=?, meta_json=? "
                    "WHERE id=?",
                    (k, label, skill, st, json.dumps(meta or {}), node_id),
                )
            else:
                self._conn.execute(
                    "INSERT INTO nodes(id, kind, label, skill, status, created_at, meta_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (node_id, k, label, skill, st, utcnow(), json.dumps(meta or {})),
                )
            self._conn.commit()

    def set_node_status(self, node_id: str, status: NodeStatus | str) -> None:
        st = status.value if isinstance(status, NodeStatus) else status
        with self._lock:
            self._conn.execute(
                "UPDATE nodes SET status=? WHERE id=?", (st, node_id)
            )
            self._conn.commit()
        self.append_event(
            EventType.STATUS_SET, {"node_id": node_id, "status": st}
        )

    def add_edge(
        self,
        kind: EdgeKind | str,
        src_id: str,
        dst_id: str,
        meta: Optional[Dict[str, Any]] = None,
    ) -> str:
        edge_id = f"e_{uuid.uuid4().hex[:12]}"
        k = kind.value if isinstance(kind, EdgeKind) else kind
        with self._lock:
            self._conn.execute(
                "INSERT INTO edges(id, kind, src_id, dst_id, created_at, meta_json) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (edge_id, k, src_id, dst_id, utcnow(), json.dumps(meta or {})),
            )
            self._conn.commit()
        return edge_id

    def create_artifact_version(
        self,
        logical_id: str,
        path: str,
        content_hash: str,
        kind: ArtifactKind | str | str,
        parent_version_id: Optional[str] = None,
        blob_path: Optional[str] = None,
        meta: Optional[Dict[str, Any]] = None,
        version_id: Optional[str] = None,
    ) -> str:
        vid = version_id or f"av_{uuid.uuid4().hex[:12]}"
        k = kind.value if isinstance(kind, ArtifactKind) else kind
        with self._lock:
            self._conn.execute(
                "INSERT INTO artifact_versions("
                "id, logical_id, path, content_hash, kind, parent_version_id, "
                "blob_path, created_at, meta_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    vid,
                    logical_id,
                    path,
                    content_hash,
                    k,
                    parent_version_id,
                    blob_path,
                    utcnow(),
                    json.dumps(meta or {}),
                ),
            )
            self._conn.commit()
        self.append_event(
            EventType.ARTIFACT_VERSION_CREATED,
            {
                "version_id": vid,
                "logical_id": logical_id,
                "path": path,
                "content_hash": content_hash,
                "kind": k,
                "parent_version_id": parent_version_id,
            },
        )
        # Ensure artifact node exists
        self.upsert_node(
            logical_id,
            NodeKind.ARTIFACT,
            label=Path(path).name,
            status=NodeStatus.CLEAN,
            meta={"kind": k, "latest_version_id": vid, "path": path},
        )
        return vid

    def ingest_file(
        self,
        src: Path,
        logical_id: str,
        kind: ArtifactKind | str,
        parent_version_id: Optional[str] = None,
        meta: Optional[Dict[str, Any]] = None,
        copy_to_workspace: bool = True,
    ) -> Dict[str, Any]:
        src = Path(src)
        digest, blob = self.blobs.put_file(src)
        rel_path = src.name
        if copy_to_workspace:
            dest = self.workspace / src.name
            # allow nested relative paths if src is under workspace already
            try:
                rel = src.relative_to(self.workspace)
                dest = self.workspace / rel
                rel_path = str(rel)
            except ValueError:
                dest = self.workspace / src.name
                rel_path = src.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.resolve() != src.resolve():
                shutil.copy2(src, dest)
        vid = self.create_artifact_version(
            logical_id=logical_id,
            path=rel_path,
            content_hash=digest,
            kind=kind,
            parent_version_id=parent_version_id,
            blob_path=str(blob),
            meta=meta,
        )
        return {
            "version_id": vid,
            "logical_id": logical_id,
            "path": rel_path,
            "content_hash": digest,
            "blob_path": str(blob),
            "kind": kind.value if isinstance(kind, ArtifactKind) else kind,
        }

    def record_step_manifest(
        self,
        manifest: StepManifest,
        session_id: Optional[str] = None,
        agent_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Apply a finished step.manifest.json into the store."""
        step_node = f"step:{manifest.step_id}"
        self.append_event(
            EventType.STEP_STARTED,
            {"step_id": manifest.step_id, "skill": manifest.skill},
            session_id=session_id,
            agent_id=agent_id,
            ts=manifest.started_at or utcnow(),
        )
        self.upsert_node(
            step_node,
            NodeKind.STEP,
            label=manifest.skill,
            skill=manifest.skill,
            status=NodeStatus.RUNNING,
            meta={
                "compute_params": manifest.compute_params,
                "presentation_params": manifest.presentation_params,
                "code_hash": manifest.code_hash,
                "env": manifest.env,
            },
        )

        # consume edges from input artifacts → step
        for inp in manifest.inputs:
            logical = inp.logical_id or f"art:{Path(inp.path).name}"
            self.upsert_node(
                logical,
                NodeKind.ARTIFACT,
                label=Path(inp.path).name,
                status=NodeStatus.CLEAN,
                meta={"kind": inp.kind, "path": inp.path, "hash": inp.hash},
            )
            self.add_edge(EdgeKind.CONSUMES, logical, step_node)

        produced: List[Dict[str, Any]] = []
        status = (
            NodeStatus.CLEAN
            if manifest.status in ("ok", "success", "clean")
            else NodeStatus.FAILED
        )
        for out in manifest.outputs:
            logical = out.logical_id or f"art:{Path(out.path).stem}"
            src_path = Path(out.path)
            if not src_path.is_absolute():
                # resolve relative to workspace or cwd
                cand = self.workspace / out.path
                if cand.exists():
                    src_path = cand
                elif Path(out.path).exists():
                    src_path = Path(out.path)
            parent = None
            with self._lock:
                row = self._conn.execute(
                    "SELECT id FROM artifact_versions WHERE logical_id=? "
                    "ORDER BY created_at DESC LIMIT 1",
                    (logical,),
                ).fetchone()
                if row:
                    parent = row["id"]
            if src_path.exists():
                info = self.ingest_file(
                    src_path,
                    logical_id=logical,
                    kind=out.kind,
                    parent_version_id=parent,
                    meta={"from_step": manifest.step_id},
                )
            else:
                # hash-only record (file already hashed by script)
                info = {
                    "version_id": self.create_artifact_version(
                        logical_id=logical,
                        path=out.path,
                        content_hash=out.hash,
                        kind=out.kind,
                        parent_version_id=parent,
                        meta={"from_step": manifest.step_id, "missing_file": True},
                    ),
                    "logical_id": logical,
                    "path": out.path,
                    "content_hash": out.hash,
                    "kind": out.kind,
                }
            self.add_edge(EdgeKind.PRODUCES, step_node, logical)
            produced.append(info)

        self.set_node_status(step_node, status)
        et = (
            EventType.STEP_FINISHED
            if status == NodeStatus.CLEAN
            else EventType.STEP_FAILED
        )
        self.append_event(
            et,
            {
                "step_id": manifest.step_id,
                "skill": manifest.skill,
                "checks": manifest.checks,
                "inputs": [i.to_dict() for i in manifest.inputs],
                "outputs": produced,
                "compute_params": manifest.compute_params,
                "presentation_params": manifest.presentation_params,
            },
            session_id=session_id,
            agent_id=agent_id,
            ts=manifest.ended_at or utcnow(),
        )
        return {"step_node": step_node, "outputs": produced, "status": status.value}

    # ----------------------------------------------------------------- queries
    def get_latest_artifact_version(self, logical_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM artifact_versions WHERE logical_id=? "
                "ORDER BY created_at DESC LIMIT 1",
                (logical_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_artifact_versions(self, logical_id: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock:
            if logical_id:
                rows = self._conn.execute(
                    "SELECT * FROM artifact_versions WHERE logical_id=? "
                    "ORDER BY created_at ASC",
                    (logical_id,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM artifact_versions ORDER BY created_at ASC"
                ).fetchall()
        return [dict(r) for r in rows]

    def current_graph(self) -> GraphSnapshot:
        with self._lock:
            nodes = [dict(r) for r in self._conn.execute("SELECT * FROM nodes").fetchall()]
            edges = [dict(r) for r in self._conn.execute("SELECT * FROM edges").fetchall()]
            arts = [
                dict(r)
                for r in self._conn.execute("SELECT * FROM artifact_versions").fetchall()
            ]
            last = self._conn.execute(
                "SELECT id, ts FROM events ORDER BY id DESC LIMIT 1"
            ).fetchone()
        for n in nodes:
            if "meta_json" in n:
                n["meta"] = json.loads(n.pop("meta_json") or "{}")
        for e in edges:
            if "meta_json" in e:
                e["meta"] = json.loads(e.pop("meta_json") or "{}")
        for a in arts:
            if "meta_json" in a:
                a["meta"] = json.loads(a.pop("meta_json") or "{}")
        return GraphSnapshot(
            nodes=nodes,
            edges=edges,
            artifact_versions=arts,
            as_of_event_id=last["id"] if last else None,
            as_of_ts=last["ts"] if last else None,
        )

    def neighbors(self, node_id: str, direction: str = "out") -> List[Dict[str, Any]]:
        with self._lock:
            if direction == "out":
                rows = self._conn.execute(
                    "SELECT * FROM edges WHERE src_id=?", (node_id,)
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM edges WHERE dst_id=?", (node_id,)
                ).fetchall()
        return [dict(r) for r in rows]

    def downstream_steps(self, artifact_logical_id: str) -> List[str]:
        """Steps that consume this artifact (directly)."""
        steps = []
        for e in self.neighbors(artifact_logical_id, "out"):
            if e["kind"] == EdgeKind.CONSUMES.value and e["dst_id"].startswith("step:"):
                steps.append(e["dst_id"])
        return steps

    def produced_artifacts(self, step_node_id: str) -> List[str]:
        arts = []
        for e in self.neighbors(step_node_id, "out"):
            if e["kind"] == EdgeKind.PRODUCES.value:
                arts.append(e["dst_id"])
        return arts

    def consumed_artifacts(self, step_node_id: str) -> List[str]:
        arts = []
        for e in self.neighbors(step_node_id, "in"):
            if e["kind"] == EdgeKind.CONSUMES.value:
                arts.append(e["src_id"])
        return arts
