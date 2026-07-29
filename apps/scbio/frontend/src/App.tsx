import { useCallback, useEffect, useState } from "react";
import {
  Background,
  Controls,
  MiniMap,
  ReactFlow,
  useEdgesState,
  useNodesState,
  type Edge,
  type Node,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { api, type GraphPayload, type Project, type TimelineEvent } from "./api";
import { AiEditPanel } from "./components/AiEditPanel";
import { NodeDetail } from "./components/NodeDetail";
import { Timeline } from "./components/Timeline";

const STATUS_COLOR: Record<string, string> = {
  clean: "#5ecf9a",
  dirty: "#d19a66",
  running: "#61afef",
  failed: "#e06c75",
  pending: "#8b9aab",
  stale: "#c678dd",
};

function toFlow(graph: GraphPayload): { nodes: Node[]; edges: Edge[] } {
  const steps = graph.nodes.filter((n) => n.kind === "step");
  const arts = graph.nodes.filter((n) => n.kind === "artifact");
  const nodes: Node[] = [];
  steps.forEach((n, i) => {
    const status = String(n.status || "pending");
    nodes.push({
      id: String(n.id),
      position: { x: 80 + i * 260, y: 80 },
      data: { label: `${n.label || n.id}\n[${status}]` },
      style: {
        border: `2px solid ${STATUS_COLOR[status] || "#8b9aab"}`,
        background: "#1a222c",
        color: "#e7eef6",
        borderRadius: 8,
        padding: 10,
        width: 160,
        whiteSpace: "pre-wrap",
        fontFamily: "IBM Plex Mono, monospace",
        fontSize: 12,
      },
    });
  });
  arts.forEach((n, i) => {
    const status = String(n.status || "pending");
    const meta = (n.meta || {}) as Record<string, unknown>;
    const kind = String(meta.kind || "artifact");
    nodes.push({
      id: String(n.id),
      position: { x: 40 + (i % 5) * 200, y: 280 + Math.floor(i / 5) * 100 },
      data: { label: `${kind}: ${n.label || n.id}` },
      style: {
        border: `1px dashed ${STATUS_COLOR[status] || "#8b9aab"}`,
        background: "#151c24",
        color: "#e7eef6",
        borderRadius: 6,
        padding: 8,
        width: 150,
        fontSize: 11,
      },
    });
  });
  const edges: Edge[] = graph.edges.map((e) => ({
    id: String(e.id),
    source: String(e.src_id),
    target: String(e.dst_id),
    label: String(e.kind || ""),
    style: { stroke: "#2a3542" },
    labelStyle: { fill: "#8b9aab", fontSize: 10 },
  }));
  return { nodes, edges };
}

export default function App() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState<string>("");
  const [graph, setGraph] = useState<GraphPayload | null>(null);
  const [events, setEvents] = useState<TimelineEvent[]>([]);
  const [asOf, setAsOf] = useState<number | undefined>(undefined);
  const [selected, setSelected] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [hermes, setHermes] = useState<string>("");
  const [nodes, setNodes, onNodesChange] = useNodesState<Node>([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([]);

  const refreshList = useCallback(async () => {
    const r = await api.listProjects();
    setProjects(r.projects);
  }, []);

  const refreshGraph = useCallback(async (id: string, eventId?: number) => {
    const g = await api.graph(id, eventId);
    setGraph(g);
    const t = await api.timeline(id);
    setEvents(t.events);
  }, []);

  useEffect(() => {
    void (async () => {
      try {
        const h = await api.health();
        setHermes(h.hermes.ok ? `Hermes ok — ${h.hermes.detail}` : `Hermes: ${h.hermes.detail}`);
        await refreshList();
      } catch (e) {
        setErr(String(e));
      }
    })();
  }, [refreshList]);

  useEffect(() => {
    if (!projectId) return;
    void refreshGraph(projectId, asOf).catch((e) => setErr(String(e)));
  }, [projectId, asOf, refreshGraph]);

  // Full replace of the React Flow graph on every snapshot — scrubbing
  // backward must *delete* nodes that did not exist at time T (no leftovers).
  useEffect(() => {
    if (!graph) {
      setNodes([]);
      setEdges([]);
      return;
    }
    const flow = toFlow(graph);
    setNodes(flow.nodes);
    setEdges(flow.edges);
    setSelected((prev) =>
      prev && flow.nodes.some((n) => n.id === prev) ? prev : null,
    );
  }, [graph, setNodes, setEdges]);

  async function createAndPlan() {
    setBusy(true);
    setErr(null);
    try {
      const p = await api.createProject(`demo-${Date.now().toString(36)}`);
      setProjectId(p.id);
      await api.plan(p.id, "thin-slice qc→normalize→cluster");
      setAsOf(undefined);
      await refreshList();
      await refreshGraph(p.id);
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  }

  async function runPipeline() {
    if (!projectId) return;
    setBusy(true);
    setErr(null);
    try {
      await api.runAll(projectId);
      setAsOf(undefined);
      await refreshGraph(projectId);
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  }

  const frameKey = `frame-${asOf ?? "live"}-${graph?.as_of_event_id ?? 0}-${nodes.length}`;
  const historical = asOf != null;

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          sc<span>bio</span> provenance
        </div>
        <select
          value={projectId}
          onChange={(e) => {
            setProjectId(e.target.value);
            setAsOf(undefined);
            setSelected(null);
          }}
        >
          <option value="">Select project…</option>
          {projects.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name || p.id}
            </option>
          ))}
        </select>
        <button className="primary" disabled={busy} onClick={() => void createAndPlan()}>
          New + Plan
        </button>
        <button disabled={busy || !projectId} onClick={() => void runPipeline()}>
          Run all stages
        </button>
        <span className="muted mono">{hermes}</span>
        {historical && (
          <span className="status-pill status-running mono">
            snapshot @ event #{asOf} · {nodes.length} nodes
          </span>
        )}
        {err && <span style={{ color: "var(--danger)" }}>{err}</span>}
      </header>

      <div className="main">
        <div className="graph-wrap">
          <ReactFlow
            key={frameKey}
            nodes={nodes}
            edges={edges}
            onNodesChange={onNodesChange}
            onEdgesChange={onEdgesChange}
            fitView
            onNodeClick={(_, n) => setSelected(n.id)}
            proOptions={{ hideAttribution: true }}
          >
            <Background gap={18} color="#2a3542" />
            <Controls />
            <MiniMap
              nodeColor={(n) => {
                const m = String(n.data?.label || "").match(/\[(\w+)\]/);
                return STATUS_COLOR[m?.[1] || "pending"] || "#8b9aab";
              }}
              maskColor="rgba(15,20,25,0.7)"
            />
          </ReactFlow>
        </div>
        <aside className="side">
          <h2>Node</h2>
          <div className="panel-body">
            <NodeDetail
              projectId={projectId}
              nodeId={selected}
              onChanged={() => {
                setAsOf(undefined);
                void refreshGraph(projectId);
              }}
            />
            <AiEditPanel
              projectId={projectId}
              logicalId={
                selected?.startsWith("art:") || selected?.startsWith("pipeline:")
                  ? selected
                  : null
              }
              onDone={() => {
                setAsOf(undefined);
                void refreshGraph(projectId);
              }}
            />
          </div>
        </aside>
      </div>

      <Timeline
        events={events}
        asOf={asOf}
        onChange={(id) => setAsOf(id)}
        onLive={() => setAsOf(undefined)}
      />
    </div>
  );
}
