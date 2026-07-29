import type { TimelineEvent } from "../api";

interface Props {
  events: TimelineEvent[];
  asOf?: number;
  onChange: (id: number) => void;
  onLive: () => void;
}

export function Timeline({ events, asOf, onChange, onLive }: Props) {
  if (!events.length) {
    return (
      <div className="timeline">
        <div className="timeline-label">
          <span>Timeline</span>
          <span>no events yet</span>
        </div>
      </div>
    );
  }
  const maxId = events[events.length - 1].id;
  const value = asOf ?? maxId;
  const current = events.find((e) => e.id === value) || events[events.length - 1];

  return (
    <div className="timeline">
      <div className="timeline-label">
        <span>
          Timeline · event #{current.id} · {current.summary}
          {asOf != null ? " · historical snapshot (later nodes hidden)" : " · live"}
        </span>
        <span className="row">
          <button type="button" onClick={onLive} disabled={asOf == null}>
            Live
          </button>
          <span>{current.ts}</span>
        </span>
      </div>
      <input
        type="range"
        min={events[0].id}
        max={maxId}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
      />
    </div>
  );
}
