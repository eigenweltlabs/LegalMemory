"use client";

import { useState } from "react";
import { Check, Copy, Plug } from "lucide-react";

import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { cn } from "@/lib/utils";

/**
 * How to point an MCP client at this demo.
 *
 * The demo republishes the appliance's own tools, so anything that speaks MCP
 * can search this index, read documents and traverse relations — which is a
 * more convincing demonstration than the chat on this page, because it happens
 * inside the tool the lawyer already uses.
 *
 * Tabbed by client, LegalWork first: it is Eigenwelt's own, it ships a
 * first-party LegalMemory connector, and it is the one surface where connecting
 * is a form rather than a command. The endpoint and the auth note sit above the
 * tabs because they are the same whichever client you use — repeating them four
 * times would bury the one line that actually differs.
 */
export function McpModal({ authEnabled }: { authEnabled: boolean }) {
  const [open, setOpen] = useState(false);
  // Read in the browser: the demo does not know its own public URL server-side,
  // and a copied localhost address would be useless to everyone else.
  const origin = typeof window === "undefined" ? "" : window.location.origin;
  const endpoint = `${origin}/mcp/`;

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <button
          type="button"
          className={cn(
            "flex items-center gap-1.5 rounded-full border border-white/15 bg-white/5 px-3 py-[5px]",
            "font-mono text-[10.5px] tracking-[0.11em] text-white/70 uppercase",
            "transition-colors duration-[var(--lm-dur-fast)]",
            "hover:border-[rgba(233,87,0,0.5)] hover:text-[var(--lm-orange-hi)]",
          )}
        >
          <Plug className="size-3" aria-hidden />
          Test with MCP
        </button>
      </DialogTrigger>

      <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-xl">
        <DialogHeader>
          <DialogTitle className="text-[17px] tracking-[-0.035em]">
            Connect your own tools
          </DialogTitle>
          <DialogDescription className="text-[13px] leading-relaxed">
            This demo republishes the appliance&apos;s MCP server. Point a client at it and
            you get the same search, document reading and relation traversal the chat on
            this page uses — over the same index, with the same permissions applied.
          </DialogDescription>
        </DialogHeader>

        <section className="mt-1">
          <span className="lm-label">Endpoint</span>
          <CopyRow value={endpoint} />
          <p className="mt-2 text-[12px] leading-relaxed text-[var(--lm-muted-2)]">
            {authEnabled ? (
              <>
                No sign-in on this endpoint. The MCP tools search and read documents;
                they call no model, so there is nothing here to meter — the sign-in on
                the rest of the demo is what protects the chat. Add the URL and connect.
              </>
            ) : (
              <>
                This deployment is open, so no sign-in is required. Every call is still
                resolved against the demo&apos;s configured identity, so a client sees exactly
                what this page sees — no more.
              </>
            )}
          </p>
        </section>

        <Tabs defaultValue="legalwork" className="mt-5">
          <TabsList className="w-full">
            <TabsTrigger value="legalwork">LegalWork</TabsTrigger>
            <TabsTrigger value="claude">Claude</TabsTrigger>
            <TabsTrigger value="codex">Codex</TabsTrigger>
            <TabsTrigger value="other">Other CLI</TabsTrigger>
          </TabsList>

          {/* LegalWork leads: it is Eigenwelt's own client, it ships a
              first-party LegalMemory connector, and it is the only one of the
              four where connecting is a form rather than a command. */}
          <TabsContent value="legalwork" className="mt-3">
            <ol className="space-y-1.5 text-[13px] leading-relaxed text-[var(--lm-fg2)]">
              <li>
                <Step n="1" /> Settings → Connections →{" "}
                <b className="font-emphasis">LegalMemory</b>, the featured first-party
                connector.
              </li>
              <li>
                <Step n="2" /> For <code className="lm-mono">appliance</code>, paste the
                origin below. LegalWork appends <code className="lm-mono">/mcp/</code>{" "}
                itself.
              </li>
              <li>
                <Step n="3" /> Connect.{" "}
                No credentials, no OAuth — the endpoint is open.
              </li>
            </ol>
            <CopyRow value={origin} />
          </TabsContent>

          <TabsContent value="claude" className="mt-3">
            <p className="text-[12.5px] leading-relaxed text-[var(--lm-fg2)]">
              Claude Code, from a terminal:
            </p>
            <CopyRow multiline value={`claude mcp add --transport http legalmemory ${endpoint}`} />
            <p className="mt-3 text-[12.5px] leading-relaxed text-[var(--lm-fg2)]">
              Claude Desktop takes the same server as a connector under{" "}
              <b className="font-emphasis">Settings → Connectors → Add custom connector</b>,
              using the endpoint above.
            </p>
          </TabsContent>

          <TabsContent value="codex" className="mt-3">
            <p className="text-[12.5px] leading-relaxed text-[var(--lm-fg2)]">
              Codex CLI, which takes a streamable HTTP server with{" "}
              <code className="lm-mono">--url</code>:
            </p>
            <CopyRow multiline value={`codex mcp add legalmemory --url ${endpoint}`} />
            <p className="mt-3 text-[12.5px] leading-relaxed text-[var(--lm-fg2)]">
              Or in <code className="lm-mono">~/.codex/config.toml</code>:
            </p>
            <CopyRow
              multiline
              value={`[mcp_servers.legalmemory]\nurl = "${endpoint}"`}
            />
          </TabsContent>

          <TabsContent value="other" className="mt-3">
            <p className="text-[12.5px] leading-relaxed text-[var(--lm-fg2)]">
              Anything that speaks MCP over streamable HTTP. Most clients take a
              block like this:
            </p>
            <CopyRow
              multiline
              value={JSON.stringify(
                { mcpServers: { legalmemory: { type: "http", url: endpoint } } },
                null,
                2,
              )}
            />
            <p className="mt-3 text-[12.5px] leading-relaxed text-[var(--lm-fg2)]">
              To try it without configuring anything, the MCP Inspector will connect to
              the endpoint directly:
            </p>
            <CopyRow multiline value={`npx @modelcontextprotocol/inspector ${endpoint}`} />
          </TabsContent>
        </Tabs>

        <section className="mt-5 rounded-xl border bg-[var(--lm-paper-2)] px-3.5 py-3">
          <span className="lm-label">What a client can do</span>
          <p className="mt-1.5 text-[12.5px] leading-relaxed text-[var(--lm-muted-2)]">
            Search the index semantically or by exact legal metadata, read a document&apos;s
            text, trace its stored relations, list matters, and download the original file.
            Read-only throughout — nothing an MCP client does can change the index.
          </p>
        </section>
      </DialogContent>
    </Dialog>
  );
}

const Step = ({ n }: { n: string }) => (
  <span className="lm-mono mr-1.5 text-[10px] text-[var(--lm-orange)]">{n}</span>
);

function CopyRow({ value, multiline }: { value: string; multiline?: boolean }) {
  const [copied, setCopied] = useState(false);

  const copy = () => {
    if (typeof navigator === "undefined" || !navigator.clipboard) return;
    navigator.clipboard.writeText(value).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    });
  };

  return (
    <div className="mt-1.5 flex items-start gap-2 rounded-xl border bg-[var(--lm-paper-2)] py-2 pr-2 pl-3">
      <code
        className={cn(
          "lm-mono min-w-0 flex-1 text-[11.5px] leading-[1.6] text-[var(--lm-ink-900)]",
          multiline ? "whitespace-pre-wrap" : "truncate",
        )}
      >
        {value}
      </code>
      <button
        type="button"
        onClick={copy}
        aria-label="Copy"
        className="flex-none rounded-lg border bg-background p-1.5 text-[var(--lm-muted-2)] transition-colors duration-[var(--lm-dur-fast)] hover:border-[rgba(233,87,0,0.35)] hover:text-[var(--lm-orange)]"
      >
        {copied ? <Check className="size-3.5" /> : <Copy className="size-3.5" />}
      </button>
    </div>
  );
}
