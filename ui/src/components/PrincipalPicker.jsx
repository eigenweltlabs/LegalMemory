import { useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle, ArrowRight, Check, ChevronDown, ShieldCheck, TerminalSquare, UserRound, UsersRound } from "lucide-react";
import { useApi } from "../hooks";

// Authorization is an exact string match that fails closed: a grant to a principal
// nobody holds is indistinguishable from no grant at all, and produces no error
// anywhere. Free-text entry therefore has no feedback loop — the operator finds out
// weeks later that a group never had access. This picker exists to close that loop:
// offer the principals the appliance has actually seen, and when the operator insists
// on one it has not seen, say so instead of accepting it silently.

const KIND_ORDER = ["group", "user", "service", "role"];
const KIND_LABEL = { group: "Groups", user: "Users", service: "Service identities", role: "Roles" };
const ORIGIN_LABEL = { source: "source ACL", directory: "mirrored directory", project: "project grant", document: "document grant", client: "registered client", config: "identity config" };

function KindIcon({ kind, size = 13 }) {
  if (kind === "user") return <UserRound size={size} />;
  if (kind === "service") return <TerminalSquare size={size} />;
  if (kind === "role") return <ShieldCheck size={size} />;
  return <UsersRound size={size} />;
}

// The prefix is a convention, not a guarantee — mirror the server's own hint so the
// kind select and the icons never disagree with what the backend would infer.
export function principalKindHint(principal) {
  const prefix = principal.includes(":") ? principal.split(":", 1)[0].toLowerCase() : "";
  return KIND_ORDER.includes(prefix) ? prefix : "group";
}

/**
 * Combobox over the principals this deployment has actually seen.
 *
 * value          current principal string, verbatim — never rewritten by this component
 * onChange       (value, match) => void; `match` is the known principal row or null
 * principals     optional pre-fetched `GET /api/principals` rows; self-fetches when omitted
 * label/hint     field chrome; `hint` renders under the input
 * kinds          optional array restricting which kinds are *suggested* (free entry is unaffected)
 * navigate       optional App `navigate`; renders the route to Access control when given
 * confirmUnknown require an explicit tick before an unseen principal can be submitted
 * required/disabled/placeholder/id  passed through to the input
 */
export default function PrincipalPicker({ value, onChange, principals, kinds, label = "Canonical principal", hint, placeholder = "group:ma-team", required = false, disabled = false, navigate, confirmUnknown = true, id = "principal-picker" }) {
  // Self-fetching keeps the component a one-line drop-in for any form; a page that
  // already loaded the list passes it in so the two stay consistent.
  const fetched = useApi("/api/principals", [], !principals);
  const known = principals || fetched.data || [];
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const wrapper = useRef(null);
  const raw = value || "";
  const probe = raw.trim().toLowerCase();

  // Grants are stored casefolded, so a differently-cased entry is still the same
  // principal. Matching case-insensitively avoids a false "does not exist" warning.
  const match = useMemo(() => known.find((item) => item.principal.toLowerCase() === probe) || null, [known, probe]);

  const groups = useMemo(() => {
    const pool = kinds?.length ? known.filter((item) => kinds.includes(item.principal_kind)) : known;
    const needle = probe;
    const hits = pool.filter((item) => !needle || item.principal.toLowerCase().includes(needle) || (item.label || "").toLowerCase().includes(needle));
    // Source-mirrored principals first: they came out of the source system rather than
    // an operator's keyboard, so they are the ones that reliably intersect.
    const ranked = [...hits].sort((a, b) => Number(b.from_source) - Number(a.from_source) || b.grants - a.grants || a.principal.localeCompare(b.principal));
    return KIND_ORDER.map((kind) => [kind, ranked.filter((item) => item.principal_kind === kind)]).filter(([, items]) => items.length);
  }, [known, kinds, probe]);

  const flat = useMemo(() => groups.flatMap(([, items]) => items), [groups]);
  useEffect(() => { setActive(0); }, [probe, open]);
  useEffect(() => {
    if (!open) return undefined;
    const away = (event) => { if (!wrapper.current?.contains(event.target)) setOpen(false); };
    document.addEventListener("mousedown", away);
    return () => document.removeEventListener("mousedown", away);
  }, [open]);

  const choose = (item) => { onChange(item.principal, item); setOpen(false); };
  const onKeyDown = (event) => {
    if (event.key === "Escape" && open) { event.stopPropagation(); setOpen(false); return; }
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      if (!open) { setOpen(true); return; }
      setActive((current) => (current + (event.key === "ArrowDown" ? 1 : flat.length - 1)) % Math.max(flat.length, 1));
      return;
    }
    // Enter picks the highlighted suggestion instead of submitting the form, so the
    // operator cannot half-type a principal and commit it with one keystroke.
    if (event.key === "Enter" && open && flat[active]) { event.preventDefault(); choose(flat[active]); }
  };

  const untrimmed = raw !== raw.trim() && raw.trim().length > 0;
  const unknown = Boolean(probe) && !match;

  return (
    <div className="principal-picker" ref={wrapper}>
      <label htmlFor={id}>{label}
        <div className={`principal-input ${unknown ? "unknown" : ""}`}>
          <i className="principal-input-kind"><KindIcon kind={match?.principal_kind || principalKindHint(raw)} /></i>
          <input id={id} className="mono" autoComplete="off" role="combobox" aria-expanded={open} aria-controls={`${id}-list`} required={required} disabled={disabled} placeholder={placeholder} value={raw}
            onChange={(event) => { const next = event.target.value; setOpen(true); onChange(next, known.find((item) => item.principal.toLowerCase() === next.trim().toLowerCase()) || null); }}
            onFocus={() => setOpen(true)} onKeyDown={onKeyDown} />
          {match?.from_source && <i className="principal-source-dot" title="Seen on a mirrored source ACL — matches the source system exactly" />}
          <button type="button" className="principal-toggle" tabIndex={-1} disabled={disabled} onClick={() => setOpen((current) => !current)} aria-label="Browse known principals"><ChevronDown size={14} /></button>
        </div>
      </label>

      {open && <div className="principal-menu" id={`${id}-list`} role="listbox">
        {groups.length ? groups.map(([kind, items]) => <div className="principal-menu-group" key={kind}>
          <span className="principal-menu-label">{KIND_LABEL[kind]}</span>
          {items.map((item) => {
            const index = flat.indexOf(item);
            return <button type="button" role="option" aria-selected={item.principal === match?.principal} className={`principal-option ${index === active ? "active" : ""} ${item.principal === match?.principal ? "chosen" : ""}`} key={item.principal} onMouseEnter={() => setActive(index)} onClick={() => choose(item)}>
              <i className="principal-option-kind"><KindIcon kind={item.principal_kind} size={12} /></i>
              <span className="principal-option-name"><code>{item.principal}</code>{item.label && <small>{item.label}</small>}</span>
              <span className="principal-option-meta">
                {item.from_source && <i className="principal-source-dot" title="Seen on a mirrored source ACL" />}
                {item.origins.map((origin) => <em key={origin}>{ORIGIN_LABEL[origin] || origin}</em>)}
                <b>{item.grants} grant{item.grants === 1 ? "" : "s"}</b>
              </span>
              {item.principal === match?.principal && <Check size={13} className="principal-option-check" />}
            </button>;
          })}
        </div>) : <div className="principal-menu-empty">{fetched.loading ? "Loading known principals…" : known.length ? "No known principal matches. Keep typing to enter one that does not exist yet." : "No principals seen yet. Sign a user in, or sync a source to mirror its directory."}</div>}
      </div>}

      {hint && !unknown && !untrimmed && <small className="principal-hint">{hint}</small>}

      {match && !untrimmed && <small className="principal-hint known"><Check size={11} /> Known principal · {match.grants} grant{match.grants === 1 ? "" : "s"} · seen on {match.origins.map((origin) => ORIGIN_LABEL[origin] || origin).join(", ")}{match.from_source ? " · matches the source system exactly" : ""}</small>}

      {untrimmed && <div className="principal-warning"><AlertTriangle size={14} /><div><strong>Leading or trailing whitespace</strong><span>Caller principals are trimmed before they are matched, so <code>{raw}</code> would never equal the identity your provider sends. Remove the space.</span></div></div>}

      {unknown && !untrimmed && <div className="principal-warning"><AlertTriangle size={14} /><div>
        <strong>{raw.trim()} has never been seen by this appliance</strong>
        <span>You can still use it — a group or client nobody has authenticated as yet has no record here. But the match is exact and fails closed: it will match nothing, with no error, until that principal actually appears (a first sign-in through the identity provider, or the next source sync mirroring its directory). Nothing will tell you it stayed inert.</span>
        {navigate && <button type="button" className="row-link" onClick={() => navigate("access")}>Review principals and grants in Access control <ArrowRight size={12} /></button>}
      </div></div>}

      {/* Native `required` gates the surrounding form's submit, so an unseen principal
          cannot be saved by reflex. Keyed on the value so each new string re-asks. */}
      {unknown && !untrimmed && confirmUnknown && <label className="principal-confirm" key={probe}><input type="checkbox" required /><span>I accept that <code>{raw.trim()}</code> grants nothing until it exists</span></label>}

      {!unknown && !untrimmed && navigate && <button type="button" className="row-link principal-policy-link" onClick={() => navigate("access")}>Set up grants for this principal in Access control <ArrowRight size={12} /></button>}
    </div>
  );
}
