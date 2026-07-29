"""scbio-provenance plugin — post_tool_call fallback capture.

When ``SCBIO_PROJECT`` is set, inspect ``write_file`` / ``terminal`` results
for newly created paths under the project workspace and ingest them as
artifacts if they are not already content-addressed in the blob store.

This is a safety net for agent side-writes that skip ``step.manifest.json``.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, Set

logger = logging.getLogger(__name__)

_TERMINAL_PATH_REGEX = re.compile(r"(?:^|\s)(/[^\s'\"`]+|\./[^\s'\"`]+)")


def _project_dir() -> Path | None:
    raw = os.environ.get("SCBIO_PROJECT")
    return Path(raw) if raw else None


def _guess_kind(path: Path) -> str:
    suf = path.suffix.lower()
    if suf in {".png", ".jpg", ".jpeg", ".svg", ".pdf"}:
        return "figure"
    if suf in {".md", ".txt", ".rst"}:
        return "doc"
    if suf in {".json"} and "metric" in path.name.lower():
        return "metrics"
    if suf in {".h5ad", ".h5", ".csv", ".tsv", ".npz"} or "adata" in path.name.lower():
        return "data"
    if path.name == "pipeline.yaml":
        return "pipeline"
    return "other"


def _extract_paths(tool_name: str, args: Dict[str, Any], result: Any) -> Set[str]:
    paths: Set[str] = set()
    if tool_name in {"write_file", "patch"}:
        p = args.get("path")
        if isinstance(p, str) and p:
            paths.add(p)
    if tool_name == "terminal":
        text = ""
        if isinstance(result, str):
            text = result
        elif isinstance(result, dict):
            text = str(result.get("output") or result.get("stdout") or "")
        for m in _TERMINAL_PATH_REGEX.finditer(text):
            paths.add(m.group(1))
    return paths


def _on_post_tool_call(
    tool_name: str,
    args: Dict[str, Any],
    result: Any,
    **kwargs: Any,
) -> None:
    project = _project_dir()
    if project is None:
        return
    if tool_name not in {"write_file", "patch", "terminal"}:
        return
    try:
        # Import lazily so the plugin loads even when scbio isn't on PYTHONPATH
        import sys

        scbio_root = Path(__file__).resolve().parents[2] / "apps" / "scbio"
        if str(scbio_root) not in sys.path:
            sys.path.insert(0, str(scbio_root))
        from scbio_provenance.store import ProvenanceStore, sha256_file

        store = ProvenanceStore(project)
        workspace = store.workspace.resolve()
        for raw in _extract_paths(tool_name, args or {}, result):
            try:
                p = Path(raw).expanduser()
                if not p.is_absolute():
                    p = (workspace / p).resolve()
                else:
                    p = p.resolve()
                if not p.exists() or not p.is_file():
                    continue
                # Only capture files inside the project workspace
                try:
                    p.relative_to(workspace)
                except ValueError:
                    continue
                # Skip manifests (handled by orchestrator)
                if p.name.endswith("manifest.json") or p.name.endswith(".params.json"):
                    continue
                digest = sha256_file(p)
                logical = f"art:bypass:{p.stem}"
                latest = store.get_latest_artifact_version(logical)
                if latest and latest.get("content_hash") == digest:
                    continue
                store.ingest_file(
                    p,
                    logical_id=logical,
                    kind=_guess_kind(p),
                    parent_version_id=latest["id"] if latest else None,
                    meta={
                        "source": "plugin:scbio-provenance",
                        "tool_name": tool_name,
                        "session_id": kwargs.get("session_id"),
                        "tool_call_id": kwargs.get("tool_call_id"),
                    },
                )
                logger.info("scbio-provenance captured %s (%s)", p, digest[:12])
            except Exception as e:  # noqa: BLE001
                logger.debug("skip path %s: %s", raw, e)
        store.close()
    except Exception as e:  # noqa: BLE001
        logger.warning("scbio-provenance hook failed: %s", e)


def register(ctx) -> None:
    ctx.register_hook("post_tool_call", _on_post_tool_call)
