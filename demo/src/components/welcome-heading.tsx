"use client";

import { useEffect, useState } from "react";

import type { TreeRoot } from "@/lib/appliance";

/**
 * The empty-thread heading.
 *
 * The count is fetched rather than hard-coded because it is the one number that
 * makes the invitation concrete: "ask anything about the documents" is a chat
 * box, "ask anything about the 13,544 documents on the left" is an index. It
 * comes from the same roots call the tree makes, so the figure on this side and
 * the figure above the tree can never disagree.
 */
export function WelcomeHeading() {
  const [count, setCount] = useState<number | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetch("/api/tree?op=roots")
      .then((response) => (response.ok ? response.json() : Promise.reject(new Error())))
      .then(({ roots }: { roots: TreeRoot[] }) => {
        if (!cancelled) setCount(roots.reduce((sum, root) => sum + root.files, 0));
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, []);

  // Smaller on a small screen, and not for taste: this sits above the composer
  // and the openers, and at display size in a phone's landscape it pushes both
  // of them off the bottom of the screen.
  return (
    <div className="compact:mb-4 mb-6 flex flex-col items-center px-4 text-center">
      <h1 className="fade-in slide-in-from-bottom-1 animate-in fill-mode-both compact:text-[26px] text-[34px] leading-[1.05] font-semibold tracking-[-0.045em] duration-200">
        LegalMemory Demo
      </h1>
      <p className="compact:mt-2 compact:text-[14px] mt-3 max-w-md text-[15px] leading-relaxed text-[var(--lm-muted-2)]">
        {/* No skeleton and no jump: until the count arrives the sentence still
            reads, and the number slots into a gap the layout already had. */}
        Ask anything about the{" "}
        <span className="font-emphasis text-foreground tabular-nums">
          {count === null ? "" : count.toLocaleString()}
        </span>{" "}
        {/* Where the documents are depends on the screen: beside this on a
            desktop, behind the tab next to it on a phone. Naming a direction
            that is not there is worse than not pointing at all. */}
        documents<span className="compact:hidden"> on the left</span>.
      </p>
    </div>
  );
}
