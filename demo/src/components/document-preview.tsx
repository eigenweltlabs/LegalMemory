"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { AlertCircle, Download, FileQuestion, Loader2, X } from "lucide-react";

import type { TreeFile } from "@/lib/appliance";
import { cn } from "@/lib/utils";
import { EmlPreview } from "./eml-preview";
import { PptxPreview } from "./pptx-preview";
import { fileKind, FileGlyph } from "./file-glyph";

/**
 * The selected document, rendered as itself.
 *
 * Every branch below shows the original bytes, with one exception. A PDF goes
 * to the browser's own viewer, a Word file is laid out from its OOXML, a
 * spreadsheet keeps its sheets and its cells. The exception is the formats a
 * browser cannot open at all — email, Outlook messages, scans — where the
 * appliance's own conversion is shown instead, and is labelled as such. A
 * preview that quietly substitutes extracted text for a signed PDF is worse
 * than no preview, because it is the version somebody will read and believe.
 */

type Mode =
  | "pdf"
  | "docx"
  | "sheet"
  | "image"
  | "html"
  | "text"
  | "eml"
  | "pptx"
  | "converted"
  | "unsupported";

function previewMode(file: TreeFile): Mode {
  const kind = fileKind(file.mime_type, file.name);
  const extension = file.name.split(".").pop()?.toLowerCase() ?? "";
  switch (kind) {
    case "pdf":
      return "pdf";
    case "word":
      // Only OOXML can be laid out in the browser; .doc and .rtf are converted.
      return extension === "docx" ? "docx" : "converted";
    case "excel":
      return "sheet";
    case "slides":
      // Only OOXML is a zip of readable parts; legacy binary .ppt is not, and
      // keeps the appliance's converted text.
      return extension === "pptx" ? "pptx" : "converted";
    case "image":
      return "image";
    case "text":
      return extension === "html" || extension === "htm" ? "html" : "text";
    case "email":
      // .eml is RFC 5322 and can be read directly. .msg is Microsoft's compound
      // binary format, which cannot, so it keeps the converted text.
      return extension === "eml" ? "eml" : "converted";
    default:
      return "unsupported";
  }
}

export function DocumentPreview({ file, onClose }: { file: TreeFile; onClose: () => void }) {
  return (
    <div className="flex h-full min-h-0 flex-col bg-background">
      <PreviewHeader file={file} onClose={onClose} />
      <div className="lm-scroll min-h-0 flex-1 overflow-auto bg-[var(--lm-paper-2)]">
        {/* Keyed on the document so switching files unmounts the previous
            renderer rather than trying to reconcile a docx into a spreadsheet. */}
        <PreviewBody key={file.source_object_id} file={file} />
      </div>
    </div>
  );
}

function PreviewHeader({ file, onClose }: { file: TreeFile; onClose: () => void }) {
  const href = `/api/preview?document_id=${encodeURIComponent(file.document_id)}&version_id=${encodeURIComponent(file.version_id)}`;
  return (
    <header className="flex-none border-b px-5 pt-4 pb-3.5">
      <div className="flex items-start gap-3">
        <FileGlyph mime={file.mime_type} name={file.name} className="mt-[3px] size-4" />
        <div className="min-w-0 flex-1">
          <h2 className="font-emphasis truncate text-[15px]">{file.title || file.name}</h2>
          <p className="lm-mono mt-1 truncate text-[10.5px] text-[var(--lm-muted-3)]">
            {file.path}
          </p>
        </div>
        <a
          href={href}
          download={file.name}
          className="flex-none rounded-lg border px-2.5 py-1.5 text-[var(--lm-muted-2)] transition-colors duration-[var(--lm-dur-fast)] hover:border-[rgba(233,87,0,0.35)] hover:text-[var(--lm-orange)]"
          title="Download the original"
        >
          <Download className="size-3.5" />
        </a>
        <button
          type="button"
          onClick={onClose}
          title="Close the preview"
          aria-label="Close the preview"
          className="flex-none rounded-lg border px-2.5 py-1.5 text-[var(--lm-muted-2)] transition-colors duration-[var(--lm-dur-fast)] hover:border-[rgba(233,87,0,0.35)] hover:text-[var(--lm-orange)]"
        >
          <X className="size-3.5" />
        </button>
      </div>
      <dl className="mt-3 flex flex-wrap items-center gap-x-5 gap-y-1.5">
        {file.matter?.title && <Fact label="Matter" value={file.matter.title} />}
        {file.version_status && <Fact label="Version" value={file.version_status} />}
        {file.size_bytes != null && <Fact label="Size" value={humanSize(file.size_bytes)} />}
        {file.language && <Fact label="Lang" value={file.language} />}
      </dl>
    </header>
  );
}

function Fact({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline gap-1.5">
      <dt className="lm-label text-[9.5px]">{label}</dt>
      <dd className="text-[12px] text-[var(--lm-fg2)]">{value}</dd>
    </div>
  );
}

function PreviewBody({ file }: { file: TreeFile }) {
  const mode = useMemo(() => previewMode(file), [file]);
  const src = `/api/preview?document_id=${encodeURIComponent(file.document_id)}&version_id=${encodeURIComponent(file.version_id)}`;

  switch (mode) {
    case "pdf":
      // Not sandboxed, deliberately: `sandbox` disables plugins, and the
      // built-in PDF viewer is one — a sandboxed frame renders blank. The
      // bytes are same-origin and PDFium sandboxes any script inside the PDF.
      return <iframe src={src} title={file.name} className="h-full w-full border-0 bg-white" />;
    case "image":
      return (
        <div className="flex min-h-full items-center justify-center p-6">
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img src={src} alt={file.name} className="max-h-full max-w-full object-contain shadow-sm" />
        </div>
      );
    case "html":
      // Sandboxed, and here it must be: this is untrusted markup from the
      // firm's estate, rendered on the demo's own origin.
      return <iframe src={src} title={file.name} sandbox="" className="h-full w-full border-0 bg-white" />;
    case "docx":
      return <DocxPreview src={src} />;
    case "sheet":
      return <SheetPreview src={src} name={file.name} />;
    case "text":
      return <TextPreview src={src} />;
    case "eml":
      return <EmlPreview src={src} />;
    case "pptx":
      return <PptxPreview src={src} name={file.name} />;
    case "converted":
      return <ConvertedText file={file} />;
    default:
      return <ConvertedText file={file} fallbackNotice />;
  }
}

// ------------------------------------------------------------------- loaders

function useBytes(src: string) {
  const [state, setState] = useState<{
    status: "loading" | "ready" | "error";
    src: string;
    buffer?: ArrayBuffer;
    error?: string;
  }>({ status: "loading", src });

  // Reset during render rather than in the effect below. Setting state inside
  // an effect schedules a second render that paints the previous document's
  // bytes under the new document's header for a frame; adjusting it here means
  // the first render after a change is already the loading state.
  if (state.src !== src) setState({ status: "loading", src });

  useEffect(() => {
    let cancelled = false;
    fetch(src)
      .then(async (response) => {
        if (!response.ok) {
          const detail = await response.json().catch(() => ({ error: "preview unavailable" }));
          throw new Error(detail.error ?? "preview unavailable");
        }
        return response.arrayBuffer();
      })
      .then((buffer) => !cancelled && setState({ status: "ready", src, buffer }))
      .catch((error: Error) => !cancelled && setState({ status: "error", src, error: error.message }));
    return () => {
      cancelled = true;
    };
  }, [src]);

  return state;
}

function DocxPreview({ src }: { src: string }) {
  const host = useRef<HTMLDivElement>(null);
  const { status, buffer, error } = useBytes(src);
  const [rendering, setRendering] = useState(false);

  useEffect(() => {
    if (status !== "ready" || !buffer || !host.current) return;
    const target = host.current;
    let cancelled = false;
    setRendering(true);
    // Imported on demand: docx-preview is the largest dependency in this app
    // and most sessions never open a Word file.
    import("docx-preview")
      .then(({ renderAsync }) =>
        renderAsync(buffer, target, undefined, {
          className: "docx",
          inWrapper: true,
          ignoreWidth: false,
          ignoreHeight: false,
          // Word's own numbering and fonts, so a numbered contract keeps its
          // clause numbers — which is most of what makes a preview citable.
          experimental: true,
        }),
      )
      .catch(() => {
        if (!cancelled) target.textContent = "";
      })
      .finally(() => !cancelled && setRendering(false));
    return () => {
      cancelled = true;
      target.replaceChildren();
    };
  }, [buffer, status]);

  if (status === "loading") return <Busy label="Loading document" />;
  if (status === "error") return <Failed message={error} />;
  return (
    <div className="p-6">
      {rendering && <Busy label="Laying out" />}
      <div ref={host} className="docx-host [&_.docx-wrapper]:!bg-transparent [&_.docx-wrapper]:!p-0 [&_section.docx]:!mb-6 [&_section.docx]:!shadow-sm" />
    </div>
  );
}

function SheetPreview({ src, name }: { src: string; name: string }) {
  const { status, buffer, error } = useBytes(src);
  const [sheets, setSheets] = useState<{ name: string; html: string }[]>([]);
  const [active, setActive] = useState(0);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    if (status !== "ready" || !buffer) return;
    let cancelled = false;
    import("xlsx")
      .then((XLSX) => {
        const book = XLSX.read(buffer, { type: "array" });
        const rendered = book.SheetNames.map((sheetName) => ({
          name: sheetName,
          html: XLSX.utils.sheet_to_html(book.Sheets[sheetName], { id: `sheet-${sheetName}` }),
        }));
        if (!cancelled) {
          setSheets(rendered);
          setActive(0);
        }
      })
      .catch(() => !cancelled && setFailed(true));
    return () => {
      cancelled = true;
    };
  }, [buffer, status]);

  if (status === "loading") return <Busy label="Loading spreadsheet" />;
  if (status === "error") return <Failed message={error} />;
  if (failed) return <Failed message={`${name} could not be parsed as a spreadsheet.`} />;
  if (!sheets.length) return <Busy label="Reading sheets" />;

  return (
    <div className="flex h-full min-h-0 flex-col">
      {sheets.length > 1 && (
        // Tabs, because a workbook's other sheets are usually where the
        // assumptions live, and a preview that shows only the first one hides
        // exactly the part somebody is checking.
        <div className="lm-scroll flex flex-none gap-1 overflow-x-auto border-b bg-background px-3 py-2">
          {sheets.map((sheet, index) => (
            <button
              key={sheet.name}
              type="button"
              onClick={() => setActive(index)}
              className={cn(
                "rounded-md px-2.5 py-1 text-[12px] whitespace-nowrap transition-colors duration-[var(--lm-dur-fast)]",
                index === active
                  ? "bg-[rgba(233,87,0,0.1)] font-medium text-[var(--lm-orange)]"
                  : "text-[var(--lm-muted-2)] hover:bg-[var(--lm-paper-2)]",
              )}
            >
              {sheet.name}
            </button>
          ))}
        </div>
      )}
      <div className="lm-scroll min-h-0 flex-1 overflow-auto p-5">
        <div
          className={cn(
            "w-fit min-w-full overflow-hidden rounded-xl border bg-background",
            "[&_table]:w-full [&_table]:border-collapse [&_table]:text-[12px]",
            "[&_td]:border-b [&_td]:border-r [&_td]:px-2.5 [&_td]:py-1.5 [&_td]:whitespace-nowrap",
            "[&_tr:first-child_td]:bg-[var(--lm-paper-2)] [&_tr:first-child_td]:font-medium",
          )}
          dangerouslySetInnerHTML={{ __html: sheets[active].html }}
        />
      </div>
    </div>
  );
}

function TextPreview({ src }: { src: string }) {
  const [text, setText] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetch(src)
      .then((response) => (response.ok ? response.text() : Promise.reject(new Error("unavailable"))))
      .then((body) => !cancelled && setText(body))
      .catch(() => !cancelled && setError("This file could not be read."));
    return () => {
      cancelled = true;
    };
  }, [src]);

  if (error) return <Failed message={error} />;
  if (text === null) return <Busy label="Loading" />;
  return (
    <div className="p-5">
      <pre className="lm-mono rounded-xl border bg-background p-5 text-[12px] leading-[1.65] whitespace-pre-wrap">
        {text}
      </pre>
    </div>
  );
}

/**
 * The appliance's own conversion, for what the browser cannot open.
 *
 * Labelled rather than presented as the document, and offering the original
 * alongside it, so nobody reads extracted text believing they have read the file.
 */
function ConvertedText({ file, fallbackNotice }: { file: TreeFile; fallbackNotice?: boolean }) {
  const [state, setState] = useState<{ text: string; more: boolean } | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const params = new URLSearchParams({
      document_id: file.document_id,
      version_id: file.version_id,
    });
    fetch(`/api/document-text?${params}`)
      .then(async (response) => {
        const body = await response.json();
        if (!response.ok) throw new Error(body.error ?? "unavailable");
        return body as { text: string; page: { has_more?: boolean } | null };
      })
      .then((body) => {
        if (!cancelled) setState({ text: body.text, more: Boolean(body.page?.has_more) });
      })
      .catch((cause: Error) => !cancelled && setError(cause.message));
    return () => {
      cancelled = true;
    };
  }, [file.document_id, file.version_id]);

  if (error) return <Failed message={error} />;
  if (!state) return <Busy label="Loading text" />;

  return (
    <div className="p-5">
      <div className="mb-3 flex items-center gap-2 rounded-lg border border-[rgba(233,87,0,0.22)] bg-[rgba(233,87,0,0.05)] px-3 py-2">
        <FileQuestion className="size-3.5 shrink-0 text-[var(--lm-orange)]" />
        <p className="text-[12px] text-[var(--lm-fg2)]">
          {fallbackNotice
            ? "No in-browser viewer for this format."
            : "This format has no in-browser viewer."}{" "}
          Showing the text LegalMemory extracted when it indexed the file — download the original
          above to see the file itself.
        </p>
      </div>
      <pre className="rounded-xl border bg-background p-5 text-[13px] leading-[1.7] whitespace-pre-wrap">
        {state.text || "This document has no extracted text."}
      </pre>
      {state.more && (
        <p className="lm-label mt-3 text-center">Truncated at 40,000 characters</p>
      )}
    </div>
  );
}

// -------------------------------------------------------------------- states

const Busy = ({ label }: { label: string }) => (
  <div className="flex h-40 items-center justify-center gap-2 text-[13px] text-[var(--lm-muted)]">
    <Loader2 className="size-3.5 animate-spin" />
    {label}
  </div>
);

const Failed = ({ message }: { message?: string }) => (
  <div className="flex h-40 flex-col items-center justify-center gap-2 px-6 text-center">
    <AlertCircle className="size-4 text-[var(--lm-muted-3)]" />
    <p className="max-w-sm text-[13px] text-[var(--lm-muted)]">
      {message ?? "This document could not be previewed."}
    </p>
  </div>
);


function humanSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB"];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value < 10 ? value.toFixed(1) : Math.round(value)} ${units[unit]}`;
}
