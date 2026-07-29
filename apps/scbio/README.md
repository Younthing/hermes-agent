# scbio — Single-cell thin-slice on Hermes

Independent app (shape **C**): Hermes is the agent runtime; this app owns
provenance, impact-tier invalidation, orchestration, and the graph+timeline UI.

## Thin-slice stages

`qc` → `normalize` → `cluster` (scanpy when installed; synthetic fallback otherwise).

## Quick start

```bash
# from repo root
source .venv/bin/activate
pip install -e apps/scbio

# optional: real scanpy stack
pip install -e "apps/scbio[scanpy]"

# API
uvicorn backend.main:app --app-dir apps/scbio --reload --port 8765

# frontend (separate terminal)
cd apps/scbio/frontend && npm install && npm run dev
```

Open http://localhost:5173 — create a project, plan a pipeline, run stages,
scrub the timeline, apply surgical edits.

## Layout

| Path | Role |
|------|------|
| `scbio_provenance/` | Event-sourced SQLite + content-addressed blobs + impact engine |
| `orchestrator/` | Deterministic planner + Hermes-backed runner + stage gates |
| `backend/` | FastAPI surface |
| `frontend/` | React Flow graph + timeline |
| `optional-skills/single-cell/` | Stage skills (repo root) |
| `plugins/scbio-provenance/` | `post_tool_call` capture fallback (repo root) |
