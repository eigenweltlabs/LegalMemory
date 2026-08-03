"use client";

import { useCallback, useRef, useState } from "react";

import type { TreeFile } from "@/lib/appliance";
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
 */
export function Workspace() {
  const [selected, setSelected] = useState<TreeFile | null>(null);
  const tree = useRef<FileTreeHandle | null>(null);

  const onReady = useCallback((handle: FileTreeHandle) => {
    tree.current = handle;
  }, []);

  // The tree owns revealing, because it owns which folders are open and where
  // the scroll is. This hands the id over and lets it do that.
  const reveal = useCallback((documentId: string) => {
    void tree.current?.reveal(documentId);
  }, []);

  return (
    // Sizes are strings: react-resizable-panels v4 reads a bare number as
    // pixels, so `defaultSize={22}` would be a 22-pixel sidebar.
    //
    // Panels carry stable ids so the group tracks them across the preview
    // opening and closing. Keying the group instead would remount it — and with
    // it the tree, which would lose every folder the user had opened the moment
    // they clicked a file in one.
    <ResizablePanelGroup orientation="horizontal" className="h-full">
      <ResizablePanel
        id="tree"
        defaultSize={selected ? "22" : "26"}
        minSize="15"
        maxSize="40"
        className="min-w-0"
      >
        <FileTree selected={selected} onSelect={setSelected} onReady={onReady} />
      </ResizablePanel>

      <ResizableHandle className="bg-[var(--lm-line)] transition-colors duration-[var(--lm-dur-fast)] hover:bg-[rgba(233,87,0,0.35)]" />

      {/* The preview exists only once there is something to preview. An empty
          third of the screen holding a placeholder is a worse answer to "what
          is this application" than two columns that do their jobs, and the
          chat and tree are both better for the room. */}
      {selected && (
        <>
          <ResizablePanel id="preview" defaultSize="46" minSize="25" className="min-w-0">
            <DocumentPreview file={selected} onClose={() => setSelected(null)} />
          </ResizablePanel>

          <ResizableHandle className="bg-[var(--lm-line)] transition-colors duration-[var(--lm-dur-fast)] hover:bg-[rgba(233,87,0,0.35)]" />
        </>
      )}

      <ResizablePanel
        id="chat"
        defaultSize={selected ? "32" : "74"}
        minSize="22"
        className="min-w-0"
      >
        <ChatPanel onReveal={reveal} />
      </ResizablePanel>
    </ResizablePanelGroup>
  );
}
