#!/usr/bin/env python3
"""Cluster + UMAP stage."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO / "apps" / "scbio"))

from scbio_provenance.client import StepRecorder  # noqa: E402
from scripts.sc_runtime import (  # noqa: E402
    cluster_umap,
    load_adata_json,
    save_adata_json,
    write_png_placeholder,
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", type=Path, required=True)
    args = ap.parse_args()
    ws: Path = args.workspace

    params_path = Path(__import__("os").environ.get("SCBIO_PARAMS", ws / "cluster.params.json"))
    params = {"compute": {}, "presentation": {}, "mode": "full"}
    if params_path.exists():
        params.update(json.loads(params_path.read_text(encoding="utf-8")))
    compute = params.get("compute") or {}
    presentation = params.get("presentation") or {}
    mode = params.get("mode") or "full"

    norm_path = ws / "norm_adata.json"
    if not norm_path.exists():
        raise SystemExit("norm_adata.json missing — run normalize first")
    adata = load_adata_json(norm_path)

    with StepRecorder(
        step_id="cluster",
        skill="sc-cluster",
        out_dir=ws,
        compute_params=compute,
        presentation_params=presentation,
        code_paths=[Path(__file__)],
    ) as rec:
        rec.add_input(norm_path, kind="data", logical_id="art:norm_adata")

        if mode == "figures_only":
            fig = ws / "umap.png"
            write_png_placeholder(
                fig,
                "UMAP rerender",
                "#E45756" if presentation.get("umap_palette") == "tab10" else "#54A24B",
            )
            rec.add_output(fig, kind="figure", logical_id="art:umap_fig")
            for p, lid, kind in [
                (ws / "cluster_adata.json", "art:cluster_adata", "data"),
                (ws / "cluster_metrics.json", "art:cluster_metrics", "metrics"),
                (ws / "cluster_notes.md", "art:cluster_notes", "doc"),
            ]:
                if p.exists():
                    rec.add_output(p, kind=kind, logical_id=lid)
            rec.checks = {"figures_rerendered": True}
        else:
            clustered, metrics, _coords = cluster_umap(
                adata,
                n_pcs=int(compute.get("n_pcs", 30)),
                n_neighbors=int(compute.get("n_neighbors", 15)),
                resolution=float(compute.get("resolution", 0.5)),
                random_state=int(compute.get("random_state", 0)),
                palette=str(presentation.get("umap_palette", "tab10")),
            )
            out_adata = ws / "cluster_adata.json"
            out_metrics = ws / "cluster_metrics.json"
            out_fig = ws / "umap.png"
            out_notes = ws / "cluster_notes.md"
            save_adata_json(out_adata, clustered)
            out_metrics.write_text(json.dumps(metrics, indent=2), encoding="utf-8")
            write_png_placeholder(
                out_fig,
                f"UMAP clusters={metrics['n_clusters']} res={compute.get('resolution', 0.5)}",
                "#E45756",
            )
            out_notes.write_text(
                "# Clustering notes\n\n"
                f"- Clusters: {metrics['n_clusters']}\n"
                f"- resolution={compute.get('resolution', 0.5)}, "
                f"n_neighbors={compute.get('n_neighbors', 15)}, "
                f"n_pcs={compute.get('n_pcs', 30)}\n"
                f"- Palette: {presentation.get('umap_palette', 'tab10')}\n"
                f"- Sizes: {metrics['cluster_sizes']}\n",
                encoding="utf-8",
            )
            rec.add_output(out_adata, kind="data", logical_id="art:cluster_adata")
            rec.add_output(out_metrics, kind="metrics", logical_id="art:cluster_metrics")
            rec.add_output(out_fig, kind="figure", logical_id="art:umap_fig")
            rec.add_output(out_notes, kind="doc", logical_id="art:cluster_notes")
            rec.checks = {"n_clusters_ge_2": metrics["n_clusters"] >= 2}

        manifest = rec.write_manifest(ws / "cluster.manifest.json")
        (ws / "step.manifest.json").write_text(
            json.dumps(manifest.to_dict(), indent=2), encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
