import { useEffect, useMemo, useState } from "react";
import { AlertTriangle, ArrowRight, Check, CircleDashed, ExternalLink, FileWarning, LoaderCircle, RefreshCw, RotateCw, Undo2, Workflow } from "lucide-react";
import { api } from "../api";
import { useApi, useExpertMode } from "../hooks";
import { SectionHeading, Status } from "../components/Primitives";

// PIPELINE_STAGE_ORDER (taxonomies.py), in execution order: seven stages. gen_evals is
// still a config key and a drain handler, but it was pulled out of insertion into the
// partner-approved environment builder, so it is neither listed nor configurable here.
const STAGES = [
  { id: "fetch", name: "Fetch", gloss: "The object and the permissions the source reports", runs: "Connector" },
  { id: "convert", name: "Parse", gloss: "Text, layout, tables, tracked changes", runs: "Docling Serve" },
  { id: "classify_matter", name: "Classify", gloss: "The matter each file belongs to", runs: "Model gateway", modelFrom: "stage" },
  { id: "relate", name: "Relate", gloss: "Documents and version chains", runs: "Model gateway", modelFrom: "stage" },
  { id: "extract_metadata", name: "Extract metadata", gloss: "Type, parties, dates, language, title", runs: "Model gateway", modelFrom: "stage" },
  { id: "extract_decisions", name: "Extract decisions", gloss: "What changed between versions", runs: "Model gateway", modelFrom: "stage" },
  { id: "index", name: "Index", gloss: "Chunks, vectors, permissions on every chunk", runs: "OpenSearch", modelFrom: "embedding" }
];

// Each model-calling stage carries its own gateway model assignment; the index stage
// embeds with the appliance-wide embedding model from retrieval config.
const stageModelOf = (step, draft) => {
  if (step.modelFrom === "stage") return draft?.pipeline?.stages?.[step.id]?.model || null;
  if (step.modelFrom === "embedding") return draft?.retrieval?.embedding_model || null;
  return null;
};

// Bar and legend order. `waiting` and `disabled` are the two buckets
// taxonomies.stage_bucket() splits out of `skipped`: blocked on the stage before it, and
// parked because the stage is switched off. Conflating either with a handler's skip reads
// as "the pipeline looked at every file and passed", which is the opposite of the truth —
// and it is what made the enabled toggle look like it did nothing.
const BUCKETS = [
  { id: "done", label: "done" },
  { id: "running", label: "running" },
  { id: "pending", label: "queued" },
  { id: "failed", label: "awaiting retry" },
  { id: "waiting", label: "waiting on the stage before" },
  { id: "disabled", label: "skipped — stage is off" },
  { id: "skipped", label: "skipped" },
  { id: "quarantined", label: "quarantined" }
];

export default function PipelinePage({ identity, navigate }) {
  const admin = Boolean(identity?.is_admin);
  const config = useApi("/api/config", [], admin);
  const status = useApi("/api/status");
  const runs = useApi("/api/runs", [], admin);
  const quarantine = useApi("/api/quarantine", [], admin);
  // Realized spend per stage, so re-running one can be priced in the firm's own numbers
  // instead of a warning nobody can act on.
  const costs = useApi("/api/costs", [], admin);
  // A stage stores the gateway-served name it calls, which may be an alias. The
  // catalogue is what turns that back into the model that will actually run and be
  // billed. Fetched once, outside the run poll below: an unreachable gateway costs
  // the resolution, never the card.
  const modelCatalog = useApi("/api/models/catalog", [], admin);
  const [expert] = useExpertMode();
  const [draft, setDraft] = useState(null);
  const [busy, setBusy] = useState("");
  const [rerun, setRerun] = useState(null);
  const [note, setNote] = useState(null);
  useEffect(() => { if (config.data) setDraft(structuredClone(config.data)); }, [config.data]);

  const activeRuns = status.data?.runs || [];
  const reloadAll = () => Promise.all([status.reload(), admin ? runs.reload() : null, admin ? quarantine.reload() : null].filter(Boolean).map((task) => task.catch(() => {})));
  // Hatchet returns as soon as the batch is submitted and the workers advance it, so the
  // page has to poll to stay truthful. Only while something is actually in flight.
  useEffect(() => {
    if (!activeRuns.length) return undefined;
    const timer = setInterval(() => { status.reload().catch(() => {}); if (admin) runs.reload().catch(() => {}); }, 5000);
    return () => clearInterval(timer);
  }, [activeRuns.length, admin, status.reload, runs.reload]);

  const work = useMemo(() => summarizeWork(status.data?.pipeline), [status.data]);
  const changes = useMemo(() => describeChanges(config.data, draft, work), [config.data, draft, work]);
  const costByStage = useMemo(() => Object.fromEntries((costs.data?.by_stage || []).map((row) => [row.stage, row])), [costs.data]);
  const upstreamForAlias = useMemo(() => Object.fromEntries((modelCatalog.data?.entries || []).map((entry) => [entry.id, entry.upstream_model])), [modelCatalog.data]);
  const processingRuns = (runs.data || []).filter((row) => {
    const workflow = String(row.workflow || "");
    return workflow.includes("insertion") || workflow === "access-refresh";
  });
  const parked = quarantine.data || [];
  const provider = draft?.components?.orchestrator_provider || config.data?.components?.orchestrator_provider;

  const apply = async (payload, { run }) => {
    setBusy("save");
    setNote(null);
    try {
      await api("/api/config", { method: "PUT", body: JSON.stringify(payload) });
      // The only trigger left on this page, and it is not a free-standing "start a run":
      // a bumped stage version or a re-enabled stage only marks rows pending, and nothing
      // picks them up until a run begins — requeue_outdated_stages and
      // requeue_newly_enabled_stages are called at the start of a run (runner.py:126,
      // hatchet.py:241), and a sync that found nothing new never hands off (sync/runs.py
      // run_handoff). Saving such a change without starting a run strands the work with
      // nothing on screen saying so.
      if (run) await api("/api/actions/pipeline", { method: "POST" });
      await config.reload();
      await reloadAll();
      costs.reload().catch(() => {});
      setNote({ tone: "ok", text: run ? "Saved. Re-running now." : "Saved." });
    } catch (error) {
      setNote({ tone: "bad", text: error.message });
    } finally {
      setBusy("");
    }
  };

  // Quarantine is otherwise terminal — no version bump reclaims it — so this is the only
  // way back for a file that failed against a service that was down at the time.
  const retryParked = async (item) => {
    setBusy(`retry:${item.source_object_id}:${item.stage}`);
    setNote(null);
    try {
      const result = await api(`/api/quarantine/${encodeURIComponent(item.source_object_id)}/retry?stage=${encodeURIComponent(item.stage)}`, { method: "POST" });
      await reloadAll();
      const after = result.invalidated_stages || [];
      setNote({ tone: "ok", text: `${stageName(result.stage)} queued again for ${item.path}${after.length ? ` — ${after.length} later stage${after.length === 1 ? "" : "s"} will re-run too` : ""}. Running now.` });
    } catch (error) {
      setNote({ tone: "bad", text: error.message });
    } finally {
      setBusy("");
    }
  };

  const updateStage = (id, key, value) => setDraft((current) => ({ ...current, pipeline: { ...current.pipeline, stages: { ...current.pipeline.stages, [id]: { ...current.pipeline.stages[id], [key]: value } } } }));
  const updatePipeline = (key, value) => setDraft((current) => ({ ...current, pipeline: { ...current.pipeline, [key]: value } }));
  const save = () => apply(draft, { run: changes.some((change) => change.run) });
  const discard = () => { setDraft(structuredClone(config.data)); setNote(null); };
  const confirmRerun = (id, version) => {
    const stage = draft.pipeline.stages[id];
    setRerun(null);
    // The token, not producer_version: the code owns the version and recomputes it on
    // every load, so a version written from here would be discarded and the re-run would
    // quietly do nothing.
    return apply({ ...draft, pipeline: { ...draft.pipeline, stages: { ...draft.pipeline.stages, [id]: { ...stage, rerun_token: version } } } }, { run: true });
  };

  return (
    <>
      <div className="hero-row compact-hero">
        <div><h1>Insertion pipeline</h1></div>
        <div className="hero-actions"><button className="secondary-button" onClick={reloadAll}><RefreshCw size={15} /> Refresh</button></div>
      </div>

      <RunState work={work} activeRuns={activeRuns} files={status.data?.counts?.source_objects || 0} lastRun={processingRuns.find((row) => row.finished_at)} />

      {note && <div className={`ins-note ${note.tone}`}>{note.tone === "bad" ? <AlertTriangle size={14} /> : <Check size={14} />}<span>{note.text}</span></div>}

      {changes.length > 0 && <div className="ins-changebar">
        <div>
          <strong>{changes.length === 1 ? "1 unsaved change" : `${changes.length} unsaved changes`}</strong>
          <ul>{changes.map((change) => <li key={change.id}>{change.text}</li>)}</ul>
        </div>
        <div className="ins-changebar-actions">
          <button className="text-button" onClick={discard} disabled={Boolean(busy)}><Undo2 size={13} /> Discard</button>
          <button className="primary-button" onClick={save} disabled={Boolean(busy)}>{busy ? "Saving…" : changes.some((change) => change.run) ? "Save and re-run" : "Save"}</button>
        </div>
      </div>}

      <div className="pipeline-layout">
        <section className="panel">
          <SectionHeading title="Stages" action={<button className="row-link" onClick={() => navigate("models")}>Models &amp; services <ArrowRight size={14} /></button>} />
          <div className="ins-stages">
            {STAGES.map((step, index) => <StageCard
              key={step.id}
              step={step}
              index={index}
              stage={draft?.pipeline?.stages?.[step.id]}
              saved={config.data?.pipeline?.stages?.[step.id]}
              model={stageModelOf(step, draft)}
              upstream={upstreamForAlias[stageModelOf(step, draft)]}
              totals={work.byStage[step.id]}
              admin={admin}
              locked={changes.length > 0 || Boolean(busy)}
              onToggle={(value) => updateStage(step.id, "enabled", value)}
              onAttempts={(value) => updateStage(step.id, "max_attempts", value)}
              onRerun={() => setRerun(step.id)}
            />)}
          </div>
        </section>

        <aside className="pipeline-sidebar">
          <section className="panel compact-panel ins-settings">
            <SectionHeading title="Runs" />
            <label className="ins-check"><input type="checkbox" checked={draft?.pipeline?.auto_insert_after_sync !== false} disabled={!admin || !draft} onChange={(event) => updatePipeline("auto_insert_after_sync", event.target.checked)} /><span>Start after every sync that brought something new</span></label>
            <p className="ins-provider">Executed by {provider === "hatchet" ? "Hatchet workers" : "the in-process runner"}.{expert && provider === "hatchet" && draft?.components?.orchestrator_ui_url && <a href={draft.components.orchestrator_ui_url} target="_blank" rel="noreferrer">Run detail <ExternalLink size={11} /></a>}</p>
            {draft && <>
              <label>Max file size (MB)<input type="number" min="1" value={draft.pipeline.max_file_mb} disabled={!admin} onChange={(event) => updatePipeline("max_file_mb", Number(event.target.value))} /></label>
              <label>Claim timeout (seconds)<input type="number" min="10" value={draft.pipeline.claim_timeout_seconds} disabled={!admin} onChange={(event) => updatePipeline("claim_timeout_seconds", Number(event.target.value))} /><small>A stage held longer than this is retried.</small></label>
              <label>Retry backoff (seconds)<input type="number" min="1" value={draft.pipeline.retry_base_seconds} disabled={!admin} onChange={(event) => updatePipeline("retry_base_seconds", Number(event.target.value))} /></label>
            </>}
          </section>
        </aside>
      </div>

      {admin && <section className="panel run-history">
        <SectionHeading title="Recent processing runs" action={<span className="table-count">{processingRuns.length} run(s)</span>} />
        {processingRuns.length ? <div className="data-table run-ledger">
          <div className="table-head ins-run-head"><span>Run</span><span>Started</span><span>Duration</span><span>Result</span><span>Status</span></div>
          {processingRuns.map((item) => <div className="table-row ins-run-head" key={item.id}>
            <span className="primary-cell"><i className="run-icon"><Workflow size={15} /></i><span><strong>{item.workflow === "access-refresh" ? "Access refresh" : "Insertion"}</strong><small className="mono">{String(item.id || "").slice(0, 8)}</small></span></span>
            <span className="plain-cell">{item.started_at ? new Date(item.started_at).toLocaleString() : "—"}</span>
            <span className="plain-cell mono subtle">{duration(item.started_at, item.finished_at)}</span>
            <RunResultCell run={item} />
            <span><Status value={item.status} /></span>
          </div>)}
        </div> : <div className="quiet-row"><CircleDashed size={16} /> No processing run yet.</div>}
      </section>}

      {admin && parked.length > 0 && <section className="panel run-history">
        <SectionHeading title="Quarantined files" copy="Parked after the last attempt failed. Nothing retries them on its own." action={<span className="table-count">{parked.length} parked</span>} />
        <div className="data-table run-ledger">
          <div className="table-head quarantine-head"><span>File</span><span>Stage</span><span>Error</span><span>Attempts</span><span>Status</span><span /></div>
          {parked.map((item) => <div className="table-row quarantine-head" key={`${item.source_object_id}-${item.stage}`}>
            <span className="primary-cell"><i className="run-icon"><FileWarning size={15} /></i><span><strong>{item.path}</strong></span></span>
            <span className="mono subtle plain-cell">{stageName(item.stage)}</span>
            <span className="ledger-fail"><strong>{item.error?.class || item.error?.reason || "—"}</strong><small>{item.error?.message || ""}</small></span>
            <span className="mono plain-cell">{item.attempts}</span>
            <span><Status value="quarantined" /></span>
            <span className="plain-cell">
              <button
                className="secondary-button small"
                disabled={Boolean(busy)}
                // A deterministic failure — an unreadable file, an oversized blob — will
                // park again on the first attempt. Say so before it is clicked.
                title={item.error?.deterministic ? "This failure was deterministic: it will quarantine again unless the file itself changed." : ""}
                onClick={() => retryParked(item)}
              >{busy === `retry:${item.source_object_id}:${item.stage}` ? "Retrying…" : <><RotateCw size={12} /> Retry</>}</button>
            </span>
          </div>)}
        </div>
      </section>}

      {rerun && <RerunModal
        step={STAGES.find((step) => step.id === rerun)}
        stage={draft.pipeline.stages[rerun]}
        files={rerunnable(work.byStage[rerun])}
        downstream={STAGES.slice(STAGES.findIndex((step) => step.id === rerun) + 1).map((step) => step.name)}
        cost={costByStage[rerun]}
        busy={Boolean(busy)}
        onClose={() => setRerun(null)}
        onConfirm={(version) => confirmRerun(rerun, version)}
      />}
    </>
  );
}

function RunState({ work, activeRuns, files, lastRun }) {
  const running = activeRuns[0];
  const tone = running ? "is-running" : work.busy || work.quarantined || !files ? "is-idle" : "is-clear";
  const headline = running
    ? `Running · ${running.current_step || "starting"}`
    : work.busy
      ? `Idle · ${work.busy.toLocaleString()} stage steps outstanding`
      : !files
        ? "Nothing has entered the pipeline"
        : `Idle · ${files.toLocaleString()} files through all ${STAGES.length} stages`;
  const detail = running
    ? `${activeRuns.length > 1 ? `${activeRuns.length} runs in flight. ` : ""}Started ${relative(running.started_at)}.`
    : work.quarantined
      ? `${work.quarantined.toLocaleString()} quarantined.`
      : !files
        ? "Connect a source and sync it."
        : lastRun ? `Last run ${relative(lastRun.finished_at)}.` : "";
  return <div className={`pipeline-state ${tone}`}>
    <span className="pipeline-state-mark">{running ? <LoaderCircle size={16} /> : tone === "is-clear" ? <Check size={16} /> : <CircleDashed size={16} />}</span>
    <div className="pipeline-state-text"><strong>{headline}</strong>{detail && <span>{detail}</span>}</div>
    {running && <div className="pipeline-state-meter"><div className="large-progress"><i style={{ width: `${Math.round((running.progress || 0) * 100)}%` }} /></div><small>{Math.round((running.progress || 0) * 100)}%</small></div>}
  </div>;
}

function StageCard({ step, index, stage, saved, model, upstream, totals, admin, locked, onToggle, onAttempts, onRerun }) {
  const on = stage ? stage.enabled !== false : saved?.enabled !== false;
  const unsaved = Boolean(stage && saved && (stage.enabled !== false) !== (saved.enabled !== false));
  const files = rerunnable(totals);
  return <article className={`ins-stage ${on ? "" : "is-off"}`}>
    <div className="ins-stage-head">
      <span className="ins-step">{String(index + 1).padStart(2, "0")}</span>
      <div className="ins-stage-id"><strong>{step.name}</strong><span>{step.gloss}</span></div>
      {/* The chip is the model that will run and be billed; the alias trails it because
          that is the value saved in config and the one to change when this stage should
          call something else. The chip falls back to the bare alias whenever the catalogue
          cannot resolve it — the gateway was unreachable, or the alias is one it no longer
          serves — so the card never invents a model. The alias turns the container's
          uppercasing back off, as .ins-stage-runs b already does for the model: both are
          literals an operator has to match in config, and JUDGE-DEFAULT is not that string. */}
      <span className="ins-stage-runs">{step.runs}{model && <b className="mono">{upstream || model}</b>}{model && upstream && <span style={{ textTransform: "none", letterSpacing: 0 }}>via {model}</span>}</span>
      <Status value={stageStatus(on, totals)} />
    </div>

    <StageWork totals={totals} />

    {stage && <div className="ins-stage-controls">
      <div className="ins-control">
        <button type="button" role="switch" aria-checked={on} className={`ins-switch ${on ? "on" : ""}`} disabled={!admin} onClick={() => onToggle(!on)}><i /></button>
        <span className="ins-switch-text">{on ? "Runs on every file" : offCopy(totals)}{unsaved && <em>unsaved</em>}</span>
      </div>
      <div className="ins-control ins-version">
        <span className="ins-control-label">Version</span>
        <code>{stage.producer_version || "—"}</code>
        <button
          className="secondary-button small"
          disabled={!admin || locked || !files}
          title={locked ? "Save your changes first" : files ? "" : "No file has completed this stage yet"}
          onClick={onRerun}
        ><RotateCw size={12} /> Re-run all files</button>
      </div>
      <label className="ins-attempts">Attempts before quarantine<input type="number" min="1" max="20" value={stage.max_attempts} disabled={!admin} onChange={(event) => onAttempts(Number(event.target.value))} /></label>
    </div>}
  </article>;
}

function StageWork({ totals }) {
  if (!totals?.total) return <div className="ins-work"><span className="ins-work-empty">No file has reached this stage.</span></div>;
  const parts = BUCKETS.filter((bucket) => totals[bucket.id] > 0);
  return <div className="ins-work">
    <div className="ins-bar">{parts.map((bucket) => <i key={bucket.id} className={`b-${bucket.id}`} style={{ width: `${(totals[bucket.id] / totals.total) * 100}%` }} />)}</div>
    <div className="ins-legend">{parts.map((bucket) => <span key={bucket.id} className={`b-${bucket.id}`}><i /><b>{totals[bucket.id].toLocaleString()}</b> {bucket.label}</span>)}</div>
  </div>;
}

/** The producer-version lever, stated as what it does rather than what it is called. */
function RerunModal({ step, stage, files, downstream, cost, busy, onClose, onConfirm }) {
  const [version, setVersion] = useState(bumpVersion(stage.rerun_token || "0"));
  const submit = (event) => { event.preventDefault(); onConfirm(version.trim()); };
  return <div className="modal-backdrop" onMouseDown={onClose}>
    <form className="form-modal" onSubmit={submit} onMouseDown={(event) => event.stopPropagation()}>
      <div><span className="eyebrow">Stage version</span><h2>Re-run {step.name}</h2></div>
      <ul className="ins-consequence">
        <li><b>{files.toLocaleString()}</b> files run {step.name} again.</li>
        {downstream.length > 0 && <li>The {downstream.length} stages after it run again too: {downstream.join(", ")}.</li>}
        {cost ? <li>{step.name} has cost <b>${Number(cost.cost_usd).toFixed(2)}</b> in model usage so far. This charges it again.</li> : null}
      </ul>
      <label>New version<input className="mono" required autoFocus value={version} onChange={(event) => setVersion(event.target.value)} /><small>A file whose last run used a different version runs again.</small></label>
      <div className="modal-actions">
        <button type="button" className="text-button" onClick={onClose}>Cancel</button>
        <button className="primary-button" disabled={busy || !version.trim() || version.trim() === stage.producer_version}>{busy ? "Starting…" : `Re-run ${files.toLocaleString()} files`}</button>
      </div>
    </form>
  </div>;
}

// Run counters are free-form JSON and differ per executor: the local runner stores
// PipelineRun's scalar fields (processed/done/skipped/retried/quarantined), while the
// Hatchet batch stores objects_total/objects_completed, a nested {stage: {status: count}}
// map, and object_ids — the full id list of every file in the batch. Only scalars are
// allowed into the cell: rendering a nested map gives "[object Object]" and rendering an id
// list stretches one row past every other column. Anything else is reduced to a size,
// behind a disclosure.
const COUNTER_LABELS = { objects_total: "files", objects_completed: "files done", processed: "processed", done: "done", skipped: "skipped", retried: "retried", quarantined: "quarantined" };
const COUNTER_ORDER = ["objects_completed", "objects_total", "processed", "done", "quarantined", "retried", "skipped"];
const COUNTER_LIMIT = 3;

function RunResultCell({ run }) {
  if (run.error) return <span className="ledger-fail"><strong>{run.error.class || "failed"}</strong><small title={run.error.message || ""}>{run.error.message || "no message recorded"}</small></span>;
  const chips = counterChips(run.counters);
  const stages = stageRows(run.counters);
  const extras = detailRows(run.counters);
  if (!chips.length && !stages.length && !extras.length) return <span className="subtle">—</span>;
  return <span className="counter-cell">
    {chips.length > 0 && <span className="counter-chips">{chips.map((chip) => <span key={chip.id}><b>{chip.value}</b>{chip.label}</span>)}</span>}
    {(stages.length > 0 || extras.length > 0) && <details className="counter-detail"><summary>{stages.length ? `per stage (${stages.length})` : "detail"}</summary><div className="counter-detail-body">{stages.map((row) => <span key={row.stage}><b>{row.stage}</b>{row.text}</span>)}{extras.map((row) => <span key={row.key}><b>{row.key}</b>{row.text}</span>)}</div></details>}
  </span>;
}

function counterChips(counters) {
  if (!isPlainObject(counters)) return [];
  const chips = [];
  const complete = counters.objects_completed;
  const total = counters.objects_total;
  const paired = Number.isFinite(complete) && Number.isFinite(total);
  if (paired) chips.push({ id: "objects", value: `${complete}/${total}`, label: "files done" });
  const parked = quarantinedInStages(counters.stages);
  if (parked) chips.push({ id: "stage-quarantined", value: parked.toLocaleString(), label: "quarantined" });
  const ordered = [...COUNTER_ORDER.filter((key) => key in counters), ...Object.keys(counters).filter((key) => !COUNTER_ORDER.includes(key))];
  for (const key of ordered) {
    if (chips.length >= COUNTER_LIMIT) break;
    if (paired && (key === "objects_total" || key === "objects_completed")) continue;
    const value = scalarCounter(counters[key]);
    if (value === null) continue;
    if (value === "0" && chips.length) continue; // zeros are noise once a real figure is on the row
    chips.push({ id: key, value, label: COUNTER_LABELS[key] || key.split("_").join(" ") });
  }
  return chips;
}

function scalarCounter(value) {
  if (typeof value === "number") return Number.isFinite(value) ? value.toLocaleString() : null;
  if (typeof value === "boolean") return value ? "yes" : "no";
  if (typeof value === "string") return value ? (value.length > 16 ? `${value.slice(0, 15)}…` : value) : null;
  return null; // arrays and nested maps never render inline, whatever the backend starts sending
}

// Raw status counts, not a done/total ratio: a run's own counters cannot tell a real
// `skipped` apart from a stage skipped while waiting for its predecessor, so any ratio
// built from them would overstate progress. Show what the orchestrator recorded.
function stageRows(counters) {
  const stages = isPlainObject(counters) ? counters.stages : null;
  if (!isPlainObject(stages)) return [];
  return Object.entries(stages).slice(0, 12).map(([stage, states]) => {
    if (!isPlainObject(states)) return { stage: stageName(stage), text: scalarCounter(states) ?? "—" };
    const text = Object.entries(states)
      .filter(([, value]) => typeof value === "number" && value > 0)
      .sort((left, right) => right[1] - left[1])
      .slice(0, 3)
      .map(([status, value]) => `${value.toLocaleString()} ${status}`)
      .join(" · ");
    return { stage: stageName(stage), text: text || "—" };
  });
}

function quarantinedInStages(stages) {
  if (!isPlainObject(stages)) return 0;
  return Object.values(stages).reduce((sum, states) => sum + (isPlainObject(states) && typeof states.quarantined === "number" ? states.quarantined : 0), 0);
}

function detailRows(counters) {
  if (!isPlainObject(counters)) return [];
  const rows = [];
  for (const [key, value] of Object.entries(counters)) {
    if (key === "stages" && isPlainObject(value)) continue; // rendered as its own breakdown
    if (Array.isArray(value)) rows.push({ key, text: `${value.length.toLocaleString()} ${value.length === 1 ? "entry" : "entries"}` });
    else if (isPlainObject(value)) rows.push({ key, text: `${Object.keys(value).length} ${Object.keys(value).length === 1 ? "key" : "keys"}` });
  }
  return rows.slice(0, 8);
}

function isPlainObject(value) {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function summarizeWork(pipeline) {
  const byStage = {};
  let busy = 0;
  let quarantined = 0;
  for (const step of STAGES) {
    const states = isPlainObject(pipeline?.[step.id]) ? pipeline[step.id] : {};
    const entry = { total: 0 };
    for (const bucket of BUCKETS) entry[bucket.id] = Number(states[bucket.id]) || 0;
    // Any bucket the backend adds later still counts into the total, so a stage can never
    // report a full bar over work this page has no colour for.
    for (const value of Object.values(states)) if (typeof value === "number") entry.total += value;
    byStage[step.id] = entry;
    busy += entry.pending + entry.running + entry.failed;
    quarantined += entry.quarantined;
  }
  return { byStage, busy, quarantined };
}

/** What the switch has actually done, once it has done something. */
function offCopy(totals) {
  const parked = totals?.disabled || 0;
  if (!parked) return "Off — files skip it; later stages run without its output";
  return `Off — ${parked.toLocaleString()} ${parked === 1 ? "file has" : "files have"} skipped it; later stages run without its output`;
}

/** Rows a version bump would actually requeue: runner.py only touches done and skipped.
 *  `waiting` and `disabled` are stored as skipped too, but only `disabled` rows belong to
 *  a stage that already ran its decision; a waiting row has not been reached yet. */
function rerunnable(totals) {
  return (totals?.done || 0) + (totals?.skipped || 0) + (totals?.disabled || 0);
}

function stageStatus(on, totals) {
  if (!on) return "off";
  if (!totals?.total) return "idle";
  if (totals.running) return "running";
  if (totals.pending) return "queued";
  if (totals.quarantined) return "quarantined";
  if (totals.failed) return "retrying";
  if (totals.waiting) return "waiting";
  if (totals.done) return "completed";
  return "skipped";
}

function describeChanges(saved, draft, work) {
  if (!saved || !draft) return [];
  const rows = [];
  for (const step of STAGES) {
    const before = saved.pipeline?.stages?.[step.id];
    const after = draft.pipeline?.stages?.[step.id];
    if (!before || !after) continue;
    if ((before.enabled !== false) !== (after.enabled !== false)) {
      rows.push(after.enabled === false
        ? { id: `${step.id}-off`, text: `${step.name} off — new files will skip it` }
        : { id: `${step.id}-on`, text: `${step.name} on — the files that skipped it are queued again`, run: true });
    }
    if (before.producer_version !== after.producer_version) {
      rows.push({ id: `${step.id}-version`, text: `${step.name} version ${before.producer_version} → ${after.producer_version} — re-runs ${rerunnable(work.byStage[step.id]).toLocaleString()} files and every stage after it`, run: true });
    }
    if (Number(before.max_attempts) !== Number(after.max_attempts)) {
      rows.push({ id: `${step.id}-attempts`, text: `${step.name} attempts ${before.max_attempts} → ${after.max_attempts}` });
    }
  }
  const before = saved.pipeline || {};
  const after = draft.pipeline || {};
  if ((before.auto_insert_after_sync !== false) !== (after.auto_insert_after_sync !== false)) {
    rows.push({ id: "auto", text: after.auto_insert_after_sync === false ? "Runs no longer start after a sync" : "Runs start after every sync that brought something new" });
  }
  for (const [key, label, unit] of [["max_file_mb", "Max file size", " MB"], ["claim_timeout_seconds", "Claim timeout", "s"], ["retry_base_seconds", "Retry backoff", "s"]]) {
    if (Number(before[key]) !== Number(after[key])) rows.push({ id: key, text: `${label} ${before[key]}${unit} → ${after[key]}${unit}` });
  }
  return rows;
}

/** Mirrors _bump_version in web/app.py so the UI and a rebuild produce the same next value. */
function bumpVersion(current) {
  const base = current || "0";
  const cut = base.lastIndexOf("-");
  const head = cut > 0 ? base.slice(0, cut) : "";
  const tail = cut > 0 ? base.slice(cut + 1) : "";
  const numeric = tail.length > 0 && [...tail].every((character) => character >= "0" && character <= "9");
  return head && numeric ? `${head}-${Number(tail) + 1}` : `${base}-r2`;
}

function stageName(id) {
  return STAGES.find((step) => step.id === id)?.name || id;
}

function duration(startedAt, finishedAt) {
  if (!startedAt) return "—";
  const start = new Date(startedAt).getTime();
  const end = finishedAt ? new Date(finishedAt).getTime() : Date.now();
  const seconds = Math.max(0, Math.round((end - start) / 1000));
  if (!Number.isFinite(seconds)) return "—";
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
  return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
}

function relative(timestamp) {
  if (!timestamp) return "recently";
  const seconds = Math.round((Date.now() - new Date(timestamp).getTime()) / 1000);
  if (!Number.isFinite(seconds)) return "recently";
  if (seconds < 60) return "moments ago";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ago`;
  return `${Math.floor(seconds / 86400)} d ago`;
}
