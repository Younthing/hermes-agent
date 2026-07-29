"""Shared synthetic single-cell runtime (scanpy if available, else stdlib)."""

from __future__ import annotations

import hashlib
import json
import math
import random
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def has_scanpy() -> bool:
    try:
        import scanpy  # noqa: F401
        import anndata  # noqa: F401
        import numpy  # noqa: F401
        return True
    except Exception:
        return False


def _seeded(rng_seed: int = 0) -> random.Random:
    return random.Random(rng_seed)


def generate_raw_counts(
    n_cells: int = 500,
    n_genes: int = 300,
    n_types: int = 4,
    seed: int = 0,
) -> Dict[str, Any]:
    """Synthetic count matrix as JSON (portable without anndata)."""
    rng = _seeded(seed)
    genes = [f"Gene{i:04d}" for i in range(n_genes)]
    # last 10 genes are "mito"
    for i in range(min(10, n_genes)):
        genes[-(i + 1)] = f"MT-{i+1}"
    cells = []
    X = []
    for c in range(n_cells):
        ctype = c % n_types
        # sprinkle some low-quality cells
        low_q = rng.random() < 0.08
        high_mito = rng.random() < 0.06
        row = []
        for g, gene in enumerate(genes):
            base = 2 + (g % 7) + ctype * 3
            if gene.startswith("MT-"):
                base = 40 if high_mito else 5
            if low_q:
                base = max(0, base // 5)
            val = max(0, int(rng.gauss(base, base * 0.4 + 1)))
            row.append(val)
        cells.append(
            {
                "cell_id": f"CELL_{c:04d}",
                "batch": f"b{c % 2}",
                "true_type": f"type_{ctype}",
            }
        )
        X.append(row)
    return {"genes": genes, "cells": cells, "X": X, "n_cells": n_cells, "n_genes": n_genes}


def save_adata_json(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


def load_adata_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def qc_filter(
    raw: Dict[str, Any],
    *,
    min_genes: int = 200,
    max_genes: int = 5000,
    max_mito_pct: float = 20.0,
    min_cells: int = 3,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    genes = raw["genes"]
    mito_idx = [i for i, g in enumerate(genes) if g.startswith("MT-")]
    keep_cells = []
    cell_meta = []
    metrics_per_cell = []
    for ci, row in enumerate(raw["X"]):
        n_genes = sum(1 for v in row if v > 0)
        total = sum(row) or 1
        mito = sum(row[i] for i in mito_idx)
        mito_pct = 100.0 * mito / total
        metrics_per_cell.append(
            {"cell_id": raw["cells"][ci]["cell_id"], "n_genes": n_genes, "mito_pct": mito_pct, "total": total}
        )
        if n_genes >= min_genes and n_genes <= max_genes and mito_pct <= max_mito_pct:
            keep_cells.append(ci)
            cell_meta.append(raw["cells"][ci])

    # gene filter
    keep_genes = []
    for gi in range(len(genes)):
        expressed = sum(1 for ci in keep_cells if raw["X"][ci][gi] > 0)
        if expressed >= min_cells:
            keep_genes.append(gi)

    X2 = [[raw["X"][ci][gi] for gi in keep_genes] for ci in keep_cells]
    out = {
        "genes": [genes[i] for i in keep_genes],
        "cells": cell_meta,
        "X": X2,
        "n_cells": len(keep_cells),
        "n_genes": len(keep_genes),
        "layer": "counts",
    }
    metrics = {
        "n_cells_before": raw["n_cells"],
        "n_cells_after": out["n_cells"],
        "n_genes_before": raw["n_genes"],
        "n_genes_after": out["n_genes"],
        "min_genes": min_genes,
        "max_genes": max_genes,
        "max_mito_pct": max_mito_pct,
        "fraction_kept": out["n_cells"] / max(1, raw["n_cells"]),
        "per_cell_sample": metrics_per_cell[:20],
    }
    return out, metrics


def normalize_hvg(
    adata: Dict[str, Any],
    *,
    target_sum: float = 1e4,
    n_top_genes: int = 2000,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    X = adata["X"]
    n_cells = len(X)
    n_genes = len(X[0]) if X else 0
    # library-size normalize + log1p
    norm = []
    for row in X:
        s = sum(row) or 1.0
        scale = target_sum / s
        norm.append([math.log1p(v * scale) for v in row])

    # variance-based HVG
    means = []
    vars_ = []
    for gi in range(n_genes):
        col = [norm[ci][gi] for ci in range(n_cells)]
        mu = sum(col) / max(1, n_cells)
        var = sum((x - mu) ** 2 for x in col) / max(1, n_cells)
        means.append(mu)
        vars_.append(var)
    order = sorted(range(n_genes), key=lambda i: vars_[i], reverse=True)
    top = order[: min(n_top_genes, n_genes)]
    top_set = set(top)
    hvg_flags = [i in top_set for i in range(n_genes)]
    out = {
        "genes": adata["genes"],
        "cells": adata["cells"],
        "X": norm,
        "n_cells": n_cells,
        "n_genes": n_genes,
        "layer": "lognorm",
        "hvg": hvg_flags,
        "hvg_indices": top,
    }
    hvg_info = {
        "n_top_genes": len(top),
        "genes": [adata["genes"][i] for i in top[:50]],
        "variances_top": [vars_[i] for i in top[:50]],
    }
    return out, hvg_info


def cluster_umap(
    adata: Dict[str, Any],
    *,
    n_pcs: int = 10,
    n_neighbors: int = 15,
    resolution: float = 0.5,
    random_state: int = 0,
    palette: str = "tab10",
) -> Tuple[Dict[str, Any], Dict[str, Any], List[Tuple[float, float]]]:
    """Lightweight PCA-ish + kNN Leiden-ish clustering without heavy deps."""
    rng = _seeded(random_state)
    X = adata["X"]
    n_cells = len(X)
    # use HVG columns if present
    idxs = adata.get("hvg_indices") or list(range(min(50, len(X[0]) if X else 0)))
    idxs = idxs[: max(n_pcs * 3, 10)]

    def _row_ok(row: List[float], ix: List[int]) -> bool:
        return all(j < len(row) for j in ix)

    feats = [
        [row[j] for j in idxs] if _row_ok(row, idxs) else row[: len(idxs)]
        for row in X
    ]

    # center
    dim = len(feats[0]) if feats else 0
    means = [sum(feats[i][d] for i in range(n_cells)) / max(1, n_cells) for d in range(dim)]
    centered = [[feats[i][d] - means[d] for d in range(dim)] for i in range(n_cells)]

    # fake PCA: take first n_pcs dims after random projection
    pcs = []
    for i in range(n_cells):
        vec = centered[i][:n_pcs]
        while len(vec) < n_pcs:
            vec.append(0.0)
        pcs.append(vec)

    # kNN graph (brute force)
    k = min(n_neighbors, max(1, n_cells - 1))

    def dist(a, b):
        return sum((x - y) ** 2 for x, y in zip(a, b)) ** 0.5

    neighbors = []
    for i in range(n_cells):
        ds = [(dist(pcs[i], pcs[j]), j) for j in range(n_cells) if j != i]
        ds.sort()
        neighbors.append([j for _, j in ds[:k]])

    # simple label propagation clustering modulated by resolution
    n_clusters = max(2, min(n_cells // 20, int(2 + resolution * 8)))
    labels = [i % n_clusters for i in range(n_cells)]
    for _ in range(5):
        new_labels = labels[:]
        for i in range(n_cells):
            votes: Dict[int, int] = {}
            for j in neighbors[i]:
                votes[labels[j]] = votes.get(labels[j], 0) + 1
            if votes:
                new_labels[i] = max(votes, key=votes.get)
        labels = new_labels

    # UMAP-like 2D: use first 2 PCs + jitter
    coords = []
    for i in range(n_cells):
        x = pcs[i][0] if pcs[i] else 0.0
        y = pcs[i][1] if len(pcs[i]) > 1 else 0.0
        coords.append((x + rng.uniform(-0.1, 0.1), y + rng.uniform(-0.1, 0.1)))

    cells = []
    for i, c in enumerate(adata["cells"]):
        cc = dict(c)
        cc["cluster"] = str(labels[i])
        cc["umap_x"] = coords[i][0]
        cc["umap_y"] = coords[i][1]
        cells.append(cc)

    out = {
        **adata,
        "cells": cells,
        "pcs": pcs,
        "labels": labels,
        "n_clusters": len(set(labels)),
        "palette": palette,
    }
    metrics = {
        "n_clusters": out["n_clusters"],
        "n_pcs": n_pcs,
        "n_neighbors": n_neighbors,
        "resolution": resolution,
        "cluster_sizes": {
            str(k): labels.count(k) for k in sorted(set(labels))
        },
    }
    return out, metrics, coords


def write_png_placeholder(path: Path, title: str, color: str = "#4C78A8") -> None:
    """Write a minimal valid 1x1 PNG, or a tiny PPM-like text fallback marked .png."""
    # Minimal 1x1 PNG (blue-ish) — always valid bytes for hashing/provenance demos
    # PNG header + IHDR + IDAT + IEND for a 1x1 pixel
    import struct
    import zlib

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    # Parse hex color
    color = color.lstrip("#")
    if len(color) != 6:
        color = "4C78A8"
    r, g, b = int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)
    raw = bytes([0, r, g, b])  # filter none + RGB
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    data = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
    # Append title as tEXt-like trailing comment in a custom private chunk is overkill;
    # store sidecar note instead.
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.with_suffix(".png.txt").write_text(title, encoding="utf-8")


def content_fingerprint(obj: Any) -> str:
    blob = json.dumps(obj, sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()
