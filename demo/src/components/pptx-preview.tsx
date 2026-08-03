"use client";

import { useEffect, useState } from "react";

/**
 * A PowerPoint deck, read out of its own OOXML.
 *
 * A .pptx is a zip: one XML part per slide, the speaker notes beside them, and
 * the pictures in `ppt/media`. That is enough to show what a slide says and
 * what it shows, which is what someone checking a citation needs.
 *
 * It is deliberately not a pixel render. Faithful layout means implementing
 * DrawingML — themes, placeholders inherited from masters, autofit, tables,
 * charts, transforms in EMUs — and the failure mode of a half-implemented
 * version is a slide that looks authoritative and has moved somebody's numbers.
 * Structure is preserved instead: slide order, the title, the body in its
 * original reading order, the pictures, and the notes. Anyone who needs the
 * real thing has the download button in the header.
 *
 * Parsing runs on the bytes already in the browser, with DOMParser rather than
 * a regex, because a regex over XML mis-handles exactly the decks that matter:
 * ones with entities, namespaces, or text split across formatting runs.
 */

type Shape = { kind: "title" | "body"; lines: string[] };
type Slide = { index: number; shapes: Shape[]; images: string[]; notes: string };

const NS_A = "http://schemas.openxmlformats.org/drawingml/2006/main";
const NS_P = "http://schemas.openxmlformats.org/presentationml/2006/main";
const NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships";

/** `ppt/slides/slide10.xml` sorts after `slide9.xml` only if you compare numbers. */
function slideNumber(path: string): number {
  return Number(path.match(/slide(\d+)\.xml$/)?.[1] ?? 0);
}

/**
 * The text of one shape, paragraph by paragraph.
 *
 * PowerPoint splits a single sentence across `<a:r>` runs whenever formatting
 * changes mid-word, so runs are joined within a paragraph and only paragraphs
 * become separate lines. `<a:br>` is an explicit line break and is honoured.
 */
function shapeText(sp: Element): string[] {
  const lines: string[] = [];
  for (const p of Array.from(sp.getElementsByTagNameNS(NS_A, "p"))) {
    let line = "";
    for (const node of Array.from(p.childNodes)) {
      if (node.nodeType !== 1) continue;
      const el = node as Element;
      if (el.localName === "r") line += el.textContent ?? "";
      else if (el.localName === "br") line += "\n";
      else if (el.localName === "fld") line += el.textContent ?? "";
    }
    const trimmed = line.trim();
    if (trimmed) lines.push(...trimmed.split("\n").map((s) => s.trim()).filter(Boolean));
  }
  return lines;
}

/** A shape is a title if the slide layout says so, not if it happens to be first. */
function isTitle(sp: Element): boolean {
  const ph = sp.getElementsByTagNameNS(NS_P, "ph")[0];
  const type = ph?.getAttribute("type") ?? "";
  return type === "title" || type === "ctrTitle";
}

export function PptxPreview({ src, name }: { src: string; name: string }) {
  const [slides, setSlides] = useState<Slide[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setSlides(null);
    setError(null);

    (async () => {
      try {
        const [{ default: JSZip }, response] = await Promise.all([
          // Imported on demand — the deck is the rare case in this corpus and
          // nobody browsing Word documents should pay for the unzipper.
          import("jszip"),
          fetch(src),
        ]);
        if (!response.ok) throw new Error(`preview responded ${response.status}`);
        const zip = await JSZip.loadAsync(await response.arrayBuffer());

        const parser = new DOMParser();
        const paths = Object.keys(zip.files)
          .filter((p) => /^ppt\/slides\/slide\d+\.xml$/.test(p))
          .sort((a, b) => slideNumber(a) - slideNumber(b));
        if (!paths.length) throw new Error("no slides found");

        // Data URIs rather than blob URLs: a blob URL has to be revoked, and
        // getting that wrong on a component that remounts per document leaks a
        // few megabytes per click.
        const media = new Map<string, string>();
        const mediaFor = async (target: string): Promise<string | null> => {
          const path = `ppt/media/${target.split("/").pop()}`;
          if (media.has(path)) return media.get(path)!;
          const entry = zip.file(path);
          if (!entry) return null;
          const ext = path.split(".").pop()?.toLowerCase() ?? "png";
          const mime = ext === "svg" ? "svg+xml" : ext === "jpg" ? "jpeg" : ext;
          const uri = `data:image/${mime};base64,${await entry.async("base64")}`;
          media.set(path, uri);
          return uri;
        };

        const parsed: Slide[] = [];
        for (const path of paths) {
          const xml = parser.parseFromString(await zip.file(path)!.async("text"), "application/xml");

          const shapes: Shape[] = [];
          for (const sp of Array.from(xml.getElementsByTagNameNS(NS_P, "sp"))) {
            const lines = shapeText(sp);
            if (lines.length) shapes.push({ kind: isTitle(sp) ? "title" : "body", lines });
          }

          // Pictures are referenced by relationship id, which resolves through
          // the slide's own _rels part.
          const images: string[] = [];
          const relsPath = path.replace(/slides\/(slide\d+\.xml)$/, "slides/_rels/$1.rels");
          const relsFile = zip.file(relsPath);
          if (relsFile) {
            const rels = parser.parseFromString(await relsFile.async("text"), "application/xml");
            const byId = new Map<string, string>();
            for (const rel of Array.from(rels.getElementsByTagName("Relationship"))) {
              byId.set(rel.getAttribute("Id") ?? "", rel.getAttribute("Target") ?? "");
            }
            for (const blip of Array.from(xml.getElementsByTagNameNS(NS_A, "blip"))) {
              const target = byId.get(blip.getAttributeNS(NS_R, "embed") ?? "");
              if (!target) continue;
              const uri = await mediaFor(target);
              if (uri) images.push(uri);
            }
          }

          const n = slideNumber(path);
          const notesFile = zip.file(`ppt/notesSlides/notesSlide${n}.xml`);
          let notes = "";
          if (notesFile) {
            const nx = parser.parseFromString(await notesFile.async("text"), "application/xml");
            notes = Array.from(nx.getElementsByTagNameNS(NS_P, "sp"))
              .flatMap((sp) => shapeText(sp))
              // The notes part repeats the slide number as its own shape.
              .filter((line) => line !== String(n))
              .join("\n");
          }

          parsed.push({ index: n, shapes, images, notes });
        }

        if (!cancelled) setSlides(parsed);
      } catch (cause) {
        if (!cancelled) setError(cause instanceof Error ? cause.message : "could not be read");
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [src]);

  if (error) {
    return (
      <div className="p-6 text-[13px] text-[var(--lm-muted-2)]">
        {name} could not be read as a presentation ({error}).
      </div>
    );
  }
  if (!slides) {
    return <div className="p-6 text-[13px] text-[var(--lm-muted-3)]">Reading slides…</div>;
  }

  return (
    <div className="mx-auto max-w-[820px] space-y-4 p-6">
      {slides.map((slide) => (
        <article
          key={slide.index}
          className="rounded-[10px] border bg-white p-6 shadow-sm"
          // 16:9 is the modern default, but a deck with one line of text should
          // not be padded to a full slide's height — min, not fixed.
          style={{ minHeight: 180 }}
        >
          <div className="lm-mono mb-3 text-[10px] tracking-[0.11em] text-[var(--lm-muted-3)] uppercase">
            Slide {slide.index}
          </div>

          {slide.shapes.map((shape, i) =>
            shape.kind === "title" ? (
              <h3 key={i} className="font-emphasis mb-3 text-[17px] leading-snug">
                {shape.lines.join(" ")}
              </h3>
            ) : (
              <ul key={i} className="mb-3 space-y-1.5 last:mb-0">
                {shape.lines.map((line, j) => (
                  <li key={j} className="text-[13.5px] leading-relaxed text-[var(--lm-fg2)]">
                    {line}
                  </li>
                ))}
              </ul>
            ),
          )}

          {slide.images.length > 0 && (
            <div className="mt-4 flex flex-wrap gap-3">
              {slide.images.map((uri, i) => (
                // eslint-disable-next-line @next/next/no-img-element
                <img
                  key={i}
                  src={uri}
                  alt=""
                  className="max-h-[240px] max-w-full rounded border object-contain"
                />
              ))}
            </div>
          )}

          {slide.notes && (
            <div className="mt-4 border-t pt-3">
              <div className="lm-label mb-1 text-[9.5px]">Speaker notes</div>
              <p className="text-[12.5px] leading-relaxed whitespace-pre-wrap text-[var(--lm-muted-2)]">
                {slide.notes}
              </p>
            </div>
          )}

          {!slide.shapes.length && !slide.images.length && (
            <p className="text-[13px] text-[var(--lm-muted-3)]">This slide has no text or pictures.</p>
          )}
        </article>
      ))}
    </div>
  );
}
