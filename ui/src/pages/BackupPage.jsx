import { useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle, Archive, Check, CircleDashed, Clock, Copy, DatabaseBackup, HardDrive, KeyRound, RotateCcw, ListChecks, LoaderCircle, Lock, Play, Plus, RefreshCw, Save, Search, ShieldCheck, Trash2, X } from "lucide-react";
import { api } from "../api";
import { useApi } from "../hooks";
import { Badge, EmptyState, SectionHeading, Status } from "../components/Primitives";

// Each source flag maps to the component name(s) backup/components.py::plan() reports for
// it. The cross-reference is the point of the panel: a toggle that is on and whose
// component is not ready is a store the firm believes it is backing up and is not — the
// unmounted Keycloak volume nobody notices until a restore. `postgres/ki` is deliberately
// absent: it has no flag, because a backup without it is not a backup.
const SOURCES = [
  { key: "gateway_databases", title: "Gateway databases", why: "LiteLLM's spend ledger and Langfuse's model-call traces. Audit records a firm may be required to produce.", components: ["postgres/litellm", "postgres/langfuse"] },
  { key: "orchestrator_database", title: "Orchestrator database", why: "Hatchet's workflow definitions, run history and durable queue.", components: ["postgres/hatchet"] },
  { key: "search_index", title: "Search index", why: "Rebuildable from Postgres by re-embedding every chunk — which costs real money and hours.", components: ["opensearch/snapshot"] },
  { key: "artifact_blobs", title: "Artifact blobs", why: "Content-addressed originals. Re-fetchable only while the source still exists and still holds the file.", components: ["files/artifact-blobs"] },
  { key: "uploaded_files", title: "Uploaded files", why: "Uploaded through this console. There is no upstream copy — losing this loses the documents.", components: ["files/uploaded"] },
  { key: "connector_staging", title: "Connector staging", why: "Mid-sync scratch. Meaningful only to a scan already running, and a restore starts no scan.", components: ["files/connector-staging"] },
  { key: "identity_volume", title: "Identity volume", why: "Keycloak's data: users, sessions, client secrets, realm signing keys.", components: ["volumes/keycloak"] },
  { key: "orchestrator_config_volume", title: "Orchestrator config volume", why: "Hatchet's generated server config — what makes an already-issued client token valid.", components: ["volumes/hatchet-config"] },
  { key: "environment_secrets", title: "Deployment secrets", why: "The KI_* environment, above all KI_CONNECTOR_CREDENTIAL_KEY. Without it every restored credential is undecryptable ciphertext.", components: ["secrets/environment"], needsEncryption: true }
];

const RETENTION_FIELDS = [
  { key: "daily", label: "Daily", hint: "Newest N backups", max: 365 },
  { key: "weekly", label: "Weekly", hint: "Newest in each of the last N ISO weeks", max: 520 },
  { key: "monthly", label: "Monthly", hint: "Newest in each of the last N months", max: 120 },
  { key: "yearly", label: "Yearly", hint: "Newest in each of the last N years", max: 50 },
  { key: "min_keep", label: "Never fewer than", hint: "Floor under every rule above", min: 1, max: 100 }
];

export default function BackupPage({ identity }) {
  const admin = Boolean(identity?.is_admin);
  const config = useApi("/api/config", [], admin);
  // The most important request on the page: it is what says the share was unmounted in
  // March, and it is re-asked after every save because saving is what changes the answer.
  const preflight = useApi("/api/backup/preflight", [], admin);
  const backups = useApi("/api/backup/backups?limit=50", [], admin);
  const runs = useApi("/api/runs", [], admin);
  const [draft, setDraft] = useState(null);
  const [busy, setBusy] = useState("");
  const [note, setNote] = useState(null);
  const [startConfirm, setStartConfirm] = useState(false);
  const [pruneConfirm, setPruneConfirm] = useState(false);
  const [prune, setPrune] = useState(null);
  const [extra, setExtra] = useState("");
  // One open result per backup id: verifying two rows at once is fine, and each answer
  // belongs under the row it was asked about rather than in a modal that hides the table.
  const [rows, setRows] = useState({});
  // Secrets are held by the appliance, not by config.json, so they load and save on their
  // own path. Values are never returned — only whether one is set and its fingerprint.
  const secrets = useApi("/api/backup/secrets", [], admin);
  const [freshKey, setFreshKey] = useState(null);
  // Restoring reads from the configured destination by default, but a recovery onto fresh
  // hardware starts with a drive mounted somewhere this appliance has never heard of.
  const [restoreFrom, setRestoreFrom] = useState("");
  const [restoreList, setRestoreList] = useState(null);
  const [applyStores, setApplyStores] = useState({ databases: true, files: true, search_index: true, volumes: true });
  const [applyConfirm, setApplyConfirm] = useState("");
  const [elsewhere, setElsewhere] = useState(false);
  // The dismiss button stays disabled until the key has been copied. It is shown once,
  // and "I have saved it" clicked reflexively over a key nobody copied is how a firm
  // discovers, months later, that its backups cannot be opened.
  const [copied, setCopied] = useState(false);
  const [replaceKey, setReplaceKey] = useState(false);
  useEffect(() => { if (config.data) setDraft(structuredClone(config.data)); }, [config.data]);

  const backup = draft?.backup;
  const report = preflight.data;
  const componentsByName = useMemo(() => Object.fromEntries((report?.components || []).map((item) => [item.name, item])), [report]);
  // Backups and restores are both runs on this ledger and both are followed here. Filtering
  // to "backup" meant pressing Try it started a job the page then reported nothing about:
  // no banner, no polling, no row in the history.
  const backupRuns = useMemo(
    () => (runs.data || []).filter((row) => ["backup", "restore"].includes(String(row.workflow || ""))),
    [runs.data],
  );
  const inFlight = backupRuns.filter((row) => row.status === "queued" || row.status === "running");
  const running = inFlight[0];

  // A backup is minutes to hours of dumping and transferring; the request that starts one
  // returns as soon as the run row is reserved. Poll while something is actually in
  // flight, and only then — an idle appliance re-asking every five seconds is noise.
  useEffect(() => {
    if (!inFlight.length) return undefined;
    const timer = setInterval(() => { preflight.reload().catch(() => {}); runs.reload().catch(() => {}); }, 5000);
    return () => clearInterval(timer);
  }, [inFlight.length, preflight.reload, runs.reload]);
  // The destination listing only changes when a run ends, so it is refreshed on that edge
  // rather than on every poll — listing reads one manifest per backup from the share.
  const wasRunning = useRef(0);
  useEffect(() => {
    if (wasRunning.current && !inFlight.length) backups.reload().catch(() => {});
    wasRunning.current = inFlight.length;
  }, [inFlight.length, backups.reload]);

  const secretByName = useMemo(
    () => Object.fromEntries(((secrets.data || {}).secrets || []).map((item) => [item.name, item])),
    [secrets.data],
  );
  const keySet = Boolean(secretByName.encryption_key?.set);
  // Whether the page holds anything the Save button still has to write. The master switch
  // is at the top and its Save is at the bottom, so the page has to say so rather than
  // leave somebody to discover it by reloading.
  const dirty = Boolean(draft && config.data && JSON.stringify(draft.backup) !== JSON.stringify(config.data.backup));

  // The destination is presented as two plain questions — where, and whether to store only
  // what changed — rather than as three backend "kinds" nobody outside this codebase has
  // a reason to know about. restic is the deduplicating one; local and s3 are not.
  const kind = backup?.destination.kind;
  const efficient = kind === "restic";
  const place = kind === "s3" || (kind === "restic" && backup?.destination.bucket) ? "cloud" : "folder";
  const placeReady = place === "cloud" ? Boolean(backup?.destination.bucket) : Boolean(backup?.destination.path);
  const setKind = (next) => setDraft((current) => ({ ...current, backup: { ...current.backup, destination: { ...current.backup.destination, kind: next }, encrypt: next !== "restic" } }));
  const setPlace = (next) => {
    if (next === "cloud") setKind(efficient ? "restic" : "s3");
    else { setDraft((current) => ({ ...current, backup: { ...current.backup, destination: { ...current.backup.destination, bucket: "" } } })); setKind(efficient ? "restic" : "local"); }
  };
  const setEfficient = (next) => setKind(next ? "restic" : place === "cloud" ? "s3" : "local");
  const scheduleState = backup?.schedule.enabled
    ? (report?.schedule?.watcher_alive === false ? "Switched on, but nothing on this machine is watching the clock — see the status box above." : "A backup is taken every night at the time below.")
    : "Backups only happen when somebody presses “Back up now”.";

  const saveSecret = async (name, value) => {
    setBusy(`secret-${name}`);
    setNote(null);
    try {
      await api("/api/backup/secrets", { method: "POST", body: JSON.stringify({ name, value }) });
      await secrets.reload().catch(() => {});
      await preflight.reload().catch(() => {});
      setReplaceKey(false);
      setNote({ tone: "ok", text: value ? "Saved." : "Removed." });
      return true;
    } catch (error) {
      setNote({ tone: "bad", text: error.message });
      return false;
    } finally { setBusy(""); }
  };

  const generateKey = async () => {
    setBusy("genkey");
    setNote(null);
    try {
      const result = await api("/api/backup/generate-key", { method: "POST" });
      // Shown once, deliberately. A key that exists only on the machine the backups
      // protect cannot open them after the day that machine is gone.
      setFreshKey(result);
      setCopied(false);
      await secrets.reload().catch(() => {});
      await preflight.reload().catch(() => {});
      setReplaceKey(false);
    } catch (error) {
      setNote({ tone: "bad", text: error.message });
    } finally { setBusy(""); }
  };

  // The one control on this page that takes effect on click rather than on Save. A switch
  // at the top of a long page whose Save button is at the bottom is a switch that appears
  // to do something and does not survive a reload — which is exactly how it read. It
  // commits whatever else is on screen with it, because that is what somebody flipping it
  // means by "on".
  const toggleEnabled = async () => {
    if (!draft) return;
    const next = { ...draft, backup: { ...draft.backup, enabled: !draft.backup.enabled } };
    setDraft(next);
    setBusy("enable");
    setNote(null);
    try {
      await api("/api/config", { method: "PUT", body: JSON.stringify(next) });
      await config.reload();
      await preflight.reload().catch(() => {});
      setNote({ tone: "ok", text: next.backup.enabled ? "Backups are on." : "Backups are off." });
    } catch (error) {
      setDraft(draft);
      setNote({ tone: "bad", text: error.message });
    } finally { setBusy(""); }
  };

  const loadRestorable = async (path) => {
    setBusy("restore-list");
    setNote(null);
    try {
      const query = path ? `?path=${encodeURIComponent(path)}` : "";
      const result = await api(`/api/backup/restorable${query}`);
      setRestoreList(result.backups || []);
      if (!(result.backups || []).length) setNote({ tone: "bad", text: "No backups were found there." });
    } catch (error) {
      setRestoreList([]);
      setNote({ tone: "bad", text: error.message });
    } finally { setBusy(""); }
  };

  const startRestore = async (backupId, applying) => {
    setBusy("restore");
    setNote(null);
    setApplyConfirm("");
    try {
      const result = await api("/api/actions/restore", { method: "POST", body: JSON.stringify({
        backup_id: backupId,
        source_path: restoreFrom,
        apply_databases: applying && applyStores.databases,
        apply_files: applying && applyStores.files,
        apply_search_index: applying && applyStores.search_index,
        apply_volumes: applying && applyStores.volumes,
      }) });
      await runs.reload().catch(() => {});
      setRows((current) => ({
        ...current,
        [backupId]: {
          ...current[backupId],
          started: applying
            ? "Restoring now. Follow it in the banner at the top of this page, and do not use the appliance until it finishes."
            : "Checking it now. Nothing on this appliance changes. Follow it in the banner at the top of this page.",
        },
      }));
      setNote({ tone: "ok", text: applying
        ? `Restoring ${result.backup_id}. Do not use the appliance until it finishes.`
        : `Checking ${result.backup_id}. Nothing on this appliance is changed by it.` });
    } catch (error) {
      setNote({ tone: "bad", text: error.message });
    } finally { setBusy(""); }
  };

  const update = (key, value) => setDraft((current) => ({ ...current, backup: { ...current.backup, [key]: value } }));
  const updateIn = (section, key, value) => setDraft((current) => ({ ...current, backup: { ...current.backup, [section]: { ...current.backup[section], [key]: value } } }));
  // config.py refuses to save environment_secrets without encrypt (a 422 the operator can
  // do nothing with), so turning encryption off clears the flag here instead of letting
  // the save fail. The secrets toggle says why it is disabled rather than silently being so.
  const setEncrypt = (value) => setDraft((current) => ({ ...current, backup: { ...current.backup, encrypt: value, sources: { ...current.backup.sources, environment_secrets: value && current.backup.sources.environment_secrets } } }));

  const save = async () => {
    setBusy("save");
    setNote(null);
    try {
      // The whole AppConfig, never a fragment: it is validated as one object and a partial
      // body would drop every section this page does not render.
      await api("/api/config", { method: "PUT", body: JSON.stringify(draft) });
      await config.reload();
      await preflight.reload().catch(() => {});
      setNote({ tone: "ok", text: "Saved." });
    } catch (error) {
      setNote({ tone: "bad", text: error.message });
    } finally { setBusy(""); }
  };

  const startBackup = async (force) => {
    setBusy("start");
    setNote(null);
    setStartConfirm(false);
    try {
      const result = await api("/api/actions/backup", { method: "POST", body: JSON.stringify({ force: Boolean(force) }) });
      await runs.reload().catch(() => {});
      setNote({ tone: "ok", text: `Backup ${result.backup_id} started. It runs in the background; this page follows it.` });
    } catch (error) {
      setNote({ tone: "bad", text: error.message });
    } finally { setBusy(""); }
  };

  const runPrune = async (dryRun) => {
    setBusy(dryRun ? "prune-preview" : "prune");
    setNote(null);
    setPruneConfirm(false);
    try {
      const result = await api("/api/actions/backup-prune", { method: "POST", body: JSON.stringify({ dry_run: dryRun }) });
      setPrune(result);
      if (!dryRun) { await backups.reload().catch(() => {}); setNote({ tone: "ok", text: result.pruned.length ? `Deleted ${result.pruned.length} backup(s) from the destination.` : "Nothing matched the retention rules — nothing was deleted." }); }
    } catch (error) {
      setNote({ tone: "bad", text: error.message });
    } finally { setBusy(""); }
  };

  const askRow = async (backupId, kind) => {
    setRows((current) => ({ ...current, [backupId]: { ...current[backupId], busy: kind, error: null } }));
    try {
      const path = kind === "verify" ? "/api/actions/backup-verify" : "/api/actions/backup-restore-plan";
      const result = await api(path, { method: "POST", body: JSON.stringify({ backup_id: backupId }) });
      // Only ever one open result per row. Asking both questions used to stack two
      // near-identical ten-row inventories on top of each other.
      setRows((current) => ({ ...current, [backupId]: { busy: "", [kind]: result } }));
    } catch (error) {
      setRows((current) => ({ ...current, [backupId]: { ...current[backupId], busy: "", error: error.message } }));
    }
  };
  const closeRow = (backupId) => setRows((current) => ({ ...current, [backupId]: undefined }));

  const unsettled = report?.unsettled_documents || 0;
  const wouldRefuse = Boolean(backup?.require_settled_pipeline && unsettled);
  const addExtraPath = () => {
    const path = extra.trim();
    if (!path || backup.sources.extra_paths.includes(path)) { setExtra(""); return; }
    updateIn("sources", "extra_paths", [...backup.sources.extra_paths, path]);
    setExtra("");
  };

  return (
    <>
      <div className="hero-row compact-hero">
        <div className="bk-hero-left">
          <h1>Backup</h1>
          {backup && <button
            type="button"
            className={`bk-master ${backup.enabled ? "is-on" : ""}`}
            disabled={!admin || Boolean(busy)}
            onClick={toggleEnabled}
            title={backup.enabled ? "Backups can run. Click to switch off." : "Nothing is being backed up. Click to switch on."}
          >
            <span className="bk-master-dot" />
            {busy === "enable" ? "Saving…" : backup.enabled ? "On" : "Off"}
          </button>}
        </div>
        <div className="hero-actions">
          {startConfirm
            ? <div className="row-confirm"><button className="primary-button" disabled={!admin || Boolean(busy)} title={wouldRefuse ? `${unsettled.toLocaleString()} documents are mid-pipeline. Starting anyway captures them half-processed.` : ""} onClick={() => startBackup(wouldRefuse)}><Play size={15} /> {wouldRefuse ? `Start anyway — ${unsettled.toLocaleString()} mid-pipeline` : "Start backup"}</button><button className="text-button" onClick={() => setStartConfirm(false)}>Cancel</button></div>
            : <button className="primary-button" disabled={!admin || !backup?.enabled || Boolean(busy) || Boolean(running)} title={backup && !backup.enabled ? "Backups are switched off." : running ? "A backup is already running." : ""} onClick={() => setStartConfirm(true)}><DatabaseBackup size={15} /> Back up now…</button>}
          <button className="secondary-button" disabled={!admin || !draft || Boolean(busy)} onClick={save}><Save size={15} /> {busy === "save" ? "Saving…" : "Save configuration"}</button>
        </div>
      </div>

      {!admin && <div className="quiet-row"><Lock size={15} /> Backups are an administrator view.</div>}
      {note && <div className={`bk-note ${note.tone}`}>{note.tone === "bad" ? <AlertTriangle size={14} /> : <Check size={14} />}<span>{note.text}</span></div>}
      {running && <div className="bk-note running"><LoaderCircle size={14} className="spin" /><span>{running.workflow === "restore" ? (running.counters?.applying ? "Restoring" : "Checking a backup") : "Backing up"} — {running.current_step || "starting"}{running.counters?.backup_id ? ` · ${running.counters.backup_id}` : ""}. Started {relative(running.started_at)}.</span></div>}

      <StatusPanel report={report} loading={preflight.loading} error={preflight.error} enabled={backup?.enabled} lastRun={backupRuns[0]} onRefresh={() => { preflight.reload().catch(() => {}); backups.reload().catch(() => {}); }} />

      {backup && <section className="panel bk-panel bk-setup">
        <SectionHeading title="Set up backups" copy="Three things, in order. Nothing is backed up until all three are done." />

        <div className={`bk-step ${placeReady ? "is-done" : ""}`}>
          <span className="bk-step-n">{placeReady ? <Check size={15} /> : 1}</span>
          <div className="bk-step-body">
            <strong>Where should the copies go?</strong>
            <p className="bk-step-why">Somewhere this machine breaking cannot take with it. A drive on this machine is not a backup.</p>
            <div className="bk-choices">
              <button type="button" className={`bk-choice ${place === "folder" ? "is-on" : ""}`} disabled={!admin} onClick={() => setPlace("folder")}>
                <HardDrive size={18} /><strong>A folder</strong><small>A network drive, NAS, or plugged-in disk</small>
              </button>
              <button type="button" className={`bk-choice ${place === "cloud" ? "is-on" : ""}`} disabled={!admin} onClick={() => setPlace("cloud")}>
                <Archive size={18} /><strong>Cloud storage</strong><small>S3, MinIO, Wasabi, or similar</small>
              </button>
            </div>

            {place === "folder"
              ? <FolderPicker value={backup.destination.path} admin={admin} onPick={(next) => updateIn("destination", "path", next)} />
              : <>
                <div className="bk-field-row">
                  <label className="bk-field">Bucket name<input className="mono" disabled={!admin} placeholder="firm-backups" value={backup.destination.bucket} onChange={(event) => updateIn("destination", "bucket", event.target.value)} /></label>
                  <label className="bk-field">Server address<input className="mono" disabled={!admin} placeholder="leave empty for Amazon S3" value={backup.destination.endpoint_url} onChange={(event) => updateIn("destination", "endpoint_url", event.target.value)} /><small>For MinIO or Wasabi, paste their address here.</small></label>
                  <label className="bk-field">Region<input className="mono" disabled={!admin} value={backup.destination.region} onChange={(event) => updateIn("destination", "region", event.target.value)} /></label>
                </div>
                <div className="bk-field-row">
                  <SecretField label="Access key" name="s3_access_key_id" status={secretByName.s3_access_key_id} admin={admin} busy={busy} onSave={saveSecret} />
                  <SecretField label="Secret key" name="s3_secret_access_key" status={secretByName.s3_secret_access_key} admin={admin} busy={busy} onSave={saveSecret} />
                </div>
              </>}

            <label className="toggle-row bk-efficient">
              <span><strong>Only store what changed each night</strong><small>Recommended. Without it every night stores a complete second copy of everything, up to 19 of them under the default keep-rules.</small></span>
              <input type="checkbox" disabled={!admin} checked={efficient} onChange={(event) => setEfficient(event.target.checked)} />
            </label>
          </div>
        </div>

        <div className={`bk-step ${keySet ? "is-done" : ""}`}>
          <span className="bk-step-n">{keySet ? <Check size={15} /> : 2}</span>
          <div className="bk-step-body">
            <strong>The key that locks the backups</strong>
            <p className="bk-step-why">Backups hold client documents and leave this machine, so they are encrypted. Without this key nobody can open them — including us.</p>
            {freshKey && <div className="bk-freshkey">
              <div className="bk-freshkey-head"><KeyRound size={17} /><strong>Save this key now — it is not shown again</strong></div>
              <code className="bk-freshkey-value">{freshKey.key}</code>
              <p>{freshKey.warning}</p>
              <div className="bk-freshkey-actions">
                <button className="secondary-button small" onClick={() => { navigator.clipboard.writeText(freshKey.key); setCopied(true); }}><Copy size={13} /> {copied ? "Copied" : "Copy"}</button>
                <button className="primary-button small" disabled={!copied} title={copied ? "" : "Copy it first."} onClick={() => { setFreshKey(null); setCopied(false); }}><Check size={13} /> I have saved it somewhere safe</button>
              </div>
            </div>}
            {keySet
              ? <div className="bk-key-set">
                  <Check size={15} />
                  <div><strong>A key is set.</strong><small>Fingerprint <code>{secretByName.encryption_key?.fingerprint}</code></small></div>
                  <button className="text-button" disabled={!admin || Boolean(busy)} onClick={() => setReplaceKey((value) => !value)}>{replaceKey ? "Cancel" : "Replace…"}</button>
                </div>
              : <div className="bk-key-actions">
                  <button className="primary-button" disabled={!admin || Boolean(busy)} onClick={generateKey}><KeyRound size={15} /> {busy === "genkey" ? "Generating…" : "Generate a key for me"}</button>
                  <span className="bk-or">or</span>
                  <SecretField label="Paste a key you already have" name="encryption_key" status={secretByName.encryption_key} admin={admin} busy={busy} onSave={saveSecret} />
                </div>}
            {replaceKey && keySet && <div className="bk-key-replace">
              <div className="bk-warn"><AlertTriangle size={14} /><span>Changing the key does not re-encrypt the backups you already have. Those still need the old key — keep it.</span></div>
              <div className="bk-key-actions">
                <button className="secondary-button" disabled={!admin || Boolean(busy)} onClick={generateKey}><KeyRound size={15} /> Generate a new one</button>
                <span className="bk-or">or</span>
                <SecretField label="Paste a different key" name="encryption_key" status={null} admin={admin} busy={busy} onSave={saveSecret} />
              </div>
            </div>}
          </div>
        </div>

        <div className={`bk-step ${backup.schedule.enabled ? "is-done" : ""}`}>
          <span className="bk-step-n">{backup.schedule.enabled ? <Check size={15} /> : 3}</span>
          <div className="bk-step-body">
            <strong>When should they run?</strong>
            <p className="bk-step-why">Leave this off and backups only happen when you press the button.</p>
            <label className="toggle-row"><span><strong>Run automatically, every night</strong><small>{scheduleState}</small></span><input type="checkbox" disabled={!admin} checked={backup.schedule.enabled} onChange={(event) => updateIn("schedule", "enabled", event.target.checked)} /></label>
            {backup.schedule.enabled && <div className="bk-field-row">
              <label className="bk-field">Time<input type="time" disabled={!admin} value={`${String(backup.schedule.hour).padStart(2, "0")}:${String(backup.schedule.minute).padStart(2, "0")}`} onChange={(event) => { const [h, m] = event.target.value.split(":"); updateIn("schedule", "hour", Number(h) || 0); updateIn("schedule", "minute", Number(m) || 0); }} /><small>Pick a quiet hour — backups are heavy.</small></label>
              <label className="bk-field">Timezone<select className="mono" disabled={!admin} value={backup.schedule.timezone} onChange={(event) => updateIn("schedule", "timezone", event.target.value)}>{TIMEZONES.map((zone) => <option key={zone} value={zone}>{zone}</option>)}</select><small>{scheduleLocal(backup.schedule)}</small></label>
            </div>}
          </div>
        </div>

        <div className="bk-save-row">
          <button className="primary-button" disabled={!admin || !draft || Boolean(busy)} onClick={save}><Save size={15} /> {busy === "save" ? "Saving…" : "Save these settings"}</button>
          <small>{dirty ? "You have unsaved changes on this page." : "Keys are saved the moment you set them. Everything else is saved with this button."}</small>
        </div>
      </section>}

      {backup && <details className="panel bk-panel bk-advanced">
        <summary><ListChecks size={15} /> Advanced — what is captured, how long copies are kept, and the safety limits</summary>

        <SectionHeading title="What is captured" copy="Everything switched on here is captured on every run. Turning one off is a statement that the firm accepts losing it." action={<span className="table-count">{SOURCES.filter((item) => backup.sources[item.key]).length + 1} of {SOURCES.length + 1} stores</span>} />
        <div className="bk-always"><ShieldCheck size={14} /><div><strong>The index itself — always captured</strong><small>Documents, permissions, the audit trail and the saved connector logins. There is no switch for it: a backup without it is not a backup.{componentsByName["postgres/ki"]?.problem ? ` It is not ready: ${componentsByName["postgres/ki"].problem}` : ""}</small></div>{componentsByName["postgres/ki"] && <Status value={componentsByName["postgres/ki"].ready ? "ok" : "error"} />}</div>
        <div className="bk-sources">
          {SOURCES.map((item) => {
            const on = Boolean(backup.sources[item.key]);
            // The plan only lists a component while its flag is on, so a missing entry
            // after a toggle simply means the change has not been saved yet.
            const found = item.components.map((name) => componentsByName[name]).filter(Boolean);
            const broken = on ? found.filter((component) => !component.ready) : [];
            // restic encrypts for itself, so 'encrypt' being off there is not unprotected.
            const blocked = item.needsEncryption && !(backup.encrypt || backup.destination.kind === "restic");
            return <label className={`toggle-row bk-source ${on && broken.length ? "is-broken" : ""}`} key={item.key}>
              <span>
                <strong>{item.title}{on && broken.length ? <Badge tone="red">not ready</Badge> : null}</strong>
                <small>{item.why}</small>
                {blocked && <small className="bk-blocked"><Lock size={10} /> Requires encryption, which is on unless you turned it off.</small>}
                {broken.map((component) => <small className="bk-broken" key={component.name}><AlertTriangle size={10} /> <code>{component.name}</code> {component.problem}</small>)}
                {on && !broken.length && found.length > 0 && <small className="bk-ready"><Check size={10} /> {found.map((component) => component.name).join(", ")} ready{describeDetail(found)}</small>}
              </span>
              <input type="checkbox" disabled={!admin || blocked} checked={on} onChange={(event) => updateIn("sources", item.key, event.target.checked)} />
            </label>;
          })}
        </div>
        <label className="bk-field" style={{ marginTop: "12px" }}>Extra folders<input className="mono" disabled={!admin} placeholder="/data/something-else, /data/another" value={extra || (backup.sources.extra_paths || []).join(", ")} onChange={(event) => setExtra(event.target.value)} onBlur={() => { updateIn("sources", "extra_paths", extra.split(",").map((item) => item.trim()).filter(Boolean)); setExtra(""); }} /><small>Anything else on disk this deployment keeps. Comma separated.</small></label>

        <SectionHeading title="How long copies are kept" copy="A copy is kept if it is one of the newest daily, or the newest in one of the recent weeks, months or years. Counting periods rather than days means a machine switched off for a fortnight still keeps its history." />
        <div className="form-columns bk-retention">
          {RETENTION_FIELDS.map((field) => <label key={field.key}>{field.label}<input type="number" min={field.min ?? 0} max={field.max} disabled={!admin} value={backup.retention[field.key]} onChange={(event) => updateIn("retention", field.key, Number(event.target.value))} /><small>{field.hint}</small></label>)}
        </div>
        <label className="toggle-row" style={{ marginTop: "12px" }}><span><strong>Delete old copies automatically after each backup</strong><small>What keeps a nightly schedule affordable — and what deletes the firm's only off-machine copy if the rules are wrong. The newest backup is never deleted.</small></span><input type="checkbox" disabled={!admin} checked={backup.retention.prune_enabled} onChange={(event) => updateIn("retention", "prune_enabled", event.target.checked)} /></label>
        <div className="rebuild-row">
          <div><strong><Trash2 size={13} /> Apply the keep-rules now</strong><small>Preview first: it reports what would be deleted without touching anything. Deleting is permanent and it removes copies that live off this machine — the ones a recovery depends on. Save first, or the preview answers for the saved rules.</small></div>
          {pruneConfirm
            ? <div className="row-confirm"><button className="secondary-button small" disabled={!admin || Boolean(busy)} onClick={() => runPrune(false)}><Trash2 size={13} /> Confirm — delete {prune?.pruned?.length ? `${prune.pruned.length} backup(s)` : "them"}</button><button className="text-button" onClick={() => setPruneConfirm(false)}>Cancel</button></div>
            : <div className="row-confirm"><button className="secondary-button small" disabled={!admin || Boolean(busy)} onClick={() => runPrune(true)}><ListChecks size={13} /> {busy === "prune-preview" ? "Checking…" : "Preview"}</button><button className="secondary-button small" disabled={!admin || Boolean(busy)} onClick={() => setPruneConfirm(true)}><Trash2 size={13} /> Delete old copies…</button></div>}
        </div>
        {prune && <div className="bk-prune">
          <strong>{prune.dry_run ? "Preview" : "Deleted"} — {prune.total} backup(s) at the destination, {prune.kept} kept, {prune.pruned.length} {prune.dry_run ? "would be deleted" : "deleted"}.</strong>
          <div className="bk-prune-list">{(prune.decisions || []).map((decision) => <span className={decision.keep ? "" : "drop"} key={decision.backup_id}><code>{decision.backup_id}</code><small>{decision.taken_at ? new Date(decision.taken_at).toLocaleString() : "unreadable name"}</small><em>{decision.keep ? (decision.reasons || []).join(", ") || "kept" : prune.dry_run ? "would be deleted" : "deleted"}</em></span>)}</div>
        </div>}

        <SectionHeading title="Safety limits" />
        <label className="toggle-row"><span><strong>Wait for the appliance to be idle</strong><small>A backup taken mid-import holds an index that knows about files it has not finished storing. “Back up now” can override this; the nightly run waits instead.</small></span><input type="checkbox" disabled={!admin} checked={backup.require_settled_pipeline} onChange={(event) => update("require_settled_pipeline", event.target.checked)} /></label>
        {backup.schedule.enabled && <div className="form-columns" style={{ marginTop: "12px" }}>
          <label>Give up waiting after (minutes)<input type="number" min="0" max="1440" disabled={!admin} value={backup.schedule.defer_limit_minutes} onChange={(event) => updateIn("schedule", "defer_limit_minutes", Number(event.target.value))} /><small>0–1440. A machine that is never idle would otherwise never be backed up, and nothing would say so.</small></label>
          <label>Largest single store (GB)<input type="number" min="1" max="10000" disabled={!admin} value={backup.max_component_gb} onChange={(event) => update("max_component_gb", Number(event.target.value))} /><small>A guard against a runaway archive, not a total.</small></label>
        </div>}
        {!backup.schedule.enabled && <div className="form-columns" style={{ marginTop: "12px" }}><label>Largest single store (GB)<input type="number" min="1" max="10000" disabled={!admin} value={backup.max_component_gb} onChange={(event) => update("max_component_gb", Number(event.target.value))} /><small>A guard against a runaway archive, not a total.</small></label></div>}
      </details>}
      <section className="panel bk-panel">
        <SectionHeading
          title="Your backups"
          copy="Read from the backups themselves, never from a list this appliance keeps — during a recovery what matters is what is actually on the drive."
          action={<span className="table-count">{(restoreList || backups.data || []).length} backup(s)</span>}
        />
        <div className="bk-restore-source">
          <div className="bk-restore-source-body">
            <HardDrive size={15} />
            <span>In <code>{restoreFrom || report?.destination?.location || "the configured destination"}</code></span>
            <button className="text-button" onClick={() => setElsewhere((value) => !value)}>{elsewhere ? "Cancel" : "Look somewhere else…"}</button>
          </div>
          {/* Only when asked for. A recovery onto fresh hardware needs to point at a drive
              this appliance has never seen; every other restore reads what is configured,
              and an empty folder picker beside a working default reads as unfinished. */}
          {elsewhere && <div className="bk-restore-elsewhere">
            <FolderPicker value={restoreFrom} admin={admin} label="a folder holding backups" onPick={(next) => { setRestoreFrom(next); setElsewhere(false); loadRestorable(next); }} />
            {restoreFrom && <button className="text-button" onClick={() => { setRestoreFrom(""); setRestoreList(null); setElsewhere(false); }}>Back to the configured destination</button>}
          </div>}
        </div>
        {backups.error
          ? <EmptyState title="The destination could not be listed" copy={backups.error.message} action={<button className="secondary-button" onClick={() => backups.reload().catch(() => {})}><RefreshCw size={14} /> Try again</button>} />
          : (backups.data || []).length
            ? <div className="data-table">
              <div className="table-head backup-head"><span>Backup</span><span>Taken</span><span>Components</span><span>Size</span><span>Encryption</span><span /></div>
              {backups.data.map((item) => {
                const state = rows[item.backup_id] || {};
                // Wrapped, because the verify/plan answer has to appear directly under the
                // row it was asked about and a CSS-grid row cannot hold a full-width child.
                return <div className="bk-row" key={item.backup_id}>
                  <div className={`table-row backup-head ${item.complete ? "" : "bk-incomplete"}`}>
                    <span className="primary-cell"><i className="bk-icon">{item.complete ? <Archive size={15} /> : <AlertTriangle size={15} />}</i><span><strong className="mono">{item.backup_id}</strong>{item.complete ? <small>{item.appliance?.package_version ? `v${item.appliance.package_version}` : "version not recorded"}{item.appliance?.index_name ? ` · ${item.appliance.index_name}` : ""}</small> : <small>{item.problem || "no manifest — this backup was never finished"}</small>}</span></span>
                    <span className="plain-cell">{item.created_at ? new Date(item.created_at).toLocaleString() : "—"}</span>
                    <span className="mono plain-cell">{item.complete ? (item.components || []).length : "—"}</span>
                    <span className="plain-cell"><strong className="mono">{item.complete ? bytes(item.total_stored_bytes) : "—"}</strong>{item.complete && item.total_plaintext_bytes ? <small>{bytes(item.total_plaintext_bytes)} before compression</small> : null}</span>
                    <span>{item.complete ? (item.encrypted ? <Badge tone="green">encrypted</Badge> : <Badge tone="red">plaintext</Badge>) : <Badge tone="red">incomplete</Badge>}</span>
                    <span className="plain-cell bk-row-actions">
                      {/* An incomplete backup has no manifest, so there is nothing to verify
                          against and nothing a restore could follow. Offering the buttons
                          would suggest it is a candidate for recovery; it is not. */}
                      {item.complete && <>
                        <button className="secondary-button small" disabled={!admin || Boolean(state.busy)} onClick={() => askRow(item.backup_id, "verify")} title="Reads the whole backup back and re-checks it. Changes nothing."><Search size={13} /> {state.busy === "verify" ? "Checking…" : "Check it"}</button>
                        <button className="secondary-button small" disabled={!admin || Boolean(state.busy) || Boolean(running)} onClick={() => askRow(item.backup_id, "plan")} title="See what restoring this would do."><RotateCcw size={13} /> {state.busy === "plan" ? "Reading…" : "Restore from this…"}</button>
                      </>}
                    </span>
                  </div>
                  {(state.verify || state.plan || state.error) && <div className="bk-detail">
                    <button className="bk-detail-close" title="Close" onClick={() => closeRow(item.backup_id)}><X size={13} /></button>
                    {state.error && <p className="bk-detail-bad"><AlertTriangle size={13} /> {state.error}</p>}
                    {state.started && running && <p className="bk-detail-note"><LoaderCircle size={13} className="spin" /> {state.started}</p>}
                    {state.verify && <VerifyResult result={state.verify} />}
                    {state.plan && <>
                      <RestorePlan plan={state.plan} />
                      {state.plan.blockers?.length
                        ? <div className="bk-warn"><AlertTriangle size={14} /><span>This backup cannot be restored onto this appliance until the problems above are resolved.</span></div>
                        : <div className="bk-restore-choices">
                            <div className="bk-restore-choice">
                              <div><strong>Try it first</strong><small>Copies the whole backup out and checks every part of it opens. Nothing on this appliance changes. This is how you find out a backup works before the day you need it to.</small></div>
                              <button className="secondary-button" disabled={Boolean(busy) || Boolean(running)} onClick={() => startRestore(item.backup_id, false)}><ShieldCheck size={14} /> {busy === "restore" ? "Starting…" : running ? "Something is already running" : "Try it — changes nothing"}</button>
                            </div>
                            <div className="bk-restore-choice is-danger">
                              <div>
                                <strong>Put it back</strong>
                                <small>Replaces what is on this appliance now with what is in this backup. It cannot be undone. Stop using the appliance first.</small>
                                <div className="bk-restore-stores">
                                  {RESTORE_STORES.map((store) => (
                                    <label className="toggle-row" key={store.key}>
                                      <span><strong>{store.title}</strong><small>{store.why}</small></span>
                                      <input type="checkbox" checked={applyStores[store.key]} onChange={(event) => setApplyStores((current) => ({ ...current, [store.key]: event.target.checked }))} />
                                    </label>
                                  ))}
                                </div>
                                {state.plan?.steps?.some((step) => !step.restorable_here) && <small className="bk-restore-offline"><AlertTriangle size={12} /> Sign-in and the orchestrator cannot be replaced right now — the restore agent is not reachable, and those containers have to be stopped by something. <code className="mono">scripts/restore-backup.sh</code> does the whole stack.</small>}
                              </div>
                              {applyConfirm === item.backup_id
                                ? <div className="row-confirm"><button className="primary-button" disabled={Boolean(busy)} onClick={() => startRestore(item.backup_id, true)}><AlertTriangle size={14} /> Yes, overwrite this appliance</button><button className="text-button" onClick={() => setApplyConfirm("")}>Cancel</button></div>
                                : <button className="secondary-button" disabled={Boolean(busy) || Boolean(running) || !Object.values(applyStores).some(Boolean)} onClick={() => setApplyConfirm(item.backup_id)}><RotateCcw size={14} /> Put it back…</button>}
                            </div>
                          </div>}
                    </>}
                  </div>}
                </div>;
              })}
            </div>
            : <EmptyState title={admin ? "Nothing at the destination yet" : "Administrators only"} copy={admin ? "A backup appears here once one has been written and its manifest closed." : "Backups are an administrator view."} />}
      </section>

      {admin && <section className="panel bk-panel">
        <SectionHeading title="Recent backups and restores" action={<span className="table-count">{backupRuns.length} run(s)</span>} />
        {backupRuns.length ? <div className="data-table">
          <div className="table-head bk-run-head"><span>Run</span><span>Started</span><span>Result</span><span>Status</span></div>
          {backupRuns.map((row) => <div className="table-row bk-run-head" key={row.id}>
            <span className="primary-cell"><i className="bk-icon"><DatabaseBackup size={15} /></i><span><strong className="mono">{row.counters?.backup_id || String(row.id || "").slice(0, 8)}</strong><small>{row.counters?.trigger ? `triggered ${row.counters.trigger}` : "manual"}</small></span></span>
            <span className="plain-cell">{row.started_at ? new Date(row.started_at).toLocaleString() : "—"}</span>
            <span>{row.error ? <span className="ledger-fail"><strong>{outcomeHeadline(row.error)}</strong><small>{row.error.message || "Nothing was recorded about why."}</small></span> : <span className="counter-chips">{runChips(row.counters).map((chip) => <span key={chip.id}><b>{chip.value}</b>{chip.label}</span>)}</span>}</span>
            <span><Status value={runStatus(row)} /></span>
          </div>)}
        </div> : <div className="quiet-row"><CircleDashed size={16} /> No backup has ever run on this appliance.</div>}
      </section>}
    </>
  );
}

/**
 * The panel this page exists for. A schedule that has been writing to an unmounted share
 * since March fails silently in every other view; here the destination says so on its own
 * line, before anything else on the page.
 */
function StatusPanel({ report, loading, error, enabled, lastRun, onRefresh }) {
  if (error) return <section className="panel bk-panel"><SectionHeading title="Status" /><EmptyState title="Preflight could not run" copy={error.message} /></section>;
  if (!report) return <section className="panel bk-panel"><SectionHeading title="Status" /><div className="quiet-row"><CircleDashed size={16} /> {loading ? "Checking the destination…" : "Administrators only."}</div></section>;
  const destination = report.destination || {};
  const encryption = report.encryption || {};
  const last = report.last_backup;
  const schedule = report.schedule || {};
  const tone = !enabled ? "is-off" : report.ok ? "is-clear" : "is-bad";
  return (
    <section className="panel bk-panel bk-status">
      <SectionHeading title="Status" copy="Checked now, against the destination as it is at this moment." action={<button className="secondary-button small" onClick={onRefresh}><RefreshCw size={13} /> Re-check</button>} />
      <div className={`bk-verdict ${tone}`}>
        <span>{!enabled ? <CircleDashed size={17} /> : report.ok ? <Check size={17} /> : <AlertTriangle size={17} />}</span>
        <div><strong>{!enabled ? "Backups are switched off" : report.ok ? "Ready — the next backup would run" : `${report.problems.length} problem${report.problems.length === 1 ? "" : "s"} would stop the next backup`}</strong><span>{!enabled ? "Nothing is being written anywhere. Turn them on below once a destination is configured." : report.ok ? "The destination is writable and every enabled store is ready." : "Fix these before relying on the schedule."}</span></div>
      </div>

      {report.problems.length > 0 && <ul className="bk-problems">{report.problems.map((problem) => <li key={problem}><AlertTriangle size={13} /><span>{problem}</span></li>)}</ul>}
      {report.warnings.length > 0 && <ul className="bk-warnings">{report.warnings.map((warning) => <li key={warning}><CircleDashed size={13} /><span>{warning}</span></li>)}</ul>}

      <div className="bk-facts">
        <div><span><Clock size={12} /> Schedule</span><strong>{schedule.enabled ? `${schedule.at} ${schedule.timezone || "UTC"}` : "Manual only"}</strong><small>{!schedule.enabled ? "Nothing runs unless somebody presses the button." : schedule.watcher_alive ? "A scheduler is watching the clock." : "Switched on, but nothing is watching the clock — no backup will run on its own."}</small>{schedule.enabled ? <Status value={schedule.watcher_alive ? "ok" : "error"} /> : null}</div>
        <div><span><HardDrive size={12} /> Destination</span><strong className="mono">{destination.location || destination.error || "not configured"}</strong><small>{describeDestination(destination)}{destination.endpoint_url ? ` · ${destination.endpoint_url}` : ""}{destination.region ? ` · ${destination.region}` : ""}</small>{"writable" in destination ? <Status value={destination.writable ? "ok" : "error"} /> : null}</div>
        <div><span><HardDrive size={12} /> Free space</span><strong className="mono">{destination.free_bytes === null || destination.free_bytes === undefined ? "n/a" : bytes(destination.free_bytes)}</strong><small>{destination.total_bytes ? `of ${bytes(destination.total_bytes)}` : "object storage reports no quota"}</small></div>
        <div><span><KeyRound size={12} /> Encryption</span><strong className="mono">{encryption.enabled ? encryption.key_fingerprint || "key unreadable" : "off"}</strong><small>{!encryption.enabled ? "backups leave this appliance in the clear" : encryption.performed_by === "destination" ? "held by the destination" : encryption.key_set ? "set in Security below" : "no key set yet"}</small></div>
        <div><span><Clock size={12} /> Last backup</span><strong className="mono">{last ? last.status : "never"}</strong><small>{last ? `${last.finished_at ? relative(last.finished_at) : last.started_at ? `started ${relative(last.started_at)}` : "no timestamp"}${last.counters?.bytes_stored ? ` · ${bytes(last.counters.bytes_stored)}` : ""}` : "nothing has ever been written"}</small>{last?.error ? <small className="bk-broken">{last.error.message || last.error.class || "failed"}</small> : null}</div>
        <div><span><DatabaseBackup size={12} /> Staging</span><strong className="mono">{report.staging?.path || "—"}</strong><small>{report.staging?.exists ? "present — one component at a time is written here" : "created on the first run"}</small></div>
        <div><span><CircleDashed size={12} /> Mid-pipeline</span><strong className="mono">{(report.unsettled_documents || 0).toLocaleString()}</strong><small>{report.unsettled_documents ? "documents still moving — a backup taken now is internally ragged" : "everything is settled"}</small></div>
      </div>
    </section>
  );
}

/** Per component, because "verify failed" without saying which store is unusable is a
 *  sentence an operator cannot act on at two in the morning. */
/** Pick a folder by clicking through what the appliance can actually see.
 *
 * Typing a path means typing where a folder lives *inside this container*, which is not
 * something the person configuring backups has any reason to know, and it cannot be
 * checked until the first backup fails on it. This lists the volumes this appliance has
 * been given, with free space, and walks into them. Writability is answered by writing,
 * because a folder that looks fine and is mounted read-only is exactly the failure this
 * replaces.
 */
function FolderPicker({ value, admin, onPick, label }) {
  const [open, setOpen] = useState(false);
  const [at, setAt] = useState(null);
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [creating, setCreating] = useState("");

  const load = async (path) => {
    setBusy(true);
    setError(null);
    try {
      const query = path ? `?path=${encodeURIComponent(path)}` : "";
      const result = await api(`/api/backup/folders${query}`);
      setData(result);
      setAt(result.path || null);
    } catch (exception) {
      setError(exception.message);
    } finally { setBusy(false); }
  };

  useEffect(() => { if (open && !data) load(value || null).catch(() => {}); }, [open]);

  const makeFolder = async () => {
    if (!creating.trim() || !at) return;
    setBusy(true);
    setError(null);
    try {
      const result = await api("/api/backup/folders", { method: "POST", body: JSON.stringify({ path: at, name: creating.trim() }) });
      setData(result);
      setCreating("");
    } catch (exception) {
      setError(exception.message);
    } finally { setBusy(false); }
  };

  return (
    <div className="bk-picker">
      <div className="bk-picked">
        <HardDrive size={15} />
        <div><strong>{value || (label ? "Nothing chosen" : "No folder chosen yet")}</strong><small>{value ? (label ? "Backups are read from here." : "Backups are written here.") : (label ? "Point this at a drive you have mounted." : "Pick where the copies should go.")}</small></div>
        <button type="button" className="secondary-button small" disabled={!admin} onClick={() => setOpen((current) => !current)}>{open ? "Close" : value ? "Change…" : "Choose a folder…"}</button>
      </div>

      {open && <div className="bk-browser">
        {error && <div className="bk-warn"><AlertTriangle size={14} /><span>{error}</span></div>}

        {!at && <div className="bk-places">
          {(data?.places || []).map((place) => (
            <button type="button" key={place.path} className="bk-place" disabled={busy} onClick={() => load(place.path)}>
              <HardDrive size={17} />
              <div><strong>{place.label}</strong><small><code>{place.path}</code></small></div>
              <span className="bk-place-free">{place.free_bytes != null ? `${bytes(place.free_bytes)} free` : ""}{place.writable ? "" : " · read-only"}</span>
            </button>
          ))}
          {!busy && !(data?.places || []).length && <EmptyState title="No drives are mounted" copy="Mount the drive you want backups on into this container, and it will appear here." />}
        </div>}

        {at && <>
          <div className="bk-crumbs">
            <button type="button" className="text-button" disabled={busy} onClick={() => { setAt(null); load(null); }}>All drives</button>
            <span>/</span>
            <code>{at}</code>
            {data?.parent && <button type="button" className="text-button" disabled={busy} onClick={() => load(data.parent)}>Up one</button>}
          </div>

          <div className="bk-folder-list">
            {(data?.entries || []).map((entry) => (
              <button type="button" key={entry.path} className="bk-folder" disabled={busy} onClick={() => load(entry.path)}>
                <strong>{entry.name}</strong>
                <small>{entry.empty ? "empty" : "has contents"}{entry.writable ? "" : " · read-only"}</small>
              </button>
            ))}
            {!busy && !(data?.entries || []).length && <span className="bk-folder-empty">No sub-folders here.</span>}
          </div>

          <div className="bk-picker-actions">
            <span className="bk-new-folder">
              <input placeholder="New folder name" value={creating} disabled={busy} onChange={(event) => setCreating(event.target.value)} />
              <button type="button" className="secondary-button small" disabled={busy || !creating.trim()} onClick={makeFolder}><Plus size={13} /> Create</button>
            </span>
            <button type="button" className="primary-button small" disabled={busy || !data?.writable} title={data?.writable ? "" : "This folder is read-only, so backups cannot be written to it."} onClick={() => { onPick(at); setOpen(false); }}>
              <Check size={13} /> Use this folder
            </button>
          </div>
          {data && !data.writable && <div className="bk-warn"><AlertTriangle size={14} /><span>This folder is read-only for the appliance, so backups cannot be written here.</span></div>}
        </>}
      </div>}
    </div>
  );
}

/** A password box for a secret the appliance holds and never gives back.
 *
 * Shows whether one is set and its fingerprint, never a value — a secret an API can
 * return is a secret that ends up in a browser history, a proxy log and a screenshot.
 */
function SecretField({ label, name, status, admin, busy, onSave }) {
  const [value, setValue] = useState("");
  const working = busy === `secret-${name}`;
  return (
    <label className="bk-field bk-secret">
      {label}
      <span className="bk-secret-row">
        <input
          type="password"
          className="mono"
          autoComplete="new-password"
          disabled={!admin || working}
          placeholder={status?.set ? "•••••••• — set" : "paste it here"}
          value={value}
          onChange={(event) => setValue(event.target.value)}
        />
        <button type="button" className="secondary-button small" disabled={!admin || working || !value.trim()} onClick={async () => { if (await onSave(name, value.trim())) setValue(""); }}>{working ? "Saving…" : "Save"}</button>
      </span>
      {status?.set && <small>Set · fingerprint <code>{status.fingerprint}</code></small>}
    </label>
  );
}

function VerifyResult({ result }) {
  return <div className="bk-result">
    <div className="bk-result-head">
      <strong>{result.ok ? <><Check size={13} /> This backup is readable</> : <><AlertTriangle size={13} /> This backup failed its check</>}</strong>
      <span>{result.checked} store(s) read back{result.deep ? " and decrypted" : " (checksums only)"}</span>
    </div>
    {result.problems?.length > 0 && <ul className="bk-problems">{result.problems.map((problem) => <li key={problem}><AlertTriangle size={13} /><span>{problem}</span></li>)}</ul>}
    {result.warnings?.length > 0 && <ul className="bk-warnings">{result.warnings.map((warning) => <li key={warning}><CircleDashed size={13} /><span>{warning}</span></li>)}</ul>}
    <details className="bk-fold">
      <summary>Store by store</summary>
      <div className="bk-component-list">{(result.components || []).map((component) => <div key={component.name} className={component.ok ? "" : "bad"}>
        <span>{component.ok ? <Check size={12} /> : <AlertTriangle size={12} />}<code>{component.name}</code></span>
        <small>{bytes(component.stored_bytes)}{component.decrypted ? " · decrypted" : ""}</small>
        {component.problems?.length > 0 && <em>{component.problems.join(" · ")}</em>}
      </div>)}</div>
    </details>
  </div>;
}

/** A report, never a button. Restoring overwrites live databases and volumes with the
 *  containers stopped, so it lives in the CLI where an operator has to mean it. */
/** What restoring this backup would do, said in one line.
 *
 * It used to open with ten rows naming every component and the command that would restore
 * it. None of that helps somebody decide; it is what you want *after* deciding, when
 * something has gone wrong. The verdict, the problems and the two things you can do come
 * first, and the inventory folds away underneath.
 */
function RestorePlan({ plan }) {
  const steps = plan.steps || [];
  const offline = steps.filter((step) => !step.restorable_here);
  const total = steps.reduce((sum, step) => sum + (step.bytes || 0), 0);
  return <div className="bk-result">
    <div className="bk-result-head">
      <strong>{plan.ok ? <><Check size={13} /> This backup can be restored</> : <><AlertTriangle size={13} /> This backup cannot be restored here</>}</strong>
      <span>{steps.length} stores · {bytes(total)}{plan.created_at ? ` · taken ${new Date(plan.created_at).toLocaleString()}` : ""}</span>
    </div>
    {/* The count of what a *restore from here* covers, said separately from what the
        backup holds. Folding the two into "8 of 10 stores" read as though the backup
        were missing two, when what it means is that two of them cannot be written back
        into a stack that is running. */}
    {offline.length > 0 && <p className="bk-result-note">
      <Lock size={12} />
      <span>{steps.length - offline.length} of these can be put back from this page. The other {offline.length === 1 ? "one" : offline.length} ({offline.map((step) => step.name).join(", ")}) belong to containers that have to be stopped first — they are in the backup, they just cannot be written while the stack is up. <code className="mono">scripts/restore-backup.sh</code> does those.</span>
    </p>}
    {plan.blockers?.length > 0 && <ul className="bk-problems">{plan.blockers.map((blocker) => <li key={blocker}><AlertTriangle size={13} /><span>{blocker}</span></li>)}</ul>}
    {plan.warnings?.length > 0 && <ul className="bk-warnings">{plan.warnings.map((warning) => <li key={warning}><CircleDashed size={13} /><span>{warning}</span></li>)}</ul>}
    <details className="bk-fold">
      <summary>Store by store</summary>
      <div className="bk-component-list">{steps.map((step) => <div key={step.name} className={step.restorable_here ? "" : "offline"}>
        <span>{step.restorable_here ? <Check size={12} /> : <Lock size={12} />}<code>{step.name}</code>{!step.restorable_here && <Badge tone="purple">offline only</Badge>}</span>
        <small>{bytes(step.bytes)} · {step.kind}</small>
        <em>{step.how}</em>
      </div>)}</div>
    </details>
  </div>;
}

/** Sizes are the whole point of a backup page, so they are always a size and never a raw
 *  byte count — and never "0 B" for an unknown, which reads as an empty store. */
function bytes(value) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return "—";
  let size = Number(value);
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) { size /= 1024; unit += 1; }
  return `${size >= 100 || unit === 0 ? Math.round(size) : size.toFixed(1)} ${units[unit]}`;
}

const RUN_LABELS = { components_captured: "captured", components_planned: "planned", bytes_stored: "stored", warnings: "warnings", pruned: "pruned", seconds: "seconds" };

/** What went wrong, in words rather than in the name of a Python class.
 *
 * The ledger stores the exception's class alongside its message, which is the right thing
 * to keep — during an investigation the class is what ties a row to a line of code. It is
 * the wrong thing to lead with on a page an administrator reads: a row whose headline was
 * "StrandedRun" or "RestoredMidFlight" told them the name of a symbol and nothing about
 * their backups. Known outcomes get a sentence; anything unrecognised falls back to the
 * message, and the class is still on the row as small print for whoever needs it.
 */
const OUTCOME_HEADLINES = {
  StrandedRun: "Abandoned — nothing was running it",
  RestoredRun: "Not a real run — restored from a backup that caught it mid-flight",
  RestartRequired: "Restored, but some services still need restarting",
  RestoredMidFlight: "Not a real run — restored from a backup that caught it mid-flight",
  WorkerRestarted: "Cancelled — the worker restarted while this was queued",
  WorkerMissingWorkflow: "Cancelled — no worker could run it",
  BackupRunFailed: "The backup failed",
  RestoreError: "The restore failed",
  ComponentError: "A store could not be captured",
  BackupCryptoError: "Could not be encrypted or decrypted",
  DestinationError: "The destination could not be written to",
};

/** Rows that never ran are not failures, and a red "Failed" beside "Not a real run" is
 *  the page contradicting itself. Both of these are the appliance tidying up after
 *  something else, so they read as cancelled — which is what happened to them.
 */
const NOT_A_FAILURE = new Set(["StrandedRun", "RestoredRun", "RestoredMidFlight", "WorkerRestarted", "WorkerMissingWorkflow", "RestartRequired"]);

function runStatus(row) {
  return row.error && NOT_A_FAILURE.has(row.error.class) ? "cancelled" : row.status;
}

function outcomeHeadline(error) {
  return OUTCOME_HEADLINES[error?.class] || error?.message?.split(".")[0] || "Failed";
}

function runChips(counters) {
  if (!counters || typeof counters !== "object") return [];
  const chips = [];
  if (Number.isFinite(counters.components_captured)) chips.push({ id: "components", value: `${counters.components_captured}/${counters.components_planned ?? counters.components_captured}`, label: "components" });
  if (Number.isFinite(counters.bytes_stored)) chips.push({ id: "bytes", value: bytes(counters.bytes_stored), label: "stored" });
  if (counters.verified !== undefined) chips.push({ id: "verified", value: counters.verified ? "yes" : "no", label: "verified" });
  for (const key of ["warnings", "pruned", "seconds"]) {
    if (Number.isFinite(counters[key]) && counters[key]) chips.push({ id: key, value: counters[key].toLocaleString(), label: RUN_LABELS[key] });
  }
  return chips;
}

/** Directory sizes the preflight already measured, so a toggle can say what it costs. */
/** The destination in the words the setup panel uses.
 *
 * "local", "s3" and "restic" are backend names. Showing them here meant the same setting
 * had two vocabularies — a switch called "only store what changed" that made the status
 * box say "local" — and left an administrator to guess they were the same thing.
 */
function describeDestination(destination) {
  if (!destination || !destination.kind) return "—";
  const where = destination.kind === "s3" || String(destination.location || "").startsWith("s3:") ? "Cloud storage" : "Folder";
  return destination.deduplicated ? `${where} · only changes stored` : `${where} · full copy each night`;
}

function describeDetail(components) {
  const total = components.reduce((sum, component) => sum + (Number(component.detail?.bytes) || 0), 0);
  return total ? ` · ${bytes(total)} on disk` : "";
}

/** The schedule is stored in UTC and must be read as UTC, but an operator still wants to
 *  know whether "02:00" is the middle of their night or the middle of their morning. */
// Ordered the way a recovery is decided: the index first, because without it the rest is
// files nobody can find; the search index last, because it is the one thing here that can
// be rebuilt from the others.
const RESTORE_STORES = [
  { key: "databases", title: "The index and its databases", why: "Documents, permissions, the audit trail, connector logins." },
  { key: "files", title: "Stored files", why: "The originals of every fetched document, and anything uploaded here." },
  { key: "search_index", title: "The search index", why: "Rebuildable from the databases, at the cost of re-embedding everything." },
  { key: "volumes", title: "Sign-in and the orchestrator", why: "Keycloak's users, sessions and realm keys, and the orchestrator's config. Those containers are stopped and started again while this happens." },
];

const DESTINATION_COPY = {
  local: "In practice a NAS, SMB share or external disk mounted into this container. Stores every night in full.",
  s3: "MinIO on the firm's own hardware, Wasabi, AWS — the off-site leg of 3-2-1. Stores every night in full.",
  restic: "Stores each night as the difference from the night before, and encrypts and verifies what it stores. The one to choose at scale.",
};

// The zones a browser knows, so an operator picks rather than types. Falls back to a
// short list on the browsers that do not implement supportedValuesOf.
const TIMEZONES = (() => {
  const here = (() => { try { return Intl.DateTimeFormat().resolvedOptions().timeZone; } catch { return "UTC"; } })();
  let all;
  try {
    all = Intl.supportedValuesOf("timeZone");
  } catch {
    all = ["UTC", "Europe/Berlin", "Europe/London", "America/New_York", "America/Los_Angeles", "Asia/Tokyo"];
  }
  // This browser's zone and UTC first. Six hundred names in alphabetical order is a list
  // in which the answer somebody wants is the hardest one to reach.
  const pinned = [here, "UTC"].filter((zone, index, list) => zone && list.indexOf(zone) === index);
  return [...pinned, ...all.filter((zone) => !pinned.includes(zone))];
})();

function scheduleLocal(schedule) {
  if (!schedule.timezone) return "";
  // What the operator asked for is a wall time in the firm's zone; what they are looking
  // at is a browser somewhere else. Show both, because a backup window that reads 02:00
  // and fires during the working day is the mistake this line exists to prevent.
  const now = new Date();
  const parts = new Intl.DateTimeFormat("en-CA", { timeZone: schedule.timezone, year: "numeric", month: "2-digit", day: "2-digit" }).format(now);
  const guess = new Date(`${parts}T${String(schedule.hour).padStart(2, "0")}:${String(schedule.minute).padStart(2, "0")}:00`);
  // Resolve the named zone's offset for that date by round-tripping through it.
  const asUtc = new Date(guess.toLocaleString("en-US", { timeZone: "UTC" }));
  const asZone = new Date(guess.toLocaleString("en-US", { timeZone: schedule.timezone }));
  const instant = new Date(guess.getTime() + (asUtc.getTime() - asZone.getTime()));
  const here = Intl.DateTimeFormat().resolvedOptions().timeZone;
  if (here === schedule.timezone) return "This browser is in the same timezone.";
  return `${String(schedule.hour).padStart(2, "0")}:${String(schedule.minute).padStart(2, "0")} in ${schedule.timezone} is ${instant.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })} here.`;
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
