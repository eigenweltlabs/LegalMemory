import { useState } from "react";
import { ChevronDown, ChevronRight, Eye, EyeOff, RefreshCw, Search, ShieldCheck, FileWarning } from "lucide-react";
import { api } from "../api";
import { useApi } from "../hooks";
import { Badge, EmptyState, SectionHeading, Status } from "../components/Primitives";

const FACET_LABELS = {
  doc_type: "Document types",
  area_of_law: "Areas of law",
  service: "Services",
  clause: "Clauses",
};

export default function OntologyPage({ identity }) {
  const info = useApi("/api/ontology");
  const health = useApi("/api/health/doc-types");
  const admin = Boolean(identity?.is_admin);
  const [query, setQuery] = useState("");
  const [results, setResults] = useState(null);
  const [saving, setSaving] = useState(false);
  const [pendingDisabled, setPendingDisabled] = useState(null); // null = mirror server

  const disabled = pendingDisabled ?? info.data?.disabled_nodes ?? [];
  const dirty = pendingDisabled !== null;

  const toggle = (nodeId) => {
    const current = new Set(disabled);
    if (current.has(nodeId)) current.delete(nodeId); else current.add(nodeId);
    setPendingDisabled([...current].sort());
  };
  const saveScope = async () => {
    setSaving(true);
    try {
      await api("/api/ontology/scope", { method: "PUT", body: JSON.stringify({ disabled_nodes: disabled }) });
      setPendingDisabled(null);
      await Promise.all([info.reload(), health.reload()]);
    } finally { setSaving(false); }
  };
  const search = async (event) => {
    event.preventDefault();
    setResults(query.trim() ? await api(`/api/ontology/search?q=${encodeURIComponent(query)}`) : null);
  };

  return (
    <>
      <div className="hero-row compact-hero">
        <div>
          <span className="eyebrow">Document-type ontology</span>
          <h1>The plugged ontology is the taxonomy.</h1>
          <p>Every document type is a node of the active artifact. Toggle branches off to scope the vocabulary; the extraction agent, retrieval filters, and MCP tools all follow the same view.</p>
        </div>
        <div className="hero-actions">
          <button className="secondary-button" onClick={() => Promise.all([info.reload(), health.reload()])}><RefreshCw size={15} /> Refresh</button>
          {admin && dirty && <button className="primary-button" onClick={saveScope} disabled={saving}>{saving ? "Applying…" : "Apply scope"}</button>}
        </div>
      </div>

      <div className="pipeline-layout">
        <section className="pipeline-builder">
          <div className="builder-header">
            <div><span className="eyebrow">Active artifact</span><h2>{info.data ? `${info.data.artifact.name} · ${info.data.artifact.version}` : "Loading…"}</h2></div>
            <div>
              {info.data && <Badge tone="purple">{info.data.visible_nodes} visible nodes</Badge>}
              {info.data && <Badge>fingerprint {info.data.fingerprint}</Badge>}
            </div>
          </div>

          <form onSubmit={search} className="quiet-row" style={{ gap: 8, marginBottom: 14 }}>
            <Search size={15} />
            <input className="mono" style={{ flex: 1 }} placeholder="Find a node (label, synonym, definition)…" value={query} onChange={(e) => setQuery(e.target.value)} />
            <button className="secondary-button small" type="submit">Search</button>
          </form>
          {results && <div className="data-table" style={{ marginBottom: 14 }}>
            {results.length ? results.map((node) => <div className="table-row" style={{ gridTemplateColumns: "1fr" }} key={node.id}>
              <span><strong>{node.label}</strong> <small className="mono subtle">{node.id}</small><br /><small className="subtle">{(node.path || []).join(" › ")}</small></span>
            </div>) : <EmptyState title="No match" copy="Try a synonym — search covers labels, synonyms, and definitions." />}
          </div>}

          {Object.entries(info.data?.facets || {}).map(([facet, section]) => (
            <div key={facet} style={{ marginBottom: 18 }}>
              <div className="quiet-row" style={{ gap: 8, marginBottom: 6 }}>
                <span className="eyebrow">{FACET_LABELS[facet] || facet}</span>
                <Badge>{section.visible_nodes} nodes</Badge>
                <small className="mono subtle">{section.fingerprint}</small>
              </div>
              {(section.roots || []).map((root) => (
                <TreeNode key={root.id} node={root} depth={0} disabledSet={new Set(disabled)} onToggle={toggle} admin={admin} />
              ))}
            </div>
          ))}
        </section>

        <aside className="pipeline-sidebar">
          <section className="panel compact-panel">
            <SectionHeading eyebrow="Vocabulary health" title="Depth pressure" copy="Documents typed at shallow nodes mean the ontology lacks a fitting subtree there. The exact shallow nodes below show where to extend or re-scope." />
            {health.data ? <>
              {Object.entries(health.data.branches || {}).map(([label, stats]) => stats.total > 0 && <div key={label} style={{ marginBottom: 8 }}>
                <small>{label}: {stats.shallow} of {stats.total} shallow ({Math.round(stats.share * 100)}%)</small>
                <div className="table-progress"><i style={{ width: `${Math.round(stats.share * 100)}%` }} /></div>
              </div>)}
              {(health.data.alerts || []).map((alert, i) => <div className="quiet-row" key={i}><FileWarning size={14} /> <small>{alert.message}</small></div>)}
              <small className="stage-hint">
                untyped: {health.data.untyped_documents} · typed under stale scope: {health.data.stale_typed_documents}
              </small>
            </> : <EmptyState title="No data yet" copy="Health fills in as documents classify." />}
          </section>
          <section className="panel compact-panel">
            <SectionHeading eyebrow="How scoping works" title="Toggles, not forks" />
            <div className="policy-list">
              <div><EyeOff size={14} /><span>Disabling a node hides its whole subtree</span></div>
              <div><ShieldCheck size={14} /><span>Documents on hidden nodes re-type to the nearest visible ancestor</span></div>
              <div><RefreshCw size={14} /><span>Applying a scope change re-runs only affected documents</span></div>
            </div>
          </section>
        </aside>
      </div>
    </>
  );
}

function TreeNode({ node, depth, disabledSet, onToggle, admin }) {
  const [open, setOpen] = useState(false);
  const [children, setChildren] = useState(null);
  const isDisabled = disabledSet.has(node.id);
  const expand = async () => {
    if (!open && children === null && node.children > 0) {
      const payload = await api(`/api/ontology/children?node_id=${encodeURIComponent(node.id)}`);
      setChildren(payload.children || []);
    }
    setOpen(!open);
  };
  return (
    <div style={{ marginLeft: depth ? 18 : 0 }}>
      <div className="quiet-row" style={{ gap: 6, opacity: isDisabled ? 0.45 : 1 }}>
        {node.children > 0
          ? <button className="row-link" style={{ padding: 0 }} onClick={expand}>{open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}</button>
          : <span style={{ width: 14 }} />}
        <span title={node.definition || ""}><strong>{node.label}</strong>{node.children > 0 && <small className="subtle"> · {node.children}</small>}</span>
        {admin && <button className="row-link" style={{ marginLeft: "auto", padding: 0 }} title={isDisabled ? "Enable this subtree" : "Disable this subtree"} onClick={() => onToggle(node.id)}>
          {isDisabled ? <EyeOff size={14} /> : <Eye size={14} />}
        </button>}
      </div>
      {open && (children || []).map((child) => (
        <TreeNode key={child.id} node={child} depth={depth + 1} disabledSet={disabledSet} onToggle={onToggle} admin={admin} />
      ))}
    </div>
  );
}
