"use client";

import {
  FileSpreadsheet,
  FileText,
  File as FileIcon,
  Image as ImageIcon,
  Mail,
  Presentation,
} from "lucide-react";

import { cn } from "@/lib/utils";

/**
 * What kind of thing a row is, at eleven pixels.
 *
 * Colour is doing real work here rather than decorating: at this size the
 * silhouettes of a Word and an Excel icon are nearly identical, and the tint is
 * what separates a brief from a damages model when scanning a folder. It stays
 * muted so a hundred rows do not turn into a fruit salad, and the selected row
 * takes the product orange — the one row that is allowed to be loud.
 */

export type FileKind = "pdf" | "word" | "excel" | "slides" | "image" | "email" | "text" | "other";

const EXTENSIONS: Record<string, FileKind> = {
  pdf: "pdf",
  doc: "word",
  docx: "word",
  rtf: "word",
  odt: "word",
  xls: "excel",
  xlsx: "excel",
  xlsm: "excel",
  csv: "excel",
  ods: "excel",
  ppt: "slides",
  pptx: "slides",
  odp: "slides",
  png: "image",
  jpg: "image",
  jpeg: "image",
  gif: "image",
  webp: "image",
  tif: "image",
  tiff: "image",
  bmp: "image",
  svg: "image",
  eml: "email",
  msg: "email",
  txt: "text",
  md: "text",
  json: "text",
  html: "text",
  htm: "text",
  xml: "text",
};

export function fileKind(mime: string | null | undefined, name: string): FileKind {
  const extension = name.split(".").pop()?.toLowerCase();
  // The extension first: connectors report `application/octet-stream` often
  // enough that trusting the MIME type alone leaves a folder of grey icons.
  if (extension && EXTENSIONS[extension]) return EXTENSIONS[extension];

  const type = (mime ?? "").toLowerCase();
  if (type.includes("pdf")) return "pdf";
  if (type.includes("wordprocessing") || type.includes("msword")) return "word";
  if (type.includes("spreadsheet") || type.includes("excel") || type === "text/csv") return "excel";
  if (type.includes("presentation") || type.includes("powerpoint")) return "slides";
  if (type.startsWith("image/")) return "image";
  if (type.includes("rfc822") || type.includes("outlook")) return "email";
  if (type.startsWith("text/") || type.includes("json") || type.includes("xml")) return "text";
  return "other";
}

const GLYPHS: Record<FileKind, { icon: typeof FileIcon; tint: string }> = {
  pdf: { icon: FileText, tint: "text-[#B3261E]" },
  word: { icon: FileText, tint: "text-[#2352DE]" },
  excel: { icon: FileSpreadsheet, tint: "text-[#1E7A54]" },
  slides: { icon: Presentation, tint: "text-[#C2600A]" },
  image: { icon: ImageIcon, tint: "text-[#8669B9]" },
  email: { icon: Mail, tint: "text-[var(--lm-muted-2)]" },
  text: { icon: FileText, tint: "text-[var(--lm-muted-2)]" },
  other: { icon: FileIcon, tint: "text-[var(--lm-muted-3)]" },
};

export function FileGlyph({
  mime,
  name,
  active,
  className,
}: {
  mime: string | null | undefined;
  name: string;
  active?: boolean;
  className?: string;
}) {
  const { icon: Icon, tint } = GLYPHS[fileKind(mime, name)];
  return (
    <Icon
      className={cn("size-3.5 shrink-0", active ? "text-[var(--lm-orange)]" : tint, className)}
      aria-hidden
    />
  );
}
