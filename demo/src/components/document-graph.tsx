"use client";

import { useMemo, useState } from "react";

import { useCompact } from "@/lib/use-compact";
import { cn } from "@/lib/utils";

/**
 * The document graph, drawn.
 *
 * Modelled on the "With a structured document graph" scene in the launch film,
 * which is the light one: a paper field, the document a large orange hub, its
 * neighbours as dark or grey discs with their names centred beneath them, and
 * relations drawn orange against grey context edges.
 *
 * The film's other graph scene is on black. That one works because the whole
 * frame is black around it; dropped into a paper-white transcript the same panel
 * is a hole punched in the page. Same graph, same palette, the surface the page
 * is already using.
 *
 * Positions come from a small deterministic force relaxation rather than a
 * running simulation. A live simulation in a chat transcript re-lays-out every
 * time React re-renders, so the graph a reader was looking at moves under them
 * mid-sentence. Seeding from the document id means the same document always
 * draws the same shape.
 */

export interface GraphNode {
  documentId: string;
  title: string;
  meta?: string | null;
  /** Relation type, e.g. "references" or "same matter". */
  kind: string;
  /** True when the appliance stored this relation with evidence behind it. */
  stored: boolean;
  evidence?: string | null;
}

/**
 * The frame the graph is drawn in, and how much fits inside it.
 *
 * An SVG scales to the column it is given, and its type scales with it: this
 * picture at 620 units wide, rendered into the 340 a phone has, is a correct
 * drawing of the graph with six-pixel labels — which is a diagram of a diagram.
 * So a narrow screen gets its own frame, roughly the width it will be drawn at,
 * where eleven units is eleven pixels. Fewer nodes and shorter labels are what
 * that frame will hold: past this they collide, and the card still says how many
 * there are in total.
 */
const FRAME = {
  wide: { width: 620, height: 360, nodes: 9, perLine: 26, rootPerLine: 34 },
  // No rootPerLine: the hub's name is HTML above the picture at this size, and
  // wraps to the card rather than to a line length guessed here.
  compact: { width: 340, height: 400, nodes: 5, perLine: 17 },
} as const;

type Frame = (typeof FRAME)[keyof typeof FRAME];

/** mulberry32 — small, seeded, and good enough for a layout. */
function seeded(seed: number) {
  return () => {
    seed |= 0;
    seed = (seed + 0x6d2b79f5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const hash = (value: string) => {
  let h = 2166136261;
  for (let i = 0; i < value.length; i += 1) {
    h ^= value.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return h >>> 0;
};

interface Placed {
  x: number;
  y: number;
}

/**
 * Relax the neighbours around a fixed centre.
 *
 * Springs hold each node at a readable distance from the root; pairwise
 * repulsion stops labels stacking. Sixty passes is well past the point where
 * this settles for the ten-odd nodes it ever draws.
 */
function layout(count: number, seed: number, frame: Frame): Placed[] {
  const { width: WIDTH, height: HEIGHT } = frame;
  const random = seeded(seed);
  const centre = { x: WIDTH / 2, y: HEIGHT / 2 };
  const radius = Math.min(WIDTH, HEIGHT) * 0.36;

  // The ring is drawn wider than it is tall because the frame is, and because
  // labels need horizontal room. A frame taller than it is wide has neither
  // reason, so it gets a circle.
  const stretch = Math.max(1, (WIDTH / HEIGHT) * 0.78);

  const nodes: Placed[] = Array.from({ length: count }, (_, index) => {
    // A jittered ring, so the relaxation starts somewhere sane and the result
    // reads as organic rather than as a wheel.
    const angle = (index / count) * Math.PI * 2 + random() * 0.7;
    const distance = radius * (0.78 + random() * 0.44);
    return {
      x: centre.x + Math.cos(angle) * distance * stretch,
      y: centre.y + Math.sin(angle) * distance,
    };
  });

  for (let pass = 0; pass < 70; pass += 1) {
    for (let i = 0; i < nodes.length; i += 1) {
      const node = nodes[i];

      const dx = node.x - centre.x;
      const dy = node.y - centre.y;
      const distance = Math.hypot(dx, dy) || 0.001;
      const pull = (distance - radius * 1.15) * 0.07;
      node.x -= (dx / distance) * pull;
      node.y -= (dy / distance) * pull;

      // Labels sit beneath their node, so the separation that matters is
      // wider than it is tall.
      for (let j = 0; j < nodes.length; j += 1) {
        if (i === j) continue;
        const other = nodes[j];
        const ox = (node.x - other.x) * 0.62;
        const oy = node.y - other.y;
        const gap = Math.hypot(ox, oy) || 0.001;
        if (gap < 78) {
          const push = (78 - gap) * 0.2;
          node.x += (ox / gap) * push * 1.6;
          node.y += (oy / gap) * push;
        }
      }
    }
  }

  // Keep the label inside the frame, not the node: the disc is nine units wide
  // and the name under it is as wide as the line it was wrapped to.
  const inset = frame.perLine * 3.2;
  return nodes.map((node) => ({
    x: Math.max(inset, Math.min(WIDTH - inset, node.x)),
    y: Math.max(36, Math.min(HEIGHT - 56, node.y)),
  }));
}

export function DocumentGraph({
  rootTitle,
  nodes,
  totalRelated,
  onReveal,
}: {
  rootTitle: string;
  nodes: GraphNode[];
  totalRelated: number;
  onReveal: (documentId: string) => void;
}) {
  const [active, setActive] = useState<string | null>(null);
  const compact = useCompact();
  const frame = compact ? FRAME.compact : FRAME.wide;
  const { width: WIDTH, height: HEIGHT } = frame;

  const shown = useMemo(() => nodes.slice(0, frame.nodes), [nodes, frame]);
  const positions = useMemo(
    () => layout(shown.length, hash(rootTitle + shown.length), frame),
    [shown, rootTitle, frame],
  );

  const labels = useMemo(
    () => shown.map((node) => wrapLabel(node.title, frame.perLine, 2)),
    [shown, frame],
  );

  const centre = { x: WIDTH / 2, y: HEIGHT / 2 };
  const focused = shown.find((node) => node.documentId === active) ?? null;
  const storedCount = shown.filter((node) => node.stored).length;

  /**
   * Hovering is how this is read, and a touch screen has no hover.
   *
   * So on one, the first tap on a document says what the relation is and the
   * second opens it — the caption is the half of the graph that carries the
   * evidence, and going straight to the document skips it. With a pointer,
   * hover has already said it and a click means open.
   */
  const activate = (documentId: string) => {
    if (compact && active !== documentId) {
      setActive(documentId);
      return;
    }
    onReveal(documentId);
  };

  return (
    <div className="bg-[var(--lm-paper-2)]">
      {/* The film's legend, in the site's pill: what the picture is counting. */}
      <div className="flex flex-wrap items-center gap-1.5 px-3.5 pt-3">
        <Legend swatch="var(--lm-orange)" label={`Relation ${storedCount}`} />
        <Legend swatch="var(--lm-muted-3)" label={`Context ${shown.length - storedCount}`} />
      </div>

      {/* On a narrow frame the hub's name is the widest thing in the picture and
          it sits in the middle of it, where the neighbours' own names are. Moved
          up here it collides with nothing, wraps as text rather than as an SVG
          label, and leaves the middle of a small graph to the graph. */}
      {compact && (
        <p className="font-emphasis px-3.5 pt-2.5 text-[12.5px] leading-snug">{rootTitle}</p>
      )}

      <svg
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        className="block h-auto w-full"
        role="img"
        aria-label={`Document graph for ${rootTitle}: ${totalRelated} related documents`}
      >
        {shown.map((node, index) => {
          const point = positions[index];
          const isActive = node.documentId === active;
          return (
            <line
              key={`edge-${node.documentId}`}
              x1={centre.x}
              y1={centre.y}
              x2={point.x}
              y2={point.y}
              // A stored relation was derived from the documents and carries
              // evidence; shared-matter context is filing. The film draws that
              // difference in colour, so this does too.
              stroke={
                isActive
                  ? "#E95700"
                  : node.stored
                    ? "rgba(233,87,0,0.45)"
                    : "rgba(14,10,7,0.18)"
              }
              strokeWidth={isActive ? 2.2 : node.stored ? 1.3 : 0.9}
            />
          );
        })}

        {/* The document itself: the orange hub, named beneath it — or, on a
            narrow frame, named above the picture instead. */}
        <circle cx={centre.x} cy={centre.y} r={19} fill="#E95700" />
        {!compact && (
          <text
            x={centre.x}
            y={centre.y + 38}
            textAnchor="middle"
            className="fill-[var(--lm-ink-900)] text-[12.5px]"
            style={{ fontWeight: 560 }}
          >
            {wrapLabel(rootTitle, FRAME.wide.rootPerLine, 2).map((line, lineIndex) => (
              <tspan key={line + lineIndex} x={centre.x} dy={lineIndex === 0 ? 0 : 14}>
                {line}
              </tspan>
            ))}
          </text>
        )}

        {shown.map((node, index) => {
          const point = positions[index];
          const isActive = node.documentId === active;
          return (
            <g
              key={node.documentId}
              // Pointer only: a browser sends these for a tap as well, and a
              // node that "hovers" on the way to being tapped would open on the
              // first tap, which is the behaviour `activate` exists to avoid.
              onPointerEnter={(event) =>
                event.pointerType === "mouse" && setActive(node.documentId)
              }
              onPointerLeave={(event) => event.pointerType === "mouse" && setActive(null)}
              onClick={() => activate(node.documentId)}
              className="cursor-pointer"
            >
              {/* An invisible disc so the pointer target is comfortable without
                  drawing a node the size of the target. */}
              <circle cx={point.x} cy={point.y} r={compact ? 26 : 22} fill="transparent" />
              <circle
                cx={point.x}
                cy={point.y}
                r={isActive ? 11 : 9}
                fill={
                  isActive
                    ? "#E95700"
                    : node.stored
                      ? "var(--lm-ink-900)"
                      : "var(--lm-muted)"
                }
              />
              <text
                x={point.x}
                y={point.y + 23}
                textAnchor="middle"
                className={cn(
                  "text-[11px]",
                  isActive
                    ? "fill-[var(--lm-orange)]"
                    : "fill-[var(--lm-ink-900)]",
                )}
                style={{ fontWeight: isActive ? 560 : 500 }}
              >
                {labels[index].map((line, lineIndex) => (
                  <tspan key={line + lineIndex} x={point.x} dy={lineIndex === 0 ? 0 : 12.5}>
                    {line}
                  </tspan>
                ))}
              </text>
              {node.meta && (
                <text
                  x={point.x}
                  y={point.y + 23 + labels[index].length * 12.5}
                  textAnchor="middle"
                  className="fill-[var(--lm-muted-3)] text-[9.5px]"
                >
                  {truncate(node.meta, 24)}
                </text>
              )}
            </g>
          );
        })}
      </svg>

      {/* The caption is the graph's other half: an edge you cannot interrogate
          is decoration. Hovering a node — tapping it, without a pointer — says
          what the relation is and, for a stored one, the evidence the appliance
          recorded for it. */}
      <div className="min-h-[46px] border-t px-3.5 py-2.5">
        {focused ? (
          <>
            <span
              className={cn(
                "lm-mono text-[9.5px] tracking-[0.11em] uppercase",
                focused.stored ? "text-[var(--lm-orange)]" : "text-[var(--lm-muted-3)]",
              )}
            >
              {focused.kind.replace(/_/g, " ")}
            </span>
            {compact && (
              <span className="lm-mono ml-2 text-[9.5px] tracking-[0.11em] text-[var(--lm-muted-3)] uppercase">
                Tap again to open
              </span>
            )}
            <p className="mt-1 line-clamp-2 text-[11.5px] leading-[1.5] text-[var(--lm-muted-2)]">
              {focused.evidence ?? focused.title}
            </p>
          </>
        ) : (
          <p className="text-[11.5px] text-[var(--lm-muted-3)]">
            {totalRelated > shown.length ? `Showing ${shown.length} of ${totalRelated}. ` : ""}
            {compact
              ? "Tap a document for the relation, again to open it."
              : "Hover a document for the relation, click to open it."}
          </p>
        )}
      </div>
    </div>
  );
}

function Legend({ swatch, label }: { swatch: string; label: string }) {
  return (
    <span className="flex items-center gap-1.5 rounded-full border bg-background px-2.5 py-[3px]">
      <span className="size-[5px] rounded-full" style={{ background: swatch }} aria-hidden />
      <span className="lm-mono text-[9.5px] tracking-[0.11em] text-[var(--lm-muted-2)] uppercase">
        {label}
      </span>
    </span>
  );
}

/**
 * A document name over up to two lines.
 *
 * Legal filenames are long and front-loaded with the least distinguishing part
 * — "Compensation Data and Actual Wage Analysis for H-1B LCA Filings" cut at
 * twenty characters is "Compensation Data an…", which names nothing. SVG has no
 * text wrapping, so the break is computed here and rendered as tspans.
 *
 * Breaking on words, and only mid-word when a single word is longer than the
 * line, keeps the wrap where a reader would put it.
 */
function wrapLabel(value: string, perLine: number, maxLines: number): string[] {
  const words = value.split(/\s+/);
  const lines: string[] = [];
  let current = "";

  for (const word of words) {
    const candidate = current ? `${current} ${word}` : word;
    if (candidate.length <= perLine) {
      current = candidate;
      continue;
    }
    if (current) lines.push(current);
    if (lines.length === maxLines) break;
    // A single word wider than the line still has to break somewhere.
    current = word.length > perLine ? `${word.slice(0, perLine - 1)}…` : word;
  }
  if (current && lines.length < maxLines) lines.push(current);

  // Mark the truncation on the last line when there was more to say.
  const rendered = lines.join(" ");
  if (rendered.replace(/…$/, "").length < value.replace(/\s+/g, " ").length && lines.length) {
    const last = lines[lines.length - 1];
    if (!last.endsWith("…")) {
      lines[lines.length - 1] =
        last.length >= perLine ? `${last.slice(0, perLine - 1)}…` : `${last}…`;
    }
  }
  return lines;
}

const truncate = (value: string, limit: number) =>
  value.length > limit ? `${value.slice(0, limit - 1)}…` : value;
