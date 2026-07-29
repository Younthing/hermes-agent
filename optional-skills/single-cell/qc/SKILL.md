---
name: sc-qc
description: Filter cells/genes and compute scRNA-seq QC metrics.
version: 0.1.0
author: Hermes Agent
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [single-cell, scanpy, qc, bioinformatics]
    category: single-cell
    related_skills: [sc-normalize, sc-cluster, sc-pipeline-plan]
---

# sc-qc Skill

Thin-slice single-cell QC: filter low-quality cells/genes, emit metrics, a
violin placeholder figure, and a short interpretation draft.

## When to Use

- Starting a scRNA-seq downstream pipeline
- Re-running QC after changing thresholds (`min_genes`, `max_mito_pct`, …)

## Prerequisites

- Project workspace managed by `apps/scbio`
- Optional: `scanpy`/`anndata` (synthetic stdlib backend used otherwise)

## How to Run

```bash
python optional-skills/single-cell/qc/scripts/run.py --workspace "$SCBIO_WORKSPACE"
```

Or via the scbio orchestrator (`POST /projects/{id}/run/qc`).

## Quick Reference

| Param (compute) | Default | Effect |
|-----------------|---------|--------|
| min_genes | 200 | Drop cells with fewer genes |
| max_genes | 5000 | Drop doublets-ish high-count cells |
| max_mito_pct | 20 | Drop high mitochondrial fraction |
| min_cells | 3 | Drop rare genes |

Presentation: `violin_color` (T1 impact — figure only).

## Procedure

1. Load `raw_counts.json` (generated if missing).
2. Apply filters; write `qc_adata.json`, `qc_metrics.json`, `qc_violin.png`, `qc_notes.md`.
3. Emit `qc.manifest.json` / `step.manifest.json` with compute vs presentation params.

## Pitfalls

- Changing compute thresholds is **T3/T2** for downstream — expect normalize/cluster dirty.
- Changing only `violin_color` is **T1** — figures_only rerun.

## Verification

- `qc_adata.json` exists; metrics `n_cells_after > 0`; checks in manifest pass.
