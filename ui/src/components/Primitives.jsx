import { AlertTriangle, Check, ChevronRight, CircleDashed, LoaderCircle } from "lucide-react";

export function Badge({ children, tone = "neutral" }) {
  return <span className={`badge badge-${tone}`}>{children}</span>;
}

export function Status({ value }) {
  const normalized = (value || "unknown").toLowerCase();
  // The connector page states its own words ("searchable", "not indexed", "never
  // synced"): a connection's state is a sentence about where it is in setup, not a
  // database enum, and it still has to pick up the right tone here.
  const good = ["active", "completed", "ok", "configured", "success", "searchable"].includes(normalized);
  const busy = ["running", "queued", "syncing", "indexing"].includes(normalized);
  const bad = ["error", "failed", "quarantined", "denied", "unreachable", "sync failed"].includes(normalized);
  const Icon = good ? Check : busy ? LoaderCircle : bad ? AlertTriangle : CircleDashed;
  return <span className={`status status-${good ? "good" : busy ? "busy" : bad ? "bad" : "neutral"}`}><Icon size={13} />{value || "unknown"}</span>;
}

export function EmptyState({ title, copy, action }) {
  return (
    <div className="empty-state">
      <div className="empty-glyph"><CircleDashed size={24} /></div>
      <strong>{title}</strong>
      {copy && <p>{copy}</p>}
      {action}
    </div>
  );
}

export function SectionHeading({ eyebrow, title, copy, action }) {
  return (
    <div className="section-heading">
      <div>
        {eyebrow && <span className="eyebrow">{eyebrow}</span>}
        <h2>{title}</h2>
        {copy && <p>{copy}</p>}
      </div>
      {action && <div className="section-action">{action}</div>}
    </div>
  );
}

export function Metric({ label, value, note, accent = false }) {
  return (
    <div className={`metric ${accent ? "metric-accent" : ""}`}>
      <span>{label}</span>
      <strong>{value}</strong>
      {note && <small>{note}</small>}
    </div>
  );
}

export function RowLink({ children }) {
  return <button className="row-link">{children}<ChevronRight size={15} /></button>;
}
