"""Deterministic + Hermes-backed pipeline planner → pipeline.yaml."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from scbio_provenance.model import ArtifactKind, EventType
from scbio_provenance.store import ProvenanceStore, sha256_bytes, utcnow

DEFAULT_STAGES: List[Dict[str, Any]] = [
    {
        "id": "qc",
        "skill": "sc-qc",
        "description": "Quality control: filter cells/genes, compute QC metrics",
        "inputs": [{"logical_id": "art:raw_counts", "kind": "data", "path": "raw_counts.csv"}],
        "outputs": [
            {"logical_id": "art:qc_adata", "kind": "data", "path": "qc_adata.json"},
            {"logical_id": "art:qc_metrics", "kind": "metrics", "path": "qc_metrics.json"},
            {"logical_id": "art:qc_fig", "kind": "figure", "path": "qc_violin.png"},
            {"logical_id": "art:qc_notes", "kind": "doc", "path": "qc_notes.md"},
        ],
        "compute_params": {
            "min_genes": 200,
            "max_genes": 5000,
            "max_mito_pct": 20.0,
            "min_cells": 3,
        },
        "presentation_params": {"violin_color": "#4C78A8"},
    },
    {
        "id": "normalize",
        "skill": "sc-normalize",
        "description": "Normalize, log1p, select highly variable genes",
        "inputs": [{"logical_id": "art:qc_adata", "kind": "data", "path": "qc_adata.json"}],
        "outputs": [
            {"logical_id": "art:norm_adata", "kind": "data", "path": "norm_adata.json"},
            {"logical_id": "art:hvg", "kind": "metrics", "path": "hvg.json"},
            {"logical_id": "art:norm_notes", "kind": "doc", "path": "norm_notes.md"},
        ],
        "compute_params": {
            "target_sum": 1.0e4,
            "n_top_genes": 2000,
            "flavor": "seurat",
        },
        "presentation_params": {},
    },
    {
        "id": "cluster",
        "skill": "sc-cluster",
        "description": "PCA, neighbors, Leiden clustering, UMAP",
        "inputs": [{"logical_id": "art:norm_adata", "kind": "data", "path": "norm_adata.json"}],
        "outputs": [
            {"logical_id": "art:cluster_adata", "kind": "data", "path": "cluster_adata.json"},
            {"logical_id": "art:cluster_metrics", "kind": "metrics", "path": "cluster_metrics.json"},
            {"logical_id": "art:umap_fig", "kind": "figure", "path": "umap.png"},
            {"logical_id": "art:cluster_notes", "kind": "doc", "path": "cluster_notes.md"},
        ],
        "compute_params": {
            "n_pcs": 30,
            "n_neighbors": 15,
            "resolution": 0.5,
            "random_state": 0,
        },
        "presentation_params": {"umap_palette": "tab10", "point_size": 8},
    },
]


def compile_pipeline(
    intent: str = "",
    stages: Optional[List[str]] = None,
    overrides: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Compile a machine-readable pipeline.yaml structure (no LLM required)."""
    wanted = set(stages) if stages else {s["id"] for s in DEFAULT_STAGES}
    selected = []
    for s in DEFAULT_STAGES:
        if s["id"] not in wanted:
            continue
        stage = json.loads(json.dumps(s))  # deep copy
        if overrides and s["id"] in overrides:
            ov = overrides[s["id"]]
            stage["compute_params"].update(ov.get("compute_params") or {})
            stage["presentation_params"].update(ov.get("presentation_params") or {})
        selected.append(stage)
    return {
        "version": 1,
        "name": "single-cell-downstream",
        "intent": intent or "Run thin-slice scRNA-seq: qc → normalize → cluster",
        "created_at": utcnow(),
        "stages": selected,
        "gates": {
            "require_outputs": True,
            "require_checks": True,
        },
    }


def write_pipeline(store: ProvenanceStore, pipeline: Dict[str, Any]) -> Dict[str, Any]:
    text = yaml.safe_dump(pipeline, sort_keys=False)
    data = text.encode("utf-8")
    digest, blob = store.blobs.put_bytes(data, suffix=".yaml")
    dest = store.workspace / "pipeline.yaml"
    dest.write_bytes(data)
    parent = None
    prev = store.get_latest_artifact_version("pipeline:main")
    if prev:
        parent = prev["id"]
    vid = store.create_artifact_version(
        logical_id="pipeline:main",
        path="pipeline.yaml",
        content_hash=digest,
        kind=ArtifactKind.PIPELINE,
        parent_version_id=parent,
        blob_path=str(blob),
        meta={"intent": pipeline.get("intent", "")},
    )
    store.append_event(
        EventType.PIPELINE_PLANNED,
        {
            "version_id": vid,
            "path": "pipeline.yaml",
            "content_hash": digest,
            "parent_version_id": parent,
            "stages": [s["id"] for s in pipeline.get("stages", [])],
        },
    )
    return {
        "version_id": vid,
        "path": str(dest),
        "content_hash": digest,
        "pipeline": pipeline,
    }


def load_pipeline(path: Path) -> Dict[str, Any]:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def try_hermes_plan(intent: str, timeout_s: float = 60.0) -> Optional[str]:
    """Optional: ask Hermes AIAgent for a planning note. Returns text or None.

    Never required for the thin slice — deterministic compile_pipeline is the
    source of truth for pipeline.yaml.
    """
    try:
        from run_agent import AIAgent  # type: ignore
    except Exception:
        return None
    try:
        agent = AIAgent(
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            max_iterations=3,
            enabled_toolsets=["file"],
            disabled_toolsets=["browser", "web", "terminal", "delegation"],
        )
        prompt = (
            "You are planning a single-cell RNA-seq thin-slice pipeline. "
            "Reply with ONE short paragraph confirming stages qc→normalize→cluster. "
            f"User intent: {intent}"
        )
        return agent.chat(prompt)
    except Exception as e:  # noqa: BLE001
        return f"[hermes planner unavailable: {e}]"
