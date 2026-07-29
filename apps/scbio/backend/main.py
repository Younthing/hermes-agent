"""FastAPI entry for scbio thin-slice app."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.api.routes import router
from backend.config import get_data_root

app = FastAPI(
    title="scbio",
    description="Single-cell thin-slice: provenance DAG + impact-tier reruns on Hermes",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)


@app.on_event("startup")
def _startup() -> None:
    get_data_root().mkdir(parents=True, exist_ok=True)


@app.get("/health")
def health():
    hermes_ok = False
    hermes_msg = ""
    try:
        from run_agent import AIAgent  # noqa: F401

        hermes_ok = True
        hermes_msg = "AIAgent importable"
    except Exception as e:  # noqa: BLE001
        hermes_msg = str(e)
    return {
        "ok": True,
        "data_root": str(get_data_root()),
        "hermes": {"ok": hermes_ok, "detail": hermes_msg},
    }


def cli() -> None:
    import uvicorn

    uvicorn.run(
        "backend.main:app",
        host=os.environ.get("SCBIO_HOST", "127.0.0.1"),
        port=int(os.environ.get("SCBIO_PORT", "8765")),
        reload=bool(os.environ.get("SCBIO_RELOAD")),
    )


if __name__ == "__main__":
    cli()
