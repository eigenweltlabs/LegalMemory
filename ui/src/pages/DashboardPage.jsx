import { useMemo, useState } from "react";
import { ArrowRight, Braces, CheckCircle2, FileStack, Layers3, LoaderCircle, Play, Radio, RefreshCw, ShieldCheck, TriangleAlert } from "lucide-react";
import { api } from "../api";
import { useApi } from "../hooks";
import { EmptyState, Metric, SectionHeading, Status } from "../components/Primitives";

const STAGE_LABELS = {
  fetch: "Fetch",
  convert: "Parse",
  classify_matter: "Classify",
  relate: "Relate",
  extract_metadata: "Extract",
  extract_decisions: "Decisions",
  index: "Index"
};

export default function DashboardPage({ navigate, identity }) {
  const status = useApi("/api/status");
  const sources = useApi("/api/sources");
  const runs = useApi("/api/runs?limit=15", [], Boolean(identity?.is_admin));
  const projects = useApi("/api/projects");
  const components = useApi("/api/components", [], Boolean(identity?.is_admin));
  const counts = status.data?.counts || {};
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState("");

  const runPipeline = async () => {
    setBusy(true);
    setFailed("");
    try {
      await api("/api/actions/pipeline", { method: "POST" });
      await status.reload();
    } catch (caught) {
      setFailed(caught.message || "The pipeline could not be started.");
    } finally {
      setBusy(false);
    }
  };

  const refresh = () => { status.reload(); sources.reload(); runs.reload().catch(() => {}); };

  return (
    <>
      <div className="hero-row compact-hero">
        <div><h1>Overview</h1></div>
        <div className="hero-actions">
          <button className="secondary-button" onClick={refresh}><RefreshCw size={15} /> Refresh</button>
          {identity?.is_admin && <button className="primary-button" disabled={busy} onClick={runPipeline}>{busy ? <LoaderCircle size={15} className="spin" /> : <Play size={15} />} Run insertion pipeline</button>}
        </div>
      </div>

      {failed && <div className="form-error">{failed}</div>}

      {/* Never a bare count: a note that says what the number is made of, or why it is
          zero. Every one of these is counted through what the caller can actually
          reach, so none of them may be worded as though a project were involved. */}
      <div className="metric-grid metric-grid-five">
        <Metric label="Documents" value={counts.documents ?? "—"} note={counts.chunks ? `${counts.chunks.toLocaleString()} chunks indexed` : "not indexed yet"} accent />
        <Metric label="Matters" value={counts.matters ?? "—"} note={counts.matters ? "grouped from the documents you can see" : "nothing classified yet"} />
        <Metric label="Connected sources" value={counts.sources ?? "—"} note={!counts.sources ? "none connected" : counts.source_objects ? `${counts.source_objects.toLocaleString()} objects mirrored` : "nothing synced yet"} />
        <Metric label="Projects" value={counts.projects ?? "—"} note={counts.projects ? "visible to you" : "none — access comes from source ACLs"} />
        <Metric label="Quarantined" value={counts.quarantined ?? "—"} note={counts.quarantined ? "parked after retries" : "nothing parked"} />
      </div>

      <Attention status={status} sources={sources} runs={runs} navigate={navigate} />

      <div className="two-column">
        <section className="panel">
          <SectionHeading title="Projects" action={<button className="row-link" onClick={() => navigate("access")}>Manage access <ArrowRight size={14} /></button>} />
          {(projects.data || []).length ? <div className="project-list">{projects.data.slice(0, 6).map((project) => <button key={project.id} onClick={() => navigate("data")}><div className="project-monogram">{project.key.slice(0, 2).toUpperCase()}</div><div><strong>{project.name}</strong><span>{project.key} · {project.documents} documents</span></div><Status value={project.status} /></button>)}</div> : <EmptyState title="No projects" copy="Documents follow their source's permissions until you create one." action={identity?.is_admin && <button className="secondary-button" onClick={() => navigate("access")}>Create project</button>} />}
        </section>
        <section className="panel">
          <SectionHeading title="Services" action={<button className="row-link" onClick={() => navigate("models")}>All services <ArrowRight size={14} /></button>} />
          {identity?.is_admin ? <div className="component-mini-list">{(components.data || []).slice(0, 5).map((component) => <div key={component.role}><div className="component-icon">{component.role.includes("Model") ? <Braces size={16} /> : component.role.includes("Search") ? <Layers3 size={16} /> : <FileStack size={16} />}</div><div><strong>{component.name}</strong><span>{component.role}</span></div><Status value={component.status} /></div>)}</div> : <div className="permission-note"><ShieldCheck size={18} /><div><strong>Administrators only</strong><p>Service endpoints are hidden from project members.</p></div></div>}
        </section>
      </div>

      {/* The console shows what is in the index; the questions are asked from the tool
          the lawyer already works in. That surface is MCP, and it is one click away. */}
      <button className="pointer-row" onClick={() => navigate("external")}>
        <i><Radio size={16} /></i>
        <div><strong>Ask the index from your AI client</strong><span>MCP tools answer with citations, scoped to the caller.</span></div>
        <ArrowRight size={14} />
      </button>
    </>
  );
}

// last_error is orchestrator-written JSON ({class, message, …}) but a plain string
// arrives too. Whatever it is, the row must not read "no reason" when there is one.
function runReason(run) {
  const error = run.error;
  if (!error) return `Stopped at ${run.current_step || "an unrecorded step"}.`;
  if (typeof error === "string") return error.slice(0, 160);
  const message = error.message || JSON.stringify(error);
  return `${error.class ? `${error.class}: ` : ""}${message}`.slice(0, 160);
}

/**
 * The one question a dashboard has to answer: is anything wrong right now.
 *
 * The stage strip it replaces reported "34/34" seven times over on an idle appliance —
 * a full-width restatement of "nothing happened", with the pipeline page one click away
 * for anyone who wanted the detail. What is left is what an operator would act on.
 */
function Attention({ status, sources, runs, navigate }) {
  const inFlight = status.data?.runs || [];
  const counts = status.data?.counts || {};
  const pipeline = status.data?.pipeline || {};
  const rows = sources.data || [];
  const history = runs.data || [];

  const issues = useMemo(() => {
    const list = [];
    for (const [stage, states] of Object.entries(pipeline)) {
      if (states.failed) list.push({ key: `failed:${stage}`, tone: "bad", title: `${states.failed} failed at ${STAGE_LABELS[stage] || stage}`, detail: "Retried and still failing.", page: "pipeline" });
    }
    if (counts.quarantined) list.push({ key: "quarantined", tone: "bad", title: `${counts.quarantined} quarantined`, detail: "Parked after repeated failures. Nothing retries them on its own.", page: "pipeline" });
    if (!inFlight.length) {
      const waiting = Object.values(pipeline).reduce((sum, states) => sum + (states.waiting || 0) + (states.pending || 0), 0);
      if (waiting) list.push({ key: "waiting", tone: "warn", title: `${waiting} documents waiting, nothing running`, detail: "Run the insertion pipeline to move them.", page: "pipeline" });
    }
    for (const source of rows) {
      if (["error", "failed", "unreachable", "sync failed"].includes((source.status || "").toLowerCase())) list.push({ key: `source:${source.id}`, tone: "bad", title: `${source.display_name} is not syncing`, detail: `Connection status: ${source.status}.`, page: "connectors", focus: { source: source.id } });
      else if (!source.last_sync_at) list.push({ key: `never:${source.id}`, tone: "warn", title: `${source.display_name} has never synced`, detail: "Nothing from this connection is searchable yet.", page: "connectors", focus: { source: source.id } });
      if (source.pending_deletion?.object_count) list.push({ key: `deletion:${source.id}`, tone: "warn", title: `${source.pending_deletion.object_count} objects pending deletion at ${source.display_name}`, detail: "The source reported them gone; they still answer searches until the deletion is confirmed.", page: "connectors", focus: { source: source.id } });
    }
    // A run that died — or that the sweeper gave up on — leaves no failed stage rows
    // behind, so it is the one failure nothing above would report. Bounded to a day:
    // last week's crash is history, not something to act on now.
    const since = Date.now() - 24 * 3600 * 1000;
    for (const run of history) {
      if (run.status !== "failed") continue;
      if (new Date(run.finished_at || run.started_at || 0).getTime() < since) continue;
      list.push({ key: `run:${run.id}`, tone: "bad", title: `${run.workflow} failed`, detail: runReason(run), page: "pipeline" });
    }
    return list;
  }, [pipeline, counts.quarantined, rows, history, inFlight.length]);

  const lastSync = useMemo(() => rows.map((source) => source.last_sync_at).filter(Boolean).sort().at(-1), [rows]);

  return (
    <section className="panel attention-panel">
      <SectionHeading title="Needs attention" action={<button className="row-link" onClick={() => navigate("pipeline")}>Open pipeline <ArrowRight size={14} /></button>} />
      {issues.length ? (
        <div className="attention-list">
          {issues.map((issue) => (
            <button className={`attention-row ${issue.tone}`} key={issue.key} onClick={() => navigate(issue.page, issue.focus)}>
              <TriangleAlert size={15} />
              <div><strong>{issue.title}</strong><span>{issue.detail}</span></div>
              <ArrowRight size={14} />
            </button>
          ))}
        </div>
      ) : (
        <div className="quiet-row"><CheckCircle2 size={16} /> Nothing to act on{lastSync ? `. Last sync ${new Date(lastSync).toLocaleString()}` : ""}{counts.source_objects ? `, ${counts.source_objects.toLocaleString()} objects mirrored` : ""}.</div>
      )}
      {inFlight.length > 0 && (
        <div className="active-runs">
          {inFlight.map((run) => <div className="run-row" key={run.id}><div className="pulse-dot" /><div><strong>{run.workflow}</strong><span>{run.current_step || "Starting"}</span></div><div className="run-progress"><i style={{ width: `${Math.round((run.progress || 0) * 100)}%` }} /></div><Status value={run.status} /></div>)}
        </div>
      )}
    </section>
  );
}
