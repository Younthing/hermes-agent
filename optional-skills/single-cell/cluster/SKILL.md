---
name: sc-cluster
description: PCA, neighbors, Leiden-style clusters, and UMAP.
version: 0.1.0
author: Hermes Agent
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [single-cell, scanpy, cluster, umap]
    category: single-cell
    related_skills: [sc-qc, sc-normalize, sc-pipeline-plan]
---

# sc-cluster Skill

Dimensionality reduction, graph clustering, and UMAP visualization.

## When to Use

- After normalization produced `norm_adata.json`
- Tuning `resolution` / `n_neighbors` (T2) or UMAP palette (T1)

## Prerequisites

- `norm_adata.json` in the workspace

## How to Run

```bash
python optional-skills/single-cell/cluster/scripts/run.py --workspace "$SCBIO_WORKSPACE"
```

## Quick Reference

| Param | Section | Default | Impact |
|-------|---------|---------|--------|
| n_pcs | compute | 30 | T2 |
| n_neighbors | compute | 15 | T2 |
| resolution | compute | 0.5 | T2 |
| umap_palette | presentation | tab10 | T1 |
| point_size | presentation | 8 | T1 |

## Procedure

1. Load `norm_adata.json`.
2. Reduce / cluster / embed; write `cluster_adata.json`, metrics, `umap.png`, notes.
3. Emit manifests. `mode=figures_only` re-renders UMAP without touching data hashes.

## Pitfalls

- Palette-only edits must use figures_only so data hashes short-circuit.
- Resolution changes may change cluster counts — downstream annotation (future) would dirty.

## Verification

- `n_clusters >= 2` in checks; `umap.png` exists.
