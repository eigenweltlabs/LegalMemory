"use client";

import { useCallback, useRef, useState } from "react";

import type { TreeFile } from "@/lib/appliance";
import { cn } from "@/lib/utils";
import {
  ResizableHandle,
  ResizablePanel,
  ResizablePanelGroup,
} from "@/components/ui/resizable";
import { ChatPanel } from "./chat-panel";
import { DocumentPreview } from "./document-preview";
import { FileTree, type FileTreeHandle } from "./file-tree";

/**
 * Tree, document, chat.
 *
 * The three panels share exactly one piece of state — which document is
 * selected — and one action: reveal. An answer citing a document and a click in
 * the tree end in the same place, which is what makes the citations feel like
 * part of the file browser rather than a footnote next to it.
 *
 * Below the compact breakpoint the same three panels become tabs. They are not
 * rebuilt for it: the CSS in globals.css lays them on top of each other and
 * shows the one named by `data-active`, so switching tabs costs nothing and a
 * phone turned sideways does not throw away the conversation. Which is why the
 * tab bar is the only thing here that knows about screen size — everything
 * else is the same component on both.
 */

type Pane = "tree" | "preview" | "chat";

export function Workspace() {
  const [selected, setSelected] = useState<TreeFile | null>(null);
  // Ignored entirely on a wide screen, where all three panels are on show. The
  // chat is where a visitor starts: the openers are the invitation, and the
  // tree is one tab away rather than in front of the thing to do.
  const [pane, setPane] = useState<Pane>("chat");
  // The tab the current document was opened from, so closing it goes back where
  // the reader was — the tree if they were browsing, the chat if they followed
  // a citation — rather than to whichever tab we picked as a default.
  const cameFrom = useRef<Pane>("chat");
  const tree = useRef<FileTreeHandle | null>(null);

  const onReady = useCallback((handle: FileTreeHandle) => {
    tree.current = handle;
  }, []);

  const select = useCallback(
    (file: TreeFile) => {
      setSelected(file);
      if (pane !== "preview") cameFrom.current = pane;
      setPane("preview");
    },
    [pane],
  );

  const close = useCallback(() => {
    setSelected(null);
    setPane(cameFrom.current);
  }, []);

  // The tree owns revealing, because it owns which folders are open and where
  // the scroll is. This hands the id over and lets it do that; the tree calls
  // `select` with what it found, which brings the document to the front.
  const reveal = useCallback((documentId: string) => {
    void tree.current?.reveal(documentId);
  }, []);

  // A tab that is not on screen cannot be the open one. Nothing should reach
  // this — closing the preview picks a tab — but a blank screen is the failure
  // mode if something ever does.
  const active: Pane = pane === "preview" && !selected ? "chat" : pane;

  return (
    <div className="flex h-full min-h-0 flex-col">
      <PaneTabs active={active} hasPreview={Boolean(selected)} onSelect={setPane} />

      {/* Sizes are strings: react-resizable-panels v4 reads a bare number as
          pixels, so `defaultSize={22}` would be a 22-pixel sidebar.

          Panels carry stable ids so the group tracks them across the preview
          opening and closing. Keying the group instead would remount it — and
          with it the tree, which would lose every folder the user had opened
          the moment they clicked a file in one. */}
      <ResizablePanelGroup orientation="horizontal" className="min-h-0 flex-1">
        <ResizablePanel
          id="tree"
          data-active={active === "tree" || undefined}
          defaultSize={selected ? "22" : "26"}
          minSize="15"
          maxSize="40"
          className="min-w-0"
        >
          <FileTree selected={selected} onSelect={select} onReady={onReady} />
        </ResizablePanel>

        <ResizableHandle className="bg-[var(--lm-line)] transition-colors duration-[var(--lm-dur-fast)] hover:bg-[rgba(233,87,0,0.35)]" />

        {/* The preview exists only once there is something to preview. An empty
            third of the screen holding a placeholder is a worse answer to "what
            is this application" than two columns that do their jobs, and the
            chat and tree are both better for the room. */}
        {selected && (
          <>
            <ResizablePanel
              id="preview"
              data-active={active === "preview" || undefined}
              defaultSize="46"
              minSize="25"
              className="min-w-0"
            >
              <DocumentPreview file={selected} onClose={close} />
            </ResizablePanel>

            <ResizableHandle className="bg-[var(--lm-line)] transition-colors duration-[var(--lm-dur-fast)] hover:bg-[rgba(233,87,0,0.35)]" />
          </>
        )}

        <ResizablePanel
          id="chat"
          data-active={active === "chat" || undefined}
          defaultSize={selected ? "32" : "74"}
          minSize="22"
          className="min-w-0"
        >
          <ChatPanel onReveal={reveal} />
        </ResizablePanel>
      </ResizablePanelGroup>
    </div>
  );
}

/**
 * The three panels, as tabs, on a screen that holds one of them.
 *
 * Named for what each panel calls itself — the tree's header says Documents and
 * the chat's says Ask — so the tab and the header a tap away are the same word.
 * The middle tab appears with the document and leaves with it: a tab that opens
 * an empty pane is a tab that should not be there.
 */
function PaneTabs({
  active,
  hasPreview,
  onSelect,
}: {
  active: Pane;
  hasPreview: boolean;
  onSelect: (pane: Pane) => void;
}) {
  const tabs: Array<{ id: Pane; label: string }> = [
    { id: "tree", label: "Documents" },
    ...(hasPreview ? [{ id: "preview" as const, label: "Preview" }] : []),
    { id: "chat", label: "Ask" },
  ];

  return (
    <nav
      aria-label="Panels"
      className="compact:flex hidden flex-none items-stretch border-b bg-background"
    >
      {tabs.map((tab) => {
        const current = tab.id === active;
        return (
          <button
            key={tab.id}
            type="button"
            onClick={() => onSelect(tab.id)}
            aria-current={current ? "true" : undefined}
            className={cn(
              "relative h-11 flex-1 font-mono text-[10px] tracking-[0.11em] uppercase",
              "transition-colors duration-[var(--lm-dur-fast)]",
              current ? "text-foreground" : "text-[var(--lm-muted-3)]",
            )}
          >
            {tab.label}
            {/* The one mark of orange on this bar, on the one thing it names. */}
            {current && (
              <span
                className="absolute inset-x-0 bottom-0 h-[2px] bg-[var(--lm-orange)]"
                aria-hidden
              />
            )}
          </button>
        );
      })}
    </nav>
  );
}
