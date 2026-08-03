"use client";

import { useEffect, useRef, useState } from "react";

/**
 * A PowerPoint deck, rendered as slides.
 *
 * `pptx-preview` reads the theme, the slide master and the layout, so shapes
 * land where the author put them and inherit the type they were given. That
 * inheritance is the whole difficulty of the format: a slide's XML usually does
 * not say what size its title is, only that it *is* the title, and everything
 * else comes from parts one or two levels up. Reading the slide alone gets the
 * words in the right order and nothing else right, which produces a page that
 * looks authoritative while having quietly moved somebody's numbers.
 *
 * The library is loaded on demand. It pulls in echarts to draw embedded charts,
 * which is a large dependency to hand to somebody who only opened a Word file.
 */

/** EMU — English Metric Units, PowerPoint's internal unit. 914400 to the inch. */
const DEFAULT_ASPECT = 3 / 4;

/**
 * The deck's own slide dimensions, from `ppt/presentation.xml`.
 *
 * Guessing this is not safe. A widescreen deck is 13.33in x 7.5in and a classic
 * one is 10in x 7.5in — the same height, a different width — so assuming 16:9
 * gives a 4:3 deck a box a quarter too short and clips every slide top and
 * bottom. The library renders into exactly the box it is given and does not
 * measure the deck itself, so the caller has to.
 */
async function slideAspect(bytes: ArrayBuffer): Promise<number> {
  try {
    const { default: JSZip } = await import("jszip");
    const zip = await JSZip.loadAsync(bytes);
    const xml = await zip.file("ppt/presentation.xml")?.async("text");
    const size = xml?.match(/<p:sldSz[^>]*\bcx="(\d+)"[^>]*\bcy="(\d+)"/);
    if (!size) return DEFAULT_ASPECT;
    const cx = Number(size[1]);
    const cy = Number(size[2]);
    return cx > 0 && cy > 0 ? cy / cx : DEFAULT_ASPECT;
  } catch {
    return DEFAULT_ASPECT;
  }
}

export function PptxPreview({ src, name }: { src: string; name: string }) {
  const host = useRef<HTMLDivElement>(null);
  const [error, setError] = useState<string | null>(null);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    const node = host.current;
    if (!node) return;

    let cancelled = false;
    // The previewer is untyped beyond `init`, and it owns the DOM it is given.
    let previewer: { destroy?: () => void } | null = null;

    setError(null);
    setReady(false);

    (async () => {
      try {
        const [{ init }, response] = await Promise.all([import("pptx-preview"), fetch(src)]);
        if (!response.ok) throw new Error(`preview responded ${response.status}`);
        const bytes = await response.arrayBuffer();
        if (cancelled) return;

        const aspect = await slideAspect(bytes);
        if (cancelled) return;

        // Sized from the pane it is in rather than a constant: this sits in a
        // resizable split, and a fixed width either overflows or leaves a
        // margin the deck did not ask for. The pagination controls the library
        // draws sit inside the slide box, so no room is reserved for them.
        const width = Math.max(node.clientWidth - 32, 320);
        node.replaceChildren();
        previewer = init(node, { width, height: Math.round(width * aspect), mode: "slide" });
        await (previewer as { preview: (b: ArrayBuffer) => Promise<unknown> }).preview(bytes);
        if (!cancelled) setReady(true);
      } catch (cause) {
        if (!cancelled) setError(cause instanceof Error ? cause.message : "could not be read");
      }
    })();

    return () => {
      cancelled = true;
      // The previewer registers listeners on the nodes it created; dropping the
      // children without telling it leaves those behind on every file switch.
      try {
        previewer?.destroy?.();
      } catch {
        // A half-initialised previewer has nothing to tear down.
      }
      node.replaceChildren();
    };
  }, [src]);

  return (
    <div className="min-h-full p-4">
      {error && (
        <div className="p-2 text-[13px] text-[var(--lm-muted-2)]">
          {name} could not be read as a presentation ({error}). The original is still
          downloadable from the header.
        </div>
      )}
      {!ready && !error && (
        <div className="p-2 text-[13px] text-[var(--lm-muted-3)]">Rendering slides…</div>
      )}
      <div
        ref={host}
        className="[&_canvas]:max-w-full [&_img]:max-w-full"
        // The library draws a white slide on the page's own background; the
        // shadow is what separates the two without redrawing its chrome.
        style={{ filter: ready ? "drop-shadow(0 2px 10px rgba(14,10,7,0.10))" : undefined }}
      />
    </div>
  );
}
