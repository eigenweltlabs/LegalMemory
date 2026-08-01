import { useState } from "react";
import { ArrowRight, Braces, Clipboard, Code2, Copy, Plus, Radio, ShieldCheck, TerminalSquare } from "lucide-react";
import { api } from "../api";
import { useApi } from "../hooks";
import { Badge, EmptyState, SectionHeading, Status } from "../components/Primitives";
import PrincipalPicker from "../components/PrincipalPicker";

export default function ExternalPage({ identity, navigate }) {
  const clients = useApi("/api/external-clients", [], Boolean(identity?.is_admin));
  // Read from the server, never listed here: a hand-kept copy of this list had drifted
  // five tools behind what the MCP server registers.
  const tools = useApi("/api/mcp/tools", [], Boolean(identity?.is_admin));
  const [modal, setModal] = useState(false);
  const origin = window.location.origin;
  return (
    <>
      <div className="hero-row compact-hero"><div><h1>External access</h1></div>{identity?.is_admin && <div className="hero-actions"><button className="primary-button" onClick={() => setModal(true)}><Plus size={15} /> Register client</button></div>}</div>

      <div className="endpoint-grid"><EndpointCard icon={<Radio size={20} />} title="MCP" endpoint={`${origin}/mcp/`} copy="Claude, Codex, and other MCP clients" /><EndpointCard icon={<Braces size={20} />} title="REST" endpoint={`${origin}/api/search`} copy="Applications and internal services" /><EndpointCard icon={<Code2 size={20} />} title="OpenAPI" endpoint={`${origin}/openapi.json`} copy="Generate a typed client" /></div>

      <div className="external-layout"><section className="panel"><SectionHeading title="MCP tools" action={(tools.data || []).length ? <span className="table-count">{tools.data.length} registered</span> : null} /><ToolList tools={tools} /></section><section className="panel code-panel"><SectionHeading title="Connect an MCP client" /><pre>{`{
  "mcpServers": {
    "knowledge-index": {
      "url": "${origin}/mcp/",
      "headers": {
        "Authorization": "Bearer <OIDC_TOKEN>"
      }
    }
  }
}`}</pre><button className="secondary-button" onClick={() => navigator.clipboard.writeText(`${origin}/mcp/`)}><Copy size={14} /> Copy endpoint</button><div className="code-security-note"><ShieldCheck size={18} /><div><strong>No caller-supplied ACL</strong><p>Principals come from the validated token. A tool call cannot ask for a wider scope.</p></div></div></section></div>

      {identity?.is_admin && <section className="panel client-registry"><SectionHeading title="Registered clients" copy="A client reads nothing until its principal holds a project grant." action={<button className="row-link" onClick={() => navigate("access")}>Manage grants <ArrowRight size={14} /></button>} />{(clients.data || []).length ? <div className="data-table"><div className="table-head client-head"><span>Client</span><span>Protocol</span><span>Principal</span><span>Projects</span><span>Last used</span><span>Status</span></div>{clients.data.map((client) => <div className="table-row client-head" key={client.id}><span className="primary-cell"><i className="client-icon"><TerminalSquare size={15} /></i><span><strong>{client.name}</strong></span></span><span><Badge>{client.kind.toUpperCase()}</Badge></span><span className="mono">{client.principal}</span><span>{client.allowed_project_ids.length ? `${client.allowed_project_ids.length} project${client.allowed_project_ids.length === 1 ? "" : "s"}` : "Every project it is granted"}</span><span>{client.last_used_at ? new Date(client.last_used_at).toLocaleString() : "Never"}</span><span><Status value={client.status} /></span></div>)}</div> : <EmptyState title="No clients registered" copy="Register an MCP or REST client to call the index from another tool." action={<button className="secondary-button" onClick={() => setModal(true)}><Plus size={14} /> Register client</button>} />}</section>}
      {modal && <ClientModal navigate={navigate} onClose={() => setModal(false)} onSaved={async () => { setModal(false); await clients.reload(); }} />}
    </>
  );
}

// Whatever the server registered, in the order it registered it. Never a literal list:
// this panel is the answer to "what can a connected client do", and a stale answer to
// that question is worse than none.
function ToolList({ tools }) {
  if (tools.error) return <EmptyState title="Tool list unavailable" copy={tools.error.message} />;
  if (!tools.data) return <div className="tool-list-empty">{tools.loading ? "Loading…" : "Administrator only."}</div>;
  if (!tools.data.length) return <EmptyState title="No tools registered" copy="The MCP endpoint is reachable but exposes nothing." />;
  return <div className="tool-list">{tools.data.map((tool, index) => <div key={tool.name}>
    <span className="tool-index">{String(index + 1).padStart(2, "0")}</span>
    <div><code>{tool.name}</code><p>{tool.summary}</p></div>
    <Badge tone={tool.tags.includes("scope") ? "purple" : "neutral"}>{tool.tags[0] || "tool"}</Badge>
  </div>)}</div>;
}

function EndpointCard({ icon, title, endpoint, copy }) { return <article className="endpoint-card"><div className="endpoint-icon">{icon}</div><div><h3>{title}</h3><p>{copy}</p></div><code>{endpoint}</code><button className="icon-button" onClick={() => navigator.clipboard.writeText(endpoint)}><Clipboard size={15} /></button></article>; }
function ClientModal({ onClose, onSaved, navigate }) {
  const [form, setForm] = useState({ name: "", kind: "mcp", principal: "", secret_ref: "vault://knowledge-index/", allowed_project_ids: [] });
  const save = async (event) => { event.preventDefault(); await api("/api/external-clients", { method: "POST", body: JSON.stringify(form) }); onSaved(); };
  // A machine identity is normally new by definition, so the picker's unknown-principal
  // warning is the point here: it says out loud that registering the client grants it
  // nothing until someone adds that principal to a project.
  return <div className="modal-backdrop" onMouseDown={onClose}><form className="form-modal" onSubmit={save} onMouseDown={(event) => event.stopPropagation()}><div><h2>Register external client</h2></div><label>Name<input required placeholder="Legal drafting assistant" value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} /></label><label>Protocol<select value={form.kind} onChange={(event) => setForm({ ...form, kind: event.target.value })}><option value="mcp">MCP</option><option value="api">REST API</option></select></label><PrincipalPicker id="client-principal" required navigate={navigate} confirmUnknown={false} value={form.principal} onChange={(value) => setForm((current) => ({ ...current, principal: value }))} placeholder="service:legal-drafting-assistant" hint="Grant it to projects afterwards — registration alone gives it no access." /><label>Secret reference<input value={form.secret_ref} onChange={(event) => setForm({ ...form, secret_ref: event.target.value })} /></label><div className="modal-actions"><button type="button" className="text-button" onClick={onClose}>Cancel</button><button className="primary-button">Register client</button></div></form></div>;
}
