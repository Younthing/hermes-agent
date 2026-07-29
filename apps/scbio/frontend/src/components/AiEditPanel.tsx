import { useState } from "react";
import { api } from "../api";

interface Props {
  projectId: string;
  logicalId: string | null;
  onDone: () => void;
}

export function AiEditPanel({ projectId, logicalId, onDone }: Props) {
  const [instruction, setInstruction] = useState("Soften wording; keep facts.");
  const [scope, setScope] = useState("local");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");

  async function run() {
    if (!projectId || !logicalId) return;
    setBusy(true);
    setMsg("");
    try {
      const r = (await api.aiEdit(projectId, {
        logical_id: logicalId,
        instruction,
        scope,
        impact_tier: "T0",
      })) as { tier?: string; preview?: string };
      setMsg(`tier=${r.tier} preview=${(r.preview || "").slice(0, 120)}`);
      onDone();
    } catch (e) {
      setMsg(String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card">
      <h3>Scoped AI edit</h3>
      <div className="muted" style={{ marginBottom: 6 }}>
        Target: {logicalId || "(select doc/artifact node)"}
      </div>
      <div className="row" style={{ marginBottom: 6 }}>
        <label className="muted">scope</label>
        <select value={scope} onChange={(e) => setScope(e.target.value)}>
          <option value="local">local</option>
          <option value="global">global</option>
          <option value="agent">agent</option>
        </select>
      </div>
      <textarea value={instruction} onChange={(e) => setInstruction(e.target.value)} />
      <div className="row" style={{ marginTop: 8 }}>
        <button
          className="primary"
          disabled={busy || !projectId || !logicalId}
          onClick={() => void run()}
        >
          Apply AI edit
        </button>
      </div>
      {msg && <div className="mono muted">{msg}</div>}
    </div>
  );
}
