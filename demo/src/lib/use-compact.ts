"use client";

import { useSyncExternalStore } from "react";

/**
 * One definition of "too small for three columns".
 *
 * Narrow, or short and not wide: a phone in portrait is the first case, a phone
 * in landscape the second — 844 by 390 is wide enough for three columns and has
 * nowhere near the height to read a document in one. The second clause stops
 * below tablet width so a laptop with the devtools open keeps its columns.
 *
 * The same query is a Tailwind variant in globals.css, spelled identically.
 * Everything that can be done in CSS is done there — this is for the handful of
 * decisions that are not styling: how many nodes a graph draws, whether a Word
 * file is laid out at its own page width or reflowed to the pane.
 */
export const COMPACT_MEDIA =
  "(max-width: 767.98px), (max-width: 1023.98px) and (max-height: 599.98px)";

let query: MediaQueryList | null = null;
const list = () => (query ??= window.matchMedia(COMPACT_MEDIA));

function subscribe(onChange: () => void) {
  const media = list();
  media.addEventListener("change", onChange);
  return () => media.removeEventListener("change", onChange);
}

/**
 * The server has no viewport, so it renders the wide layout and the first client
 * render agrees with it; the effect after hydration corrects a phone. Nothing
 * that decides page structure reads this — the structure is CSS — so the
 * correction costs a second render of a graph, not a flash of the wrong screen.
 */
export const useCompact = () =>
  useSyncExternalStore(
    subscribe,
    () => list().matches,
    () => false,
  );
