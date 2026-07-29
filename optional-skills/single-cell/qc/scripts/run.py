#!/usr/bin/env python3
"""QC stage: filter cells/genes, emit metrics + figure + notes."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow running without install
ROOT = Path(__file__).resolve().parents[4]  # repo root (optional-skills/... -> 4 up? )
# optional-skills/single-cell/qc/scripts/run.py -> parents: scripts, qc, single-cell, optional-skills, repo
REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO / "apps" / "scbio"))

from scbio_provenance.client import StepRecorder  # noqa: E402
from scripts.sc_runtime import (  # noqa: E402
    generate_raw_counts,
    load_adata_json,
    qc_filter,
    save_adata_json,
    write_png_placeholder,
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", type=Path, required=True)
    args = ap.parse_args()
    ws: Path = args.workspace
    ws.mkdir(parents=True, exist_ok=True)

    params_path = Path(__import__("os").environ.get("SCBIO_PARAMS", ws / "qc.params.json"))
    params = {"compute": {}, "presentation": {}, "mode": "full"}
    if params_path.exists():
        params.update(json.loads(params_path.read_text(encoding="utf-8")))
    compute = params.get("compute") or {}
    presentation = params.get("presentation") or {}
    mode = params.get("mode") or "full"

    raw_path = ws / "raw_counts.csv"
    # We store JSON sibling for the synthetic matrix; CSV is a pointer index
    raw_json = ws / "raw_counts.json"
    if not raw_json.exists():
        raw = generate_raw_counts()
        save_adata_json(raw_json, raw)
        # CSV header for human visibility
        raw_path.write_text(
            "cell_id,batch,true_type\n"
            + "\n".join(
                f"{c['cell_id']},{c['batch']},{c['true_type']}" for c in raw["cells"]
            )
            + "\n",
            encoding="utf-8",
        )
    else:
        raw = load_adata_json(raw_json)

    with StepRecorder(
        step_id="qc",
        skill="sc-qc",
        out_dir=ws,
        compute_params=compute,
        presentation_params=presentation,
        code_paths=[Path(__file__)],
    ) as rec:
        rec.add_input(raw_json, kind="data", logical_id="art:raw_counts")

        if mode == "figures_only":
            # re-render only
            color = presentation.get("violin_color", "#4C78A8")
            fig = ws / "qc_violin.png"
            write_png_placeholder(fig, "QC violin (rerender)", color)
            rec.add_output(fig, kind="figure", logical_id="art:qc_fig")
            # keep existing data outputs referenced
            for p, lid, kind in [
                (ws / "qc_adata.json", "art:qc_adata", "data"),
                (ws / "qc_metrics.json", "art:qc_metrics", "metrics"),
                (ws / "qc_notes.md", "art:qc_notes", "doc"),
            ]:
                if p.exists():
                    rec.add_output(p, kind=kind, logical_id=lid)
            rec.checks = {"figures_rerendered": True}
        else:
            filtered, metrics = qc_filter(
                raw,
                min_genes=int(compute.get("min_genes", 200)),
                max_genes=int(compute.get("max_genes", 5000)),
                max_mito_pct=float(compute.get("max_mito_pct", 20.0)),
                min_cells=int(compute.get("min_cells", 3)),
            )
            out_adata = ws / "qc_adata.json"
            out_metrics = ws / "qc_metrics.json"
            out_fig = ws / "qc_violin.png"
            out_notes = ws / "qc_notes.md"
            save_adata_json(out_adata, filtered)
            out_metrics.write_text(json.dumps(metrics, indent=2), encoding="utf-8")
            write_png_placeholder(
                out_fig,
                f"QC violin n={metrics['n_cells_after']}",
                presentation.get("violin_color", "#4C78A8"),
            )
            out_notes.write_text(
                "# QC notes\n\n"
                f"- Kept {metrics['n_cells_after']}/{metrics['n_cells_before']} cells "
                f"({metrics['fraction_kept']:.2%}).\n"
                f"- Genes: {metrics['n_genes_after']}/{metrics['n_genes_before']}.\n"
                f"- Thresholds: min_genes={compute.get('min_genes', 200)}, "
                f"max_mito_pct={compute.get('max_mito_pct', 20)}.\n",
                encoding="utf-8",
            )
            rec.add_output(out_adata, kind="data", logical_id="art:qc_adata")
            rec.add_output(out_metrics, kind="metrics", logical_id="art:qc_metrics")
            rec.add_output(out_fig, kind="figure", logical_id="art:qc_fig")
            rec.add_output(out_notes, kind="doc", logical_id="art:qc_notes")
            rec.checks = {
                "n_cells_after_gt_0": metrics["n_cells_after"] > 0,
                "fraction_kept_gt_0.1": metrics["fraction_kept"] > 0.1,
            }

        # also write stage-specific manifest copy name
        manifest = rec.write_manifest(ws / "qc.manifest.json")
        (ws / "step.manifest.json").write_text(
            json.dumps(manifest.to_dict(), indent=2), encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
