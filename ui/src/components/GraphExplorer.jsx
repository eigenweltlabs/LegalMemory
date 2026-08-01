import { useEffect, useMemo, useRef, useState } from "react";
import cytoscape from "cytoscape";
import { Expand, Eye, EyeOff, FileText, Focus, Minus, Plus, Search, Tags, X } from "lucide-react";

const COLORS = {
  project: "#171717",
  matter: "#3c3c3a",
  document: "#696864",
  version: "#6250c7",
  thread: "#c66a32",
  source: "#1769aa",
  source_object: "#188d82"
};

const EDGE_COLORS = {
  references: "#d04f4f",
  responds_to: "#cc7832",
  supersedes: "#7048c8",
  annex_of: "#2877b5",
  belongs_to_thread: "#c66a32",
  observed_as: "#188d82",
  version_of: "#8d84c8",
  contains: "#b4b3ae"
};

export default function GraphExplorer({ graph, loading, onOpenDocument }) {
  const containerRef = useRef(null);
  const cyRef = useRef(null);
  const [selected, setSelected] = useState(null);
  const [fullscreen, setFullscreen] = useState(false);
  const [showLabels, setShowLabels] = useState(true);
  const [showEdgeLabels, setShowEdgeLabels] = useState(false);
  const [hiddenKinds, setHiddenKinds] = useState(new Set());
  const [edgeKind, setEdgeKind] = useState("all");
  const [layoutMode, setLayoutMode] = useState("clustered");
  const [nodeQuery, setNodeQuery] = useState("");

  const nodeKinds = useMemo(() => Object.entries(graph?.summary?.by_kind || {}), [graph]);
  const edgeKinds = useMemo(() => Object.entries(graph?.summary?.by_edge_kind || {}), [graph]);
  const visibleGraph = useMemo(() => {
    const nodes = (graph?.nodes || []).filter((node) => !hiddenKinds.has(node.kind));
    const nodeIds = new Set(nodes.map((node) => node.id));
    const edges = (graph?.edges || []).filter((edge) => nodeIds.has(edge.source) && nodeIds.has(edge.target) && (edgeKind === "all" || edge.kind === edgeKind));
    return { nodes, edges };
  }, [graph, hiddenKinds, edgeKind]);
  const elements = useMemo(() => [
    ...visibleGraph.nodes.map((node) => ({ data: node })),
    ...visibleGraph.edges.map((edge) => ({ data: edge }))
  ], [visibleGraph]);
  const nodeById = useMemo(() => new Map((graph?.nodes || []).map((node) => [node.id, node])), [graph]);
  const neighbours = useMemo(() => {
    if (selected?.type !== "node") return [];
    return (graph?.edges || []).filter((edge) => edge.source === selected.data.id || edge.target === selected.data.id).map((edge) => {
      const otherId = edge.source === selected.data.id ? edge.target : edge.source;
      return { edge, node: nodeById.get(otherId), direction: edge.source === selected.data.id ? "out" : "in" };
    }).filter((item) => item.node);
  }, [graph, nodeById, selected]);

  useEffect(() => {
    if (!containerRef.current || !elements.length) return undefined;
    const cy = cytoscape({
      container: containerRef.current,
      elements,
      minZoom: 0.03,
      maxZoom: 5,
      pixelRatio: "auto",
      hideEdgesOnViewport: true,
      textureOnViewport: true,
      style: graphStyles(showLabels, showEdgeLabels),
      layout: layoutOptions(layoutMode, visibleGraph)
    });
    cy.on("tap", "node", (event) => setSelected({ type: "node", data: event.target.data() }));
    cy.on("tap", "edge", (event) => setSelected({ type: "edge", data: event.target.data() }));
    cy.on("tap", (event) => {
      if (event.target === cy) setSelected(null);
    });
    cyRef.current = cy;
    requestAnimationFrame(() => cy.fit(undefined, 32));
    return () => {
      cy.destroy();
      cyRef.current = null;
    };
  }, [elements, layoutMode]);

  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;
    cy.style()
      .selector("node")
      .style("label", showLabels ? "data(label)" : "")
      .selector("edge")
      .style("label", showEdgeLabels ? "data(kind)" : "")
      .update();
  }, [showLabels, showEdgeLabels]);

  useEffect(() => {
    const frame = requestAnimationFrame(() => {
      cyRef.current?.resize();
      cyRef.current?.fit(undefined, 36);
    });
    return () => cancelAnimationFrame(frame);
  }, [fullscreen]);

  const zoom = (factor) => {
    const cy = cyRef.current;
    if (!cy) return;
    cy.zoom({ level: cy.zoom() * factor, renderedPosition: { x: cy.width() / 2, y: cy.height() / 2 } });
  };

  const toggleKind = (kind) => setHiddenKinds((current) => {
    const next = new Set(current);
    if (next.has(kind)) next.delete(kind); else next.add(kind);
    return next;
  });

  const focusNode = (node) => {
    const cy = cyRef.current;
    if (!cy) return;
    const element = cy.getElementById(node.id);
    if (!element.length) return;
    cy.elements().unselect();
    element.select();
    cy.animate({ fit: { eles: element.closedNeighborhood(), padding: 90 }, duration: 350 });
    setSelected({ type: "node", data: node });
  };

  // A document node carries the documents.id as its entity_id, so the page can hand it
  // straight to the record endpoint. Fullscreen has to be dropped first: the shell is
  // fixed at z-index 100 and the record drawer at 80, so a record opened from fullscreen
  // would render behind the graph and leave the button looking as dead as it used to be.
  const openDocument = onOpenDocument
    ? (documentId) => {
      setFullscreen(false);
      onOpenDocument(documentId);
    }
    : null;

  const locate = () => {
    const needle = nodeQuery.trim().toLocaleLowerCase();
    if (!needle) return;
    const match = visibleGraph.nodes.find((node) => `${node.label} ${node.entity_id} ${JSON.stringify(node.properties || {})}`.toLocaleLowerCase().includes(needle));
    if (match) focusNode(match);
  };

  return (
    <div className={`graph-shell ${fullscreen ? "graph-fullscreen" : ""}`}>
      <div className="graph-filter-bar">
        <div className="graph-node-search"><Search size={14} /><input value={nodeQuery} onChange={(event) => setNodeQuery(event.target.value)} onKeyDown={(event) => event.key === "Enter" && locate()} placeholder="Find node by title, path or ID…" /><button onClick={locate}>Find</button></div>
        <div className="graph-kind-filters">{nodeKinds.map(([kind, count]) => <button key={kind} className={hiddenKinds.has(kind) ? "off" : "on"} onClick={() => toggleKind(kind)}><i style={{ background: COLORS[kind] || "#777" }} />{pretty(kind)} <span>{count.toLocaleString()}</span></button>)}</div>
        <div className="graph-selectors"><label className="graph-edge-filter"><Focus size={13} /><select value={layoutMode} onChange={(event) => setLayoutMode(event.target.value)}><option value="clustered">Matter clusters · fast</option><option value="grid">Grid · fastest</option><option value="concentric">Entity rings · fast</option></select></label><label className="graph-edge-filter"><Tags size={13} /><select value={edgeKind} onChange={(event) => setEdgeKind(event.target.value)}><option value="all">All edge types ({graph?.summary?.edges || 0})</option>{edgeKinds.map(([kind, count]) => <option value={kind} key={kind}>{pretty(kind)} ({count})</option>)}</select></label></div>
      </div>
      <div className="graph-canvas" ref={containerRef} />
      {loading && <div className="graph-loading">Compiling the complete authorized graph…</div>}
      {!loading && !elements.length && (
        <div className="graph-empty">
          <div className="graph-empty-mark" />
          <strong>No visible graph data</strong>
          <span>Change the filters or enable another entity type.</span>
        </div>
      )}
      <div className="graph-tools">
        <button onClick={() => zoom(1.22)} aria-label="Zoom in"><Plus size={16} /></button>
        <button onClick={() => zoom(0.82)} aria-label="Zoom out"><Minus size={16} /></button>
        <button onClick={() => cyRef.current?.fit(undefined, 36)} aria-label="Fit graph"><Focus size={16} /></button>
        <button className={showLabels ? "active" : ""} onClick={() => setShowLabels((value) => !value)} aria-label="Toggle node labels">{showLabels ? <Eye size={16} /> : <EyeOff size={16} />}</button>
        <button className={showEdgeLabels ? "active" : ""} onClick={() => setShowEdgeLabels((value) => !value)} aria-label="Toggle edge labels"><Tags size={16} /></button>
        <button onClick={() => setFullscreen((value) => !value)} aria-label="Toggle fullscreen"><Expand size={16} /></button>
      </div>
      <div className="graph-legend">
        <span className="graph-count">Showing {visibleGraph.nodes.length.toLocaleString()} / {graph?.summary?.nodes?.toLocaleString() || 0} nodes · {visibleGraph.edges.length.toLocaleString()} / {graph?.summary?.edges?.toLocaleString() || 0} edges</span>
        {graph?.summary?.truncated && <strong>Projection truncated</strong>}
      </div>
      {selected && <GraphInspector selected={selected} nodeById={nodeById} neighbours={neighbours} onClose={() => setSelected(null)} onFocus={focusNode} onOpenDocument={openDocument} />}
    </div>
  );
}

function GraphInspector({ selected, nodeById, neighbours, onClose, onFocus, onOpenDocument }) {
  const item = selected.data;
  const source = selected.type === "edge" ? nodeById.get(item.source) : null;
  const target = selected.type === "edge" ? nodeById.get(item.target) : null;
  return <aside className="graph-inspector complete-graph-inspector">
    <div className="inspector-kicker"><span><i className={`entity-dot ${item.kind}`} />{selected.type} · {pretty(item.kind)}</span><button className="icon-button" onClick={onClose} aria-label="Close"><X size={17} /></button></div>
    <h2>{selected.type === "node" ? item.label : pretty(item.kind)}</h2>
    <p>{selected.type === "node" ? describe(item) : `${source?.label || item.source} → ${target?.label || item.target}`}</p>
    {selected.type === "node" && <div className="inspector-id"><code>{item.entity_id}</code><button onClick={() => navigator.clipboard.writeText(item.entity_id)}>Copy ID</button></div>}
    {/* Guarded on the handler rather than called through `?.`: this button spent a
        release rendering on every document node with no prop behind the optional call,
        so it looked live and did nothing. A missing button is honest; a dead one is a
        bug report waiting. The copy says "record" and not "complete record" because the
        drawer the page opens is the summary, not the full-record drawer. */}
    {selected.type === "node" && item.kind === "document" && onOpenDocument && <button className="primary-button inspector-open-document" onClick={() => onOpenDocument(item.entity_id)}><FileText size={14} /> Open document record</button>}
    {selected.type === "edge" && <div className="edge-endpoints"><button onClick={() => source && onFocus(source)}>{source?.label || item.source}</button><span>→</span><button onClick={() => target && onFocus(target)}>{target?.label || item.target}</button></div>}
    <h3>All properties</h3>
    <dl>{Object.entries(item.properties || {}).filter(([, value]) => value !== null && value !== "").map(([key, value]) => <div key={key}><dt>{pretty(key)}</dt><dd>{renderValue(value)}</dd></div>)}</dl>
    {selected.type === "node" && <><h3>Connections ({neighbours.length})</h3><div className="graph-neighbour-list">{neighbours.map(({ edge, node, direction }) => <button key={edge.id} onClick={() => onFocus(node)}><i style={{ background: COLORS[node.kind] || "#777" }} /><span><strong>{node.label}</strong><small>{direction === "out" ? "→" : "←"} {pretty(edge.kind)} · {pretty(node.kind)}</small></span></button>)}</div></>}
    <details className="graph-raw-json"><summary>Raw graph record</summary><pre>{JSON.stringify(item, null, 2)}</pre></details>
  </aside>;
}

function renderValue(value) {
  if (typeof value === "object") return <pre className="inline-json">{JSON.stringify(value, null, 2)}</pre>;
  return String(value);
}

function layoutOptions(name, graph) {
  if (name === "grid") return { name: "grid", animate: false, fit: true, padding: 40, avoidOverlap: true, condense: true };
  if (name === "concentric") return {
    name: "concentric",
    animate: false,
    fit: true,
    padding: 40,
    avoidOverlap: true,
    minNodeSpacing: 8,
    concentric: (node) => ({ project: 7, source: 6, matter: 5, thread: 4, document: 3, version: 2, source_object: 1 }[node.data("kind")] || 0),
    levelWidth: () => 1
  };
  const positions = clusteredPositions(graph);
  return {
    name: "preset",
    animate: false,
    fit: true,
    padding: 40,
    positions: (node) => positions.get(node.id()) || { x: 0, y: 0 }
  };
}

function clusteredPositions(graph) {
  const nodes = graph?.nodes || [];
  const edges = graph?.edges || [];
  const positions = new Map();
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const children = new Map();
  const append = (map, key, value) => map.set(key, [...(map.get(key) || []), value]);

  for (const edge of edges) {
    if (edge.kind === "contains" || edge.kind === "version_of" || edge.kind === "observed_as") {
      append(children, edge.source, edge.target);
    }
  }

  const matters = nodes.filter((node) => node.kind === "matter").sort(byLabel);
  const assigned = new Set();
  const CELL_X = 190;
  const CELL_Y = 178;
  const CLUSTER_GAP = 170;
  const clusterSpecs = matters.map((matter) => {
    const contained = (children.get(matter.id) || []).map((id) => byId.get(id)).filter(Boolean);
    const documents = contained.filter((node) => node.kind === "document").sort(byLabel);
    const threads = contained.filter((node) => node.kind === "thread").sort(byLabel);
    const columns = Math.max(2, Math.ceil(Math.sqrt(Math.max(documents.length, 1) * 1.35)));
    const rows = Math.max(1, Math.ceil(documents.length / columns));
    return {
      matter,
      documents,
      threads,
      columns,
      width: Math.max(520, columns * CELL_X + 120),
      height: 150 + rows * CELL_Y + 90
    };
  });
  const totalArea = clusterSpecs.reduce((sum, cluster) => sum + cluster.width * cluster.height, 0);
  const targetWidth = Math.max(1800, Math.sqrt(totalArea * 1.45));
  let cursorX = 0;
  let cursorY = 120;
  let rowHeight = 0;
  let contentWidth = 0;

  for (const cluster of clusterSpecs) {
    if (cursorX > 0 && cursorX + cluster.width > targetWidth) {
      cursorX = 0;
      cursorY += rowHeight + CLUSTER_GAP;
      rowHeight = 0;
    }
    const centerX = cursorX + cluster.width / 2;
    positions.set(cluster.matter.id, { x: centerX, y: cursorY + 28 });
    assigned.add(cluster.matter.id);

    cluster.threads.forEach((thread, index) => {
      const offset = (index - (cluster.threads.length - 1) / 2) * 62;
      positions.set(thread.id, { x: centerX + offset, y: cursorY + 82 });
      assigned.add(thread.id);
    });

    cluster.documents.forEach((document, index) => {
      const column = index % cluster.columns;
      const row = Math.floor(index / cluster.columns);
      const x = cursorX + 155 + column * CELL_X;
      const y = cursorY + 145 + row * CELL_Y;
      positions.set(document.id, { x, y });
      assigned.add(document.id);

      const versions = (children.get(document.id) || []).map((id) => byId.get(id)).filter((node) => node?.kind === "version").sort(byLabel);
      versions.forEach((version, versionIndex) => {
        const versionX = x + (versionIndex - (versions.length - 1) / 2) * 36;
        positions.set(version.id, { x: versionX, y: y + 50 });
        assigned.add(version.id);
        const sourceObjects = (children.get(version.id) || []).map((id) => byId.get(id)).filter((node) => node?.kind === "source_object").sort(byLabel);
        sourceObjects.forEach((sourceObject, sourceIndex) => {
          positions.set(sourceObject.id, {
            x: versionX + (sourceIndex - (sourceObjects.length - 1) / 2) * 30,
            y: y + 96
          });
          assigned.add(sourceObject.id);
        });
      });
    });

    cursorX += cluster.width + CLUSTER_GAP;
    rowHeight = Math.max(rowHeight, cluster.height);
    contentWidth = Math.max(contentWidth, cursorX - CLUSTER_GAP);
  }

  const topLevel = nodes.filter((node) => !assigned.has(node.id) && (node.kind === "source" || node.kind === "project")).sort(byLabel);
  topLevel.forEach((node, index) => {
    positions.set(node.id, { x: contentWidth / 2 + (index - (topLevel.length - 1) / 2) * 180, y: 0 });
    assigned.add(node.id);
  });

  const orphans = nodes.filter((node) => !assigned.has(node.id)).sort((left, right) => left.kind.localeCompare(right.kind) || byLabel(left, right));
  const orphanColumns = Math.max(1, Math.ceil(Math.sqrt(orphans.length * 1.5)));
  const orphanY = cursorY + rowHeight + CLUSTER_GAP;
  orphans.forEach((node, index) => {
    positions.set(node.id, {
      x: 90 + (index % orphanColumns) * 90,
      y: orphanY + Math.floor(index / orphanColumns) * 90
    });
  });
  return positions;
}

function byLabel(left, right) {
  return String(left?.label || left?.id || "").localeCompare(String(right?.label || right?.id || ""));
}

function graphStyles(showLabels, showEdgeLabels) {
  return [
    {
      selector: "node",
      style: {
        width: "data(size)", height: "data(size)",
        "background-color": (ele) => COLORS[ele.data("kind")] || "#74736e",
        "border-width": 0, label: showLabels ? "data(label)" : "", color: "#343432",
        "font-size": 8, "font-family": "Inter, sans-serif", "text-wrap": "ellipsis",
        "text-max-width": 110, "text-valign": "bottom", "text-margin-y": 6,
        "min-zoomed-font-size": 7, opacity: 0.92
      }
    },
    {
      selector: "node:selected",
      style: {
        "background-color": "#5f46ff", "border-color": "#ffffff", "border-width": 3,
        "overlay-color": "#5f46ff", "overlay-opacity": 0.12, "overlay-padding": 9
      }
    },
    {
      selector: "edge",
      style: {
        width: (ele) => ele.data("properties")?.stored ? 1.35 : 0.55,
        "line-color": (ele) => EDGE_COLORS[ele.data("kind")] || "#aaa9a4",
        "target-arrow-color": (ele) => EDGE_COLORS[ele.data("kind")] || "#aaa9a4",
        "target-arrow-shape": (ele) => ele.data("kind") === "contains" ? "none" : "triangle",
        "arrow-scale": 0.55, "curve-style": "bezier",
        opacity: (ele) => {
          if (ele.data("kind") === "contains" && ele.data("source")?.startsWith("source:")) return 0.06;
          return ele.data("properties")?.stored ? 0.68 : 0.25;
        },
        label: showEdgeLabels ? "data(kind)" : "", "font-size": 7, color: "#777671",
        "text-rotation": "autorotate", "text-background-color": "#fafaf8",
        "text-background-opacity": 0.8, "text-background-padding": 2,
        "min-zoomed-font-size": 7
      }
    },
    {
      selector: "edge:selected",
      style: { width: 2.5, "line-color": "#5f46ff", "target-arrow-color": "#5f46ff", opacity: 1 }
    }
  ];
}

function describe(node) {
  if (node.kind === "project") return "Authorization boundary containing sources, matters and documents.";
  if (node.kind === "matter") return "Legal matter with its complete visible document and communication context.";
  if (node.kind === "document") return `Logical work product with ${node.properties?.versions || 0} authorized version(s) and ${node.properties?.chunks || 0} searchable chunk(s).`;
  if (node.kind === "version") return "One concrete, content-addressed document version.";
  if (node.kind === "thread") return "Reconstructed communication thread connected through stored relations.";
  if (node.kind === "source") return "Connected DMS or filesystem source.";
  if (node.kind === "source_object") return "Exact source-system observation and path for a document version.";
  return "Entity in the permission-filtered knowledge graph.";
}

function pretty(value) { return String(value || "").replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase()); }
