import { useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle, ArrowRight, Ban, Building2, Check, CornerDownRight, FileText, Link2, Plus, Save, Search, ShieldBan, ShieldCheck, UserRound, UsersRound, X } from "lucide-react";
import { api } from "../api";
import { useApi } from "../hooks";
import { Badge, EmptyState, SectionHeading } from "../components/Primitives";
import PrincipalPicker, { principalKindHint } from "../components/PrincipalPicker";

const SCOPE_LABEL = { source: "source ACL", project: "project grant", document: "document grant" };

export default function AccessPage({ identity, focus }) {
  const admin = Boolean(identity?.is_admin);
  const projects = useApi("/api/projects");
  const principals = useApi("/api/principals", [], admin);
  const [showProjectForm, setShowProjectForm] = useState(false);
  const [lookup, setLookup] = useState("");
  const checkRef = useRef(null);
  // Jobs 2 and 3 write grants that job 1 then has to report — refetch the lookup so the
  // page never shows an answer it has itself just invalidated.
  const [revision, setRevision] = useState(0);
  const askAbout = (principal) => { setLookup(principal); setRevision((current) => current + 1); checkRef.current?.scrollIntoView({ behavior: "smooth", block: "start" }); };
  // Picked out of the command palette: ask the question the operator came here to ask.
  useEffect(() => { if (focus?.principal) askAbout(focus.principal); }, [focus]);

  return (
    <>
      <div className="hero-row compact-hero">
        <div><h1>Access control</h1></div>
      </div>

      <Onboarding />

      <div ref={checkRef}><AccessCheck principals={principals.data || []} value={lookup} onValue={setLookup} revision={revision} /></div>

      {admin && <div className="ac-write-row">
        <GrantPanel effect="allow" projects={projects.data || []} principals={principals.data || []} onSaved={askAbout} />
        <GrantPanel effect="deny" projects={projects.data || []} principals={principals.data || []} onSaved={askAbout} />
      </div>}

      <ProjectsPanel projects={projects} principals={principals.data || []} admin={admin} onNew={() => setShowProjectForm(true)} onAsk={askAbout} />

      {admin && <GroupAliases principals={principals.data || []} />}
      {admin && <AdminSettings principals={principals.data || []} />}

      {showProjectForm && <ProjectModal onClose={() => setShowProjectForm(false)} onSaved={async () => { setShowProjectForm(false); await projects.reload(); }} />}
    </>
  );
}

// The question this page failed to answer: "what do I do so Ursula can use the index?".
// The answer is "nothing, here" — so it is stated before anything else, as steps rather
// than as an explanation of the permission model.
function Onboarding() {
  return (
    <section className="panel ac-onboarding">
      <div className="ac-onboarding-head"><strong>Someone new needs access?</strong><span>There is no user list here to add them to.</span></div>
      <ol className="ac-steps">
        <li><b>1</b><div><strong>Add them to the group at the source</strong><span>Entra, SharePoint, your directory. The next sync mirrors it.</span></div></li>
        <li><b>2</b><div><strong>They sign in with their work account</strong><span>Same login as everywhere else. No invite from here.</span></div></li>
        <li><b>3</b><div><strong>They see exactly what the source allows</strong><span>Every search, every AI tool, same answer.</span></div></li>
      </ol>
    </section>
  );
}

/** Job 1 — look a person up and show what they can reach, and on whose authority. */
function AccessCheck({ principals, value, onValue, revision }) {
  const [filter, setFilter] = useState("");
  const [probe, setProbe] = useState("");
  // Typing in the filter box must not re-query on every keystroke; the answer is a
  // permission evaluation over the whole corpus, not an autocomplete.
  useEffect(() => { const timer = setTimeout(() => setProbe(filter.trim()), 250); return () => clearTimeout(timer); }, [filter]);
  const asked = value.trim();
  const path = asked ? `/api/access/explain?principal=${encodeURIComponent(asked)}${probe ? `&query=${encodeURIComponent(probe)}` : ""}&limit=60` : null;
  const result = useApi(path, [revision], Boolean(asked));
  const data = result.data;
  const nothing = data && !data.is_admin && !data.groups.length && !data.local_grants.length;

  return (
    <section className="panel ac-check">
      <SectionHeading eyebrow="Check" title="Can this person see it?" />
      <div className="ac-check-form">
        <PrincipalPicker id="access-check" label="Person or group" value={value} onChange={(next) => onValue(next)} principals={principals} confirmUnknown={false} placeholder="user:ursula@firm.example" hint="They appear after a first sign-in, or a sync that mirrors their group." />
        <label className="ac-filter">Filter documents<span className="ac-filter-input"><Search size={13} /><input placeholder="engagement letter" value={filter} onChange={(event) => setFilter(event.target.value)} /></span></label>
      </div>

      {!asked && <EmptyState title="Nobody selected" copy="Pick a person or group above." />}
      {asked && result.error && <div className="ac-alert"><AlertTriangle size={15} /><div><strong>Could not evaluate</strong><span>{result.error.message}</span></div></div>}
      {asked && !data && result.loading && <div className="ac-loading">Evaluating…</div>}

      {data && <>
        <div className="ac-verdict">
          <div className="ac-verdict-main"><span>Documents reachable</span><strong>{data.documents.visible}<i>of {data.documents.total}</i></strong></div>
          <div><span>Mirrored groups</span><strong>{data.groups.length}</strong></div>
          <div><span>Grants made here</span><strong>{data.local_grants.length}</strong></div>
          <div><span>Combination mode</span><strong className="mono">{data.source_acl_mode}</strong></div>
        </div>

        {data.is_admin && <div className="ac-note admin"><ShieldCheck size={15} /><div><strong>Administrator — sees every document</strong><span>Holds <code>role:admin</code>, which skips project, document and source grants entirely.</span></div></div>}

        {nothing && <div className="ac-note warn"><AlertTriangle size={15} /><div><strong>Reaches nothing</strong><span>In no mirrored group and named by no grant here. Add them to the group that owns the documents at the source, then sync that connector.</span></div></div>}

        {Boolean(data.groups.length) && <div className="ac-chain">
          <div className="ac-chain-self"><UserRound size={14} /><code>{data.principal}</code></div>
          <div className="ac-chain-groups">{data.groups.map((group) => <div className="ac-group" key={group.principal}>
            <CornerDownRight size={13} />
            <div>
              <code>{group.principal}</code>
              <span>{group.label || group.source} · {group.member_count} member{group.member_count === 1 ? "" : "s"} · opens {group.documents} document{group.documents === 1 ? "" : "s"} · {group.direct ? "direct member" : "through a nested group"}</span>
              {/* Entra reports this group only by its object id, so the mirrored member
                  list is the only thing that tells two GUIDs apart. */}
              {Boolean(group.members.length) && <small>{group.members.join(", ")}{group.member_count > group.members.length ? ` +${group.member_count - group.members.length}` : ""}</small>}
            </div>
          </div>)}</div>
        </div>}

        {Boolean(data.local_grants.length) && <div className="ac-locals">{data.local_grants.map((grant, index) => <span className={`ac-local ${grant.effect}`} key={`${grant.scope}-${grant.target_id}-${index}`}>{grant.effect === "deny" ? <Ban size={11} /> : <Check size={11} />}<b>{grant.effect}</b> {grant.role} on {grant.scope} <strong>{grant.target}</strong> <em>as {grant.principal}</em></span>)}</div>}

        <div className="ac-doc-head"><span>Document</span><span>Access</span><span>Why</span></div>
        {data.documents.items.length ? <div className="ac-docs">{data.documents.items.map((item) => <DocumentVerdict key={item.id} item={item} />)}</div> : <div className="ac-loading">No document matches “{probe}”.</div>}
        {data.documents.listed < data.documents.total && !probe && <small className="ac-more">Showing the {data.documents.listed} most recently updated of {data.documents.total}. Filter to reach the rest.</small>}
      </>}
    </section>
  );
}

function DocumentVerdict({ item }) {
  const denied = item.denied_by.length > 0;
  // A group nobody is mirrored into cannot be the fix, so the actionable memberships
  // come first and the rest collapse into a count — otherwise every blocked row is
  // four identical GUIDs tall and reads as noise.
  const routes = useMemo(() => [...item.source_allows].sort((a, b) => b.members - a.members), [item.source_allows]);
  return (
    <div className={`ac-doc ${item.visible ? "yes" : "no"}`}>
      <span className="ac-doc-name"><FileText size={14} /><span><strong>{item.title}</strong><small>{item.path || item.source || "no source observation"}</small></span></span>
      <span>{item.visible ? <Badge tone="green">visible</Badge> : denied ? <Badge tone="red">denied</Badge> : <Badge>blocked</Badge>}</span>
      <span className="ac-doc-why">
        {denied && item.denied_by.map((entry, index) => <em className="deny" key={`d${index}`}><Ban size={10} /> {SCOPE_LABEL[entry.scope]} denies <code>{entry.principal}</code></em>)}
        {!denied && item.visible && item.allowed_by.map((entry, index) => <em key={`a${index}`}><Check size={10} /> {SCOPE_LABEL[entry.scope]} allows <code>{entry.principal}</code></em>)}
        {/* A blocked document is the actual complaint ("why can't she see this?"), so the
            answer names the memberships that would open it rather than saying "no match". */}
        {!denied && !item.visible && (routes.length ? <em className="need">Needs membership of {routes.slice(0, 2).map((entry) => <code key={entry.principal} title={`${entry.members} mirrored member${entry.members === 1 ? "" : "s"}`}>{entry.principal}{entry.members ? "" : " · nobody mirrored"}</code>)}{routes.length > 2 ? <b>+{routes.length - 2}</b> : null}</em> : <em className="need">No mirrored ACL and no grant here.</em>)}
      </span>
    </div>
  );
}

/** Jobs 2 and 3 — one form, two very different acts, so they never share a button. */
function GrantPanel({ effect, projects, principals, onSaved }) {
  const deny = effect === "deny";
  const [principal, setPrincipal] = useState("");
  const [kind, setKind] = useState("group");
  const [role, setRole] = useState("viewer");
  const [target, setTarget] = useState(null);
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [done, setDone] = useState("");
  const ready = Boolean(principal.trim() && target) && (!deny || confirmed);

  const submit = async (event) => {
    event.preventDefault();
    setBusy(true); setError(""); setDone("");
    try {
      const path = target.kind === "project" ? `/api/projects/${target.id}/grants` : `/api/documents/${target.id}/grants`;
      await api(path, { method: "POST", body: JSON.stringify({ principal: principal.trim(), principal_kind: kind, role, effect }) });
      setDone(`${deny ? "Denied" : "Granted"} on ${target.label}.`);
      onSaved(principal.trim());
      setTarget(null); setConfirmed(false);
    } catch (caught) { setError(caught.message); } finally { setBusy(false); }
  };

  return (
    <form className={`panel ac-grant ${deny ? "ac-grant-deny" : ""}`} onSubmit={submit}>
      <SectionHeading eyebrow={deny ? "Wall" : "Exception"} title={deny ? "Make sure someone never sees it" : "Give access the source did not"} copy={deny ? "A deny beats every allow, here and at the source. It cannot be overridden." : "For the rare document the source shares with the wrong group."} />
      <PrincipalPicker id={`${effect}-principal`} label="Person or group" value={principal} onChange={(next, match) => { setPrincipal(next); setKind(match ? match.principal_kind : (next.includes(":") ? principalKindHint(next) : kind)); }} principals={principals} hint={deny ? "Exact match. A principal they do not actually hold walls off nobody." : "Exact match. A principal they do not actually hold grants nothing."} />
      <TargetPicker id={`${effect}-target`} projects={projects} value={target} onChange={setTarget} />
      {!deny && <label>Role<select value={role} onChange={(event) => setRole(event.target.value)}><option>viewer</option><option>editor</option><option>admin</option><option>owner</option></select></label>}
      {deny && <label className="ac-confirm"><input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} /><span>{principal.trim() && target ? <>Wall <code>{principal.trim()}</code> off from <b>{target.label}</b> permanently.</> : "Pick a person and a target first."}</span></label>}
      {error && <div className="ac-alert"><AlertTriangle size={14} /><div><strong>Not saved</strong><span>{error}</span></div></div>}
      {done && <div className="ac-note ok"><Check size={14} /><div><strong>{done}</strong></div></div>}
      <button className={deny ? "ac-deny-button" : "primary-button"} type="submit" disabled={!ready || busy}>{deny ? <ShieldBan size={14} /> : <ShieldCheck size={14} />} {busy ? "Saving…" : deny ? "Deny access" : "Grant access"}</button>
    </form>
  );
}

/** Picks the thing a grant lands on: a project boundary, or one document by name. */
function TargetPicker({ id, projects, value, onChange }) {
  const [term, setTerm] = useState("");
  const [probe, setProbe] = useState("");
  const [open, setOpen] = useState(false);
  const wrapper = useRef(null);
  useEffect(() => { const timer = setTimeout(() => setProbe(term.trim()), 220); return () => clearTimeout(timer); }, [term]);
  useEffect(() => {
    if (!open) return undefined;
    const away = (event) => { if (!wrapper.current?.contains(event.target)) setOpen(false); };
    document.addEventListener("mousedown", away);
    return () => document.removeEventListener("mousedown", away);
  }, [open]);
  const documents = useApi(probe.length > 1 ? `/api/documents?limit=8&query=${encodeURIComponent(probe)}` : null, [], probe.length > 1);
  const matched = useMemo(() => projects.filter((item) => !probe || `${item.key} ${item.name}`.toLowerCase().includes(probe.toLowerCase())), [projects, probe]);

  return (
    <div className="ac-target" ref={wrapper}>
      <label htmlFor={id}>Where
        <span className="ac-target-input">{value?.kind === "project" ? <Building2 size={13} /> : <FileText size={13} />}
          <input id={id} autoComplete="off" placeholder={value ? value.label : "Project key or document title"} value={term} onFocus={() => setOpen(true)} onChange={(event) => { setTerm(event.target.value); setOpen(true); }} />
          {value && <button type="button" className="chip-remove" title="Clear" onClick={() => { onChange(null); setTerm(""); }}><X size={10} /></button>}
        </span>
      </label>
      {value && <small className="ac-target-chosen">{value.kind === "project" ? "Project" : "Document"} · {value.label}</small>}
      {open && <div className="ac-target-menu">
        {matched.length > 0 && <span className="principal-menu-label">Projects</span>}
        {matched.map((item) => <button type="button" key={item.id} onClick={() => { onChange({ kind: "project", id: item.id, label: `${item.key} · ${item.name}` }); setTerm(""); setOpen(false); }}><Building2 size={12} /><span>{item.key} · {item.name}</span><small>{item.documents} docs</small></button>)}
        {probe.length > 1 && <span className="principal-menu-label">Documents</span>}
        {probe.length > 1 && (documents.data || []).map((item) => <button type="button" key={item.id} onClick={() => { onChange({ kind: "document", id: item.id, label: item.title || item.id }); setTerm(""); setOpen(false); }}><FileText size={12} /><span>{item.title || item.id}</span><small>{item.doc_type || "document"}</small></button>)}
        {probe.length <= 1 && !matched.length && <div className="principal-menu-empty">Type at least two characters to find a document.</div>}
        {probe.length > 1 && !documents.loading && !(documents.data || []).length && !matched.length && <div className="principal-menu-empty">Nothing matches “{probe}”.</div>}
      </div>}
    </div>
  );
}

function ProjectsPanel({ projects, principals, admin, onNew, onAsk }) {
  const [selectedId, setSelectedId] = useState("");
  const selected = projects.data?.find((item) => item.id === selectedId);
  const grants = useApi(selectedId ? `/api/projects/${selectedId}/grants` : null, [selectedId], Boolean(selectedId && selected?.can_manage));
  return (
    <section className="panel ac-projects">
      <SectionHeading eyebrow="Local boundaries" title="Projects" copy="Optional. A project groups documents so one grant covers all of them." action={admin && <button className="secondary-button small" onClick={onNew}><Plus size={14} /> New project</button>} />
      {(projects.data || []).length ? <div className="ac-project-list">{projects.data.map((project) => <button className={selectedId === project.id ? "active" : ""} key={project.id} onClick={() => setSelectedId(selectedId === project.id ? "" : project.id)}><div className="project-monogram">{project.key.slice(0, 2).toUpperCase()}</div><div><strong>{project.name}</strong><span>{project.key} · {project.documents} documents · {project.sources} sources</span></div>{project.can_manage && <Badge tone="purple">manage</Badge>}</button>)}</div> : <EmptyState title="No projects" copy="Only needed when you want a boundary of your own." />}
      {selected && (selected.can_manage ? ((grants.data || []).length ? <div className="ac-project-grants">{grants.data.map((grant) => <div key={grant.id}><span className="principal-cell">{grant.principal_kind === "user" ? <UserRound size={14} /> : <UsersRound size={14} />}<span><strong>{grant.principal}</strong><small>{grant.role} · {grant.origin}</small></span></span><Badge tone={grant.effect === "deny" ? "red" : "green"}>{grant.effect}</Badge><button type="button" className="row-link" onClick={() => onAsk(grant.principal)}>Check <ArrowRight size={12} /></button></div>)}</div> : <div className="ac-loading">No grants on {selected.key}. Members reach its documents through the source instead.</div>) : <div className="ac-loading">Only this project's owners can see its grants.</div>)}
    </section>
  );
}

function GroupAliases({ principals }) {
  const config = useApi("/api/config");
  const [sourceGroup, setSourceGroup] = useState("");
  const [signInGroup, setSignInGroup] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const aliases = config.data?.security?.principal_aliases || {};
  const groupAliases = Object.entries(aliases).filter(([source, target]) => source.startsWith("group:") || target.startsWith("group:"));
  const normalizeGroup = (value) => {
    const cleaned = value.trim().toLowerCase();
    return cleaned && !cleaned.startsWith("group:") ? `group:${cleaned}` : cleaned;
  };
  const saveAliases = async (next, action) => {
    setBusy(action); setError("");
    try {
      const updated = structuredClone(config.data);
      updated.security.principal_aliases = next;
      await api("/api/config", { method: "PUT", body: JSON.stringify(updated) });
      await config.reload();
      return true;
    } catch (caught) { setError(caught.message); return false; }
    finally { setBusy(""); }
  };
  const add = async () => {
    const source = normalizeGroup(sourceGroup);
    const target = normalizeGroup(signInGroup);
    if (!source || !target) return;
    if (await saveAliases({ ...aliases, [source]: target }, "add")) {
      setSourceGroup(""); setSignInGroup("");
    }
  };
  const remove = (source) => saveAliases(Object.fromEntries(Object.entries(aliases).filter(([key]) => key !== source)), source);

  return <section className="panel">
    <SectionHeading eyebrow="Identity bridge" title="Source group aliases" copy="Use this only for a legacy connector or an external group that the source tenant cannot enumerate. It maps that source group to a group your sign-in provider already asserts." />
    {groupAliases.length > 0 && <div className="ac-project-grants">{groupAliases.map(([source, target]) => <div key={source}>
      <span className="principal-cell"><Link2 size={14} /><span><strong>{source}</strong><small>source permission</small></span></span>
      <span className="mono">{target}</span>
      <button type="button" className="row-link" disabled={Boolean(busy)} onClick={() => remove(source)}>{busy === source ? "Removing…" : "Remove"}</button>
    </div>)}</div>}
    <div className="form-columns">
      <PrincipalPicker id="source-group-alias" label="Group at the source" kinds={["group"]} confirmUnknown={false} value={sourceGroup} onChange={setSourceGroup} principals={principals} placeholder="group:google:litigation@firm.example" hint="The principal shown on the mirrored document ACL." />
      <PrincipalPicker id="signin-group-alias" label="Matching sign-in group" kinds={["group"]} confirmUnknown={false} value={signInGroup} onChange={setSignInGroup} principals={principals} placeholder="group:litigation" hint="A group present in the caller's validated sign-in token." />
    </div>
    {error && <div className="ac-alert"><AlertTriangle size={15} /><div><strong>Alias not saved</strong><span>{error}</span></div></div>}
    <button type="button" className="secondary-button small" disabled={!config.data || Boolean(busy) || !sourceGroup.trim() || !signInGroup.trim()} onClick={add}><Link2 size={13} /> {busy === "add" ? "Saving…" : "Add group alias"}</button>
  </section>;
}

function AdminSettings({ principals }) {
  const config = useApi("/api/config");
  const [draft, setDraft] = useState(null);
  const [saving, setSaving] = useState(false);
  useEffect(() => { if (config.data) setDraft(structuredClone(config.data)); }, [config.data]);
  const update = (key, value) => setDraft((current) => ({ ...current, security: { ...current.security, [key]: value } }));
  const save = async () => { setSaving(true); try { await api("/api/config", { method: "PUT", body: JSON.stringify(draft) }); await config.reload(); } finally { setSaving(false); } };
  if (!draft) return null;
  const security = draft.security;
  const isHeader = security.auth_mode === "trusted_header";

  return (
    <section className="panel security-panel">
      <SectionHeading eyebrow="Administrators" title="Who runs this appliance" copy="Members of these provider groups can read and change everything here." action={<button className="secondary-button small" onClick={save} disabled={saving}><Save size={14} /> {saving ? "Saving…" : "Save"}</button>} />
      <AdminGroupsField principals={principals} value={security.admin_groups || []} onChange={(next) => update("admin_groups", next)} />
      <details className="advanced-options">
        <summary>Identity gateway<small>{security.auth_mode}</small></summary>
        <div>
          <div className="form-columns">
            <label>Authentication mode<select value={security.auth_mode} onChange={(event) => update("auth_mode", event.target.value)}><option value="trusted_header">trusted_header</option><option value="oidc">oidc</option></select></label>
            <label>Unknown ACL policy<select value={security.unknown_acl_policy} onChange={(event) => update("unknown_acl_policy", event.target.value)}><option value="deny">deny</option><option value="allow">allow</option></select></label>
          </div>
          <div className="form-columns">
            <label>Trusted header name<input className="mono" value={security.trusted_header_name} disabled={!isHeader} onChange={(event) => update("trusted_header_name", event.target.value)} /></label>
            <label>Subject claim<input className="mono" value={security.subject_claim} onChange={(event) => update("subject_claim", event.target.value)} /></label>
            <label>Username claim<input className="mono" value={security.username_claim} onChange={(event) => update("username_claim", event.target.value)} /></label>
            <label>Groups claim<input className="mono" value={security.groups_claim} onChange={(event) => update("groups_claim", event.target.value)} /></label>
            <label>OIDC issuer<input className="mono" value={security.oidc_issuer} disabled={isHeader} onChange={(event) => update("oidc_issuer", event.target.value)} /></label>
            <label>OIDC audience<input className="mono" value={security.oidc_audience} disabled={isHeader} onChange={(event) => update("oidc_audience", event.target.value)} /></label>
          </div>
        </div>
      </details>
    </section>
  );
}

// Admin group names are stored bare and turned into `group:<name>` principals by the
// identity resolver. Picking them as whole principals keeps the operator looking at the
// exact string authorization will compare, rather than half of it inside a CSV field.
function AdminGroupsField({ principals, value, onChange }) {
  const [draft, setDraft] = useState("");
  const add = () => {
    const name = draft.trim().replace(/^group:/i, "").replace(/^\/+|\/+$/g, "");
    if (name && !value.some((item) => item.toLowerCase() === name.toLowerCase())) onChange([...value, name]);
    setDraft("");
  };
  return (
    <div className="security-field admin-groups-field">
      <div className="admin-group-chips">{value.length ? value.map((group) => <span className="principal-chip" key={group}><UsersRound size={11} /><code>group:{group}</code><button type="button" className="chip-remove" title={`Remove ${group}`} onClick={() => onChange(value.filter((item) => item !== group))}><X size={10} /></button></span>) : <span className="admin-groups-none"><AlertTriangle size={12} /> No administrator group configured — nobody is promoted to <code>role:admin</code> by their group membership.</span>}</div>
      <PrincipalPicker id="admin-group" label="Add an administrator group" kinds={["group"]} confirmUnknown={false} value={draft} onChange={(next) => setDraft(next)} principals={principals} placeholder="group:knowledge-index-admins" hint="Matched casefolded against the groups claim your provider sends." />
      <button type="button" className="secondary-button small" onClick={add} disabled={!draft.trim()}><Plus size={13} /> Add administrator group</button>
    </div>
  );
}

function ProjectModal({ onClose, onSaved }) {
  const [form, setForm] = useState({ key: "", name: "", description: "" });
  const save = async (event) => { event.preventDefault(); await api("/api/projects", { method: "POST", body: JSON.stringify(form) }); onSaved(); };
  return <div className="modal-backdrop" onMouseDown={onClose}><form className="form-modal" onSubmit={save} onMouseDown={(event) => event.stopPropagation()}><div><span className="eyebrow">Local boundary</span><h2>Create project</h2><p>You become the owner. Attach sources and documents afterwards.</p></div><label>Project key<input required placeholder="M-2026-0042" value={form.key} onChange={(event) => setForm({ ...form, key: event.target.value })} /></label><label>Name<input required placeholder="Acquisition Helios" value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} /></label><label>Description<textarea rows="3" placeholder="Optional" value={form.description} onChange={(event) => setForm({ ...form, description: event.target.value })} /></label><div className="modal-actions"><button type="button" className="text-button" onClick={onClose}>Cancel</button><button className="primary-button" type="submit">Create project</button></div></form></div>;
}
