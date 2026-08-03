import { useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle, ArrowRight, ArrowUp, Check, ChevronDown, ChevronRight, ExternalLink, Folder, FolderPlus, FolderSync, FolderTree, HardDrive, Info, KeyRound, Link2, ListChecks, Play, RefreshCw, Search, ShieldAlert, ShieldCheck, Sparkles, Trash2, X } from "lucide-react";
import { api } from "../api";
import { useApi } from "../hooks";
import { Badge, EmptyState, SectionHeading, Status } from "../components/Primitives";

// A connection is only useful at the end of four steps — authorize, scope, sync, index —
// and each of them used to end in silence with a counter reading 0. Every state below
// therefore carries the action that leaves it, rendered where the operator already is.
const JOURNEY_STEPS = [{ id: "connect", label: "Connected" }, { id: "scope", label: "Scoped" }, { id: "sync", label: "Synced" }, { id: "index", label: "Indexed" }];
const RUN_ACTIVE = ["queued", "running"];
const CONNECTOR_PRIORITY = { local_fs: 0, sharepoint_online: 1, google_drive: 2, onedrive: 3 };

export default function ConnectorsPage({ identity, navigate, focus, connected, onClearConnected }) {
  const admin = Boolean(identity?.is_admin);
  const catalog = useApi("/api/connectors/catalog", [], admin);
  const sources = useApi("/api/sources");
  const projects = useApi("/api/projects");
  const config = useApi("/api/config", [], admin);
  const runs = useApi("/api/runs?limit=60", [], admin);
  const status = useApi("/api/status");
  const [filter, setFilter] = useState("");
  const [connect, setConnect] = useState(null); // catalog connector being connected
  const [detailId, setDetailId] = useState("");
  const [scopeFor, setScopeFor] = useState(""); // source whose folders are being chosen
  const [busy, setBusy] = useState(""); // "sync:<id>" | "sync:*" | "pipeline" | "auth:<id>"
  const [note, setNote] = useState(null); // what the last action actually did
  const [actionError, setActionError] = useState("");
  const [chase, setChase] = useState(0); // an action was pressed: poll even before its run row exists
  const ledIn = useRef(""); // which ?connected= kind already opened the picker

  const rows = sources.data || [];
  const byKind = useMemo(() => Object.fromEntries((catalog.data || []).map((item) => [item.id, item])), [catalog.data]);
  // Grid order: connectors that work, then the planned legal DMS roster (in the API's
  // prominence order — the names a firm looks for, ahead of the generic estate), then
  // everything implemented but not launch-enabled.
  const visible = (catalog.data || [])
    .filter((item) => !item.internal && `${item.name} ${item.category}`.toLowerCase().includes(filter.toLowerCase()))
    .sort((left, right) => {
      const rank = (item) => CONNECTOR_PRIORITY[item.id] ?? (item.planned ? 5 : item.connectable !== false ? 4 : 6);
      if (rank(left) !== rank(right)) return rank(left) - rank(right);
      if (left.planned && right.planned) return 0;
      return left.name.localeCompare(right.name);
    });
  const detail = rows.find((source) => source.id === detailId) || null;
  const scopeSource = rows.find((source) => source.id === scopeFor) || null;
  // The OAuth callback reports only which connector kind went live (?connected=<kind>,
  // see App.jsx), so the connection it created is identified rather than named: same
  // kind, past the handshake, never synced and not scoped yet. Exactly one match is led
  // straight into the folder picker. Two are genuinely ambiguous and are asked about,
  // because saving a selection onto the wrong connection tombstones its documents.
  const freshlyConnected = useMemo(() => (connected && byKind[connected]?.supports_scoping
    ? rows.filter((source) => source.kind === connected && source.status !== "pending_auth" && !source.scope?.scoped && !source.last_sync_at)
    : []), [connected, byKind, rows]);
  useEffect(() => {
    if (!admin || ledIn.current === connected || freshlyConnected.length !== 1) return;
    ledIn.current = connected;
    setScopeFor(freshlyConnected[0].id);
  }, [admin, connected, freshlyConnected]);
  // Opened from the command palette or from an overview alert: show that connection.
  useEffect(() => { if (focus?.source) setDetailId(focus.source); }, [focus]);
  // Documents the last scans believe are deleted, still searchable while the deletion is
  // confirmed. Silence here is the thing being fixed: nothing else on this page would
  // tell an operator that the index is holding documents the source says are gone.
  const confirming = rows.filter((source) => (source.pending_deletion?.object_count || 0) > 0);
  const awaitingAuth = rows.filter((source) => source.status === "pending_auth");
  // Every deployment registers its own OAuth app, so the operator needs the exact
  // redirect URI this appliance will be called back on.
  const redirectUri = config.data?.connectors
    ? `${(config.data.connectors.public_base_url || "").replace(/\/$/, "")}${config.data.connectors.oauth_callback_path || ""}`
    : "";

  const runRows = runs.data || [];
  const activeRuns = runRows.filter((run) => RUN_ACTIVE.includes(run.status));
  const processingActive = activeRuns.find(isProcessingRun) || null;
  const indexedTotal = status.data?.pipeline?.index?.done || 0;
  const objectsTotal = status.data?.counts?.source_objects || 0;
  const journeys = useMemo(() => Object.fromEntries(rows.map((source) => {
    const syncRun = runRows.find((run) => run.workflow === "source-sync" && run.source_id === source.id) || null;
    const handoff = syncRun?.counters?.insertion_run_id ? runRows.find((run) => run.id === syncRun.counters.insertion_run_id) : null;
    const indexed = indexedFor(source, { rows, indexedTotal, objectsTotal });
    // The insertion pipeline is shared, so the active run only belongs on a card whose
    // documents it is actually going to touch. Without the pending gate, one
    // connection's 20-file conversion painted an "indexing" bar on every card.
    const waiting = typeof source.pending_pipeline_count === "number" ? source.pending_pipeline_count > 0 : true;
    return [source.id, journeyOf(source, { entry: byKind[source.kind], syncRun, insertionRun: handoff || (waiting ? processingActive : null), indexed, admin })];
  })), [rows, runRows, byKind, indexedTotal, objectsTotal, processingActive, admin]);
  // A connection that has arrived is not a to-do: only the ones still short of the index
  // get a card, so the stack empties as the estate comes online instead of growing. A
  // connection confirming a deletion has arrived — it is indexed and searchable — and its
  // banner already says so, so it is not "setup in progress" either.
  const openJourneys = rows.filter((source) => !["ready", "confirming"].includes(journeys[source.id]?.id));

  const refresh = () => { sources.reload().catch(() => {}); status.reload().catch(() => {}); if (admin) runs.reload().catch(() => {}); };
  // Runs advance in a worker (or, on a single-VM install, inside the request), so the
  // only truthful progress is polled. Bounded to the moments something is actually
  // moving: while a run is in flight, and for a short while after a button was pressed,
  // since the run row appears a beat later.
  useEffect(() => {
    if (!admin || (!activeRuns.length && !chase)) return undefined;
    let ticks = 0;
    const timer = setInterval(() => {
      ticks += 1;
      refresh();
      if (!activeRuns.length && ticks >= 8) setChase(0);
    }, 2500);
    return () => clearInterval(timer);
  }, [admin, activeRuns.length, chase]); // eslint-disable-line

  const syncNow = async (sourceId, lead = "") => {
    setBusy(sourceId ? `sync:${sourceId}` : "sync:*");
    setActionError(""); setNote(null);
    try {
      const result = await api("/api/actions/sync", { method: "POST", body: JSON.stringify(sourceId ? { source_id: sourceId } : {}) });
      // 202 with `runs` means the scan is in flight and /api/runs owns the truth from
      // here. A build that still answers synchronously reports `results` once it has
      // already finished. Both are said out loud where the button was pressed.
      if (Array.isArray(result?.runs)) setNote({ tone: "ok", text: `${lead}${result.runs.length ? `Sync started for ${plural(result.runs.length, "connection")}. Progress is below.` : "Nothing started — every connection already has a sync in flight."}`, skipped: result.skipped || [] });
      else {
        const results = result?.results || [];
        const failed = results.filter((item) => !item.ok);
        setNote(failed.length
          ? { tone: "bad", text: `${lead}${plural(failed.length, "connection")} did not sync.`, failures: failed.map((item) => `${rows.find((source) => source.id === item.source_id)?.display_name || item.source_id}: ${item.error}`) }
          : { tone: "ok", text: `${lead}Sync finished for ${plural(results.length, "connection")}. Run the insertion pipeline to make the documents searchable.` });
      }
      setChase((current) => current + 1);
    } catch (caught) { setActionError(`Sync could not be started: ${caught.message}`); }
    finally { setBusy(""); refresh(); }
  };

  const runPipeline = async () => {
    setBusy("pipeline"); setActionError(""); setNote(null);
    try {
      await api("/api/actions/pipeline", { method: "POST" });
      setNote({ tone: "ok", text: "Insertion run started. Progress is below." });
      setChase((current) => current + 1);
    } catch (caught) { setActionError(`The insertion pipeline could not be started: ${caught.message}`); }
    finally { setBusy(""); refresh(); }
  };

  const authorize = async (source) => {
    setBusy(`auth:${source.id}`); setActionError("");
    try {
      const started = await api(`/api/connectors/${source.id}/authorize`, { method: "POST" });
      window.location.href = started.authorization_url;
    } catch (caught) { setActionError(caught.message); setBusy(""); }
  };

  const act = (kind, source) => {
    if (kind === "scope") setScopeFor(source.id);
    else if (kind === "sync") syncNow(source.id);
    else if (kind === "pipeline") runPipeline();
    else if (kind === "authorize") authorize(source);
    else if (kind === "data") navigate?.("data");
    else if (kind === "open") setDetailId(source.id);
  };
  const actionBusy = (kind, source) => busy === "pipeline" ? kind === "pipeline" : busy === `${kind}:${source.id}`;

  return (
    <>
      <div className="hero-row compact-hero">
        <div><h1>Connectors</h1></div>
        <div className="hero-actions">
          <button className="secondary-button" onClick={() => syncNow("")} disabled={!admin || !rows.length || Boolean(busy)} title="Scan every connection for new and changed objects"><RefreshCw size={15} /> {busy === "sync:*" ? "Syncing…" : "Sync all"}</button>
          {admin && <button className="primary-button" onClick={() => setConnect({ id: "local_fs", provider: "native" })}><FolderPlus size={15} /> Add local folder</button>}
        </div>
      </div>

      <SpendWarning admin={admin} />

      {connected && <div className="form-note connect-note"><span><Check size={13} /> <strong>{byKind[connected]?.name || connected.replaceAll("_", " ")}</strong> authorized. {freshlyConnected.length ? "Choose the folders it should sync." : "It stays empty until a sync runs."}</span><span className="connect-note-actions">{admin && freshlyConnected.map((source) => <button className="text-button" key={source.id} onClick={() => setScopeFor(source.id)}><FolderTree size={13} /> {freshlyConnected.length > 1 ? `Choose folders — ${source.display_name}` : "Choose folders"}</button>)}<button className="icon-mini" title="Dismiss" onClick={() => onClearConnected?.()}><X size={13} /></button></span></div>}

      {actionError && <div className="form-error">{actionError}</div>}
      {note && <div className={`form-note action-note ${note.tone === "bad" ? "action-note-bad" : ""}`}>
        <span>{note.tone === "bad" ? <AlertTriangle size={13} /> : <Check size={13} />} {note.text}</span>
        {note.failures?.length > 0 && <ul>{note.failures.map((line) => <li key={line}>{line}</li>)}</ul>}
        {/* A source already syncing is a real answer to "sync now", and the one the
            operator is most likely to misread as nothing having happened. The reason
            arrives as prose from the API and is repeated verbatim rather than mapped. */}
        {note.skipped?.length > 0 && <ul>{note.skipped.map((item) => <li key={item.source_id}><b>{item.display_name || item.source_id}</b> was not started: {item.reason}</li>)}</ul>}
        <button className="icon-mini" title="Dismiss" onClick={() => setNote(null)}><X size={13} /></button>
      </div>}

      {confirming.map((source) => <div className="notice-banner" key={`pending-${source.id}`}><ShieldAlert size={15} /><div>
        <strong>{source.display_name}: {plural(source.pending_deletion.object_count, "document")} look deleted — confirming ({source.pending_deletion.confirmations} of {source.pending_deletion.required} syncs).</strong>
        <span>{deletionNote(source.pending_deletion)}</span>
      </div></div>)}

      {/* A new connection can no longer reach this state: it does not exist until the
          provider has authorized it. What is left here is a connection that was created
          before that was true, or one whose grant was withdrawn at the source. */}
      {admin && awaitingAuth.map((source) => <div className="notice-banner" key={`auth-${source.id}`}><KeyRound size={15} /><div>
        <strong>{source.display_name} has never been authorized.</strong>
        <span>Nothing syncs until someone signs in at the provider.</span>
      </div></div>)}

      {openJourneys.length > 0 && <section className="journey-stack">
        <div className="journey-stack-head"><span className="eyebrow">Setup in progress</span><h2>{plural(openJourneys.length, "connection")} short of the index</h2></div>
        {openJourneys.map((source) => <JourneyCard key={source.id} source={source} entry={byKind[source.kind]} journey={journeys[source.id]} admin={admin} busyFor={actionBusy} onAct={act} onOpen={() => setDetailId(source.id)} />)}
      </section>}

      <section className="panel">
        <SectionHeading title="Connections" action={<span className="table-count">{plural(rows.length, "connection")}</span>} />
        {sources.loading && !sources.data ? <div className="quiet-row"><RefreshCw size={15} /> Loading connections…</div>
          : sources.error ? <div className="form-error">Could not load connections: {sources.error.message} <button className="text-button" onClick={() => sources.reload().catch(() => {})}>Retry</button></div>
          : rows.length ? (
            <div className="data-table source-table">
              <div className="table-head"><span>Connection</span><span>Permissions</span><span>Documents</span><span>Scope</span><span>Last sync</span><span>State</span><span>Next step</span></div>
              {rows.map((source) => {
                const entry = byKind[source.kind];
                const scope = source.scope || {};
                const journey = journeys[source.id] || {};
                return <div className="table-row clickable-row" key={source.id} role="button" tabIndex={0} onClick={() => setDetailId(source.id)} onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); setDetailId(source.id); } }} title="Open connection details">
                  <span className="primary-cell"><i className="source-logo">{source.kind.slice(0, 2).toUpperCase()}</i><span><strong>{source.display_name}</strong><small>{source.kind.replaceAll("_", " ")}</small></span></span>
                  <span><AclBadge source={source} /></span>
                  {/* Never a bare count: "0 objects" is what sent an operator looking for a
                      broken connector when the answer was "no sync has run yet". */}
                  <span className="count-cell"><strong className="mono">{journey.synced ? journey.synced.toLocaleString() : "—"}</strong><small>{syncedNote(journey)}</small></span>
                  <span className="stack-cell">{scope.scoped ? <><strong className="subtle">{plural(scope.root_count, "folder")}</strong><small>and everything below</small></> : <><strong className="subtle">Whole source</strong><small>{entry ? (entry.supports_scoping ? "not scoped yet" : "no folder tree") : ""}</small></>}</span>
                  <span>{relative(source.last_sync_at)}</span>
                  <span className="status-cell"><Status value={journey.state || source.status} /></span>
                  <span className="next-cell" onClick={(event) => event.stopPropagation()}>{admin && journey.action
                    ? <button className="secondary-button small" disabled={Boolean(busy)} onClick={() => act(journey.action.kind, source)}>{actionBusy(journey.action.kind, source) ? "Working…" : journey.action.short || journey.action.label}</button>
                    : <span className="subtle muted">{journey.id === "syncing" || journey.id === "indexing" ? "In progress" : "—"}</span>}<ChevronRight size={14} /></span>
                </div>;
              })}
            </div>
          ) : <EmptyState title="Nothing connected yet" copy="Start with a local folder, or pick a system below." action={admin && <button className="secondary-button" onClick={() => setConnect({ id: "local_fs", provider: "native" })}>Add local folder <ArrowRight size={14} /></button>} />}
      </section>

      {admin && <section className="catalog-section">
        <SectionHeading title="Add a connection" action={<div className="search-box compact"><Search size={15} /><input placeholder="Filter connectors" value={filter} onChange={(event) => setFilter(event.target.value)} /></div>} />
        {catalog.loading && !catalog.data ? <div className="quiet-row"><RefreshCw size={15} /> Loading the connector catalog…</div>
          : catalog.error ? <div className="form-error">Could not load the connector catalog: {catalog.error.message} <button className="text-button" onClick={() => catalog.reload().catch(() => {})}>Retry</button></div>
          : visible.length ? <div className="connector-grid">
            {visible.map((connector) => {
              const isNative = connector.provider === "native";
              const connectable = connector.connectable !== false;
              return <article className={`connector-card ${connectable ? "clickable" : "card-disabled"} ${connector.recommended ? "recommended" : ""}`} key={connector.id} onClick={connectable ? () => setConnect(connector) : undefined} aria-disabled={!connectable} title={connectable ? `Connect ${connector.name}` : connector.planned ? `${connector.name} is on the roadmap` : `${connector.name} is not available yet`}>
                {connector.recommended && <span className="recommended-label"><Sparkles size={12} /> Recommended first source</span>}
                {!connectable && <span className="unavailable-label">{connector.planned ? "Planned" : "Not available yet"}</span>}
                <div className="connector-card-top"><div className="connector-logo">{isNative ? <HardDrive size={18} /> : connector.name.split(" ").map((part) => part[0]).slice(0, 2).join("")}</div><div><h3>{connector.name}</h3><span>{connector.category}</span></div>{connectable ? (isNative ? <FolderPlus size={16} /> : <Link2 size={16} />) : <X size={16} />}</div>
                {/* A roadmap entry has no implementation to describe; showing capability
                    rows would state facts about software that does not exist yet. */}
                {!connector.planned && <div className="connector-capabilities">
                  <span><RefreshCw size={13} /> {connector.incremental}</span>
                  <span className={connector.acl_sync ? "capability-good" : "capability-warn"}>{connector.acl_sync ? <ShieldCheck size={13} /> : <ShieldAlert size={13} />} {connector.acl_sync ? "Mirrors source permissions" : "No permission mirror"}</span>
                  <span>{connector.supports_scoping ? <><FolderTree size={13} /> Can sync chosen folders only</> : <><KeyRound size={13} /> {connector.auth.join(" · ")}</>}</span>
                </div>}
                {connector.planned ? <p className="custom-adapter-note">{connector.notes}</p>
                  : cardNote(connector) && <p className="custom-adapter-note">{cardNote(connector)}</p>}
              </article>;
            })}
          </div> : <EmptyState title="No connector matches" action={<button className="secondary-button" onClick={() => setFilter("")}>Clear filter</button>} />}
      </section>}

      {detail && <SourceDrawer source={detail} entry={byKind[detail.kind]} journey={journeys[detail.id]} admin={admin} navigate={navigate} busyFor={actionBusy} onAct={act} onClose={() => setDetailId("")} onChanged={refresh} onRemoved={() => { setDetailId(""); refresh(); }} />}
      {/* Saving a selection used to end with "the next sync applies it" — passive, and
          wrong, because no sync was scheduled. It now starts that sync. */}
      {scopeSource && <ScopeModal source={scopeSource} entry={byKind[scopeSource.kind]} firstRun onClose={() => setScopeFor("")} onSaved={(result) => { setScopeFor(""); refresh(); if (admin) syncNow(scopeSource.id, `${scopeSummary(result)} `); else setNote({ tone: "ok", text: scopeSummary(result) }); }} />}
      {connect?.provider === "native" && <AddLocalFolderModal kind={connect.id} projects={projects.data || []} onClose={() => setConnect(null)} onSaved={(result) => { setConnect(null); setNote(result); setChase((current) => current + 1); refresh(); }} />}
      {connect && connect.provider !== "native" && <ConfigureConnectorModal connector={connect} projects={projects.data || []} redirectUri={redirectUri} docsUrl={identity?.docs_url} onClose={() => setConnect(null)} onSaved={() => { setConnect(null); refresh(); }} />}
    </>
  );
}

// ---------------------------------------------------------------------------
// The journey: what state a connection is in, and what leaves it
// ---------------------------------------------------------------------------

function isProcessingRun(run) {
  return typeof run.workflow === "string" && (run.workflow.includes("insertion") || run.workflow === "access-refresh");
}

// Indexed counts are held per document by the pipeline ledger, which is only aggregated
// estate-wide, so a connection is given a figure of its own exactly where that figure is
// exact: nothing indexed anywhere, nothing synced here, the only connection, or an estate
// with no outstanding work. Otherwise the split between connections is unknowable from
// here and the UI says so rather than inventing a number.
function indexedFor(source, { rows, indexedTotal, objectsTotal }) {
  // The API now counts this connection's own indexed documents; the estate-wide
  // heuristics below remain only as a fallback for a payload that predates the field.
  if (typeof source.indexed_count === "number") return source.indexed_count;
  const synced = source.object_count || 0;
  if (!synced || !indexedTotal) return 0;
  if (rows.length === 1) return Math.min(indexedTotal, synced);
  if (indexedTotal >= objectsTotal) return synced;
  return null;
}

function journeyOf(source, { entry, syncRun, insertionRun, indexed, admin }) {
  const synced = source.object_count || 0;
  const scopable = Boolean(entry?.supports_scoping);
  const scoped = Boolean(source.scope?.scoped);
  const roots = source.scope?.root_count || 0;
  const base = { synced, indexed, run: null, at: 2 };
  const pending = source.pending_deletion?.object_count || 0;
  const syncing = syncRun && RUN_ACTIVE.includes(syncRun.status);
  const indexing = insertionRun && RUN_ACTIVE.includes(insertionRun.status);
  const accessRefreshing = indexing && insertionRun.workflow === "access-refresh";
  const act = (kind, label, short) => (admin ? { kind, label, short: short || label } : null);

  if (source.status === "pending_auth") return { ...base, id: "authorize", at: 0, state: "never authorized", tone: "bad", headline: "Never authorized at the provider.", detail: "Nothing is fetched until someone signs in at the provider.", action: act("authorize", "Authorize at the provider", "Authorize") };

  // The step the run reports is on the progress line already, so the sentence carries
  // what that line cannot: what a scan is for, and that indexing is still to come.
  // The observed counter is written when the scan settles, so mid-run it is absent rather
  // than zero. The headline says only what is known; the run's own step line carries the
  // count as it climbs, and inventing a "0 objects" here is precisely the lie to avoid.
  // Queued and running are not the same thing to look at. A run that has been handed to
  // the orchestrator but not picked up yet says "Syncing now" under a moving progress bar,
  // which is the appliance claiming work nothing is doing — and it is exactly what a
  // restarted worker leaves behind. Waiting says waiting.
  if (syncing && syncRun?.status === "queued" && !syncRun?.started_at) {
    return { ...base, id: "queued", state: "waiting", tone: "wait", run: syncRun, headline: "Waiting to start.", detail: "Handed to the orchestrator; no worker has picked it up yet. If it stays here, check that the worker is running." };
  }
  if (syncing) return { ...base, id: "syncing", state: "syncing", tone: "run", run: syncRun, headline: syncRun.counters?.observed ? `Syncing — ${syncRun.counters.observed.toLocaleString()} object(s) seen so far.` : "Syncing now.", detail: `${syncRun.counters?.mode === "incremental" ? "Incremental scan" : "Full scan"}. Indexing is the step after this one.` };

  if (source.status === "error" || syncRun?.status === "failed") return { ...base, id: "sync_failed", state: "sync failed", tone: "bad", headline: "The last sync failed. Nothing was removed from the index.", detail: syncRun?.error?.message || syncRun?.error?.class || "Usually credentials, network access, or a grant withdrawn at the provider.", action: act("sync", "Sync again"), secondary: entry?.needs_oauth ? act("authorize", "Re-authorize at the provider", "Re-authorize") : null };

  if (scopable && !scoped && !source.last_sync_at) return { ...base, id: "scope", at: 1, state: "never synced", tone: "wait", headline: "Authorized. Nothing synced yet.", detail: "Choose the folders to sync, or sync everything the grant reaches.", action: act("scope", "Choose folders"), secondary: act("sync", "Sync the whole source", "Sync everything") };

  if (!source.last_sync_at) return { ...base, id: "sync", state: "never synced", tone: "wait", headline: scoped ? `${plural(roots, "folder")} selected. No sync has run yet.` : "No sync has run yet.", detail: "No sync is scheduled behind this. The first one is a full scan.", action: act("sync", "Sync now") };

  if (!synced) return { ...base, id: "empty", state: "empty", tone: "wait", headline: "The last sync completed and returned no objects.", detail: scoped ? `The ${plural(roots, "selected folder")} held nothing this account can read. Widen the selection, or check its access.` : "This account can read nothing at the source. Check its access, then sync again.", action: act("sync", "Sync again"), secondary: scopable ? act("scope", "Change folders") : null };

  if (accessRefreshing) return { ...base, id: "access_refresh", at: 3, state: "updating access", tone: "run", run: insertionRun, headline: `${synced.toLocaleString()} synced · refreshing access now.`, detail: "Updating searchable permissions without parsing documents or calling models." };
  if (indexing) return { ...base, id: "indexing", at: 3, state: "indexing", tone: "run", run: insertionRun, headline: `${synced.toLocaleString()} synced · indexing now.`, detail: insertionRun.current_step || "Running" };

  // A deletion large enough to be indistinguishable from a broken connector is confirmed
  // across syncs rather than applied on one scan. The documents keep answering searches
  // while that happens, so leaving it unsaid would be the appliance quietly serving
  // documents the source says are gone.
  if (pending) return { ...base, id: "confirming", at: 4, pending, state: "confirming deletion", tone: "wait", headline: `${plural(pending, "document")} look deleted — confirming (${source.pending_deletion.confirmations} of ${source.pending_deletion.required} syncs).`, detail: "Nothing has been removed yet; they stay searchable until the syncs agree. If they come back, or a different set goes missing, the count starts over.", action: act("sync", "Sync again") };

  if (indexed === 0) return { ...base, id: "index", at: 3, state: "not indexed", tone: "wait", headline: `${synced.toLocaleString()} synced · 0 indexed. Not searchable yet.`, detail: "A search over this connection returns nothing until the insertion pipeline has run.", action: act("pipeline", "Run the insertion pipeline", "Index now") };

  if (indexed === null) return { ...base, id: "index_unknown", at: 3, state: "not indexed", tone: "wait", headline: `${synced.toLocaleString()} synced · part of the estate is still in the pipeline.`, detail: "Indexed documents are counted estate-wide, not per connection. A run drains whatever is left.", action: act("pipeline", "Run the insertion pipeline", "Index now") };

  if (indexed < synced) return { ...base, id: "partial", at: 3, state: "not indexed", tone: "wait", headline: `${indexed.toLocaleString()} of ${synced.toLocaleString()} indexed.`, detail: `${(synced - indexed).toLocaleString()} object(s) are mirrored but not searchable yet.`, action: act("pipeline", "Index the remaining documents", "Index now") };

  return { ...base, id: "ready", at: 4, state: "searchable", tone: "done", headline: `${synced.toLocaleString()} synced · ${indexed.toLocaleString()} indexed and searchable.`, detail: "Permissions are refreshed at every sync.", secondary: { kind: "data", label: "Open the data explorer", short: "Explore" } };
}

// The row cell under the synced count. Reads as a sentence about this connection rather
// than as a number that might mean anything.
function syncedNote(journey) {
  if (journey.id === "authorize") return "never authorized";
  if (journey.id === "syncing") return "syncing now";
  if (journey.id === "sync" || journey.id === "scope") return "never synced";
  if (journey.id === "empty") return "sync returned nothing";
  if (journey.id === "sync_failed") return "last sync failed";
  if (journey.id === "confirming") return `${journey.pending.toLocaleString()} look deleted`;
  if (journey.id === "access_refresh") return "refreshing access now";
  if (journey.id === "indexing") return "indexing now";
  if (journey.id === "index") return "0 indexed — not searchable";
  if (journey.id === "index_unknown") return "indexing incomplete estate-wide";
  if (journey.id === "partial") return `${journey.indexed.toLocaleString()} indexed`;
  return `${(journey.indexed ?? 0).toLocaleString()} indexed`;
}

/**
 * What a connection costs, said before one is made.
 *
 * Every indexed document is read by a model several times over — classified,
 * related to its versions, mined for metadata and decisions — so pointing this
 * page at a SharePoint site is not "adding a folder", it is authorising a spend
 * proportional to how much is behind it. A site nobody has pruned since 2014 can
 * be six figures of documents, and the first signal that this was expensive
 * should not be the invoice.
 *
 * Shown once and then dismissed for good: this is a warning for the person
 * setting the appliance up, and repeating it daily is how it stops being read.
 * Scoping folders — the control immediately below — is the actual remedy, so the
 * warning points at it rather than only alarming.
 */
function SpendWarning({ admin }) {
  const [dismissed, setDismissed] = useState(() => {
    try { return localStorage.getItem("lm.spend-warning.dismissed") === "1"; } catch { return false; }
  });
  if (!admin || dismissed) return null;
  const dismiss = () => {
    try { localStorage.setItem("lm.spend-warning.dismissed", "1"); } catch { /* private mode */ }
    setDismissed(true);
  };
  return (
    <div className="notice-banner spend-warning">
      <AlertTriangle size={15} />
      <div>
        <strong>Connecting a large source can spend a lot on models, quickly.</strong>
        <span>
          Every document is read by a model more than once — classified, related to its
          other versions, mined for metadata and decisions. A SharePoint site or shared
          drive can hold hundreds of thousands of files, and the whole of it is indexed
          unless you narrow it. Choose folders when you connect a source, watch the first
          sync on <b>Costs</b>, and widen the scope once you know the per-document price.
        </span>
      </div>
      <button className="icon-mini" title="Dismiss" onClick={dismiss}><X size={13} /></button>
    </div>
  );
}

function JourneyCard({ source, entry, journey, admin, busyFor, onAct, onOpen }) {
  if (!journey) return null;
  const percent = journey.run ? Math.round((journey.run.progress || 0) * 100) : 0;
  return <article className={`journey-card tone-${journey.tone}`}>
    <div className="journey-head">
      <div className="journey-title"><i className="source-logo">{source.kind.slice(0, 2).toUpperCase()}</i><div><strong>{source.display_name}</strong><span>{entry?.name || source.kind.replaceAll("_", " ")}</span></div></div>
      <Status value={journey.state} />
    </div>

    <ol className="journey-steps">
      {JOURNEY_STEPS.map((step, index) => {
        const skipped = step.id === "scope" && !entry?.supports_scoping;
        const state = skipped ? "skip" : index < journey.at ? "done" : index === journey.at ? journey.tone === "bad" ? "bad" : "now" : "todo";
        return <li key={step.id} className={`journey-step is-${state}`}><i>{state === "done" ? <Check size={11} /> : state === "bad" ? <AlertTriangle size={11} /> : index + 1}</i><span>{skipped ? "No folder tree" : step.label}</span></li>;
      })}
    </ol>

    <div className="journey-body">
      <div className="journey-copy"><strong>{journey.headline}</strong><p>{journey.detail}</p></div>
      <div className="journey-metrics">
        <div><span>Synced</span><b>{journey.synced.toLocaleString()}</b></div>
        <div><span>Indexed</span><b>{journey.indexed === null ? "—" : journey.indexed.toLocaleString()}</b></div>
      </div>
    </div>

    {/* A full scan has no denominator — the connector does not know how many objects it
        will return until it has returned them — so the run reports progress 0 throughout
        and carries a running count in its step instead. A bar pinned at 0% would read as
        "stuck", which is the failure this page exists to remove, so an unmeasured run
        shows movement and its own count rather than a percentage. */}
    {journey.run && ((journey.run.progress || 0) > 0
      ? <div className="journey-progress"><div className="large-progress"><i style={{ width: `${percent}%` }} /></div><small>{percent}% · {journey.run.current_step || journey.run.status}{runCounters(journey.run) ? ` · ${runCounters(journey.run)}` : ""}</small></div>
      : <div className="journey-progress"><div className="large-progress indeterminate"><i /></div><strong className="journey-live">{journey.run.current_step || journey.run.status}</strong></div>)}

    <div className="journey-actions">
      {admin && journey.action && <button className="primary-button small" disabled={busyFor(journey.action.kind, source)} onClick={() => onAct(journey.action.kind, source)}>{journey.action.kind === "pipeline" ? <Play size={14} /> : journey.action.kind === "scope" ? <FolderTree size={14} /> : journey.action.kind === "authorize" ? <KeyRound size={14} /> : <RefreshCw size={14} />} {busyFor(journey.action.kind, source) ? "Working…" : journey.action.label}</button>}
      {admin && journey.secondary && <button className="secondary-button small" disabled={busyFor(journey.secondary.kind, source)} onClick={() => onAct(journey.secondary.kind, source)}>{journey.secondary.label}</button>}
      {!admin && <span className="muted-copy">An administrator continues from here.</span>}
      <button className="text-button" onClick={onOpen}>Connection details <ChevronRight size={13} /></button>
    </div>
  </article>;
}

// Sync counters are the connector's own words for what it just did; only the ones an
// operator acts on are shown, and a zero among them is meaningful (nothing changed).
function runCounters(run) {
  const counters = run.counters || {};
  return ["observed", "created", "changed", "tombstoned"].filter((key) => typeof counters[key] === "number").map((key) => `${counters[key].toLocaleString()} ${key === "tombstoned" ? "removed" : key}`).join(" · ");
}

// One scan cannot tell a deleted matter from a connector that failed to enumerate it, so
// a deletion this large is applied only once consecutive scans report the same documents
// missing. The sentence has to carry both halves: nothing is gone yet, and what makes it
// go (or not).
function deletionNote(pending) {
  const remaining = Math.max(0, (pending.required || 0) - (pending.confirmations || 0));
  return `Nothing has been removed — they are still indexed and still answer searches. ${remaining === 1 ? "One more sync" : `${remaining} more syncs`} reporting the same documents missing will remove them; if they come back, or a different set goes missing, the count starts again. A permission withdrawn at the source looks exactly like this, so check the connection's access if nobody deleted anything.`;
}

function AclBadge({ source }) {
  if (source.mirrors_acls === true) return <Badge tone="green">Mirrored</Badge>;
  if (source.mirrors_acls === false) return <Badge tone="red">Grant required</Badge>;
  return <Badge>Local grants</Badge>;
}

// Only a warning earns a note. "Click to connect" explained the card to itself.
// Private-corpus sources (OneDrive, Gmail, Outlook Mail) carry no card note: they mirror
// permissions like every other connector, and the broad-grant caution lives in the
// connect form, next to the field that would actually cause it.
function cardNote(connector) {
  if (connector.provider === "native") return "";
  if (!connector.acl_sync) return "No per-document permissions: nothing it indexes is searchable until you grant access.";
  return "";
}

// "mailbox" is wrong for a personal drive and "drive" is wrong for a mailbox, and the
// same warning is shown for Gmail, Outlook Mail and OneDrive, so the noun follows the
// connector's own category rather than being hard-coded to mail.
function privateNoun(connector) {
  return connector.category === "Mail" ? "mailbox" : "personal drive";
}

// ---------------------------------------------------------------------------
// Connection details
// ---------------------------------------------------------------------------

function SourceDrawer({ source, entry, journey, admin, navigate, busyFor, onAct, onClose, onChanged, onRemoved }) {
  const [scopeOpen, setScopeOpen] = useState(false);
  const [scopeNote, setScopeNote] = useState(null);
  const [confirmRemove, setConfirmRemove] = useState(false);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const summary = source.last_sync_summary || {};
  const pendingDeletion = source.pending_deletion?.object_count ? source.pending_deletion : null;
  const scope = source.scope || { scoped: false, roots: [], root_count: 0 };
  const isLocal = source.mirrors_acls === null || source.mirrors_acls === undefined;
  const needsOAuth = Boolean(entry?.needs_oauth);

  const authorize = async () => {
    setBusy("auth"); setError("");
    try {
      const started = await api(`/api/connectors/${source.id}/authorize`, { method: "POST" });
      window.location.href = started.authorization_url;
    } catch (caught) { setError(caught.message); setBusy(""); }
  };
  const remove = async () => {
    setBusy("remove"); setError("");
    try { await api(`/api/sources/${source.id}`, { method: "DELETE" }); onRemoved(); }
    catch (caught) { setError(caught.message); setBusy(""); }
  };

  return (
    <div className="drawer-backdrop" onMouseDown={onClose}>
      <aside className="document-drawer" onMouseDown={(event) => event.stopPropagation()}>
        <div className="drawer-header"><span className="eyebrow">Connection details</span><button className="icon-button" onClick={onClose}><X size={17} /></button></div>
        <div className="drawer-title"><div className="large-document-icon"><Link2 size={22} /></div><div><h2>{source.display_name}</h2><span>{entry?.name || source.kind.replaceAll("_", " ")} · {entry?.category || "Filesystem"}</span></div></div>

        <div className="drawer-meta">
          <div><span>Synced</span><strong>{(journey?.synced ?? source.object_count).toLocaleString()}</strong></div>
          <div><span>Indexed</span><strong>{journey?.indexed === null ? "—" : (journey?.indexed ?? 0).toLocaleString()}</strong></div>
          <div><span>Last sync</span><strong>{relative(source.last_sync_at)}</strong></div>
          <div><span>State</span><Status value={journey?.state || source.status} /></div>
        </div>

        {/* The drawer is where a row click lands, so the next step has to be here too —
            leaving it on the page behind would be the dead end this whole flow is about. */}
        {journey && (journey.action || journey.secondary) && <div className="drawer-next">
          <div><strong>{journey.headline}</strong><small>{journey.detail}</small></div>
          <div className="drawer-next-actions">
            {admin && journey.action && <button className="primary-button small" disabled={busyFor(journey.action.kind, source)} onClick={() => { if (journey.action.kind === "scope") setScopeOpen(true); else onAct(journey.action.kind, source); }}>{busyFor(journey.action.kind, source) ? "Working…" : journey.action.label}</button>}
            {admin && journey.secondary && <button className="secondary-button small" disabled={busyFor(journey.secondary.kind, source)} onClick={() => { if (journey.secondary.kind === "scope") setScopeOpen(true); else onAct(journey.secondary.kind, source); }}>{journey.secondary.label}</button>}
          </div>
        </div>}

        {error && <div className="form-error" style={{ marginTop: "14px" }}>{error}</div>}

        <section className="drawer-section">
          <h3>Sync health</h3>
          {source.status === "pending_auth" ? <>
            <div className="notice-banner inline"><KeyRound size={14} /><div><strong>Never authorized at the provider.</strong><span>It syncs nothing until someone signs in.</span></div></div>
            {admin && <button className="secondary-button small drawer-action" onClick={authorize} disabled={busy === "auth"}><KeyRound size={13} /> {busy === "auth" ? "Opening the provider…" : "Authorize now"}</button>}
          </> : source.status === "error" ? <>
            <div className="notice-banner inline"><AlertTriangle size={14} /><div><strong>The last sync failed. Nothing was removed.</strong><span>Usually credentials, network access, or a grant withdrawn at the provider. Re-authorizing repairs this same connection.</span></div></div>
            {/* Without this an OAuth connection whose grant was revoked has no route back
                except delete-and-recreate, re-entering the client id, secret and config. */}
            {admin && needsOAuth && <button className="secondary-button small drawer-action" onClick={authorize} disabled={busy === "auth"}><KeyRound size={13} /> {busy === "auth" ? "Opening the provider…" : "Re-authorize at the provider"}</button>}
          </>
            : summary.mode ? <div className="sync-counters">
              <div><span>Mode</span><strong>{summary.mode === "incremental" ? "Incremental" : "Full scan"}</strong></div>
              <div><span>Observed</span><strong>{(summary.observed ?? 0).toLocaleString()}</strong></div>
              <div><span>New</span><strong>{(summary.created ?? 0).toLocaleString()}</strong></div>
              <div><span>Updated</span><strong>{(summary.changed ?? 0).toLocaleString()}</strong></div>
              <div><span>Removed</span><strong>{(summary.tombstoned ?? 0).toLocaleString()}</strong></div>
            </div> : <p className="muted-copy">No sync has completed yet.</p>}
          {/* Held deletions are shown next to the counters, not instead of them: the last
              sync succeeded, and both facts are true at once. */}
          {pendingDeletion && <div className="notice-banner inline"><ShieldAlert size={14} /><div>
            <strong>{plural(pendingDeletion.object_count, "document")} look deleted — confirming ({pendingDeletion.confirmations} of {pendingDeletion.required} syncs).</strong>
            <span>{deletionNote(pendingDeletion)}</span>
          </div></div>}
          <p className="muted-copy drawer-note">A sync mirrors objects and permissions. The insertion pipeline is what makes them searchable.</p>
        </section>

        <section className="drawer-section">
          <h3>Permissions</h3>
          {isLocal ? <p className="muted-copy">A local folder has no source permissions: project and document grants decide who can search it.</p>
            : source.mirrors_acls ? <>
              <div className="form-note"><b>Permissions are mirrored at every sync.</b> A local grant cannot widen them.</div>
              {entry?.notes && <p className="muted-copy drawer-note">{entry.notes}</p>}
            </> : <>
              <div className="notice-banner inline"><ShieldAlert size={14} /><div><strong>{entry?.name || source.kind} reports no per-document permissions.</strong><span>Its documents stay invisible — a search returns no results, not an error — until you grant access under Access control.</span></div></div>
              {navigate && <button className="secondary-button small drawer-action" onClick={() => navigate("access")}>Open access control <ArrowRight size={13} /></button>}
            </>}
        </section>

        <section className="drawer-section">
          <h3>Synced folders</h3>
          {entry?.supports_scoping ? <>
            {scope.scoped ? <div className="scope-selected">{scope.roots.map((root) => <span className="scope-chip" key={root.id}><FolderTree size={11} /> {root.title}</span>)}</div>
              : <p className="muted-copy">Everything this connection can reach is synced.</p>}
            <p className="muted-copy drawer-note">A selected folder covers <b>everything below it, including sub-folders added later</b>.</p>
            {scopeNote && <div className="form-note">{scopeSummary(scopeNote)}</div>}
            {admin && <button className="secondary-button small drawer-action" onClick={() => { setScopeNote(null); setScopeOpen(true); }} disabled={source.status === "pending_auth"}><FolderTree size={13} /> {scope.scoped ? "Change folders…" : "Choose folders…"}</button>}
            {source.status === "pending_auth" && <p className="muted-copy drawer-note">Authorize first to browse folders.</p>}
          </> : <p className="muted-copy">{entry ? `${entry.name} syncs as a whole — it has no folder tree to scope to.` : admin ? "This source type syncs as a whole." : "Folder scoping is managed by an administrator."}</p>}
        </section>

        <section className="drawer-section">
          <h3>Change detection</h3>
          {source.event_delivery?.status === "active" ? <>
            <div className="grant-compact"><RefreshCw size={14} /><span>Live provider events · {source.event_delivery.active} of {source.event_delivery.targets} target(s)</span><Badge>event driven</Badge></div>
            <p className="muted-copy drawer-note">{source.event_delivery.detail} The <b>{source.sync_policy?.interval || "configured"}</b> policy interval is retained only to reconcile anything the provider or broker missed.</p>
          </> : <>
            <div className="grant-compact"><RefreshCw size={14} /><span>{source.sync_policy?.mode || "manual"}{source.sync_policy?.interval ? ` · reconcile every ${source.sync_policy.interval}` : ""}</span><Badge>{source.event_delivery?.status === "pending" ? "events pending" : source.event_delivery?.status === "unconfigured" ? "events not configured" : "scheduled"}</Badge></div>
            <p className="muted-copy drawer-note">{source.event_delivery?.detail || "The scheduler applies this connection's policy interval."}</p>
          </>}
          <p className="muted-copy drawer-note"><b>Policy</b> is the rule saved on this connection. The <b>scheduler</b> is the appliance process that enforces its reconciliation interval.</p>
        </section>

        {admin && <section className="drawer-section">
          <h3>Remove</h3>
          <div className="rebuild-row">
            <div><strong><Trash2 size={13} /> Remove this connection</strong><small>Deletes {source.object_count.toLocaleString()} sync record(s) and stops their documents being retrievable. Nothing is deleted at {entry?.name || "the source"}.</small></div>
            {confirmRemove ? <div className="row-confirm"><button className="secondary-button small" onClick={remove} disabled={busy === "remove"}><Check size={13} /> {busy === "remove" ? "Removing…" : "Confirm remove"}</button><button className="text-button" onClick={() => setConfirmRemove(false)}>Cancel</button></div>
              : <button className="secondary-button small" onClick={() => setConfirmRemove(true)}><Trash2 size={13} /> Remove…</button>}
          </div>
        </section>}

        {/* Saving a scope from here offers the sync it needs, rather than reporting that
            some future sync will apply it — there is no future sync until one is run. */}
        {scopeOpen && <ScopeModal source={source} entry={entry} onClose={() => setScopeOpen(false)} onSaved={(result) => { setScopeOpen(false); setScopeNote(result); onChanged(); if (admin) onAct("sync", source); }} />}
      </aside>
    </div>
  );
}

function scopeSummary(result) {
  const scoped = result?.scope?.scoped;
  const count = result?.scope?.root_count || 0;
  const what = scoped ? `${plural(count, "folder")} selected, and everything below ${count === 1 ? "it" : "them"}.` : "The whole source is synced.";
  return `${what} ${result?.would_remove_existing ? "The next scan removes indexed documents outside the selection." : result?.changed ? "The selection is saved." : "Nothing changed."}`;
}

// ---------------------------------------------------------------------------
// Folder scoping
// ---------------------------------------------------------------------------

// One screenful of a wide folder. A matter drive with 4 000 clients in one level would
// otherwise render 4 000 rows nobody scrolls through, so the rest is asked for.
const BRANCH_PAGE = 60;

function ScopeModal({ source, entry, firstRun, onClose, onSaved }) {
  const [branches, setBranches] = useState({});
  const [expanded, setExpanded] = useState(() => new Set());
  const [parents, setParents] = useState({});
  // Titles are kept for every node ever listed, not just the visible ones: a selection
  // made four levels down still has to be able to say where it came from. Seeded from the
  // stored roots so an existing scope reads properly before anything has been browsed.
  const [titles, setTitles] = useState(() => Object.fromEntries((source.scope?.roots || []).map((root) => [root.id, root.title || root.id])));
  const [shown, setShown] = useState({});
  const [here, setHere] = useState("");
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState(() => Object.fromEntries((source.scope?.roots || []).map((root) => [root.id, { id: root.id, type: root.type || "folder", title: root.title || root.id }])));
  const [confirmed, setConfirmed] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const initial = useMemo(() => (source.scope?.roots || []).map((root) => root.id), [source.scope]);

  const load = async (key) => {
    setBranches((current) => ({ ...current, [key]: { loading: true, error: "", nodes: [] } }));
    try {
      const result = await api(`/api/sources/${source.id}/browse${key ? `?node=${encodeURIComponent(key)}` : ""}`);
      const nodes = result?.nodes || [];
      setBranches((current) => ({ ...current, [key]: { loading: false, error: "", nodes } }));
      setParents((current) => ({ ...current, ...Object.fromEntries(nodes.map((node) => [node.source_node_id, key])) }));
      setTitles((current) => ({ ...current, ...Object.fromEntries(nodes.map((node) => [node.source_node_id, node.title || node.source_node_id])) }));
    } catch (caught) {
      setBranches((current) => ({ ...current, [key]: { loading: false, error: caught.message, nodes: [] } }));
    }
  };
  useEffect(() => { load(""); }, []); // eslint-disable-line

  // "Documents" is a meaningless name on its own — every SharePoint site has one — so a
  // node is always shown with the trail it sits under.
  const pathOf = (id) => {
    const parts = [];
    for (let cursor = id; cursor; cursor = parents[cursor]) parts.unshift(titles[cursor] || cursor);
    return parts.join(" / ");
  };
  const toggleExpand = (id) => {
    const open = expanded.has(id);
    setExpanded((current) => {
      const next = new Set(current);
      if (open) next.delete(id);
      else { next.add(id); if (!branches[id]) load(id); }
      return next;
    });
    setHere(open ? parents[id] || "" : id);
  };
  // A selected folder already covers everything under it, so a descendant cannot be
  // selected separately — showing that is the clearest way to teach the subtree rule.
  const coveredBy = (id) => {
    let parent = parents[id];
    while (parent) {
      if (selected[parent]) return selected[parent];
      parent = parents[parent];
    }
    return null;
  };
  const toggleSelect = (node) => {
    const id = node.source_node_id;
    setSelected((current) => {
      const next = { ...current };
      if (next[id]) delete next[id];
      else next[id] = { id, type: node.node_type || "folder", title: node.title || id };
      return next;
    });
    setConfirmed(false);
  };

  const chosen = Object.values(selected);
  const removed = initial.filter((id) => !selected[id]);
  const added = chosen.filter((root) => !initial.includes(root.id));
  const changed = removed.length > 0 || added.length > 0;
  // Narrowing is destructive only when this source already contributed objects. A first
  // selection against an empty source removes nothing and must not demand a frightening,
  // false confirmation.
  const narrowing = Number(source.object_count || 0) > 0 && chosen.length > 0 && (removed.length > 0 || initial.length === 0);
  const widening = chosen.length === 0 && initial.length > 0;
  const needle = query.trim().toLowerCase();
  const hit = (node) => (node.title || "").toLowerCase().includes(needle);
  // The tree is loaded lazily, so a filter can only reach what the provider has already
  // been asked for. A branch is kept when it, or anything already loaded beneath it,
  // matches — which is also what makes a match several levels down visible at all.
  const branchHits = (key, guard = new Set()) => {
    const branch = branches[key];
    if (!branch || guard.has(key)) return false;
    guard.add(key);
    return branch.nodes.some((node) => hit(node) || branchHits(node.source_node_id, guard));
  };
  const root = branches[""] || {};

  const save = async () => {
    setSaving(true); setError("");
    try {
      const roots = chosen.map(({ id, type, title }) => ({ id, type, title }));
      onSaved(await api(`/api/sources/${source.id}/scope`, { method: "PUT", body: JSON.stringify({ roots }) }));
    } catch (caught) { setError(caught.message); setSaving(false); }
  };

  const renderBranch = (key, depth) => {
    const branch = branches[key];
    const pad = { paddingLeft: `${depth * 15 + 4}px` };
    if (!branch) return null;
    if (branch.loading) return <div className="scope-loading" style={pad} key={`${key}-loading`}><RefreshCw size={11} /> Loading…</div>;
    if (branch.error) return <div className="scope-loading scope-failed" style={pad} key={`${key}-error`}><AlertTriangle size={11} /> {branch.error}<button type="button" className="text-button" onClick={() => load(key)}>Retry</button></div>;
    const listed = needle ? branch.nodes.filter((node) => hit(node) || branchHits(node.source_node_id)) : branch.nodes;
    // An empty tree is a real answer from the provider and is reported as one: a grant
    // that reaches nothing looks exactly like this, and pretending otherwise would send
    // an operator hunting for folders that are not there.
    if (!listed.length) return <div className={`scope-empty ${depth === 0 ? "scope-empty-root" : ""}`} style={pad} key={`${key}-empty`}>{needle ? "Nothing matches the filter." : depth === 0 ? `${entry?.name || source.kind} returned no folders — this account can open nothing here. The whole source stays selected.` : "Nothing here."}</div>;
    const limit = shown[key] || BRANCH_PAGE;
    return <>
      {listed.slice(0, limit).map((node) => {
        const id = node.source_node_id;
        const cover = coveredBy(id);
        const open = expanded.has(id) || Boolean(needle && branchHits(id));
        return <div key={id}>
          <div className={`scope-node ${cover ? "covered" : ""} ${selected[id] ? "picked" : ""}`} style={pad}>
            <button type="button" className="scope-caret" disabled={!node.has_children} title={node.has_children ? "Expand" : "No sub-folders"} onClick={() => toggleExpand(id)}>{node.has_children ? (open ? <ChevronDown size={13} /> : <ChevronRight size={13} />) : <i className="scope-leaf" />}</button>
            <label title={pathOf(id)}><input type="checkbox" checked={Boolean(selected[id]) || Boolean(cover)} disabled={Boolean(cover)} onChange={() => toggleSelect(node)} /><Folder size={12} /><span className="scope-title">{node.title}</span></label>
            <small>{cover ? `included via ${cover.title}` : selected[id] ? "+ everything below" : node.item_count != null ? `${node.item_count} items` : node.node_type}</small>
          </div>
          {open && renderBranch(id, depth + 1)}
        </div>;
      })}
      {listed.length > limit && <button type="button" className="scope-more" style={pad} key={`${key}-more`} onClick={() => setShown((current) => ({ ...current, [key]: limit + BRANCH_PAGE }))}>Show {Math.min(BRANCH_PAGE, listed.length - limit)} more — {listed.length - limit} of {listed.length} not shown</button>}
    </>;
  };

  return <div className="modal-backdrop" onMouseDown={onClose}><div className="form-modal wide" onMouseDown={(event) => event.stopPropagation()}>
    <div className="drawer-header"><div><span className="eyebrow">{firstRun ? `${entry?.name || source.kind} · connected` : entry?.name || source.kind}</span><h2>{firstRun ? "Now choose what to sync" : "Choose the folders to sync"}</h2><p>A selected folder syncs <b>everything below it, including sub-folders added later</b>.</p></div><button type="button" className="icon-button" onClick={onClose}><X size={17} /></button></div>

    <div>
      <span className="eyebrow">What this connection syncs</span>
      {chosen.length ? <>
        <div className="scope-selected">{chosen.map((root) => <span className="scope-chip" key={root.id} title={pathOf(root.id)}><FolderTree size={11} /> {root.title}<button type="button" title={`Remove ${root.title}`} onClick={() => toggleSelect({ source_node_id: root.id })}><X size={11} /></button></span>)}</div>
        <small className="scope-hint">{plural(chosen.length, "folder")} — and everything below. Anything outside stays out.</small>
      </> : <div className="scope-whole"><HardDrive size={15} /><span><strong>The whole source — nothing is selected.</strong><small>Every folder this connection can reach is indexed. Pick folders below to limit it.</small></span></div>}
    </div>

    {!root.error && <div className="scope-toolbar">
      <span className="scope-crumbs" title={here ? pathOf(here) : "Top level"}><HardDrive size={11} /> {entry?.name || source.kind}{here ? ` / ${pathOf(here)}` : ""}</span>
      <span className="scope-tools">
        <label className="scope-search"><Search size={11} /><input placeholder="Filter listed folders" value={query} onChange={(event) => setQuery(event.target.value)} /></label>
        <button type="button" className="text-button" disabled={!expanded.size} onClick={() => { setExpanded(new Set()); setHere(""); }}>Collapse all</button>
      </span>
    </div>}
    {needle && <small className="scope-hint">Filters folders already listed. Expand a folder to search inside it.</small>}

    {/* Browsing can fail for reasons that have nothing to do with this appliance — an
        unlicensed service, a grant narrowed after authorization, an outage — and all of
        them look the same from here, so the provider's own words are shown rather than
        summarised, and the selection stays editable. */}
    {root.error ? <div className="notice-banner inline"><AlertTriangle size={14} /><div>
      <strong>{entry?.name || source.kind} could not list its folders.</strong>
      <span>Nothing changed: it still syncs {initial.length ? `the ${initial.length} folder(s) already chosen` : "everything it can reach"}. The provider said — <code>{root.error}</code></span>
      <button type="button" className="secondary-button small drawer-action" onClick={() => load("")} disabled={root.loading}><RefreshCw size={13} /> {root.loading ? "Retrying…" : "Try again"}</button>
    </div></div> : <div className="scope-tree">{renderBranch("", 0)}</div>}

    {narrowing && <div className="notice-banner inline"><AlertTriangle size={14} /><div>
      <strong>This narrows the scope. The next sync deletes documents from the index.</strong>
      <span>{initial.length === 0
        ? `Everything outside the ${chosen.length} folder(s) you picked is removed from the index.`
        : `Documents under the ${removed.length} folder(s) you removed are deleted from the index.`} Nothing is deleted at {entry?.name || "the source"}.</span>
    </div></div>}
    {narrowing && <label className="toggle-row"><span><strong>I understand documents will be removed from the index</strong><small>Selecting those folders again re-indexes them.</small></span><input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} /></label>}
    {widening && <div className="form-note"><b>Nothing selected: this goes back to syncing the whole source.</b> Nothing is deleted, and the next sync is a full scan.</div>}

    {/* The selection is only an instruction until something acts on it, so saving starts
        the scan that applies it instead of pointing at a sync nobody scheduled. */}
    <div className="form-note"><b>Saving starts a sync immediately.</b> What it brings back is searchable once the insertion pipeline has run.</div>

    {error && <div className="form-error">{error}</div>}
    <div className="modal-actions">
      <button type="button" className="text-button" onClick={onClose}>Cancel</button>
      {/* Syncing everything is a decision, not the absence of one, so the empty selection
          gets a button that says so rather than a disabled "no changes". */}
      <button type="button" className="primary-button" onClick={save} disabled={saving || (chosen.length > 0 && !changed) || (narrowing && !confirmed)}>{chosen.length ? <FolderTree size={15} /> : <HardDrive size={15} />} {saving ? "Saving…" : !chosen.length ? "Sync the whole source" : !changed ? "No changes" : chosen.length === 1 ? "Save and sync this folder" : `Save and sync these ${chosen.length} folders`}</button>
    </div>
  </div></div>;
}

// ---------------------------------------------------------------------------
// Local folders
// ---------------------------------------------------------------------------

function FolderPicker({ value, onChange }) {
  const [path, setPath] = useState(value || "");
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const load = async (target) => {
    setError("");
    try {
      const result = await api(`/api/fs/list${target ? `?path=${encodeURIComponent(target)}` : ""}`);
      setData(result); setPath(result.path);
    } catch (caught) { setError(caught.message); }
  };
  useEffect(() => { load(value || ""); }, []); // eslint-disable-line
  return (
    <div className="folder-picker">
      <div className="fp-bar"><button type="button" className="icon-mini" disabled={!data?.parent} title="Up one level" onClick={() => load(data.parent)}><ArrowUp size={13} /></button><input className="mono fp-path" aria-label="Folder path" value={path} onChange={(event) => setPath(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") { event.preventDefault(); load(path); } }} /><button type="button" className="icon-mini" title="Go" onClick={() => load(path)}><ArrowRight size={13} /></button><button type="button" className="secondary-button small" disabled={!data?.path || value === data.path} onClick={() => onChange(data.path)}>{value === data?.path ? "Selected" : "Use this folder"}</button></div>
      {error ? <div className="form-error">{error}</div> : <div className="fp-list">{(data?.dirs || []).length ? data.dirs.map((name) => <button type="button" key={name} className="fp-dir" onClick={() => load(`${data.path.replace(/\/$/, "")}/${name}`)}><Folder size={13} /> {name}</button>) : <span className="fp-empty">No sub-folders here. This folder will be indexed.</span>}</div>}
      <small className="fp-note"><Info size={11} /> {value ? <>Indexes <b>{value}</b> and everything under it, read-only.</> : <>Nothing selected yet.</>}</small>
    </div>
  );
}

function SystemFolderPicker({ files, onChange, onError }) {
  const inputRef = useRef(null);
  const choose = () => {
    if (!inputRef.current) return;
    inputRef.current.value = "";
    inputRef.current.click();
  };
  const selected = (event) => {
    const next = Array.from(event.target.files || []);
    onError("");
    onChange(next);
  };
  const folderName = files[0]?.webkitRelativePath?.split("/")[0] || "";
  return <div className="system-folder-picker">
    <input ref={inputRef} className="native-folder-input" type="file" webkitdirectory="" directory="" multiple onChange={selected} />
    <button type="button" className="primary-button system-folder-button" onClick={choose}><FolderPlus size={15} /> {files.length ? "Choose a different folder…" : "Choose folder…"}</button>
    {files.length ? <div className="system-folder-selection"><Folder size={16} /><span><strong>{folderName}</strong><small>{files.length} file(s) selected</small></span><Check size={15} /></div> : <small className="system-folder-note"><Info size={12} /> Nothing selected yet.</small>}
  </div>;
}

function AddLocalFolderModal({ kind, projects, onClose, onSaved }) {
  const isPlugin = kind === "plugin_drop";
  const [form, setForm] = useState({ display_name: "", root: "", project_id: "", grant_principal: "group:knowledge-index-admins", mode: "continuous", interval: "5m" });
  const [pickerMode, setPickerMode] = useState(isPlugin ? "mounted" : "system");
  const [selectedFiles, setSelectedFiles] = useState([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const setRoot = (root) => setForm((current) => ({ ...current, root, display_name: current.display_name || (root.split("/").filter(Boolean).pop() || "") }));
  const setSystemFiles = (files) => {
    setSelectedFiles(files);
    const folderName = files[0]?.webkitRelativePath?.split("/")[0] || "";
    if (folderName) setForm((current) => ({ ...current, display_name: current.display_name || folderName }));
  };
  const save = async (event) => {
    event.preventDefault(); setError(""); setBusy(true);
    try {
      let root = form.root;
      let managedImport = false;
      if (pickerMode === "system") {
        const payload = new FormData();
        const relativePaths = selectedFiles.map((file) => file.webkitRelativePath || file.name);
        selectedFiles.forEach((file) => payload.append("files", file, file.name));
        payload.append("relative_paths", JSON.stringify(relativePaths));
        const imported = await api("/api/fs/import-folder", { method: "POST", body: payload });
        root = imported.root;
        managedImport = true;
      }
      const body = { display_name: form.display_name || root.split("/").filter(Boolean).pop() || "Local folder", kind, provider: "native", root, project_id: form.project_id || null, sync_policy: { mode: pickerMode === "system" ? "manual" : form.mode, interval: form.interval }, config: managedImport ? { managed_import: true } : undefined };
      if (form.grant_principal.trim()) body.default_acl = [{ principal: form.grant_principal.trim(), principal_kind: form.grant_principal.split(":")[0] || "group", access: "allow" }];
      const created = await api("/api/sources", { method: "POST", body: JSON.stringify(body) });
      const result = await api("/api/actions/sync", { method: "POST", body: JSON.stringify(created?.id ? { source_id: created.id } : {}) });
      // A synchronous answer means the scan is already finished, so the documents are
      // sitting unindexed right now and the pipeline is started for them. An accepted
      // (202) sync hands off to insertion itself, or the connection's card offers it.
      if (Array.isArray(result?.results)) try { await api("/api/actions/pipeline", { method: "POST" }); } catch { /* the card offers it */ }
      onSaved({ tone: "ok", text: `${body.display_name} connected. The first sync is running — its progress is below.` });
    } catch (caught) { setError(caught.message); } finally { setBusy(false); }
  };
  return <div className="modal-backdrop" onMouseDown={onClose}><form className="form-modal wide" onSubmit={save} onMouseDown={(event) => event.stopPropagation()}>
    <div className="drawer-header"><div><span className="eyebrow">Native connector</span><h2>{isPlugin ? "Add a plugin drop directory" : "Add a local folder"}</h2><p>{pickerMode === "system" ? "Files are copied into managed storage, then indexed read-only." : "A server-visible folder or mounted share, indexed read-only."}</p></div><button type="button" className="icon-button" onClick={onClose}><X size={17} /></button></div>
    {!isPlugin && <div className="folder-mode-tabs"><button type="button" className={pickerMode === "system" ? "active" : ""} onClick={() => setPickerMode("system")}>System dialog</button><button type="button" className={pickerMode === "mounted" ? "active" : ""} onClick={() => setPickerMode("mounted")}>Mounted / server path</button></div>}
    <label>Folder{pickerMode === "system" ? <SystemFolderPicker files={selectedFiles} onChange={setSystemFiles} onError={setError} /> : <FolderPicker value={form.root} onChange={setRoot} />}</label>
    <div className="form-columns">
      <label>Connection name<input required placeholder={form.root.split("/").filter(Boolean).pop() || "My matters folder"} value={form.display_name} onChange={(event) => setForm({ ...form, display_name: event.target.value })} /></label>
      <label>Grant access to<input className="mono" placeholder="group:demo-users" value={form.grant_principal} onChange={(event) => setForm({ ...form, grant_principal: event.target.value })} /><small>A local folder has no source permissions: this grant decides who can search it.</small></label>
    </div>
    <div className="form-columns">
      <label>Project (optional)<select value={form.project_id} onChange={(event) => setForm({ ...form, project_id: event.target.value })}><option value="">No project</option>{projects.map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}</select></label>
      {pickerMode === "system" ? <label>Monitoring<input value="Snapshot import (manual refresh)" disabled /></label> : <label>Monitoring<select value={form.mode} onChange={(event) => setForm({ ...form, mode: event.target.value })}><option value="continuous">Continuous (watch for changes)</option><option value="manual">Manual (sync on demand)</option></select></label>}
    </div>
    {error && <div className="form-error">{error}</div>}
    <div className="modal-actions"><button type="button" className="text-button" onClick={onClose}>Cancel</button><button className="primary-button" disabled={busy || (pickerMode === "system" ? !selectedFiles.length : !form.root)}><FolderSync size={15} /> {busy ? "Importing & connecting…" : "Connect & index"}</button></div>
  </form></div>;
}

// ---------------------------------------------------------------------------
// External connectors
// ---------------------------------------------------------------------------

// The OAuth app lives in the firm's own tenant, so these are the firm's values: where to
// find them is the useful hint, and echoing the key name back is not.
const AUTH_HINTS = { client_id: "Client ID from the OAuth app", client_secret: "Shown once when the secret was created", access_token: "Paste the token from the provider" };

// What the refresh behaviour declared in providers.yaml means for whoever has to keep the
// connection alive. Generated from the provider's own oauth_type, not written per
// connector, so a provider that changes cannot leave stale advice behind.
const TOKEN_NOTES = {
  with_refresh: "The connection keeps syncing without anyone signing in again.",
  with_rotating_refresh: "Left unused past the provider's refresh lifetime, it has to be authorized again.",
  access_only: "The provider returned no refresh token. It keeps working until the token expires or is revoked; then re-authorize."
};

// A placeholder shows the shape of a value; the description explains it and belongs in
// the help text underneath. Schema authors already wrote an example into most
// descriptions, so it is lifted out rather than maintained twice.
function fieldExample(field) {
  if (AUTH_HINTS[field.name]) return AUTH_HINTS[field.name];
  const description = field.description || "";
  const quoted = /['"]([^'"]{1,64})['"]/.exec(description);
  if (quoted) return quoted[1];
  const format = /format:\s*([^\s)]+)/i.exec(description);
  if (format) return format[1];
  if (/guid/i.test(description)) return "00000000-0000-0000-0000-000000000000";
  if (field.type === "integer") return String(field.default ?? 0);
  return "";
}

// The guidance marks the words that cost a round trip when they are misread — the secret
// **Value** rather than its **Secret ID**, **Bot Token Scopes** rather than user ones.
function emphasize(text) {
  return String(text || "").split("**").map((part, index) => (index % 2 ? <b key={index}>{part}</b> : part));
}

// Generated from providers.yaml: the console prose lives beside the OAuth settings it
// describes, and the scope list is the connector's own `scope`, so the instructions
// cannot drift from what this appliance actually requests.
function RegistrationPanel({ meta, redirectUri, docsUrl, connectorId }) {
  const guide = meta.registration;
  if (!guide) return null;
  // The hosted documentation carries the same steps with screenshots and the
  // provider-side troubleshooting that does not fit in a form panel.
  const docsPage = docsUrl ? `${docsUrl.replace(/\/+$/, "")}/connectors/${String(connectorId || "").replaceAll("_", "-")}/` : "";
  const scopes = guide.scopes || [];
  const steps = [
    guide.create && { id: "create", title: `Create the app in ${guide.console || "the provider's console"}`, body: guide.create, link: guide.console_url },
    guide.app_type && { id: "app_type", title: "Set the application type", body: guide.app_type },
    guide.apis && { id: "apis", title: "Enable the API", body: guide.apis },
    { id: "redirect", title: "Register this appliance's redirect URI", body: guide.redirect_field, code: redirectUri || null, fallback: "This appliance does not know its own public URL yet: set the connector public base URL under configuration, then reopen this form." },
    { id: "scopes", title: guide.scope_label ? `Add the ${guide.scope_label.toLowerCase()}` : "Add the permissions", body: guide.scope_location, scopes },
    guide.consent && { id: "consent", title: "Grant admin consent", body: guide.consent },
    guide.secret && { id: "secret", title: "Copy the client id and secret", body: guide.secret },
    guide.account && { id: "account", title: "Sign in with the right account", body: guide.account }
  ].filter(Boolean);

  // Collapsed by default: it is reference material for the one-time registration, and an
  // operator who already has the credentials only needs the two fields below it. Expanded
  // it buries them under a page of prose.
  return <details className="registration-guide">
    <summary><ListChecks size={14} /> How to register this app in {guide.console || "the provider's console"}<ChevronDown size={14} className="reg-caret" /></summary>
    <ol className="reg-steps">
      {steps.map((step) => <li key={step.id}>
        <strong>{step.title}</strong>
        {step.body && <p>{emphasize(step.body)}</p>}
        {step.link && <a className="row-link" href={step.link} target="_blank" rel="noreferrer">Open {guide.console} <ExternalLink size={12} /></a>}
        {step.id === "redirect" && (step.code ? <code className="reg-code">{step.code}</code> : <p className="reg-warn">{step.fallback}</p>)}
        {step.scopes?.length > 0 && <div className="reg-scopes">{step.scopes.map((scope) => <code key={scope}>{scope}</code>)}</div>}
      </li>)}
    </ol>
    <p className="reg-foot">
      {TOKEN_NOTES[guide.oauth_type] || ""} Credentials are stored encrypted and used only to call {meta.name}.
      {docsPage && <> <a className="row-link" href={docsPage} target="_blank" rel="noreferrer">Full connector guide in the documentation <ExternalLink size={12} /></a></>}
    </p>
  </details>;
}

function ConfigureConnectorModal({ connector, projects, redirectUri, docsUrl, onClose, onSaved }) {
  const fields = useApi(`/api/connectors/${connector.id}/fields`, [connector.id]);
  const [form, setForm] = useState({ display_name: connector.name, project_id: "", grant_principal: "", interval: "1h" });
  const [creds, setCreds] = useState({});
  const [conf, setConf] = useState({});
  const [confirmBroad, setConfirmBroad] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const meta = fields.data;
  const isOAuth = Boolean(meta?.needs_oauth);
  const authFields = meta?.auth_fields || [];
  const configFields = meta?.config_fields || [];
  const mirrorsAcls = meta ? meta.mirrors_acls : connector.acl_sync;
  // Nobody can answer "which folders" here: this form runs before the provider has
  // authorized anything, so the appliance has never seen the drive and the operator
  // would be typing paths from memory. The backend marks the fields that ask it
  // (`superseded_by`) and they are deferred to the tree picker, which runs against the
  // real folders straight after authorization. They still submit their schema default,
  // so the connector behaves exactly as it would have.
  const deferredFields = configFields.filter((field) => field.superseded_by === "folder_picker");
  const askedFields = configFields.filter((field) => field.superseded_by !== "folder_picker");
  // A connector's settings are three different questions on one form, so they are asked
  // as three: short scalars pair up, lists need their own width, and a bool is a switch.
  const scalarFields = askedFields.filter((field) => field.type !== "boolean" && field.type !== "list");
  const listFields = askedFields.filter((field) => field.type === "list");
  const toggleFields = askedFields.filter((field) => field.type === "boolean");
  const noun = privateNoun(connector);

  // Seeded from the schema so an untouched form submits what the connector would have
  // done anyway, and so a setting that defaults to on renders on rather than blank.
  useEffect(() => {
    setConf(Object.fromEntries((meta?.config_fields || []).map((field) => [field.name, field.type === "list" ? (field.default || []).join("\n") : field.default ?? ""])));
  }, [meta]);

  const principal = form.grant_principal.trim();
  const principalKind = principal.split(":")[0] || "group";
  // A mailbox or personal drive granted to a group publishes one person's whole corpus
  // to everyone in it, so the API refuses it without confirmation.
  const broadGrant = Boolean(connector.private_corpus && principal && ["group", "role"].includes(principalKind));
  const missingAuth = authFields.some((field) => field.required && !String(creds[field.name] || "").trim());
  const missingConfig = askedFields.some((field) => field.required && field.type !== "boolean" && !String(conf[field.name] ?? "").trim());

  const save = async (event) => {
    event.preventDefault(); setError(""); setBusy(true);
    try {
      const config = configFields.reduce((acc, field) => {
        const value = conf[field.name];
        if (field.type === "boolean") return { ...acc, [field.name]: Boolean(value) };
        // An emptied list is an instruction ("filter on nothing"), not an omission, so it
        // is sent as [] rather than dropped back onto the connector's default.
        if (field.type === "list") return { ...acc, [field.name]: String(value ?? "").split("\n").map((line) => line.trim()).filter(Boolean) };
        if (field.type === "integer") return String(value ?? "").trim() === "" ? acc : { ...acc, [field.name]: Number(value) };
        return value === undefined || value === "" ? acc : { ...acc, [field.name]: value };
      }, {});
      const body = { display_name: form.display_name, kind: connector.id, project_id: form.project_id || null, config, sync_policy: { mode: "continuous", interval: form.interval } };
      for (const field of authFields) if (creds[field.name]) body[field.name] = creds[field.name];
      if (principal) body.default_acl = [{ principal, principal_kind: principalKind, access: "allow" }];
      if (broadGrant) body.confirm_broad_grant = true;
      const result = await api("/api/sources", { method: "POST", body: JSON.stringify(body) });
      // An OAuth connector answers with a provider URL and creates nothing: the connection
      // only comes into existence when the callback returns. Going straight there means an
      // operator who never signs in leaves no trace, so there is nothing to strand.
      if (result?.authorization_url) { window.location.href = result.authorization_url; return; }
      onSaved();
    } catch (caught) { setError(caught.message); setBusy(false); }
  };

  const setConfValue = (name, value) => setConf((current) => ({ ...current, [name]: value }));
  const renderField = (field, value, onChange) => {
    const label = <>{field.title || field.name}{field.required ? " *" : ""}</>;
    if (field.type === "boolean") return <label className="toggle-row" key={field.name}><span><strong>{field.title || field.name}</strong>{field.description && <small>{field.description}</small>}</span><input type="checkbox" checked={Boolean(value)} onChange={(event) => onChange(field.name, event.target.checked)} /></label>;
    if (field.type === "list") return <label key={field.name}>{label}<textarea className="mono" rows={3} placeholder={fieldExample(field)} value={value ?? ""} onChange={(event) => onChange(field.name, event.target.value)} /><small className="field-hint"><b>One value per line.</b> {field.description}</small></label>;
    return <label key={field.name}>{label}<input type={field.secret ? "password" : field.type === "integer" ? "number" : "text"} className={field.secret ? "" : "mono"} min={field.type === "integer" ? 0 : undefined} placeholder={fieldExample(field)} value={value ?? ""} onChange={(event) => onChange(field.name, event.target.value)} />{field.description && <small>{field.description}</small>}</label>;
  };

  return <div className="modal-backdrop" onMouseDown={onClose}><form className="form-modal wide" onSubmit={save} onMouseDown={(event) => event.stopPropagation()}>
    <div className="drawer-header"><div><span className="eyebrow">{connector.category}</span><h2>Connect {connector.name}</h2><p>{isOAuth ? "Paste the credentials from your firm's OAuth app, then authorize in the browser." : "Credentials are stored encrypted on this appliance."}</p></div><button type="button" className="icon-button" onClick={onClose}><X size={17} /></button></div>

    {fields.loading && !meta && <div className="quiet-row"><RefreshCw size={15} /> Loading {connector.name} settings…</div>}
    {fields.error && <div className="form-error">Could not load connector settings: {fields.error.message} <button type="button" className="text-button" onClick={() => fields.reload().catch(() => {})}>Retry</button></div>}

    {meta && <>
      {meta.notes && <div className="form-note"><b>What this connection covers.</b> {meta.notes}</div>}
      {/* Only the fail-closed case earns space here. That a connector mirrors permissions
          is the norm and is stated on the catalog card; repeating it in the form was noise. */}
      {mirrorsAcls === false && <div className="notice-banner inline"><ShieldAlert size={14} /><div>
        <strong>{meta.name} reports no per-document permissions.</strong>
        <span>Without a grant below, everything it indexes is invisible: a search returns no results, not an error.</span>
      </div></div>}

      <label>Connection name<input required value={form.display_name} onChange={(event) => setForm({ ...form, display_name: event.target.value })} /></label>

      {authFields.length > 0 && <section className="form-section">
        <span className="form-section-label">{isOAuth ? "OAuth application" : "API access"}</span>
        {isOAuth && <RegistrationPanel meta={meta} redirectUri={redirectUri} docsUrl={docsUrl} connectorId={connector.id} />}
        {isOAuth && redirectUri && <div className="form-note">Redirect URI to register in the OAuth app: <code>{redirectUri}</code></div>}
        <div className="form-columns">{authFields.map((field) => renderField(field, creds[field.name], (name, value) => setCreds({ ...creds, [name]: value })))}</div>
      </section>}

      {configFields.length > 0 && <section className="form-section">
        <span className="form-section-label">What to sync</span>
        {scalarFields.length > 1 ? <div className="form-columns">{scalarFields.map((field) => renderField(field, conf[field.name], setConfValue))}</div> : scalarFields.map((field) => renderField(field, conf[field.name], setConfValue))}
        {/* Everything here already has a working default. Folded away so the three fields
            that actually need a human — name, client id, secret — are the whole form. */}
        {(listFields.length > 0 || toggleFields.length > 0) && <details className="advanced-options">
          <summary>Advanced options<small>{[listFields.length && `${listFields.length} list`, toggleFields.length && `${toggleFields.length} toggles`].filter(Boolean).join(" · ")}</small></summary>
          <div>
            {listFields.map((field) => renderField(field, conf[field.name], setConfValue))}
            {toggleFields.length > 0 && <div className="toggle-stack">{toggleFields.map((field) => renderField(field, conf[field.name], setConfValue))}</div>}
          </div>
        </details>}
      </section>}

      <section className="form-section">
        <span className="form-section-label">Access in Knowledge Index</span>
        <div className="form-columns">
          <label>Project (optional)<select value={form.project_id} onChange={(event) => setForm({ ...form, project_id: event.target.value })}><option value="">No project</option>{projects.map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}</select></label>
          <label>Grant access to<input className="mono" placeholder="group:ma-team" value={form.grant_principal} onChange={(event) => { setForm({ ...form, grant_principal: event.target.value }); setConfirmBroad(false); }} /><small>{mirrorsAcls === false ? "Required: without it these documents are unsearchable." : "Optional — source permissions already decide who sees what."}</small></label>
        </div>

        {/* Blank is the safe answer here, so an empty field gets an explanation rather than
            a warning; the alarm is kept for the grant that actually publishes the corpus. */}
        {connector.private_corpus && (broadGrant
          ? <div className="notice-banner inline"><AlertTriangle size={14} /><div>
            <strong>Everyone in {principal} could search this person's entire {noun}.</strong>
            <span>Including matters they are not staffed on. Name the owner instead — <code>user:j.weber@firm.example</code> — or leave it empty.</span>
          </div></div>
          : <div className="form-note"><b>{meta.name} indexes one person's {noun}.</b> Leave <b>Grant access to</b> empty and the mirrored permissions decide — normally the owner alone. A <code>group:</code> or <code>role:</code> principal publishes the whole {noun} and must be confirmed.</div>)}
        {broadGrant && <label className="toggle-row"><span><strong>I confirm this exposes one person's {noun} to {principal}</strong><small>Every member can search every document it indexes.</small></span><input type="checkbox" checked={confirmBroad} onChange={(event) => setConfirmBroad(event.target.checked)} /></label>}
      </section>
    </>}

    {error && <div className="form-error">{error}</div>}
    {isOAuth && meta && <p className="muted-copy drawer-note">You sign in at {meta.name} next. Nothing is saved until you do.</p>}

    <div className="modal-actions">
      <button type="button" className="text-button" onClick={onClose}>Cancel</button>
      <button className="primary-button" disabled={busy || !meta || missingAuth || missingConfig || (broadGrant && !confirmBroad)}><FolderSync size={15} /> {busy ? (isOAuth ? "Opening the provider…" : "Connecting…") : isOAuth ? "Authorize & connect" : "Connect"}</button>
    </div>
  </form></div>;
}

function plural(count, noun) {
  return `${(count ?? 0).toLocaleString()} ${noun}${count === 1 ? "" : "s"}`;
}

function relative(value) {
  if (!value) return "Never";
  const seconds = Math.round((Date.now() - new Date(value).getTime()) / 1000);
  if (seconds < 60) return "Just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return new Date(value).toLocaleDateString();
}
