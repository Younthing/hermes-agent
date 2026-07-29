import { useEffect, useState } from "react";
import { api } from "../api";

interface Props {
  projectId: string;
  nodeId: string | null;
  onChanged: () => void;
}

export function NodeDetail({ projectId, nodeId, onChanged }: Props) {
  const [content, setContent] = useState<string>("");
  const [meta, setMeta] = useState<string>("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!projectId || !nodeId) {
      setContent("");
      setMeta("Select a node");
      return;
    }
    if (!(nodeId.startsWith("art:") || nodeId.startsWith("pipeline:"))) {
      setContent("");
      setMeta(`Step node: ${nodeId}`);
      return;
    }
    void (async () => {
      try {
        const a = (await api.artifact(projectId, nodeId)) as {
          latest?: Record<string, unknown>;
          versions?: unknown[];
          content?: string;
        };
        setMeta(
          `versions=${a.versions?.length ?? 0} latest=${a.latest?.content_hash ?? "—"}`.slice(0, 120),
        );
        setContent(a.content || "(binary or empty)");
      } catch (e) {
        setMeta(String(e));
        setContent("");
      }
    })();
  }, [projectId, nodeId]);

  async function tweakPalette() {
    if (!projectId) return;
    setBusy(true);
    try {
      await api.patchParams(projectId, "cluster", {
        presentation_params: { umap_palette: "viridis" },
        rerun: true,
      });
      onChanged();
    } finally {
      setBusy(false);
    }
  }

  async function tweakResolution() {
    if (!projectId) return;
    setBusy(true);
    try {
      await api.patchParams(projectId, "cluster", {
        compute_params: { resolution: 1.2 },
        rerun: true,
      });
      onChanged();
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card">
      <h3>{nodeId || "—"}</h3>
      <div className="muted mono">{meta}</div>
      {content && (
        <pre className="mono" style={{ whiteSpace: "pre-wrap", maxHeight: 180, overflow: "auto" }}>
          {content.slice(0, 2500)}
        </pre>
      )}
      <div className="row" style={{ marginTop: 8 }}>
        <button disabled={busy || !projectId} onClick={() => void tweakPalette()}>
          T1: palette
        </button>
        <button disabled={busy || !projectId} onClick={() => void tweakResolution()}>
          T2: resolution
        </button>
      </div>
    </div>
  );
}
