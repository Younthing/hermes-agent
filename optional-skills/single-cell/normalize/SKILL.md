---
name: sc-normalize
description: Normalize counts, log1p, and select HVGs.
version: 0.1.0
author: Hermes Agent
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [single-cell, scanpy, normalize, hvg]
    category: single-cell
    related_skills: [sc-qc, sc-cluster, sc-pipeline-plan]
---

# sc-normalize Skill

Library-size normalize, log1p transform, and select highly variable genes.

## When to Use

- After QC has produced `qc_adata.json`
- When adjusting `target_sum` or `n_top_genes`

## Prerequisites

- `qc_adata.json` in the workspace

## How to Run

```bash
python optional-skills/single-cell/normalize/scripts/run.py --workspace "$SCBIO_WORKSPACE"
```

## Quick Reference

| Param (compute) | Default | Effect |
|-----------------|---------|--------|
| target_sum | 1e4 | Per-cell target library size |
| n_top_genes | 2000 | HVG count |
| flavor | seurat | Label only in thin slice |

## Procedure

1. Load `qc_adata.json`.
2. Normalize + HVG; write `norm_adata.json`, `hvg.json`, `norm_notes.md`.
3. Emit manifests with partitioned params.

## Pitfalls

- Must run after QC; missing input fails the gate.
- Compute param changes are T2 — cascade only if data hash changes.

## Verification

- `hvg.json` has `n_top_genes > 0`; manifest checks pass.
