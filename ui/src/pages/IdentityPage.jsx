import { useMemo, useState } from "react";
import { AlertTriangle, Ban, Check, Clipboard, ExternalLink, KeyRound, Link2, LoaderCircle, Plus, Power, ShieldCheck, Trash2, UserRound, UserRoundPlus, X } from "lucide-react";
import { api } from "../api";
import { useApi } from "../hooks";
import { Badge, EmptyState, SectionHeading } from "../components/Primitives";

const GLYPH = { google: "G", entra: "MS", okta: "OK", oidc: "ID" };

export default function IdentityPage({ identity }) {
  const admin = Boolean(identity?.is_admin);
  const providers = useApi("/api/identity/providers", [], admin);
  const people = useApi("/api/identity/people", [], admin);
  const [configure, setConfigure] = useState(null);
  const [link, setLink] = useState(null);
  const [add, setAdd] = useState(false);
  // Shown exactly once, like the OAuth client secret. Nothing can read it back.
  const [issued, setIssued] = useState(null);
  const data = providers.data;
  const brokers = (data?.providers || []).filter((item) => item.configured).map((item) => item.display_name || item.kind);

  if (!admin) return <EmptyState title="Administrators only" copy="Sign-in setup is an administrator task." />;

  return (
    <>
      <div className="hero-row compact-hero"><div><span className="eyebrow">Sign-in</span><h1>Sign-in</h1><p>Configured here and written to the realm for you. There is no second console to open.</p></div></div>

      {data?.realm_error && <div className="id-alert"><AlertTriangle size={15} /><div><strong>Cannot reach the realm</strong><span>{data.realm_error}</span></div></div>}

      <section className="panel">
        <SectionHeading eyebrow="Providers" title="Where people log in" copy="Pick one, paste the client id and secret from that provider." />
        <div className="id-provider-grid">
          {(data?.catalog || []).map((entry) => {
            const state = (data.providers || []).find((item) => item.kind === entry.kind && item.alias === entry.kind);
            return <ProviderCard key={entry.kind} entry={entry} state={state} onConfigure={() => setConfigure({ entry, state })} onChanged={() => { providers.reload(); people.reload(); }} />;
          })}
        </div>
      </section>

      {Boolean(data?.token_claims?.length) && <section className="panel id-claims">
        <SectionHeading eyebrow="Realm" title="Token settings this appliance requires" copy="Asserted on every save. Nothing to do while these are green." />
        <div className="id-check-list">{data.token_claims.map((check) => <CheckRow key={check.id} check={check} />)}</div>
      </section>}

      <People people={people} onLink={setLink} onAdd={() => setAdd(true)} onIssued={setIssued} />

      {configure && <ConfigureModal {...configure} onClose={() => setConfigure(null)} onSaved={() => { setConfigure(null); providers.reload(); people.reload(); }} />}
      {link && <LinkModal person={link} candidates={people.data?.source_identities || []} onClose={() => setLink(null)} onSaved={() => { setLink(null); people.reload(); }} />}
      {add && <AddPersonModal data={people.data} brokers={brokers} onClose={() => setAdd(false)} onCreated={(result) => { setAdd(false); setIssued(result); people.reload(); }} />}
      {issued && <PasswordOnce result={issued} onClose={() => setIssued(null)} />}
    </>
  );
}

function ProviderCard({ entry, state, onConfigure, onChanged }) {
  const [busy, setBusy] = useState("");
  const [result, setResult] = useState(null);
  const configured = Boolean(state?.configured);
  const test = async () => {
    setBusy("test");
    try { setResult(await api(`/api/identity/providers/${entry.kind}/test`, { method: "POST" })); }
    catch (error) { setResult({ ok: false, checks: [{ id: "error", label: "Test failed", ok: false, detail: error.message }] }); }
    finally { setBusy(""); onChanged(); }
  };
  const remove = async () => {
    setBusy("remove");
    try { await api(`/api/identity/providers/${entry.kind}`, { method: "DELETE" }); setResult(null); }
    finally { setBusy(""); onChanged(); }
  };
  const checks = result?.checks || state?.checks || [];
  const verdict = result ? result.ok : state?.last_test_ok;

  return (
    <article className={`id-provider ${configured ? "on" : ""}`}>
      <div className="id-provider-top">
        <i className="id-glyph">{GLYPH[entry.kind] || "ID"}</i>
        <div><strong>{entry.label}</strong><span>{configured ? state.issuer || "configured" : "not configured"}</span></div>
        {configured && <Badge tone={verdict === true ? "green" : verdict === false ? "red" : "neutral"}>{verdict === true ? "tested" : verdict === false ? "failing" : "untested"}</Badge>}
      </div>
      {configured && <code className="id-client">{state.client_id}</code>}
      <RedirectUri value={state?.redirect_uri} />
      {Boolean(checks.length) && <div className="id-check-list compact">{checks.map((check) => <CheckRow key={check.id} check={check} />)}</div>}
      <div className="id-provider-actions">
        <button className="secondary-button small" onClick={onConfigure}>{configured ? "Replace credentials" : <><Plus size={13} /> Configure</>}</button>
        {configured && <button className="secondary-button small" disabled={Boolean(busy)} onClick={test}>{busy === "test" ? <LoaderCircle size={13} className="spin" /> : <ShieldCheck size={13} />} Test sign-in</button>}
        {configured && <button className="icon-mini danger" title="Remove" disabled={Boolean(busy)} onClick={remove}><Trash2 size={13} /></button>}
      </div>
    </article>
  );
}

// The redirect URI is the number one setup failure: it has to be registered at the
// provider character-for-character, and it is needed before there is anything to paste
// back here. Shown on every card, configured or not.
function RedirectUri({ value }) {
  const [copied, setCopied] = useState(false);
  if (!value) return null;
  return <div className="id-redirect">
    <span>Redirect URI to register</span>
    <div><code>{value}</code><button className="icon-mini" title="Copy" onClick={() => { navigator.clipboard.writeText(value); setCopied(true); setTimeout(() => setCopied(false), 1600); }}>{copied ? <Check size={13} /> : <Clipboard size={13} />}</button></div>
  </div>;
}

function CheckRow({ check }) {
  return <div className={`id-check ${check.ok ? "ok" : "bad"}`}>{check.ok ? <Check size={12} /> : <X size={12} />}<b>{check.label}</b><span>{check.detail}</span></div>;
}

/** The mismatch view: sign-in identity against the identities connectors mirrored. */
function People({ people, onLink, onAdd, onIssued }) {
  const data = people.data;
  const sources = data?.sources_reporting_identities || [];
  const rows = data?.people || [];
  return (
    <section className="panel id-people">
      <SectionHeading eyebrow="People" title="Signed in, and recognised by the sources" copy={sources.length ? `Matched against ${sources.join(", ")}.` : "No connector has mirrored any identities yet."} action={<><span className="table-count">{rows.length} in the realm</span><button className="secondary-button small" disabled={!data || Boolean(data.realm_error)} onClick={onAdd}><UserRoundPlus size={13} /> Add person</button></>} />
      {data?.realm_error && <div className="id-alert"><AlertTriangle size={15} /><div><strong>Cannot list users</strong><span>{data.realm_error}</span></div></div>}
      {rows.length ? <div className="data-table"><div className="table-head id-people-head"><span>Person</span><span>Last seen here</span><span>Signs in via</span><span>Source match</span><span /></div>
        {rows.map((person) => <PersonRow key={person.id} person={person} sources={sources} onLink={onLink} onIssued={onIssued} onChanged={() => people.reload()} />)}
      </div> : !data?.realm_error && <EmptyState title="No users in the realm" copy="Add a person, or configure a provider above." />}
    </section>
  );
}

function PersonRow({ person, sources, onLink, onIssued, onChanged }) {
  const [busy, setBusy] = useState("");
  const [confirm, setConfirm] = useState(false);
  const [failed, setFailed] = useState("");
  const matched = person.matched_sources.length;
  const bad = sources.length > 0 && matched === 0;
  const local = person.federated.length === 0;

  const run = async (job, path, options) => {
    setBusy(job);
    setFailed("");
    try {
      const result = await api(path, options);
      if (result?.temporary_password) onIssued(result);
    } catch (error) { setFailed(error.message); }
    finally { setBusy(""); setConfirm(false); onChanged(); }
  };

  return <div className={`table-row id-people-head ${person.enabled ? "" : "id-off"}`}>
    <span className="primary-cell"><i className="run-icon"><UserRound size={14} /></i><span><strong>{person.username}</strong><small>{person.name || person.email || "no email"}</small></span></span>
    <span>{person.last_seen ? new Date(person.last_seen).toLocaleString() : <em className="id-muted">never</em>}</span>
    <span>{person.federated.length ? person.federated.join(", ") : <em className="id-muted">password</em>}{!person.enabled && <Badge tone="red">disabled</Badge>}</span>
    <span className={`id-match ${bad ? "bad" : matched ? "ok" : ""}`}>
      {sources.length ? <>{bad ? <AlertTriangle size={12} /> : <Check size={12} />} matched in {matched} of {sources.length}</> : <em className="id-muted">no sources</em>}
      {person.alias && <small>via alias {person.alias}</small>}
      {bad && !person.alias && <small>sees nothing from {person.unmatched_sources.join(", ")}</small>}
    </span>
    <span className="id-row-actions">
      {bad && <button className="secondary-button small" onClick={() => onLink(person)}><Link2 size={13} /> Link</button>}
      {local && <button className="icon-mini" title="Reset password" disabled={Boolean(busy)} onClick={() => run("password", `/api/identity/people/${person.id}/password`, { method: "POST" })}>{busy === "password" ? <LoaderCircle size={13} className="spin" /> : <KeyRound size={13} />}</button>}
      {/* The self guard is enforced by the endpoint too; hiding the button here only
          saves an administrator from reading an error about their own account. */}
      <button className={`icon-mini ${person.enabled ? "" : "on"}`} title={person.is_self ? "This is your account" : person.enabled ? "Disable" : "Enable"} disabled={Boolean(busy) || person.is_self} onClick={() => run("enabled", `/api/identity/people/${person.id}/enabled`, { method: "POST", body: JSON.stringify({ enabled: !person.enabled }) })}>{person.enabled ? <Ban size={13} /> : <Power size={13} />}</button>
      {confirm
        ? <button className="secondary-button small danger" disabled={Boolean(busy)} onClick={() => run("delete", `/api/identity/people/${person.id}`, { method: "DELETE" })}>{busy === "delete" ? <LoaderCircle size={13} className="spin" /> : null} Delete for good</button>
        : <button className="icon-mini danger" title={person.is_self ? "This is your account" : "Delete"} disabled={Boolean(busy) || person.is_self} onClick={() => setConfirm(true)}><Trash2 size={13} /></button>}
      {failed && <small className="id-row-error">{failed}</small>}
    </span>
  </div>;
}

/** Create a person who signs in with a password, for a firm with no directory. */
function AddPersonModal({ data, brokers, onClose, onCreated }) {
  const [form, setForm] = useState({ email: "", first_name: "", last_name: "" });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const known = data?.source_identity_sources || {};
  const witnesses = data?.sources_reporting_identities || [];
  const email = form.email.trim().toLowerCase();
  // The same index the mismatch table is built from, asked while the admin still types.
  const match = known[email] || [];
  const suggestions = useMemo(() => Object.keys(known).sort().slice(0, 200), [known]);

  const save = async (event) => {
    event.preventDefault();
    setBusy(true);
    setError("");
    try { onCreated(await api("/api/identity/people", { method: "POST", body: JSON.stringify({ ...form, email }) })); }
    catch (caught) { setError(caught.message); }
    finally { setBusy(false); }
  };

  return <div className="modal-backdrop" onMouseDown={onClose}>
    <form className="form-modal" onSubmit={save} onMouseDown={(event) => event.stopPropagation()}>
      <div><span className="eyebrow">People</span><h2>Add a person</h2>{brokers.length > 0 && <p>{brokers.join(" and ")} {brokers.length > 1 ? "are" : "is"} configured. Use a local account only for someone with no account there.</p>}</div>
      <label>Email
        <input required autoFocus type="email" list="id-known-identities" placeholder="u.schmidt@firm.de" value={form.email} onChange={(event) => setForm({ ...form, email: event.target.value })} />
        <datalist id="id-known-identities">{suggestions.map((item) => <option key={item} value={item} />)}</datalist>
        <small className="field-hint">Their sign-in name. Access is decided by matching it against what the connectors mirrored.</small>
      </label>
      {/* Nothing is said until the mirrored index has actually arrived: "no source
          reports this" while the request is still in flight is the exact wrong answer. */}
      {email.includes("@") && Boolean(data) && <EmailMatch email={email} match={match} witnesses={witnesses} />}
      <div className="id-name-row">
        <label>First name<input value={form.first_name} onChange={(event) => setForm({ ...form, first_name: event.target.value })} /></label>
        <label>Last name<input value={form.last_name} onChange={(event) => setForm({ ...form, last_name: event.target.value })} /></label>
      </div>
      {error && <div className="id-alert inline"><AlertTriangle size={15} /><div><strong>Not created</strong><span>{error}</span></div></div>}
      <div className="modal-actions"><button type="button" className="text-button" onClick={onClose}>Cancel</button><button className="primary-button" disabled={busy || !email}>{busy ? "Creating…" : "Create and issue password"}</button></div>
    </form>
  </div>;
}

// The whole point of the feature: an address no source reported produces an account
// that works and shows nothing, with no error anywhere. Said here, before it is made.
function EmailMatch({ email, match, witnesses }) {
  if (!witnesses.length) return <div className="id-match-note"><Check size={13} /><span>No connector reports identities yet.</span></div>;
  if (match.length) return <div className="id-match-note ok"><Check size={13} /><span>Known to {match.join(", ")}.</span></div>;
  return <div className="id-match-note bad"><AlertTriangle size={13} /><span>No source reports <b>{email}</b>. This person will see nothing until one does.</span></div>;
}

/** Shown once, then gone. Knowledge Index never stores it and cannot show it again. */
function PasswordOnce({ result, onClose }) {
  const [copied, setCopied] = useState(false);
  const unmatched = (result.matched_sources || []).length === 0 && (result.sources_reporting_identities || []).length > 0;
  return <div className="modal-backdrop" onMouseDown={onClose}>
    <div className="form-modal" onMouseDown={(event) => event.stopPropagation()}>
      <div><span className="eyebrow">Temporary password</span><h2>{result.username}</h2><p>Shown once. They must change it at first sign-in.</p></div>
      <div className="id-secret"><code>{result.temporary_password}</code><button className="icon-mini" title="Copy" onClick={() => { navigator.clipboard.writeText(result.temporary_password); setCopied(true); setTimeout(() => setCopied(false), 1600); }}>{copied ? <Check size={13} /> : <Clipboard size={13} />}</button></div>
      {unmatched && <div className="id-match-note bad"><AlertTriangle size={13} /><span>No source reports this address yet, so they will see nothing.</span></div>}
      <div className="modal-actions"><button className="primary-button" onClick={onClose}>Done</button></div>
    </div>
  </div>;
}

function ConfigureModal({ entry, state, onClose, onSaved }) {
  const [form, setForm] = useState({ client_id: state?.client_id || "", client_secret: "", extra: state?.extra_value || "" });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const save = async (event) => {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await api("/api/identity/providers", { method: "POST", body: JSON.stringify({ kind: entry.kind, ...form }) });
      onSaved();
    } catch (caught) { setError(caught.message); }
    finally { setBusy(false); }
  };
  return <div className="modal-backdrop" onMouseDown={onClose}>
    <form className="form-modal" onSubmit={save} onMouseDown={(event) => event.stopPropagation()}>
      <div><span className="eyebrow">Sign-in</span><h2>{entry.label}</h2></div>
      <RedirectUri value={state?.redirect_uri} />
      {entry.console_url && <a className="row-link" href={entry.console_url} target="_blank" rel="noreferrer">Open {entry.console} <ExternalLink size={12} /></a>}
      {entry.field && <label>{entry.field_label}<input required placeholder={entry.field_placeholder} value={form.extra} onChange={(event) => setForm({ ...form, extra: event.target.value })} />{entry.field_hint && <small className="field-hint">{entry.field_hint}</small>}</label>}
      <label>Client ID<input required autoComplete="off" value={form.client_id} onChange={(event) => setForm({ ...form, client_id: event.target.value })} /></label>
      {/* Write-only. It goes to the realm and to an encrypted row; no endpoint returns it. */}
      <label>Client secret<input required type="password" autoComplete="new-password" placeholder={state?.configured ? "paste again to replace" : ""} value={form.client_secret} onChange={(event) => setForm({ ...form, client_secret: event.target.value })} /></label>
      {error && <div className="id-alert inline"><AlertTriangle size={15} /><div><strong>Not saved</strong><span>{error}</span></div></div>}
      <div className="modal-actions"><button type="button" className="text-button" onClick={onClose}>Cancel</button><button className="primary-button" disabled={busy}>{busy ? "Checking with the provider…" : "Save and verify"}</button></div>
    </form>
  </div>;
}

function LinkModal({ person, candidates, onClose, onSaved }) {
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const principal = `user:${(person.email || person.username || "").toLowerCase()}`;
  const options = useMemo(() => candidates.filter((item) => item && item !== (person.email || "").toLowerCase()), [candidates, person]);
  const save = async (event) => {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await api("/api/identity/aliases", { method: "POST", body: JSON.stringify({ principal, alias: `user:${value}` }) });
      onSaved();
    } catch (caught) { setError(caught.message); }
    finally { setBusy(false); }
  };
  return <div className="modal-backdrop" onMouseDown={onClose}>
    <form className="form-modal" onSubmit={save} onMouseDown={(event) => event.stopPropagation()}>
      <div><span className="eyebrow">Identity</span><h2>Link {person.username}</h2><p>Same person, different address at the source.</p></div>
      <label>Signs in as<input readOnly value={principal} /></label>
      <label>Known at the source as
        <input required list="id-source-identities" placeholder="u.schmidt@firm.de" value={value} onChange={(event) => setValue(event.target.value.trim().toLowerCase())} />
        <datalist id="id-source-identities">{options.map((item) => <option key={item} value={item} />)}</datalist>
      </label>
      {error && <div className="id-alert inline"><AlertTriangle size={15} /><div><strong>Not saved</strong><span>{error}</span></div></div>}
      <div className="modal-actions"><button type="button" className="text-button" onClick={onClose}>Cancel</button><button className="primary-button" disabled={busy || !value}>Link</button></div>
    </form>
  </div>;
}
