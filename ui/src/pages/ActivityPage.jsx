import { Activity, AlertTriangle, ExternalLink, KeyRound, RefreshCw, Search, ShieldCheck, Timer, Workflow } from "lucide-react";
import { useApi, useExpertMode } from "../hooks";
import { EmptyState, SectionHeading, Status } from "../components/Primitives";

export default function ActivityPage({ identity }) {
  const audit = useApi("/api/audit?limit=150", [], Boolean(identity?.is_admin));
  const components = useApi("/api/components", [], Boolean(identity?.is_admin));
  const [expert] = useExpertMode();
  const traces = components.data?.find((item) => item.role === "Traces");
  const events = audit.data || [];
  const denied = events.filter((item) => item.outcome === "denied").length;
  const errors = events.filter((item) => item.outcome === "error").length;
  const timed = events.filter((item) => Number(item.details?.duration_ms) > 0);
  const average = timed.length ? timed.reduce((sum, item) => sum + Number(item.details.duration_ms), 0) / timed.length : 0;
  if (!identity?.is_admin) return <EmptyState title="Administrators only" copy="The audit ledger is an administrator view." />;
  return (
    <>
      <div className="hero-row compact-hero">
        <div><h1>Activity</h1></div>
        <div className="hero-actions">
          <button className="secondary-button" onClick={() => audit.reload()}><RefreshCw size={15} /> Refresh</button>
          {expert && traces?.ui_url && <a className="primary-button" href={traces.ui_url} target="_blank" rel="noreferrer">Open traces <ExternalLink size={14} /></a>}
        </div>
      </div>
      <div className="metric-grid metric-grid-four">
        <div className="activity-metric"><Activity size={17} /><span>Recorded events</span><strong>{events.length}</strong></div>
        <div className="activity-metric"><ShieldCheck size={17} /><span>Successful</span><strong>{events.filter((item) => item.outcome === "success").length}</strong></div>
        <div className="activity-metric"><AlertTriangle size={17} /><span>Denied / errors</span><strong>{denied} / {errors}</strong></div>
        <div className="activity-metric"><Timer size={17} /><span>Average API time</span><strong>{timed.length ? `${average.toFixed(0)} ms` : "—"}</strong></div>
      </div>
      <section className="panel audit-panel">
        <SectionHeading title="Recent activity" action={<span className="table-count">latest {events.length}</span>} />
        {events.length ? <div className="audit-list">{events.map((event) => <div className="audit-row" key={event.id}>
          <div className={`audit-icon outcome-${event.outcome}`}>{icon(event.action)}</div>
          <div className="audit-main"><div><strong>{humanAction(event.action)}</strong><Status value={event.outcome} /></div><p>{event.principals?.join(" · ") || "Unauthenticated request"}</p><span>{event.target_type ? `${event.target_type}${event.target_id ? ` · ${event.target_id}` : ""}` : "system"}</span></div>
          <div className="audit-side"><strong>{relative(event.created_at)}</strong><span>{event.details?.duration_ms ? `${event.details.duration_ms} ms` : "—"}</span></div>
        </div>)}</div> : <EmptyState title="No activity recorded" copy="API and MCP calls appear here as they happen." />}
      </section>
    </>
  );
}
function icon(action = "") { if (action.includes("search")) return <Search size={15} />; if (action.includes("mcp")) return <Workflow size={15} />; if (action.includes("config")) return <KeyRound size={15} />; return <Activity size={15} />; }
function humanAction(value = "") { return value.replace(/^api\./, "").replace(/^mcp\./, "MCP · ").replaceAll("_", " ").replaceAll(".", " · "); }
function relative(value) { const seconds = Math.floor((Date.now() - new Date(value).getTime()) / 1000); if (seconds < 60) return `${Math.max(0, seconds)}s ago`; if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`; if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`; return new Date(value).toLocaleDateString(); }
