import "server-only";

import { readFile } from "node:fs/promises";
import { existsSync } from "node:fs";
import { pathToFileURL, fileURLToPath } from "node:url";
import { join } from "node:path";

// Reads the Pagefind index the docs build already ships at /docs/pagefind/.

const DOCS_DIR =
  process.env.DOCS_DIR ??
  (existsSync(join(process.cwd(), "..", "docs", "dist"))
    ? join(process.cwd(), "..", "docs", "dist")
    : "/srv/docs");

// Pagefind fetches its index shards; Node's fetch refuses file: URLs.
let fetchPatched = false;
function allowFileFetch() {
  if (fetchPatched) return;
  fetchPatched = true;
  const real = globalThis.fetch;
  globalThis.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
    const url =
      typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    if (!url.startsWith("file://")) return real(input, init);
    try {
      return new Response(new Uint8Array(await readFile(fileURLToPath(url))), { status: 200 });
    } catch {
      return new Response(null, { status: 404 });
    }
  };
}

type PagefindModule = {
  options: (opts: Record<string, unknown>) => Promise<void> | void;
  init: () => Promise<void>;
  search: (query: string) => Promise<{ results: Array<{ data: () => Promise<RawResult> }> }>;
};

type RawResult = {
  url: string;
  content?: string;
  meta?: { title?: string };
  sub_results?: Array<{ title?: string }>;
};

let pagefind: Promise<PagefindModule> | null = null;

function load(): Promise<PagefindModule> {
  if (pagefind) return pagefind;
  pagefind = (async () => {
    allowFileFetch();
    const entry = join(DOCS_DIR, "pagefind", "pagefind.js");
    if (!existsSync(entry)) throw new Error(`Pagefind index not found at ${entry}`);
    const mod = (await import(
      /* webpackIgnore: true */ pathToFileURL(entry).href
    )) as PagefindModule;
    await mod.options({ basePath: pathToFileURL(join(DOCS_DIR, "pagefind") + "/").href });
    await mod.init();
    return mod;
  })();
  pagefind.catch(() => {
    pagefind = null;
  });
  return pagefind;
}

// With a file: basePath Pagefind returns a filesystem path; the public path is
// the part from /docs/ onward.
function publicPath(url: string): string {
  let path = url.replace(/^\/?file:\/*/, "/");
  const marker = path.indexOf("/docs/");
  if (marker >= 0) return path.slice(marker);
  if (path.startsWith(DOCS_DIR)) path = path.slice(DOCS_DIR.length);
  return "/docs" + (path.startsWith("/") ? path : `/${path}`);
}

export interface ProductDocResult {
  title: string;
  url: string;
  content: string;
  sections: string[];
}

export async function searchProductDocs(query: string, limit = 3): Promise<ProductDocResult[]> {
  const mod = await load();
  const found = await mod.search(query);
  const top = found.results.slice(0, Math.max(1, Math.min(limit, 5)));
  const pages = await Promise.all(top.map((r) => r.data()));
  return pages.map((page) => ({
    title: page.meta?.title ?? "Untitled",
    url: publicPath(page.url),
    content: page.content ?? "",
    sections: (page.sub_results ?? [])
      .map((s) => s.title)
      .filter((t): t is string => Boolean(t)),
  }));
}
