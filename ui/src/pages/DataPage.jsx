import { useEffect, useMemo, useState } from "react";
import {
  ArrowRight,
  ChevronLeft,
  ChevronRight,
  Clipboard,
  Code2,
  Database,
  FileText,
  Filter,
  GitBranch,
  Grid2X2,
  Languages,
  List,
  Network,
  Search,
  ShieldCheck,
  SlidersHorizontal,
  X
} from "lucide-react";
import { api } from "../api";
import GraphExplorer from "../components/GraphExplorer";
import { useApi } from "../hooks";
import { Badge, EmptyState, SectionHeading, Status } from "../components/Primitives";

const PAGE_SIZES = [100, 250, 500, 1000];

export default function DataPage({ navigate, focus }) {
  const [view, setView] = useState("graph");
  const [query, setQuery] = useState("");
  const [projectId, setProjectId] = useState("");
  const [docType, setDocType] = useState("");
  const [filtersOpen, setFiltersOpen] = useState(false);
  const [matterId, setMatterId] = useState("");
  const [versionStatus, setVersionStatus] = useState("");
  const [language, setLanguage] = useState("");
  const [selectedId, setSelectedId] = useState(null);
  const [scope, setScope] = useState(null);
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = useState(1000);
  const projects = useApi("/api/projects");
  const moreFiltersActive = Boolean(matterId || versionStatus || language);
  const commonParams = useMemo(() => ({
    ...(projectId && { project_id: projectId }),
    ...(query && { query }),
    ...(docType && { doc_type: docType }),
    ...(matterId && { matter_id: matterId }),
    ...(versionStatus && { version_status: versionStatus }),
    ...(language && { language })
  }), [projectId, query, docType, matterId, versionStatus, language]);
  const documentPath = useMemo(() => `/api/documents?${new URLSearchParams({
    ...commonParams,
    detailed: "true",
    limit: String(pageSize),
    offset: String(page * pageSize)
  })}`, [commonParams, page, pageSize]);
  const graphPath = useMemo(() => `/api/graph?${new URLSearchParams(commonParams)}`, [commonParams]);
  const documents = useApi(documentPath);
  const graph = useApi(graphPath, [], view === "graph");
  const selected = useApi(selectedId ? `/api/documents/${selectedId}` : null, [selectedId], Boolean(selectedId));
  const rows = documents.data?.items || [];
  const pagination = documents.data?.pagination || { total: 0, offset: 0, returned: 0 };
  const pageCount = Math.max(1, Math.ceil(pagination.total / pageSize));

  useEffect(() => setPage(0), [projectId, query, docType, matterId, versionStatus, language, pageSize]);
  const projectName = useMemo(() => Object.fromEntries((projects.data || []).map((project) => [project.id, project.name])), [projects.data]);

  // Arrived from a command-palette result: open exactly what was picked. The table is
  // the view that can show one document or one matter; the graph cannot.
  useEffect(() => {
    if (!focus) return;
    if (focus.query) setQuery(focus.query);
    if (focus.matter) { setMatterId(focus.matter); setFiltersOpen(true); setView("table"); }
    if (focus.doc) { setSelectedId(focus.doc); setView("table"); }
  }, [focus]);

  const scopedSearch = async () => {
    const result = await api("/api/search", {
      method: "POST",
      body: JSON.stringify({
        query,
        project_id: projectId || null,
        doc_type: docType || null,
        matter_id: matterId || null,
        version_status: versionStatus || null,
        language: language || null,
        limit: 100
      })
    });
    setScope(result.scope);
  };

  const clearFilters = () => {
    setProjectId("");
    setDocType("");
    setMatterId("");
    setVersionStatus("");
    setLanguage("");
    setQuery("");
    setScope(null);
  };

  return (
    <>
      <div className="hero-row compact-hero data-hero">
        <div><h1>Data</h1></div>
        <div className="view-switch"><button className={view === "table" ? "active" : ""} onClick={() => setView("table")}><List size={15} /> Table</button><button className={view === "graph" ? "active" : ""} onClick={() => setView("graph")}><Network size={15} /> Graph</button></div>
      </div>

      <div className="data-toolbar">
        <div className="search-box data-search"><Search size={16} /><input placeholder="Search titles or content…" value={query} onChange={(event) => setQuery(event.target.value)} onKeyDown={(event) => event.key === "Enter" && scopedSearch()} /><kbd>↵</kbd></div>
        <label className="select-control"><Grid2X2 size={14} /><select value={projectId} onChange={(event) => { setProjectId(event.target.value); setScope(null); }}><option value="">All projects</option>{(projects.data || []).map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}</select></label>
        {view === "table" && <label className="select-control"><FileText size={14} /><select value={docType} onChange={(event) => setDocType(event.target.value)}><option value="">All types</option><optgroup label="Contract"><option value="share_purchase_agreement">Share purchase agreement</option><option value="purchase_agreement">Purchase agreement</option><option value="lease_agreement">Lease agreement</option><option value="employment_agreement">Employment agreement</option><option value="nda">NDA</option><option value="loan_agreement">Loan agreement</option></optgroup><optgroup label="Pleading & court"><option value="statement_of_claim">Statement of claim</option><option value="statement_of_defense">Statement of defense</option><option value="judgment">Judgment</option><option value="court_order">Court order</option></optgroup><optgroup label="Correspondence & internal"><option value="email">Email</option><option value="letter">Letter</option><option value="legal_opinion">Legal opinion</option><option value="due_diligence_report">Due diligence report</option><option value="internal_note">Internal note</option></optgroup></select></label>}
        <button className={`secondary-button icon-only ${filtersOpen || moreFiltersActive ? "active" : ""}`} title="More filters" onClick={() => setFiltersOpen((open) => !open)}><SlidersHorizontal size={16} /></button>
      </div>

      {filtersOpen && <div className="filter-panel">
        <label className="select-control"><GitBranch size={14} /><input className="mono" placeholder="Exact matter ID" value={matterId} onChange={(event) => { setMatterId(event.target.value); setScope(null); }} /></label>
        <label className="select-control"><Filter size={14} /><select value={versionStatus} onChange={(event) => { setVersionStatus(event.target.value); setScope(null); }}><option value="">Any version status</option><option value="draft">Draft</option><option value="final">Final</option><option value="executed">Executed</option><option value="unknown">Unknown</option></select></label>
        <label className="select-control"><Languages size={14} /><select value={language} onChange={(event) => { setLanguage(event.target.value); setScope(null); }}><option value="">Any language</option>{(documents.data?.facets?.languages || []).map((facet) => <option key={facet.value} value={facet.value}>{facet.value} ({facet.count})</option>)}</select></label>
        {(moreFiltersActive || projectId || docType || query) && <button className="text-button" onClick={clearFilters}><X size={13} /> Clear all filters</button>}
      </div>}

      <div className="scope-ribbon">
        <div className="scope-ribbon-icon"><ShieldCheck size={17} /></div>
        <div><strong>Search scope</strong><span>{scope ? `${scope.documents} document(s) across ${scope.projects} project(s) · fingerprint ${scope.fingerprint}` : "Search to see the exact candidate set."}</span></div>
      </div>

      {view === "graph" ? (
        <section className="graph-section">
          <div className="graph-section-heading"><div><h2>Knowledge graph</h2></div><div className="graph-summary-chips">{Object.entries(graph.data?.summary?.by_kind || {}).map(([kind, count]) => <Badge key={kind}>{count} {kind}{count === 1 ? "" : "s"}</Badge>)}</div></div>
          {/* Opening a record deliberately leaves the graph on screen. The drawer is a
              fixed, full-viewport overlay, so it covers the graph without switching
              views, and the cytoscape instance survives — with it the layout, zoom and
              selection the user built up to find this document in the first place. */}
          <GraphExplorer graph={graph.data} loading={graph.loading} onOpenDocument={setSelectedId} />
        </section>
      ) : (
        <section className="panel data-ledger">
          <SectionHeading title="Documents" action={<span className="table-count">{rows.length} visible</span>} />
          {rows.length ? <div className="data-table document-table">
            <div className="table-head document-head"><span>Document</span><span>Type</span><span>Project</span><span>Date</span><span>Versions</span><span>Status</span></div>
            {rows.map((document) => <button className="table-row document-head clickable-row" key={document.id} onClick={() => setSelectedId(document.id)}><span className="primary-cell"><i className="document-icon"><FileText size={15} /></i><span><strong>{document.title || "Untitled document"}</strong></span></span><span>{document.doc_type ? <Badge>{human(document.doc_type)}</Badge> : <span className="muted">Unclassified</span>}</span><span><strong className="subtle">{projectName[document.project_id] || "No project"}</strong>{document.matter_id && <small className="mono">matter {document.matter_id.slice(0, 8)}</small>}</span><span>{document.doc_date ? new Date(document.doc_date).toLocaleDateString() : "—"}</span><span className="mono">{document.versions}</span><span><Status value={document.latest_status} /></span></button>)}
          </div> : <EmptyState title={documents.data?.length ? "No document matches these filters" : "No documents yet"} copy={documents.data?.length ? "Clear a filter to see the rest." : "Connect a source and run the insertion pipeline."} action={documents.data?.length ? <button className="secondary-button" onClick={() => { setMatterId(""); setVersionStatus(""); setLanguage(""); setDocType(""); setQuery(""); }}>Clear filters</button> : <button className="secondary-button" onClick={() => navigate("connectors")}>Open connectors <ArrowRight size={14} /></button>} />}
        </section>
      )}

      {selectedId && <div className="drawer-backdrop" onMouseDown={() => setSelectedId(null)}><aside className="document-drawer" onMouseDown={(event) => event.stopPropagation()}>
        <div className="drawer-header"><span className="eyebrow">Document</span><button className="icon-button" onClick={() => setSelectedId(null)}><X size={17} /></button></div>
        {selected.loading ? <div className="drawer-loading">Loading…</div> : selected.data && <>
          <div className="drawer-title"><div className="large-document-icon"><FileText size={22} /></div><div><h2>{selected.data.document.title || "Untitled document"}</h2><span>{human(selected.data.document.doc_type || "unclassified")}</span></div></div>
          <div className="drawer-meta"><div><span>Version</span><strong>{selected.data.version.ordinal || 1}</strong></div><div><span>Status</span><Status value={selected.data.version.status} /></div><div><span>Language</span><strong>{selected.data.document.language || "—"}</strong></div><div><span>Date</span><strong>{selected.data.document.doc_date ? new Date(selected.data.document.doc_date).toLocaleDateString() : "—"}</strong></div></div>
          <section className="drawer-section"><h3>Content preview</h3><div className="content-preview">{selected.data.content?.text?.slice(0, 2600) || "No parsed text stored for this version."}</div></section>
          <section className="drawer-section"><h3>Source</h3>{selected.data.sources.map((source) => <div className="source-path" key={source.id}><GitBranch size={14} /><div><strong>{source.name}</strong><span>{source.path}</span></div></div>)}</section>
          {/* Read-only here on purpose: exceptions are narrow overrides, and the
              principals they name are picked and reviewed on the Access control page. */}
          <section className="drawer-section"><h3>Document exceptions</h3>{selected.data.grants?.length ? selected.data.grants.map((grant) => <div className="grant-compact" key={grant.id}><ShieldCheck size={14} /><span className="mono">{grant.principal}</span><Badge tone={grant.effect === "deny" ? "red" : "green"}>{grant.effect}</Badge></div>) : <p className="muted-copy">None. Project grants and source permissions apply.</p>}{navigate && <button className="row-link" onClick={() => navigate("access")}>Open access control <ArrowRight size={13} /></button>}</section>
        </>}
      </aside></div>}
    </>
  );
}

function Paginator({ page, pageCount, pagination, onPage }) {
  const first = pagination.total ? pagination.offset + 1 : 0;
  const last = pagination.offset + pagination.returned;
  return <div className="ledger-pagination"><span>Showing {first.toLocaleString()}–{last.toLocaleString()} of {pagination.total.toLocaleString()}</span><div><button disabled={page === 0} onClick={() => onPage(page - 1)}><ChevronLeft size={15} /> Previous</button><strong>Page {page + 1} / {pageCount}</strong><button disabled={page + 1 >= pageCount} onClick={() => onPage(page + 1)}>Next <ChevronRight size={15} /></button></div></div>;
}

function DocumentDrawer({ state, onClose, onSelect }) {
  const [fullContent, setFullContent] = useState(false);
  const data = state.data;
  const text = data?.content?.text || "";
  const displayedText = fullContent ? text : text.slice(0, 4000);
  const contentMetadata = data?.content ? { ...data.content, text: `[${text.length.toLocaleString()} characters; shown above]` } : null;

  useEffect(() => setFullContent(false), [data?.document?.id]);

  return <div className="drawer-backdrop" onMouseDown={onClose}><aside className="document-drawer wide-document-drawer" onMouseDown={(event) => event.stopPropagation()}>
    <div className="drawer-header"><span className="eyebrow">Complete document record</span><button className="icon-button" onClick={onClose}><X size={17} /></button></div>
    {state.loading ? <div className="drawer-loading">Loading every authorized field…</div> : state.error ? <EmptyState title="Could not load document" copy={state.error.message} /> : data && <>
      <div className="drawer-title"><div className="large-document-icon"><FileText size={22} /></div><div><h2>{data.document.title || "Untitled document"}</h2><span>{human(data.document.doc_type || "unclassified")}</span><code>{data.document.id}</code></div></div>
      <div className="drawer-meta drawer-meta-six"><div><span>Selected version</span><strong>{data.version.ordinal || 1}</strong></div><div><span>Status</span><Status value={data.version.status} /></div><div><span>Language</span><strong>{data.document.language || "—"}</strong></div><div><span>Date</span><strong>{formatDate(data.document.doc_date)}</strong></div><div><span>Versions</span><strong>{data.versions?.length || 0}</strong></div><div><span>Related</span><strong>{data.related?.result_count || 0}</strong></div></div>

      {/* What the extraction stage actually produced. All of it was being computed, stored
          and then never shown: the ontology path is the classifier's reasoning, and the
          confidence is what a firm disputing a classification would ask for first. */}
      <section className="drawer-section">
        <div className="drawer-section-title"><h3>Extracted metadata</h3>{data.document.ontology_fingerprint && <span className="mono">ontology {data.document.ontology_fingerprint}</span>}</div>
        <dl className="record-grid">
          <Record label="Type" value={data.document.doc_type_label || data.document.doc_type} />
          <Record label="Language" value={data.document.language} />
          <Record label="Document date" value={data.document.doc_date} />
          <Record label="Parties" value={(data.document.parties || []).map((party) => party.name || party).join(", ")} />
          <Record label="Identifiers" value={(data.document.identifiers || []).join(", ")} />
        </dl>
        {(data.document.doc_type_path?.length || data.document.doc_type_ancestors?.length) ? <div className="ontology-path">
          <span className="eyebrow">Ontology path</span>
          {/* Labels when the ontology can still resolve them; the stored ids otherwise, so
              a document typed under an artifact that has since been unplugged still shows
              what it was typed as rather than an empty box. */}
          <div>{(data.document.doc_type_path?.length ? data.document.doc_type_path : data.document.doc_type_ancestors).map((node, index, all) => <span key={`${node}-${index}`}><code>{node}</code>{index < all.length - 1 ? <ArrowRight size={11} /> : null}</span>)}</div>
        </div> : null}
      </section>

      {data.extractions?.length ? <section className="drawer-section">
        <h3>How this was extracted ({data.extractions.length})</h3>
        {data.extractions.map((row, index) => <div className="extraction-row" key={index}>
          <div><strong>{(row.fields || []).join(", ") || "no fields recorded"}</strong><small className="mono">{row.model} · {row.prompt_version} · {new Date(row.created_at).toLocaleString()}</small></div>
          {row.confidence != null && <Badge tone={row.confidence >= 0.8 ? "green" : row.confidence >= 0.5 ? "amber" : "red"}>{Math.round(row.confidence * 100)}% confident</Badge>}
        </div>)}
      </section> : null}

      {data.clauses?.length ? <section className="drawer-section">
        <h3>Notable clauses ({data.clauses.length})</h3>
        {data.clauses.map((clause, index) => <details className="clause-row" key={index}>
          <summary><strong>{clause.kind || clause.type || clause.title || `Clause ${index + 1}`}</strong></summary>
          <p>{clause.text || clause.summary || JSON.stringify(clause)}</p>
        </details>)}
      </section> : null}

      <section className="drawer-section"><div className="drawer-section-title"><h3>All identifiers</h3><CopyButton value={data.document.id} /></div><dl className="record-grid"><Record label="Document ID" value={data.document.id} /><Record label="Project ID" value={data.document.project_id} /><Record label="Matter ID" value={data.document.matter_id} /><Record label="Version ID" value={data.version.id} /><Record label="Content hash" value={data.version.content_hash} /><Record label="Latest final" value={data.document.latest_final_version_id} /></dl></section>

      <section className="drawer-section"><div className="drawer-section-title"><h3>Document content</h3><span>{text.length.toLocaleString()} characters</span></div><div className={`content-preview ${fullContent ? "full-content" : ""}`}>{displayedText || "No structured text artifact is available."}</div>{text.length > 4000 && <button className="secondary-button drawer-expand" onClick={() => setFullContent((value) => !value)}>{fullContent ? "Collapse content" : `View all ${text.length.toLocaleString()} characters`}</button>}</section>

      <section className="drawer-section"><h3>Source provenance ({data.sources?.length || 0})</h3>{(data.sources || []).map((source) => <div className="source-path detailed-source" key={source.id}><GitBranch size={14} /><div><strong>{source.name}</strong><span>{source.path}</span><code>{source.id} · {source.connector?.display_name || "Unknown connector"}</code></div></div>)}</section>

      <section className="drawer-section"><h3>Version history ({data.versions?.length || 0})</h3><div className="version-ledger">{(data.versions || []).map((version) => <article key={version.id}><div><strong>Version {version.ordinal || "?"}</strong><Status value={version.status} /></div><code>{version.id}</code><span>SHA-256 {version.content_hash}</span><span>{version.sources?.length || 0} source observation(s)</span>{version.status_evidence && <JsonDetails title="Status evidence" value={version.status_evidence} />}</article>)}</div></section>

      <section className="drawer-section"><h3>Related documents ({data.related?.result_count || 0})</h3><div className="related-document-list">{(data.related?.related_documents || []).map((related) => <button key={related.document_id} onClick={() => onSelect(related.document_id)}><Database size={14} /><span><strong>{related.title || "Untitled document"}</strong><small>{related.relationships.map((item) => `${item.basis}: ${item.kind}`).join(" · ")}</small></span><ChevronRight size={14} /></button>)}</div></section>

      <section className="drawer-section"><h3>Document access exceptions</h3>{data.grants?.length ? data.grants.map((grant) => <div className="grant-compact" key={grant.id}><ShieldCheck size={14} /><span>{grant.principal}</span><Badge tone={grant.effect === "deny" ? "red" : "green"}>{grant.effect}</Badge></div>) : <p className="muted-copy">No document-specific exception. Project grants and mirrored source ACLs apply.</p>}</section>

      <section className="drawer-section raw-data-section"><h3>Structured extraction and raw record</h3><JsonDetails title="Structured extraction metadata" value={contentMetadata} /><JsonDetails title="Matter metadata" value={data.matter} /><JsonDetails title="Document metadata" value={data.document} /><JsonDetails title="Selected version metadata" value={data.version} /><JsonDetails title="Entire API record" value={data} /></section>
    </>}
  </aside></div>;
}

function Record({ label, value }) {
  return <div><dt>{label}</dt><dd className="mono">{value || "—"}</dd></div>;
}

function JsonDetails({ title, value }) {
  if (value == null) return null;
  const serialized = JSON.stringify(value, null, 2);
  return <details className="json-details"><summary><Code2 size={13} />{title}<span>{serialized.length.toLocaleString()} chars</span></summary><div className="json-toolbar"><CopyButton value={serialized} /></div><pre>{serialized}</pre></details>;
}

function CopyButton({ value }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    await navigator.clipboard.writeText(String(value || ""));
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1200);
  };
  return <button className="icon-mini" onClick={copy} title="Copy"><Clipboard size={13} />{copied && <span>Copied</span>}</button>;
}

function formatDate(value) { return value ? new Date(value).toLocaleDateString() : "—"; }
function formatDateTime(value) { return value ? new Date(value).toLocaleString() : "—"; }
function human(value) { return String(value).replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase()); }
