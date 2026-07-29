#!/usr/bin/env python3
"""Normalize + HVG stage."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO / "apps" / "scbio"))

from scbio_provenance.client import StepRecorder  # noqa: E402
from scripts.sc_runtime import load_adata_json, normalize_hvg, save_adata_json  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", type=Path, required=True)
    args = ap.parse_args()
    ws: Path = args.workspace

    params_path = Path(__import__("os").environ.get("SCBIO_PARAMS", ws / "normalize.params.json"))
    params = {"compute": {}, "presentation": {}, "mode": "full"}
    if params_path.exists():
        params.update(json.loads(params_path.read_text(encoding="utf-8")))
    compute = params.get("compute") or {}

    qc_path = ws / "qc_adata.json"
    if not qc_path.exists():
        raise SystemExit("qc_adata.json missing — run qc first")

    adata = load_adata_json(qc_path)

    with StepRecorder(
        step_id="normalize",
        skill="sc-normalize",
        out_dir=ws,
        compute_params=compute,
        presentation_params=params.get("presentation") or {},
        code_paths=[Path(__file__)],
    ) as rec:
        rec.add_input(qc_path, kind="data", logical_id="art:qc_adata")
        norm, hvg = normalize_hvg(
            adata,
            target_sum=float(compute.get("target_sum", 1e4)),
            n_top_genes=int(compute.get("n_top_genes", 2000)),
        )
        out_adata = ws / "norm_adata.json"
        out_hvg = ws / "hvg.json"
        out_notes = ws / "norm_notes.md"
        save_adata_json(out_adata, norm)
        out_hvg.write_text(json.dumps(hvg, indent=2), encoding="utf-8")
        out_notes.write_text(
            "# Normalization notes\n\n"
            f"- target_sum={compute.get('target_sum', 1e4)}\n"
            f"- HVG selected: {hvg['n_top_genes']}\n"
            f"- Flavor: {compute.get('flavor', 'seurat')}\n",
            encoding="utf-8",
        )
        rec.add_output(out_adata, kind="data", logical_id="art:norm_adata")
        rec.add_output(out_hvg, kind="metrics", logical_id="art:hvg")
        rec.add_output(out_notes, kind="doc", logical_id="art:norm_notes")
        rec.checks = {"hvg_gt_0": hvg["n_top_genes"] > 0}
        manifest = rec.write_manifest(ws / "normalize.manifest.json")
        (ws / "step.manifest.json").write_text(
            json.dumps(manifest.to_dict(), indent=2), encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
