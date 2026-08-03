"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import { ChevronRight, Loader2, Search, X } from "lucide-react";

import type { TreeFile, TreeFolder, TreePage, TreeRoot } from "@/lib/appliance";
import { cn } from "@/lib/utils";
import { FileGlyph } from "./file-glyph";

/**
 * The estate as a folder tree.
 *
 * Written for an appliance holding fifty thousand documents, which rules out
 * the two obvious implementations. The client never fetches the ledger and
 * builds a tree from it, and it never renders a node for a file nobody has
 * scrolled to. What it holds instead is a flat list of the rows currently
 * visible — the roots, plus the children of each open folder — which is
 * `useVirtualizer`'s natural input and stays proportional to what is expanded
 * rather than to what exists.
 *
 * Folders arrive whole for a level, files a page at a time. A folder with ten
 * thousand exhibits in it pages as you scroll; a folder with nine loads once.
 */

const PAGE_SIZE = 200;
const ROW_HEIGHT = 30;
const INDENT = 15;

type NodeKey = string;

/** `sourceId:path` — unique across connectors that share a path, which two
 *  connectors mirroring the same share routinely do. */
const folderKey = (sourceId: string, path: string): NodeKey => `${sourceId}:${path}`;

interface FolderState {
  status: "idle" | "loading" | "error";
  folders: TreeFolder[];
  files: TreeFile[];
  total: number;
  loaded: number;
  error?: string;
}

type Row =
  | { kind: "root"; key: NodeKey; depth: number; root: TreeRoot; open: boolean }
  | {
      kind: "folder";
      key: NodeKey;
      depth: number;
      sourceId: string;
      folder: TreeFolder;
      open: boolean;
    }
  | { kind: "file"; key: NodeKey; depth: number; file: TreeFile }
  | {
      kind: "more";
      key: NodeKey;
      depth: number;
      sourceId: string;
      path: string;
      loaded: number;
      remaining: number;
    }
  | { kind: "status"; key: NodeKey; depth: number; label: string; spinning?: boolean };

export interface FileTreeHandle {
  reveal: (documentId: string) => Promise<void>;
}

interface FileTreeProps {
  selected: TreeFile | null;
  onSelect: (file: TreeFile) => void;
  onReady?: (handle: FileTreeHandle) => void;
}

export function FileTree({ selected, onSelect, onReady }: FileTreeProps) {
  const [roots, setRoots] = useState<TreeRoot[] | null>(null);
  const [rootsError, setRootsError] = useState<string | null>(null);
  const [open, setOpen] = useState<Set<NodeKey>>(new Set());
  const [folders, setFolders] = useState<Map<NodeKey, FolderState>>(new Map());
  const [query, setQuery] = useState("");
  const [matches, setMatches] = useState<TreeFile[] | null>(null);
  const [searching, setSearching] = useState(false);
  // The file a reveal is waiting to scroll to, held until its row exists.
  const [pendingReveal, setPendingReveal] = useState<string | null>(null);

  const scrollRef = useRef<HTMLDivElement>(null);
  // The loader reads open/folders state and is itself a dependency of the
  // reveal handle. Kept in refs so expanding a folder does not rebuild the
  // handle and re-run the effect that publishes it.
  const foldersRef = useRef(folders);
  foldersRef.current = folders;
  const openRef = useRef(open);
  openRef.current = open;

  // --------------------------------------------------------------- loading

  const loadPage = useCallback(
    async (sourceId: string, path: string, offset: number): Promise<FolderState | null> => {
      const key = folderKey(sourceId, path);
      setFolders((prev) => {
        const next = new Map(prev);
        const existing = next.get(key);
        next.set(key, {
          status: "loading",
          folders: existing?.folders ?? [],
          files: existing?.files ?? [],
          total: existing?.total ?? 0,
          loaded: existing?.loaded ?? 0,
        });
        return next;
      });

      try {
        const params = new URLSearchParams({
          op: "children",
          source_id: sourceId,
          offset: String(offset),
          limit: String(PAGE_SIZE),
        });
        if (path) params.set("path", path);
        const response = await fetch(`/api/tree?${params}`);
        if (!response.ok) {
          throw new Error((await response.json().catch(() => ({}))).error ?? "listing failed");
        }
        const page = (await response.json()) as TreePage;

        let settled: FolderState | null = null;
        setFolders((prev) => {
          const next = new Map(prev);
          const existing = next.get(key);
          // Appending, not replacing: this is page two of a folder somebody is
          // scrolling, and the rows above it are already on screen.
          const files = offset > 0 ? [...(existing?.files ?? []), ...page.files] : page.files;
          settled = {
            status: "idle",
            folders: page.folders,
            files,
            total: page.pagination.total,
            loaded: files.length,
          };
          next.set(key, settled);
          return next;
        });
        return settled;
      } catch (error) {
        setFolders((prev) => {
          const next = new Map(prev);
          const existing = next.get(key);
          next.set(key, {
            status: "error",
            folders: existing?.folders ?? [],
            files: existing?.files ?? [],
            total: existing?.total ?? 0,
            loaded: existing?.loaded ?? 0,
            error: error instanceof Error ? error.message : "listing failed",
          });
          return next;
        });
        return null;
      }
    },
    [],
  );

  useEffect(() => {
    let cancelled = false;
    fetch("/api/tree?op=roots")
      .then(async (response) => {
        if (!response.ok) {
          throw new Error((await response.json().catch(() => ({}))).error ?? "unreachable");
        }
        return response.json() as Promise<{ roots: TreeRoot[] }>;
      })
      .then(({ roots: found }) => {
        if (cancelled) return;
        setRoots(found);
        // One connector is not a choice, so opening it saves everyone a click.
        if (found.length === 1) {
          setOpen(new Set([folderKey(found[0].source_id, "")]));
          void loadPage(found[0].source_id, "", 0);
        }
      })
      .catch((error: Error) => !cancelled && setRootsError(error.message));
    return () => {
      cancelled = true;
    };
  }, [loadPage]);

  const toggle = useCallback(
    (sourceId: string, path: string) => {
      const key = folderKey(sourceId, path);
      setOpen((prev) => {
        const next = new Set(prev);
        if (next.has(key)) {
          next.delete(key);
        } else {
          next.add(key);
          if (!foldersRef.current.has(key)) void loadPage(sourceId, path, 0);
        }
        return next;
      });
    },
    [loadPage],
  );

  // ---------------------------------------------------------------- reveal

  /**
   * Open the path to a document and scroll it into view.
   *
   * The server does the finding: `/api/tree/locate` returns the connector, the
   * ancestor folders and the file's ordinal within its own folder, so this
   * opens exactly the folders on the path and pages straight to the one page
   * the file is on. Walking the tree from the client would mean a request per
   * level and a scan of a directory that may hold ten thousand files.
   */
  const reveal = useCallback(
    async (documentId: string) => {
      const response = await fetch(
        `/api/tree?op=locate&document_id=${encodeURIComponent(documentId)}`,
      );
      if (!response.ok) return;
      const location = (await response.json()) as {
        source_id: string;
        path: string;
        ancestors: string[];
        index: number;
        file: TreeFile | null;
      };

      const chain = ["", ...location.ancestors];
      setOpen((prev) => {
        const next = new Set(prev);
        for (const path of chain) next.add(folderKey(location.source_id, path));
        return next;
      });

      // Ancestors first and in order, so each level's folder list exists before
      // the next one is drawn under it.
      for (const path of chain) {
        const key = folderKey(location.source_id, path);
        if (!foldersRef.current.has(key)) await loadPage(location.source_id, path, 0);
      }

      // Then pages of the containing folder until the file's ordinal is covered.
      const key = folderKey(location.source_id, location.path);
      let state = foldersRef.current.get(key);
      while (state && state.loaded <= location.index && state.loaded < state.total) {
        state = (await loadPage(location.source_id, location.path, state.loaded)) ?? undefined;
      }

      const revealed = location.file;
      if (revealed) {
        onSelect(revealed);
        // Hand the row to the virtualizer to scroll to, rather than scrolling
        // to it here. At this point the folders are open but React has not
        // re-rendered, and even once it has, a virtualized list has no DOM node
        // for a row outside the current window — which is exactly the case a
        // reveal is for. The effect below picks this up when the rows exist.
        setPendingReveal(revealed.source_object_id);
        // A reveal lands in the tree, so a search that is still filtering it
        // would hide the row that was just opened.
        setQuery("");
        setMatches(null);
      }
    },
    [loadPage, onSelect],
  );

  useEffect(() => {
    onReady?.({ reveal });
  }, [onReady, reveal]);

  // ---------------------------------------------------------------- search

  useEffect(() => {
    const term = query.trim();
    if (!term) {
      setMatches(null);
      setSearching(false);
      return;
    }
    setSearching(true);
    // Server-side, and debounced: filename search across the estate is a query,
    // not a filter over something the client already holds.
    const timer = setTimeout(() => {
      fetch(`/api/tree?op=search&query=${encodeURIComponent(term)}&limit=100`)
        .then((response) => (response.ok ? response.json() : { files: [] }))
        .then(({ files }: { files: TreeFile[] }) => setMatches(files))
        .catch(() => setMatches([]))
        .finally(() => setSearching(false));
    }, 220);
    return () => clearTimeout(timer);
  }, [query]);

  // ------------------------------------------------------------------ rows

  const rows = useMemo<Row[]>(() => {
    if (matches) {
      if (searching) return [{ kind: "status", key: "searching", depth: 0, label: "Searching", spinning: true }];
      if (!matches.length) {
        return [{ kind: "status", key: "no-matches", depth: 0, label: "No matching files" }];
      }
      return matches.map((file) => ({
        kind: "file" as const,
        key: file.source_object_id,
        depth: 0,
        file,
      }));
    }

    if (rootsError) {
      return [{ kind: "status", key: "roots-error", depth: 0, label: rootsError }];
    }
    if (!roots) {
      return [{ kind: "status", key: "roots-loading", depth: 0, label: "Connecting", spinning: true }];
    }
    if (!roots.length) {
      return [{ kind: "status", key: "roots-empty", depth: 0, label: "No documents indexed" }];
    }

    const out: Row[] = [];

    const walk = (sourceId: string, path: string, depth: number) => {
      const key = folderKey(sourceId, path);
      const state = folders.get(key);
      if (!state) {
        out.push({ kind: "status", key: `${key}:loading`, depth, label: "Loading", spinning: true });
        return;
      }
      if (state.status === "error") {
        out.push({ kind: "status", key: `${key}:error`, depth, label: state.error ?? "Failed" });
        return;
      }

      for (const folder of state.folders) {
        const childKey = folderKey(sourceId, folder.path);
        const isOpen = open.has(childKey);
        out.push({ kind: "folder", key: childKey, depth, sourceId, folder, open: isOpen });
        if (isOpen) walk(sourceId, folder.path, depth + 1);
      }
      for (const file of state.files) {
        out.push({ kind: "file", key: file.source_object_id, depth, file });
      }
      if (state.loaded < state.total) {
        out.push({
          kind: "more",
          key: `${key}:more`,
          depth,
          sourceId,
          path,
          loaded: state.loaded,
          remaining: state.total - state.loaded,
        });
      }
      if (state.status === "loading" && state.loaded > 0) {
        out.push({ kind: "status", key: `${key}:paging`, depth, label: "Loading", spinning: true });
      }
    };

    for (const root of roots) {
      const key = folderKey(root.source_id, "");
      const isOpen = open.has(key);
      out.push({ kind: "root", key, depth: 0, root, open: isOpen });
      if (isOpen) walk(root.source_id, "", 1);
    }
    return out;
  }, [folders, matches, open, roots, rootsError, searching]);

  const virtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => ROW_HEIGHT,
    overscan: 14,
    getItemKey: (index) => rows[index].key,
  });

  // Scroll a revealed file into view once its row is in the list.
  //
  // Runs on every rows change rather than once, because a reveal opens folders
  // whose children arrive over several fetches: the row often does not exist on
  // the first pass and appears a render or two later. `scrollToIndex` is the
  // virtualizer's own API and works whether or not the row is currently mounted,
  // which `scrollIntoView` on a queried node cannot do.
  useEffect(() => {
    if (!pendingReveal) return;
    const index = rows.findIndex(
      (row) => row.kind === "file" && row.file.source_object_id === pendingReveal,
    );
    if (index < 0) return;
    virtualizer.scrollToIndex(index, { align: "center", behavior: "smooth" });
    setPendingReveal(null);
  }, [pendingReveal, rows, virtualizer]);

  const totalFiles = roots?.reduce((sum, root) => sum + root.files, 0) ?? 0;

  return (
    <div className="flex h-full flex-col bg-background">
      <header className="flex-none border-b px-4 pt-4 pb-3">
        <div className="flex items-baseline justify-between gap-3">
          <span className="lm-label">Documents</span>
          {roots && (
            <span className="lm-mono text-[10.5px] text-[var(--lm-muted-3)] tabular-nums">
              {totalFiles.toLocaleString()}
            </span>
          )}
        </div>
        <div className="relative mt-3">
          <Search
            className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-[var(--lm-muted-3)]"
            aria-hidden
          />
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search filenames"
            aria-label="Search filenames"
            className={cn(
              "h-8 w-full rounded-lg border bg-[var(--lm-paper-2)] pr-7 pl-8 text-[13px]",
              "placeholder:text-[var(--lm-muted-3)]",
              "focus:border-[rgba(233,87,0,0.4)] focus:bg-background focus:ring-2 focus:ring-[var(--ring)] focus:outline-none",
              "transition-[background-color,border-color] duration-[var(--lm-dur-fast)]",
            )}
          />
          {query && (
            <button
              type="button"
              onClick={() => setQuery("")}
              aria-label="Clear search"
              className="absolute top-1/2 right-2 -translate-y-1/2 text-[var(--lm-muted-3)] hover:text-foreground"
            >
              <X className="size-3.5" />
            </button>
          )}
        </div>
      </header>

      <div ref={scrollRef} className="lm-scroll min-h-0 flex-1 overflow-auto py-1.5">
        <div className="relative w-full" style={{ height: virtualizer.getTotalSize() }}>
          {virtualizer.getVirtualItems().map((item) => {
            const row = rows[item.index];
            return (
              <div
                key={item.key}
                ref={virtualizer.measureElement}
                data-index={item.index}
                className="absolute top-0 left-0 w-full"
                style={{ transform: `translateY(${item.start}px)` }}
              >
                <TreeRow
                  row={row}
                  selectedId={selected?.source_object_id ?? null}
                  onToggle={toggle}
                  onSelect={onSelect}
                  onLoadMore={loadPage}
                  showPath={Boolean(matches)}
                />
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

// ------------------------------------------------------------------- one row

interface TreeRowProps {
  row: Row;
  selectedId: string | null;
  showPath: boolean;
  onToggle: (sourceId: string, path: string) => void;
  onSelect: (file: TreeFile) => void;
  onLoadMore: (sourceId: string, path: string, offset: number) => void;
}

function TreeRow({ row, selectedId, showPath, onToggle, onSelect, onLoadMore }: TreeRowProps) {
  const pad = { paddingLeft: 10 + row.depth * INDENT };

  if (row.kind === "status") {
    return (
      <div
        style={pad}
        className="flex h-[30px] items-center gap-2 pr-3 text-[12px] text-[var(--lm-muted-3)]"
      >
        {row.spinning && <Loader2 className="size-3 animate-spin" />}
        <span className="truncate">{row.label}</span>
      </div>
    );
  }

  if (row.kind === "more") {
    return (
      <button
        type="button"
        style={pad}
        onClick={() => onLoadMore(row.sourceId, row.path, row.loaded)}
        className="flex h-[30px] w-full items-center pr-3 text-left text-[12px] text-[var(--lm-orange)] hover:underline"
      >
        {/* The count is the honest version of an infinite scroll: a folder with
            nine thousand more files in it should say so. */}
        Show {Math.min(PAGE_SIZE, row.remaining).toLocaleString()} more
        <span className="ml-1 text-[var(--lm-muted-3)]">
          of {row.remaining.toLocaleString()}
        </span>
      </button>
    );
  }

  if (row.kind === "root") {
    return (
      <button
        type="button"
        style={pad}
        data-root={row.root.source_id}
        aria-expanded={row.open}
        onClick={() => onToggle(row.root.source_id, "")}
        className="group flex h-[30px] w-full items-center gap-1.5 pr-3 text-left hover:bg-[var(--lm-paper-2)]"
      >
        <ChevronRight
          className={cn(
            "size-3.5 shrink-0 text-[var(--lm-muted-3)] transition-transform duration-[var(--lm-dur-fast)]",
            row.open && "rotate-90",
          )}
        />
        <span className="lm-label truncate text-[10px] text-[var(--lm-muted-2)]">
          {row.root.display_name}
        </span>
        <span className="lm-mono ml-auto shrink-0 text-[10px] text-[var(--lm-muted-3)] tabular-nums">
          {row.root.files.toLocaleString()}
        </span>
      </button>
    );
  }

  if (row.kind === "folder") {
    return (
      <button
        type="button"
        style={pad}
        data-folder={row.folder.path}
        aria-expanded={row.open}
        onClick={() => onToggle(row.sourceId, row.folder.path)}
        className="group flex h-[30px] w-full items-center gap-1.5 pr-3 text-left hover:bg-[var(--lm-paper-2)]"
      >
        <ChevronRight
          className={cn(
            "size-3.5 shrink-0 text-[var(--lm-muted-3)] transition-transform duration-[var(--lm-dur-fast)]",
            row.open && "rotate-90",
          )}
        />
        <span className="truncate text-[13px] text-foreground">{row.folder.name}</span>
        <span className="lm-mono ml-auto shrink-0 text-[10px] text-[var(--lm-muted-3)] opacity-0 tabular-nums group-hover:opacity-100">
          {row.folder.files.toLocaleString()}
        </span>
      </button>
    );
  }

  const active = row.file.source_object_id === selectedId;
  return (
    <button
      type="button"
      data-node={row.file.source_object_id}
      style={pad}
      onClick={() => onSelect(row.file)}
      title={row.file.path}
      className={cn(
        "group relative flex h-[30px] w-full items-center gap-1.5 pr-3 text-left",
        "transition-colors duration-[var(--lm-dur-fast)]",
        active ? "bg-[rgba(233,87,0,0.08)]" : "hover:bg-[var(--lm-paper-2)]",
      )}
    >
      {/* The one place orange marks a row. A selected document is the only
          thing on this side of the screen the eye should be able to find
          without reading. */}
      {active && (
        <span className="absolute inset-y-0 left-0 w-[2px] bg-[var(--lm-orange)]" aria-hidden />
      )}
      <span className="ml-[14px] flex shrink-0 items-center">
        <FileGlyph mime={row.file.mime_type} name={row.file.name} active={active} />
      </span>
      <span
        className={cn(
          "truncate text-[13px]",
          active ? "font-emphasis text-foreground" : "text-[var(--lm-fg2)]",
        )}
      >
        {row.file.name}
      </span>
      {showPath && (
        <span className="lm-mono ml-auto max-w-[45%] shrink-0 truncate pl-2 text-[10px] text-[var(--lm-muted-3)]">
          {row.file.path}
        </span>
      )}
    </button>
  );
}
