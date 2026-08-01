import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Activity,
  Blocks,
  BookOpen,
  Bot,
  Boxes,
  CircleDollarSign,
  CornerDownLeft,
  Database,
  DatabaseBackup,
  ExternalLink,
  FileText,
  KeyRound,
  LayoutDashboard,
  Link2,
  LoaderCircle,
  LogOut,
  Network,
  Quote,
  Scale,
  Search,
  ShieldCheck,
  UserRound,
  UsersRound,
  Workflow
} from "lucide-react";
import { api, setDevPrincipals } from "./api";
import { useApi, useExpertMode } from "./hooks";
import DashboardPage from "./pages/DashboardPage";
import ConnectorsPage from "./pages/ConnectorsPage";
import PipelinePage from "./pages/PipelinePage";
import OntologyPage from "./pages/OntologyPage";
import DataPage from "./pages/DataPage";
import AccessPage from "./pages/AccessPage";
import IdentityPage from "./pages/IdentityPage";
import ModelsPage from "./pages/ModelsPage";
import CostsPage from "./pages/CostsPage";
import ExternalPage from "./pages/ExternalPage";
import ActivityPage from "./pages/ActivityPage";
import BackupPage from "./pages/BackupPage";

const NAV = [
  { id: "overview", label: "Overview", icon: LayoutDashboard },
  { id: "connectors", label: "Connectors", icon: Blocks },
  { id: "pipeline", label: "Insertion pipeline", icon: Workflow },
  { id: "ontology", label: "Ontology", icon: Boxes },
  { id: "data", label: "Data", icon: Database },
  { id: "access", label: "Access control", icon: ShieldCheck },
  { id: "identity", label: "Sign-in", icon: KeyRound },
  { id: "models", label: "Models & services", icon: Bot },
  { id: "costs", label: "Costs", icon: CircleDollarSign },
  { id: "external", label: "External access", icon: Network },
  { id: "activity", label: "Activity", icon: Activity },
  // Appended, never inserted: the sidebar splits this list at index 5 — NAV.slice(0, 5)
  // is "Workspace" and NAV.slice(5) is "Operations". A new entry above that line pushes
  // Sign-in out of the Workspace group without anything on screen saying so.
  { id: "backup", label: "Backup", icon: DatabaseBackup }
];

const PAGES = {
  overview: DashboardPage,
  connectors: ConnectorsPage,
  pipeline: PipelinePage,
  ontology: OntologyPage,
  data: DataPage,
  access: AccessPage,
  identity: IdentityPage,
  models: ModelsPage,
  costs: CostsPage,
  external: ExternalPage,
  activity: ActivityPage,
  backup: BackupPage
};

// A page alone cannot answer "open this document": the hash carries what to open with
// it, so a palette result, a bookmark and a reload all land on the same thing.
function readRoute() {
  const [page, search] = window.location.hash.replace(/^#/, "").split("?");
  return {
    page: page || "overview",
    focus: Object.fromEntries(new URLSearchParams(search || ""))
  };
}

export default function App() {
  const [route, setRoute] = useState(readRoute);
  const [searchOpen, setSearchOpen] = useState(false);
  const [connected, setConnected] = useState("");
  const [expert, setExpert] = useExpertMode();
  const me = useApi("/api/me");
  const { page, focus } = route;

  // OAuth return: the provider redirects to /api/connectors/oauth/callback, which
  // exchanges the code and sends the browser back here. Nothing to finalize
  // client-side; the connector kind in `?connected=` only says which connection went
  // live, so the connector page can confirm it instead of silently reloading.
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const kind = params.get("connected");
    if (!kind) return;
    setConnected(kind);
    window.history.replaceState({}, "", window.location.pathname + "#connectors");
    setRoute({ page: "connectors", focus: {} });
  }, []);
  useEffect(() => {
    const onHash = () => setRoute(readRoute());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  // ⌘K has to work while the caret sits in a filter box or a modal is open, so the
  // listener is on the window and in capture: nothing downstream can swallow it.
  useEffect(() => {
    const onKey = (event) => {
      if (!(event.metaKey || event.ctrlKey) || event.key.toLowerCase() !== "k") return;
      event.preventDefault();
      setSearchOpen((open) => !open);
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, []);

  const navigate = useCallback((id, target) => {
    const query = new URLSearchParams(target || {}).toString();
    window.location.hash = query ? `${id}?${query}` : id;
    // Also applied directly: picking the same result twice leaves the hash unchanged,
    // no hashchange fires, and the page would ignore the second request.
    setRoute({ page: id, focus: target || {} });
  }, []);

  const closeSearch = useCallback(() => setSearchOpen(false), []);
  const CurrentPage = PAGES[page] || DashboardPage;
  const active = NAV.find((item) => item.id === page) || NAV[0];

  if (me.error?.status === 401) return <AuthGate />;

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark"><Boxes size={20} strokeWidth={1.8} /></div>
          <div><strong>LegalMemory</strong><span>Knowledge index</span></div>
        </div>
        <nav className="nav-list">
          <span className="nav-label">Workspace</span>
          {NAV.slice(0, 5).map((item) => <NavItem key={item.id} item={item} active={page === item.id} onClick={() => navigate(item.id)} />)}
          <span className="nav-label nav-label-space">Operations</span>
          {NAV.slice(5).map((item) => <NavItem key={item.id} item={item} active={page === item.id} onClick={() => navigate(item.id)} />)}
        </nav>
        <div className="sidebar-footer">
          {me.data?.docs_url && (
            <a className="nav-item" href={me.data.docs_url} target="_blank" rel="noreferrer">
              <BookOpen size={17} strokeWidth={1.7} /><span>Documentation</span><ExternalLink size={13} />
            </a>
          )}
          <SignedIn identity={me.data} />
        </div>
      </aside>
      <div className="app-main">
        <header className="topbar">
          <div className="breadcrumb"><span>LegalMemory</span><b>/</b><strong>{active.label}</strong></div>
          <div className="top-actions">
            <button className="command-button" onClick={() => setSearchOpen(true)}><Search size={15} /><span>Search</span><kbd>⌘ K</kbd></button>
            <button className={`text-button expert-toggle ${expert ? "on" : ""}`} onClick={() => setExpert(!expert)} title="Show links into the component dashboards — Hatchet, OpenSearch, Langfuse, LiteLLM — and the API docs.">
              <Link2 size={13} /> Service links {expert ? "on" : "off"}
            </button>
            {expert && <a className="text-button" href="/docs" target="_blank" rel="noreferrer">API docs <ExternalLink size={13} /></a>}
          </div>
        </header>
        <main className="page"><CurrentPage navigate={navigate} identity={me.data} focus={focus} connected={connected} onClearConnected={() => setConnected("")} /></main>
      </div>
      {searchOpen && <CommandPalette onClose={closeSearch} navigate={navigate} identity={me.data} />}
    </div>
  );
}

function NavItem({ item, active, onClick }) {
  const Icon = item.icon;
  return <button className={`nav-item ${active ? "active" : ""}`} onClick={onClick}><Icon size={17} strokeWidth={1.7} /><span>{item.label}</span></button>;
}

/** Who the appliance thinks you are, and the one control that changes it. */
function SignedIn({ identity }) {
  // Two ways in, so two ways out. A development identity is a string this browser
  // holds, and dropping it returns to the gate; a proxied session ends at the proxy.
  const local = Boolean(localStorage.getItem("ki.devPrincipals"));
  return (
    <div className="identity-card">
      <div className="avatar">{initials(identity?.username)}</div>
      <div><strong>{identity?.username || "Loading…"}</strong><span>{identity ? (identity.is_admin ? "Administrator" : "Member") : ""}</span></div>
      {local
        ? <button className="sign-out" title="Sign out of the development identity" onClick={() => setDevPrincipals("")}><LogOut size={14} /></button>
        : <a className="sign-out" title="Sign out" href="/oauth2/sign_out?rd=%2F"><LogOut size={14} /></a>}
    </div>
  );
}

function AuthGate() {
  const [value, setValue] = useState("user:local-admin,group:knowledge-index-admins,role:admin");
  return (
    <div className="auth-gate">
      <div className="auth-panel">
        <div className="auth-mark"><KeyRound size={27} /></div>
        <span className="eyebrow">Authentication required</span>
        <h1>Sign in</h1>
        <p>LegalMemory uses your firm's identity provider.</p>
        <a className="primary-button" href="/oauth2/start">Sign in with identity provider <ExternalLink size={15} /></a>
        <details>
          <summary>Local development access</summary>
          <label>Trusted principals<input value={value} onChange={(event) => setValue(event.target.value)} /></label>
          <button className="secondary-button" onClick={() => setDevPrincipals(value)}>Use development identity</button>
        </details>
      </div>
      <div className="auth-art">
        <div className="scope-orbit orbit-one" /><div className="scope-orbit orbit-two" /><div className="scope-orbit orbit-three" />
        <ShieldCheck size={50} />
        <strong>Identity → project grants → source ACLs → search scope</strong>
      </div>
    </div>
  );
}

/**
 * What an operator is actually looking for is a document, a matter, a connection or a
 * person — page names were the one thing they already knew how to reach.
 *
 * Every leg runs under the caller's own principals: the graph and search endpoints are
 * ACL-scoped server-side and `/api/principals` is administrator-only, so the palette
 * never decides who may see what. Nothing is listed that cannot be opened.
 */
function CommandPalette({ onClose, navigate, identity }) {
  const [query, setQuery] = useState("");
  const [term, setTerm] = useState("");
  const [active, setActive] = useState(0);
  const [found, setFound] = useState(null);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState("");
  const listRef = useRef(null);
  // Both are small, cached for the life of the palette and filtered in the browser:
  // typing must not re-ask for the connection list.
  const sources = useApi("/api/sources");
  const principals = useApi("/api/principals", [], Boolean(identity?.is_admin));

  // One round trip per pause, not per keystroke — the content leg embeds the query
  // and goes to the search backend.
  useEffect(() => {
    const timer = setTimeout(() => setTerm(query.trim()), 220);
    return () => clearTimeout(timer);
  }, [query]);

  useEffect(() => {
    if (term.length < 2) {
      setFound(null);
      setFailed("");
      return undefined;
    }
    let live = true;
    setBusy(true);
    (async () => {
      // Matters come from their own endpoint, not from the graph projection: that one
      // filters on Document.title, so a matter appeared only when one of its files
      // happened to be named after it — typing a matter's name found passages inside it
      // and not the matter. /api/matters is ACL-scoped through the caller's readable
      // documents, exactly as the graph leg is.
      const [graph, semantic, matterHits] = await Promise.allSettled([
        api(`/api/graph?query=${encodeURIComponent(term)}&limit=40`),
        api("/api/search", { method: "POST", body: JSON.stringify({ query: term, limit: 6 }) }),
        api(`/api/matters?query=${encodeURIComponent(term)}&limit=8`)
      ]);
      if (!live) return;
      const nodes = graph.status === "fulfilled" ? graph.value?.nodes || [] : [];
      const documents = nodes.filter((node) => node.kind === "document");
      const matters = matterHits.status === "fulfilled" ? matterHits.value || [] : [];
      // A document whose title already matched is not repeated as a content hit.
      const byTitle = new Set(documents.map((node) => node.entity_id));
      const hits = semantic.status === "fulfilled" ? semantic.value?.hits || [] : [];
      setFound({ documents, matters, passages: hits.filter((hit) => !byTitle.has(hit.document_id)) });
      setFailed(graph.status === "rejected" && semantic.status === "rejected" && matterHits.status === "rejected" ? graph.reason?.message || "Search is unavailable." : "");
      setBusy(false);
    })();
    return () => { live = false; };
  }, [term]);

  const groups = useMemo(() => {
    const typed = query.trim().toLowerCase();
    const out = [];
    const open = (id, target) => () => { navigate(id, target); onClose(); };

    const pages = NAV.filter((item) => item.label.toLowerCase().includes(typed));
    if (pages.length) {
      out.push({
        id: "pages",
        label: "Pages",
        items: pages.map((item) => {
          const Icon = item.icon;
          return { key: `page:${item.id}`, icon: <Icon size={15} />, title: item.label, hint: "Open", run: open(item.id) };
        })
      });
    }

    const documents = (found?.documents || []).slice(0, 6);
    if (documents.length) {
      out.push({
        id: "documents",
        label: "Documents",
        items: documents.map((node) => ({
          key: `doc:${node.entity_id}`,
          icon: <FileText size={15} />,
          title: node.label || "Untitled document",
          meta: [human(node.properties?.doc_type), node.properties?.date && new Date(node.properties.date).toLocaleDateString(), node.properties?.chunks ? `${node.properties.chunks} chunks` : "not indexed"].filter(Boolean).join(" · "),
          hint: "Open",
          run: open("data", { doc: node.entity_id })
        }))
      });
    }

    const matters = (found?.matters || []).slice(0, 4);
    if (matters.length) {
      out.push({
        id: "matters",
        label: "Matters",
        items: matters.map((matter) => ({
          key: `matter:${matter.id}`,
          icon: <Scale size={15} />,
          title: matter.title || "Untitled matter",
          meta: [`${matter.documents} document${matter.documents === 1 ? "" : "s"}`, human(matter.practice_area)].filter(Boolean).join(" · "),
          hint: "Filter",
          run: open("data", { matter: matter.id })
        }))
      });
    }

    const passages = (found?.passages || []).slice(0, 5);
    if (passages.length) {
      out.push({
        id: "passages",
        label: "In content",
        items: passages.map((hit) => ({
          key: `hit:${hit.version_id}`,
          icon: <Quote size={15} />,
          title: hit.title || "Untitled document",
          meta: (hit.excerpt || "").replace(/\s+/g, " ").slice(0, 110),
          hint: "Open",
          run: open("data", { doc: hit.document_id })
        }))
      });
    }

    const connections = typed ? (sources.data || []).filter((source) => `${source.display_name} ${source.kind}`.toLowerCase().includes(typed)).slice(0, 4) : [];
    if (connections.length) {
      out.push({
        id: "connections",
        label: "Connections",
        items: connections.map((source) => ({
          key: `source:${source.id}`,
          icon: <Link2 size={15} />,
          title: source.display_name,
          meta: [human(source.kind), `${source.object_count} object${source.object_count === 1 ? "" : "s"}`, source.last_sync_at ? `synced ${new Date(source.last_sync_at).toLocaleDateString()}` : "never synced"].filter(Boolean).join(" · "),
          hint: "Open",
          run: open("connectors", { source: source.id })
        }))
      });
    }

    const people = typed ? (principals.data || []).filter((item) => `${item.principal} ${item.label || ""}`.toLowerCase().includes(typed)).slice(0, 5) : [];
    if (people.length) {
      out.push({
        id: "people",
        label: "People & groups",
        items: people.map((item) => ({
          key: `principal:${item.principal}`,
          icon: item.principal_kind === "user" ? <UserRound size={15} /> : <UsersRound size={15} />,
          title: item.principal,
          meta: [item.label, item.grants ? `${item.grants} grant${item.grants === 1 ? "" : "s"}` : "no grants", item.from_source && "on source ACLs"].filter(Boolean).join(" · "),
          hint: "Check",
          run: open("access", { principal: item.principal })
        }))
      });
    }
    return out;
  }, [query, found, sources.data, principals.data, navigate, onClose]);

  const flat = useMemo(() => groups.flatMap((group) => group.items), [groups]);
  useEffect(() => { setActive(0); }, [groups]);
  useEffect(() => { listRef.current?.querySelector(".cmd-item.active")?.scrollIntoView({ block: "nearest" }); }, [active, groups]);

  useEffect(() => {
    const onKey = (event) => {
      if (event.key === "Escape") { event.preventDefault(); onClose(); return; }
      if (event.key === "ArrowDown" || (event.key === "Tab" && !event.shiftKey)) {
        event.preventDefault();
        setActive((index) => (flat.length ? (index + 1) % flat.length : 0));
      } else if (event.key === "ArrowUp" || (event.key === "Tab" && event.shiftKey)) {
        event.preventDefault();
        setActive((index) => (flat.length ? (index - 1 + flat.length) % flat.length : 0));
      } else if (event.key === "Enter" && flat[active]) {
        event.preventDefault();
        flat[active].run();
      }
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [flat, active, onClose]);

  return (
    <div className="modal-backdrop" onMouseDown={onClose}>
      <div className="command-palette" onMouseDown={(event) => event.stopPropagation()}>
        <div className="command-input">
          <Search size={18} />
          <input autoFocus placeholder="Documents, matters, connections, people, pages…" value={query} onChange={(event) => setQuery(event.target.value)} />
          {busy ? <LoaderCircle size={14} className="spin" /> : <kbd>esc</kbd>}
        </div>
        <div className="command-results" ref={listRef}>
          {groups.map((group) => (
            <div className="cmd-group" key={group.id}>
              <span className="nav-label">{group.label}</span>
              {group.items.map((item) => {
                const index = flat.indexOf(item);
                return (
                  <button key={item.key} className={`cmd-item ${index === active ? "active" : ""}`} onMouseMove={() => setActive(index)} onClick={item.run}>
                    <i>{item.icon}</i>
                    <span><strong>{item.title}</strong>{item.meta && <small>{item.meta}</small>}</span>
                    {index === active ? <CornerDownLeft size={13} /> : <em>{item.hint}</em>}
                  </button>
                );
              })}
            </div>
          ))}
          {!groups.length && <div className="cmd-empty">{busy ? "Searching…" : failed || (term.length < 2 ? "Type at least two characters." : `Nothing matches “${term}”.`)}</div>}
        </div>
      </div>
    </div>
  );
}

function human(value) {
  return value ? String(value).replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase()) : "";
}

function initials(name = "KI") {
  return name.split(/[\s._-]+/).filter(Boolean).slice(0, 2).map((part) => part[0]?.toUpperCase()).join("") || "KI";
}
