---
name: sc-pipeline-plan
description: Compile a machine-readable scRNA-seq pipeline.yaml.
version: 0.1.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [single-cell, planning, pipeline]
    category: single-cell
    related_skills: [sc-qc, sc-normalize, sc-cluster]
---

# sc-pipeline-plan Skill

Produce an ordered, machine-readable `pipeline.yaml` for the thin-slice
scRNA-seq flow (qc → normalize → cluster). Prefer the scbio deterministic
compiler; Hermes chat is only an optional planning note.

## When to Use

- User asks to plan a single-cell downstream analysis
- Before orchestrated execution

## How to Run

Via API: `POST /projects/{id}/plan` with `{intent, stages?}`.

Or from Python:

```python
from orchestrator.planner import compile_pipeline, write_pipeline
```

## Procedure

1. Parse user intent (stages to include, param overrides).
2. Compile `pipeline.yaml` with per-stage I/O + compute/presentation params.
3. Store as artifact `pipeline:main` (versioned).
4. Hand off to orchestrator — do not execute stages in this skill.

## Verification

- `pipeline.yaml` lists stages in order with declared outputs.
