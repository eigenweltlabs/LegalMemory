import { useEffect, useState } from "react";
import { Bot, Braces, Database, ExternalLink, FileScan, Gauge, Lock, Plus, RefreshCw, Save, Search, Server, Workflow } from "lucide-react";
import { api } from "../api";
import { useApi, useExpertMode } from "../hooks";
import { Badge, SectionHeading, Status } from "../components/Primitives";

// Every model-calling pipeline stage carries its own gateway model assignment
// (pipeline.stages.<id>.model); features that live outside the pipeline — search
// rerank and the Ask assistant — carry theirs in their own config. There is no
// intermediate slot layer: what is assigned is always a model the gateway serves.
const STAGE_ASSIGNMENTS = [
  { id: "classify_matter", label: "Classify", gloss: "The matter each file belongs to" },
  { id: "relate", label: "Relate", gloss: "Version chains and document links — usually the largest spend" },
  { id: "extract_metadata", label: "Extract metadata", gloss: "Ontology fields and rationale" },
  { id: "extract_decisions", label: "Extract decisions", gloss: "What changed between versions" },
  { id: "gen_evals", label: "Evaluation builder", gloss: "Partner-approved environment candidates" }
];

const LICENSES = { LiteLLM: "MIT core", "Docling Serve": "MIT", OpenSearch: "Apache-2.0", Hatchet: "MIT", Langfuse: "MIT core" };

const VERSION_STATUSES = ["executed", "final", "unknown", "draft"];

const COMPONENT_FIELDS = [
  { key: "litellm_url", label: "LiteLLM gateway", hint: "http://litellm:4000" },
  { key: "docling_url", label: "Docling Serve", hint: "http://docling:5001" },
  { key: "opensearch_url", label: "OpenSearch", hint: "http://opensearch:9200" },
  { key: "orchestrator_api_url", label: "Orchestrator API", hint: "Hatchet engine URL" },
  { key: "orchestrator_ui_url", label: "Orchestrator UI", hint: "Hatchet dashboard URL" },
  { key: "traces_url", label: "Traces (Langfuse)", hint: "http://localhost:3001" }
];

export default function ModelsPage({ identity }) {
  const config = useApi("/api/config", [], Boolean(identity?.is_admin));
  const components = useApi("/api/components", [], Boolean(identity?.is_admin));
  const indexStatus = useApi("/api/index/status", [], Boolean(identity?.is_admin));
  const modelCatalog = useApi("/api/models/catalog", [], Boolean(identity?.is_admin));
  const [expert] = useExpertMode();
  const [draft, setDraft] = useState(null);
  const [saving, setSaving] = useState(false);
  const [rebuildConfirm, setRebuildConfirm] = useState(false);
  const [rebuildResult, setRebuildResult] = useState(null);
  const [addingModel, setAddingModel] = useState(false);
  const admin = Boolean(identity?.is_admin);
  const locked = Boolean(indexStatus.data?.locked);
  const catalogEntries = modelCatalog.data?.entries || [];
  const catalogError = modelCatalog.data?.error;
  // `error` covers two different failures: the gateway itself was unreachable, or it
  // answered with its models and only the credential list failed (app.py, models_catalog).
  // The error alone therefore cannot say the gateway is down, and only the first failure
  // empties the catalogue — so that is what is tested. Calling a credentials outage an
  // unreachable gateway sends an administrator to restart a container that is running.
  const gatewayAnswered = catalogEntries.length > 0 || !catalogError;
  // Embedding vectors and chat completions are not interchangeable, so an assignment
  // only offers what it can actually call. A value the gateway does not serve — or that
  // no gateway could be reached to confirm — stays in the list and is labelled with
  // which of the two it is, so the saved configuration is never silently rewritten and
  // a temporary outage is never reported as a deleted model.
  const optionsFor = (kind, current) => {
    const served = catalogEntries.filter((entry) => (entry.mode || "chat") === kind);
    if (!current || served.some((entry) => entry.id === current)) return served;
    return [{ id: current, unresolved: gatewayAnswered ? "not served by this gateway" : "gateway unreachable" }, ...served];
  };
  useEffect(() => { if (config.data) setDraft(structuredClone(config.data)); }, [config.data]);
  const updateStageModel = (stage, value) => setDraft((current) => ({ ...current, pipeline: { ...current.pipeline, stages: { ...current.pipeline.stages, [stage]: { ...current.pipeline.stages[stage], model: value } } } }));
  const updateRuntime = (section, key, value) => setDraft((current) => ({ ...current, [section]: { ...current[section], [key]: value } }));
  const updateBoost = (status, value) => setDraft((current) => ({ ...current, retrieval: { ...current.retrieval, version_status_boost: { ...current.retrieval.version_status_boost, [status]: value } } }));
  const save = async () => { setSaving(true); try { await api("/api/config", { method: "PUT", body: JSON.stringify(draft) }); await Promise.all([config.reload(), components.reload(), indexStatus.reload()]); } finally { setSaving(false); } };
  const rebuild = async () => { setSaving(true); try { await api("/api/config", { method: "PUT", body: JSON.stringify(draft) }); const res = await api("/api/actions/reindex", { method: "POST" }); setRebuildResult(res); setRebuildConfirm(false); await Promise.all([config.reload(), indexStatus.reload()]); } finally { setSaving(false); } };

  return (
    <>
      <div className="hero-row compact-hero"><div><h1>Models &amp; services</h1></div><div className="hero-actions"><button className="primary-button" disabled={!draft || saving || !admin} onClick={save}><Save size={15} /> {saving ? "Saving…" : "Save configuration"}</button></div></div>

      <InferenceOffer />

      <section className="model-section"><SectionHeading title="Model assignments" copy="Models live in the LiteLLM gateway; each pipeline stage and feature is assigned one directly." action={<button className="secondary-button small" disabled={!admin} onClick={() => setAddingModel(true)}><Plus size={13} /> Add model</button>} />
        {catalogError && <div className="form-note">{gatewayAnswered ? "Model gateway answered, but its credential list did not — the assignments below are current, adding a model is what will fail" : "Model gateway unreachable — assignments keep their saved values"}. <code className="mono">{catalogError}</code></div>}
        <div className="model-grid">{draft && [
          ...STAGE_ASSIGNMENTS.map((stage) => ({ key: stage.id, label: stage.label, gloss: stage.gloss, where: `Pipeline stage: ${stage.id}`, value: draft.pipeline.stages?.[stage.id]?.model || "", set: (value) => updateStageModel(stage.id, value) })),
          { key: "rerank", label: "Search rerank", gloss: "Optional reorder of results", where: "Search — when rerank is enabled", value: draft.retrieval.rerank_model || "", set: (value) => updateRuntime("retrieval", "rerank_model", value) },
          { key: "ask", label: "Ask assistant", gloss: "Plans retrieval and writes the cited answer", where: "Ask", value: draft.ask_model || "", set: (value) => setDraft((current) => ({ ...current, ask_model: value })) }
        ].map((row) => <article className="model-card" key={row.key}><div className="model-card-heading"><div className="model-icon"><Bot size={18} /></div><div><h3>{row.label}</h3><p>{row.gloss}</p><small className="assignment-usage">{row.where}</small></div></div><label>Model<select className="mono" value={row.value} disabled={!admin} onChange={(event) => row.set(event.target.value)}>{!row.value && <option value="">Select a model…</option>}{optionsFor("chat", row.value).map((entry) => <option key={entry.id} value={entry.id}>{modelOptionLabel(entry)}</option>)}</select></label></article>)}</div></section>

      {addingModel && <AddModelModal credentials={modelCatalog.data?.credentials || []} onClose={() => setAddingModel(false)} onAdded={modelCatalog.reload} />}

      {draft && <section className="panel component-registry"><SectionHeading title="Embedding & vector index" copy="All chunks share one embedding model. Changing it needs a rebuild." />
        <div className="form-columns">
          <label>Embedding model {locked && <Lock size={10} />}<select className="mono" value={draft.retrieval.embedding_model || ""} disabled={!admin || locked} onChange={(event) => updateRuntime("retrieval", "embedding_model", event.target.value)}>{!draft.retrieval.embedding_model && <option value="">Select a model…</option>}{optionsFor("embedding", draft.retrieval.embedding_model).map((entry) => <option key={entry.id} value={entry.id}>{modelOptionLabel(entry)}</option>)}</select><small>{locked ? `Locked — ${(indexStatus.data?.chunk_count || 0).toLocaleString()} chunks indexed. Rebuild to switch.` : "Served by your LiteLLM gateway."}</small></label>
          <label>Embedding dimensions<input type="number" min="8" max="4096" className="mono" value={draft.retrieval.embedding_dimensions} disabled={!admin || locked} onChange={(event) => updateRuntime("retrieval", "embedding_dimensions", Number(event.target.value))} /><small>Must match the model's output.</small></label>
        </div>
        <div className="form-columns" style={{ marginTop: "12px" }}>
          <label>Vector engine<select value={draft.retrieval.vector_engine} disabled={!admin} onChange={(event) => updateRuntime("retrieval", "vector_engine", event.target.value)}><option value="lucene">lucene (default)</option><option value="faiss">faiss (large scale)</option></select></label>
          <label>HNSW m<input type="number" min="2" max="100" value={draft.retrieval.hnsw_m} disabled={!admin} onChange={(event) => updateRuntime("retrieval", "hnsw_m", Number(event.target.value))} /><small>16 is a good default</small></label>
          <label>HNSW ef_construction<input type="number" min="8" max="2000" value={draft.retrieval.hnsw_ef_construction} disabled={!admin} onChange={(event) => updateRuntime("retrieval", "hnsw_ef_construction", Number(event.target.value))} /><small>Higher = better recall</small></label>
        </div>
        <div className="form-columns" style={{ marginTop: "12px" }}>
          <label>Active index<input className="mono" readOnly value={indexStatus.data?.index_name || draft.retrieval.index_name} /><small>Searched now.</small></label>
          <label>Model-bound target<input className="mono" readOnly value={indexStatus.data?.derived_index_name || ""} /><small>Rebuild target for the current model.</small></label>
        </div>
        <div className="rebuild-row">
          <div><strong><Database size={13} /> Rebuild vector index</strong><small>Re-embeds {(indexStatus.data?.chunk_count || 0).toLocaleString()} chunks into {indexStatus.data?.derived_index_name || "the model-bound index"}. Runs in the background.</small></div>
          {rebuildConfirm ? <div className="row-confirm"><button className="secondary-button small" onClick={rebuild} disabled={saving || !admin}><RefreshCw size={13} /> Confirm rebuild</button><button className="text-button" onClick={() => setRebuildConfirm(false)}>Cancel</button></div> : <button className="secondary-button small" onClick={() => setRebuildConfirm(true)} disabled={!admin}><RefreshCw size={13} /> Rebuild…</button>}
        </div>
        {rebuildResult && <div className="form-note">Queued: re-embedding {(rebuildResult.chunks_to_reembed || 0).toLocaleString()} chunks into <code>{rebuildResult.target_index}</code>.</div>}
      </section>}

      {draft && <section className="panel component-registry"><SectionHeading title="Fusion & ranking" /><div className="form-columns"><label>RRF constant k<input type="number" min="1" max="1000" disabled={!admin} value={draft.retrieval.fusion_rrf_k} onChange={(event) => updateRuntime("retrieval", "fusion_rrf_k", Number(event.target.value))} /><small>1–1000 · lower sharpens contrast</small></label><label>Max chunks per document<input type="number" min="1" max="20" disabled={!admin} value={draft.retrieval.max_chunks_per_document} onChange={(event) => updateRuntime("retrieval", "max_chunks_per_document", Number(event.target.value))} /><small>1–20</small></label></div><div className="model-grid" style={{ marginTop: "12px" }}><label className="model-card">Lexical weight<input type="number" min="0" step="0.1" disabled={!admin} value={draft.retrieval.weight_lexical} onChange={(event) => updateRuntime("retrieval", "weight_lexical", Number(event.target.value))} /></label><label className="model-card">Semantic weight<input type="number" min="0" step="0.1" disabled={!admin} value={draft.retrieval.weight_semantic} onChange={(event) => updateRuntime("retrieval", "weight_semantic", Number(event.target.value))} /></label><label className="model-card">Identifier weight<input type="number" min="0" step="0.1" disabled={!admin} value={draft.retrieval.weight_identifier} onChange={(event) => updateRuntime("retrieval", "weight_identifier", Number(event.target.value))} /></label><label className="model-card">Decisions weight<input type="number" min="0" step="0.1" disabled={!admin} value={draft.retrieval.weight_decisions} onChange={(event) => updateRuntime("retrieval", "weight_decisions", Number(event.target.value))} /></label></div><div style={{ marginTop: "16px" }}><span className="eyebrow">Version status boost</span><div className="form-columns" style={{ marginTop: "10px" }}>{VERSION_STATUSES.map((statusKey) => <label key={statusKey} style={{ textTransform: "capitalize" }}>{statusKey}<input type="number" min="0" step="0.1" disabled={!admin} value={draft.retrieval.version_status_boost?.[statusKey] ?? 1} onChange={(event) => updateBoost(statusKey, Number(event.target.value))} /></label>)}</div></div><div className="form-columns" style={{ marginTop: "16px" }}><label className="toggle-row"><span><strong>Collapse per document</strong><small>Keep the strongest chunks per document, not raw chunk floods</small></span><input type="checkbox" disabled={!admin} checked={draft.retrieval.collapse_per_document} onChange={(event) => updateRuntime("retrieval", "collapse_per_document", event.target.checked)} /></label><label className="toggle-row"><span><strong>Rerank enabled</strong><small>Optional late-stage reorder of authorized candidates</small></span><input type="checkbox" disabled={!admin} checked={draft.retrieval.rerank_enabled} onChange={(event) => updateRuntime("retrieval", "rerank_enabled", event.target.checked)} /></label><label className="toggle-row"><span><strong>Graph RAG enabled</strong><small>Expand candidates along ontology relations before ranking</small></span><input type="checkbox" disabled={!admin} checked={draft.retrieval.graph_rag_enabled} onChange={(event) => updateRuntime("retrieval", "graph_rag_enabled", event.target.checked)} /></label></div></section>}

      {draft && <section className="panel component-registry"><SectionHeading title="Ingestion signals" /><div className="form-columns"><label className="toggle-row"><span><strong>Chunk contextualize</strong><small>Prefix chunks with document context before embedding</small></span><input type="checkbox" disabled={!admin} checked={draft.retrieval.chunk_contextualize} onChange={(event) => updateRuntime("retrieval", "chunk_contextualize", event.target.checked)} /></label><label className="toggle-row"><span><strong>Profile embeddings</strong><small>Document-level summary vectors</small></span><input type="checkbox" disabled={!admin} checked={draft.retrieval.profile_embeddings} onChange={(event) => updateRuntime("retrieval", "profile_embeddings", event.target.checked)} /></label><label className="toggle-row"><span><strong>Clause embeddings</strong><small>Clause-granular vectors for contract retrieval</small></span><input type="checkbox" disabled={!admin} checked={draft.retrieval.clause_embeddings} onChange={(event) => updateRuntime("retrieval", "clause_embeddings", event.target.checked)} /></label></div><div className="form-columns" style={{ marginTop: "12px" }}><label>Chunk size (chars)<input type="number" min="200" max="10000" disabled={!admin} value={draft.retrieval.chunk_chars} onChange={(event) => updateRuntime("retrieval", "chunk_chars", Number(event.target.value))} /><small>200–10000</small></label><label>Chunk overlap (chars)<input type="number" min="0" max="2000" disabled={!admin} value={draft.retrieval.chunk_overlap_chars} onChange={(event) => updateRuntime("retrieval", "chunk_overlap_chars", Number(event.target.value))} /><small>0–2000</small></label></div></section>}

      {draft && <section className="panel component-registry"><SectionHeading title="Service endpoints" /><div className="form-columns"><label>Orchestrator provider<select disabled={!admin} value={draft.components.orchestrator_provider} onChange={(event) => updateRuntime("components", "orchestrator_provider", event.target.value)}><option value="local">local (in-process runner)</option><option value="hatchet">hatchet (durable workers)</option></select></label>{COMPONENT_FIELDS.map((field) => <label key={field.key}>{field.label}<input className="mono" disabled={!admin} placeholder={field.hint} value={draft.components[field.key] || ""} onChange={(event) => updateRuntime("components", field.key, event.target.value)} /></label>)}</div></section>}

      <section className="panel component-registry"><SectionHeading title="Services" action={<span className="table-count">{(components.data || []).length} service(s)</span>} /><div className="component-table"><div className="component-head"><span>Role</span><span>Product</span><span>Endpoint</span><span>License</span><span>Status</span><span /></div>{(components.data || []).map((component) => <div className="component-row" key={component.role}><span className="primary-cell"><i className="component-icon">{componentIcon(component.role)}</i><span><strong>{component.role}</strong></span></span><span><strong>{component.name}</strong></span><span className="mono endpoint-cell">{component.api_url}</span><span><Badge tone="green">{LICENSES[component.name] || "verify at pin"}</Badge></span><span><Status value={component.status} /></span><span>{expert && component.ui_url && <a className="icon-button" href={component.ui_url} target="_blank" rel="noreferrer" title={`Open ${component.name}`}><ExternalLink size={15} /></a>}</span></div>)}</div></section>
    </>
  );
}

/**
 * Where the models on this page could run instead.
 *
 * Placed here rather than on a marketing surface because this is the page where
 * the question arises: somebody assigning an embedding model to a corpus of a
 * hundred thousand documents is, at that moment, deciding who bills them for it
 * and who is allowed to keep the text.
 *
 * Deliberately quiet — it sits above configuration an administrator came here to
 * change, and it is dismissible, because an appliance that nags on every visit
 * is one whose warnings stop being read.
 */
function InferenceOffer() {
  const [dismissed, setDismissed] = useState(() => {
    try { return localStorage.getItem("lm.inference-offer.dismissed") === "1"; } catch { return false; }
  });
  if (dismissed) return null;
  const dismiss = () => {
    try { localStorage.setItem("lm.inference-offer.dismissed", "1"); } catch { /* private mode */ }
    setDismissed(true);
  };
  return (
    <section className="panel inference-offer">
      <div className="inference-offer-body">
        <span className="eyebrow">Inference from Eigenwelt Labs</span>
        <p>
          Running a large insertion job? We host embedding and completion models with{" "}
          <strong>zero data retention</strong> at low per-token prices, and configure GPUs
          to scale to your workload — so a one-off backfill of a firm&apos;s estate does not
          have to run at retail rates on a general-purpose API.
        </p>
      </div>
      <div className="inference-offer-actions">
        <a className="secondary-button small" href="https://eigenweltlabs.com/contact?subject=LegalMemory%20inference" target="_blank" rel="noreferrer">
          Talk to the founders <ExternalLink size={13} />
        </a>
        <button className="text-button" type="button" onClick={dismiss}>Dismiss</button>
      </div>
    </section>
  );
}

/**
 * One gateway model, named by what runs rather than by what routes to it.
 *
 * An assignment stores a gateway-served name, which may be an alias. An alias alone
 * tells an operator nothing — not which model answers, not which provider is billing,
 * and not that several aliases point at the same model, which is only visible once the
 * resolved names line up. So the resolved model leads and the alias trails it; the
 * alias is still shown because it is the value that gets saved and the one the gateway
 * routes on.
 */
function modelOptionLabel(entry) {
  if (entry.unresolved) return `${entry.id} · ${entry.unresolved}`;
  return entry.upstream_model ? `${entry.upstream_model} · via ${entry.id}` : entry.id;
}

function AddModelModal({ credentials, onClose, onAdded }) {
  const [form, setForm] = useState({ model_name: "", model: "", credential_name: credentials[0]?.name || "", api_base: "", mode: "chat" });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const set = (key, value) => setForm((current) => ({ ...current, [key]: value }));
  const save = async (event) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api("/api/models/catalog", { method: "POST", body: JSON.stringify({ ...form, credential_name: form.credential_name || null, api_base: form.api_base || null }) });
      await onAdded();
      onClose();
    } catch (caught) { setError(caught.message); }
    finally { setBusy(false); }
  };
  return <div className="modal-backdrop" onMouseDown={onClose}><form className="form-modal" onSubmit={save} onMouseDown={(event) => event.stopPropagation()}>
    <div><h2>Add a model</h2><p>No key is typed here: pick a credential the gateway already holds.</p></div>
    <label>Gateway name<input required placeholder="drafting-default" className="mono" value={form.model_name} onChange={(event) => set("model_name", event.target.value)} /><small>The name stages select it by.</small></label>
    <label>Upstream model<input required placeholder="openai/gpt-4o-mini" className="mono" value={form.model} onChange={(event) => set("model", event.target.value)} /></label>
    <div className="form-columns">
      <label>Provider credential<select value={form.credential_name} onChange={(event) => set("credential_name", event.target.value)}><option value="">gateway environment</option>{credentials.map((credential) => <option key={credential.name} value={credential.name}>{credential.name}</option>)}</select></label>
      <label>Kind<select value={form.mode} onChange={(event) => set("mode", event.target.value)}><option value="chat">chat</option><option value="embedding">embedding</option><option value="rerank">rerank</option></select><small>Decides where it can be assigned.</small></label>
    </div>
    <label>API base<input placeholder="inherited from the credential" className="mono" value={form.api_base} onChange={(event) => set("api_base", event.target.value)} /></label>
    {error && <div className="form-note">{error}</div>}
    <div className="modal-actions"><button type="button" className="text-button" onClick={onClose}>Cancel</button><button className="primary-button" type="submit" disabled={busy}>{busy ? "Registering…" : "Register model"}</button></div>
  </form></div>;
}

function componentIcon(role) { if (role.includes("Model")) return <Braces size={15} />; if (role.includes("parsing")) return <FileScan size={15} />; if (role.includes("Search")) return <Search size={15} />; if (role.includes("orchestrator")) return <Workflow size={15} />; if (role.includes("Trace")) return <Gauge size={15} />; return <Server size={15} />; }
