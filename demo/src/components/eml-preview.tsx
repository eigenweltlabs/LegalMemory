"use client";

import { useEffect, useMemo, useState } from "react";
import { Paperclip } from "lucide-react";

import { cn } from "@/lib/utils";

/**
 * An .eml file, rendered as the message it is.
 *
 * Email is the format a litigation estate is mostly made of, and dumping RFC
 * 5322 into a `<pre>` buries the four things anybody actually reads — who wrote
 * it, to whom, when, and about what — under transport headers nobody does.
 *
 * The parser here is deliberately small and total: it never throws, and it
 * degrades to showing the raw source rather than showing nothing. Enough of
 * MIME is implemented to find the readable part of the messages a firm's
 * estate contains — folded headers, encoded words, quoted-printable and base64
 * bodies, multipart with a plain-text or HTML alternative. Anything it does not
 * recognise falls through to the source, which is honest and still readable.
 */

interface ParsedEmail {
  headers: Record<string, string>;
  text: string | null;
  html: string | null;
  attachments: string[];
  raw: string;
}

// ------------------------------------------------------------------ decoding
//
// Everything below works on a *byte string*: the message decoded as Latin-1, so
// that one JavaScript character is exactly one byte of the original. That is the
// only representation in which quoted-printable and base64 can be undone
// correctly, because both encode bytes and the message's charset is not known
// until its headers have been read. Decoding to text first is the bug this
// avoids: `=E2=80=94` becomes three characters, and an em dash renders as "â€".

const latin1 = (buffer: ArrayBuffer) => new TextDecoder("iso-8859-1").decode(buffer);

/** A byte string back to real bytes. */
const toBytes = (byteString: string) =>
  Uint8Array.from(byteString, (character) => character.charCodeAt(0));

/**
 * Bytes as text, under the charset that was declared for them.
 *
 * `fatal` on the declared charset so a mislabelled message falls through rather
 * than rendering a page of replacement characters. The last resort is
 * windows-1252 rather than iso-8859-1: a message that claims UTF-8 and is not
 * is nearly always Outlook writing CP1252, where 0x97 is an em dash — in
 * Latin-1 the same byte is an unprintable control character.
 */
function bytesToText(byteString: string, charset: string | undefined): string {
  const bytes = toBytes(byteString);
  for (const candidate of [charset?.toLowerCase(), "utf-8"]) {
    if (!candidate) continue;
    try {
      return new TextDecoder(candidate, { fatal: true }).decode(bytes);
    } catch {
      continue;
    }
  }
  return new TextDecoder("windows-1252").decode(bytes);
}

function decodeQuotedPrintable(input: string): string {
  return input
    // Soft line break: an `=` at end of line is a wrap, not content.
    .replace(/=(?:\r?\n|$)/g, "")
    .replace(/=([0-9A-Fa-f]{2})/g, (_, hex) => String.fromCharCode(parseInt(hex, 16)));
}

function decodeBase64(input: string): string {
  try {
    return atob(input.replace(/\s+/g, ""));
  } catch {
    return input;
  }
}

/** Undo the transfer encoding, staying in bytes. */
function decodeTransfer(body: string, encoding: string | undefined): string {
  switch ((encoding ?? "").toLowerCase().trim()) {
    case "base64":
      return decodeBase64(body);
    case "quoted-printable":
      return decodeQuotedPrintable(body);
    default:
      return body;
  }
}

/** RFC 2047 encoded words, which is how a non-ASCII subject line arrives. */
function decodeEncodedWords(value: string): string {
  return value.replace(
    /=\?([^?]+)\?([BbQq])\?([^?]*)\?=/g,
    (whole, charset: string, encoding: string, payload: string) => {
      try {
        const bytes =
          encoding.toUpperCase() === "B"
            ? decodeBase64(payload)
            : decodeQuotedPrintable(payload.replace(/_/g, " "));
        return bytesToText(bytes, charset);
      } catch {
        return whole;
      }
    },
  );
}

// ------------------------------------------------------------------- parsing

const charsetOf = (contentType: string) =>
  /charset=(?:3D)?"?([\w-]+)"?/i.exec(contentType)?.[1];

function splitHeaders(block: string): Record<string, string> {
  const headers: Record<string, string> = {};
  // Unfold first: a continuation line starts with whitespace and belongs to
  // the header above it, which is how long recipient lists arrive.
  const unfolded = block.replace(/\r?\n[ \t]+/g, " ");
  for (const line of unfolded.split(/\r?\n/)) {
    const separator = line.indexOf(":");
    if (separator <= 0) continue;
    const name = line.slice(0, separator).trim().toLowerCase();
    if (!(name in headers)) headers[name] = decodeEncodedWords(line.slice(separator + 1).trim());
  }
  return headers;
}

const HEADER_LINE =
  /^(?:mime-version|from|to|cc|bcc|subject|date|message-id|content-type|content-transfer-encoding|received|return-path|reply-to|organization|x-[\w-]+):/i;

/** Split at the first blank line: headers above, everything else below. */
function splitAtBlankLine(text: string): [string, string] {
  const blank = text.search(/\r?\n\r?\n/);
  if (blank < 0) return [text, ""];
  return [text.slice(0, blank), text.slice(blank).replace(/^\r?\n\r?\n/, "")];
}

/**
 * Headers a discovery export left inside the body.
 *
 * A message produced for review is often not a message any more: it is a text
 * file carrying a Bates number and then the original headers and text, wrapped
 * in a fresh envelope whose only real headers describe the wrapping. Read
 * literally, such a file has no sender, no date and no subject — which is
 * exactly the metadata somebody opening it wants.
 *
 * So once the body is decoded, its first lines are checked for headers, and
 * anything above them (the Bates number, a separator rule) is kept as preamble
 * rather than discarded.
 */
function promoteEmbeddedHeaders(
  body: string,
): { headers: Record<string, string>; body: string } | null {
  const lines = body.split(/\r?\n/);
  for (let index = 0, offset = 0; index < lines.length && index < 40; index += 1) {
    if (!HEADER_LINE.test(lines[index])) {
      offset += lines[index].length + 1;
      continue;
    }

    // Absorb consecutive header groups. These exports blank-line between the
    // transport headers and the From/To/Date/Subject block, so stopping at the
    // first blank line finds Message-ID and Organization and none of the four
    // fields anybody opened the message to read.
    let block = "";
    let rest = body.slice(offset);
    for (;;) {
      const [group, remainder] = splitAtBlankLine(rest);
      block += (block ? "\n" : "") + group;
      rest = remainder;
      if (!remainder || !HEADER_LINE.test(remainder)) break;
    }

    const headers = splitHeaders(block);
    // Only if it carries something a reader came for. A lone Content-Type at
    // the top of a body is a MIME artefact, not the message's metadata.
    return headers.from || headers.subject || headers.date ? { headers, body: rest } : null;
  }
  return null;
}

function parseEmail(buffer: ArrayBuffer): ParsedEmail {
  const source = latin1(buffer);
  const [headerBlock, rawBody] = splitAtBlankLine(source);
  const headers = splitHeaders(headerBlock);
  const raw = bytesToText(source, charsetOf(headers["content-type"] ?? ""));

  const contentType = headers["content-type"] ?? "";
  const boundary = /boundary="?([^";\r\n]+)"?/i.exec(contentType)?.[1];

  const attachments: string[] = [];
  let text: string | null = null;
  let html: string | null = null;

  if (boundary) {
    for (const part of rawBody.split(new RegExp(`--${escapeRegExp(boundary)}(?:--)?\\s*`))) {
      if (!part.trim()) continue;
      const [partHeaderBlock, partBody] = splitAtBlankLine(part);
      if (!partBody) continue;
      const partHeaders = splitHeaders(partHeaderBlock);
      const partType = partHeaders["content-type"] ?? "";
      const disposition = partHeaders["content-disposition"] ?? "";

      const filename =
        /filename="?([^";\r\n]+)"?/i.exec(disposition)?.[1] ??
        /name="?([^";\r\n]+)"?/i.exec(partType)?.[1];
      if (/attachment/i.test(disposition) && filename) {
        attachments.push(filename);
        continue;
      }

      const decoded = bytesToText(
        decodeTransfer(partBody, partHeaders["content-transfer-encoding"]),
        charsetOf(partType),
      );
      if (/text\/plain/i.test(partType) && text === null) text = decoded;
      else if (/text\/html/i.test(partType) && html === null) html = decoded;
    }
  } else {
    const decoded = bytesToText(
      decodeTransfer(rawBody, headers["content-transfer-encoding"]),
      charsetOf(contentType),
    );
    if (/text\/html/i.test(contentType)) html = decoded;
    else text = decoded;
  }

  if (text) {
    const promoted = promoteEmbeddedHeaders(text);
    if (promoted) {
      // The wrapper's headers stay as the fallback, so Content-Type and the
      // rest are still there; the original message's win where they collide.
      return {
        headers: { ...headers, ...promoted.headers },
        text: promoted.body,
        html,
        attachments,
        raw,
      };
    }
  }

  return { headers, text, html, attachments, raw };
}

const escapeRegExp = (value: string) => value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

// ------------------------------------------------------------------ the view

export function EmlPreview({ src }: { src: string }) {
  const [buffer, setBuffer] = useState<ArrayBuffer | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showSource, setShowSource] = useState(false);

  useEffect(() => {
    let cancelled = false;
    // Bytes, not `response.text()`. The appliance serves the original as
    // message/rfc822 with no charset, so `text()` guesses windows-1252 — and
    // the message's own charset, which is the correct answer, is stated inside
    // the bytes it just guessed at.
    fetch(src)
      .then((response) =>
        response.ok ? response.arrayBuffer() : Promise.reject(new Error("message unavailable")),
      )
      .then((bytes) => !cancelled && setBuffer(bytes))
      .catch((cause: Error) => !cancelled && setError(cause.message));
    return () => {
      cancelled = true;
    };
  }, [src]);

  const email = useMemo(() => (buffer === null ? null : parseEmail(buffer)), [buffer]);

  if (error) {
    return <p className="p-6 text-[13px] text-[var(--lm-muted)]">{error}</p>;
  }
  if (!email) {
    return <p className="p-6 text-[13px] text-[var(--lm-muted)]">Loading message…</p>;
  }

  const { headers } = email;
  const body = email.text ?? (email.html ? null : email.raw);

  return (
    <div className="compact:p-3 p-5">
      <article className="overflow-hidden rounded-xl border bg-background">
        <header className="compact:px-4 border-b px-5 py-4">
          <h3 className="font-emphasis text-[15px] leading-snug">
            {headers.subject || "(no subject)"}
          </h3>
          <dl className="mt-3 grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1.5">
            <Row label="From" value={headers.from} />
            <Row label="To" value={headers.to} />
            <Row label="Cc" value={headers.cc} />
            <Row label="Date" value={headers.date} />
          </dl>
          {email.attachments.length > 0 && (
            <div className="mt-3 flex flex-wrap items-center gap-1.5">
              {email.attachments.map((name) => (
                <span
                  key={name}
                  className="flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-[11.5px] text-[var(--lm-fg2)]"
                >
                  <Paperclip className="size-3 text-[var(--lm-muted-3)]" />
                  {name}
                </span>
              ))}
            </div>
          )}
        </header>

        <div className="compact:px-4 px-5 py-4">
          {showSource ? (
            <pre className="lm-mono text-[11.5px] leading-[1.6] whitespace-pre-wrap">
              {email.raw}
            </pre>
          ) : email.html && !email.text ? (
            // Sandboxed with no allowances at all: this is untrusted HTML from
            // a mailbox, and it renders on the demo's own origin.
            <iframe
              srcDoc={email.html}
              title={headers.subject || "message"}
              sandbox=""
              className="h-[60dvh] w-full border-0"
            />
          ) : (
            <pre className="text-[13px] leading-[1.7] whitespace-pre-wrap">
              {(body ?? "").trim() || "This message has no readable body."}
            </pre>
          )}
        </div>

        <footer className="compact:px-4 compact:py-3.5 flex justify-end border-t px-5 py-2.5">
          <button
            type="button"
            onClick={() => setShowSource((current) => !current)}
            className={cn(
              "lm-label transition-colors duration-[var(--lm-dur-fast)]",
              "hover:text-[var(--lm-orange)]",
            )}
          >
            {showSource ? "Show message" : "Show source"}
          </button>
        </footer>
      </article>
    </div>
  );
}

function Row({ label, value }: { label: string; value?: string }) {
  if (!value) return null;
  return (
    <>
      <dt className="lm-label pt-[3px] text-[9.5px]">{label}</dt>
      <dd className="min-w-0 text-[12.5px] break-words text-[var(--lm-fg2)]">{value}</dd>
    </>
  );
}
