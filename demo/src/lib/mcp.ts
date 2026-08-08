import "server-only";

import { experimental_createMCPClient as createMCPClient } from "@ai-sdk/mcp";
import { z } from "zod";

import { identityHeaders, mcpUrl } from "./appliance";

/**
 * A client for the appliance's MCP endpoint, per request.
 *
 * Deliberately not a module-level singleton. The identity is carried in the
 * transport headers, so a cached client is a cached identity, and the first
 * thing that would break when this demo grows a second user is the guarantee
 * the product is built to make. The endpoint is mounted stateless, so opening a
 * connection costs one round trip and nothing is lost by paying it.
 */
export async function openMcpClient() {
  return createMCPClient({
    transport: {
      type: "http",
      url: mcpUrl(),
      headers: identityHeaders(),
    },
  });
}

export type McpClient = Awaited<ReturnType<typeof openMcpClient>>;

/**
 * The tools the chat is allowed to call, with the shape it may call them in.
 *
 * The appliance serves fifteen, covering billing, entity resolution, ontology
 * traversal and the rest of the estate. This demo answers questions about
 * document contents, and a model holding tools for work it has been told to
 * refuse will eventually find a way to use them.
 *
 * Declaring the schemas rather than letting the client discover them does three
 * things. The AI SDK emits typed tool parts instead of `dynamic-tool` parts, so
 * the interface can render a card per tool by name. The model is offered a
 * handful of parameters instead of the appliance's full filter surface, which
 * is what keeps tool calls well-formed on a small, fast model. And the JSON
 * Schema the gateway forwards is one this file controls — the server's optional
 * arguments are `string | None`, which becomes a nullable union that several
 * providers reject outright.
 */
const searchFilters = {
  matter_id: z.string().optional().describe("Restrict to one matter."),
  doc_type: z.string().optional().describe("Ontology node id for a document type."),
  only_final: z
    .boolean()
    .optional()
    .describe("Only authoritative final/executed versions. Default false searches drafts too."),
  limit: z.number().int().min(1).max(20).optional().describe("Results to return; prefer 5-8."),
  offset: z
    .number()
    .int()
    .min(0)
    .optional()
    .describe("Skip this many results. Pass page.next_offset to continue a search."),
};

export const CHAT_TOOL_SCHEMAS = {
  search_semantic: {
    inputSchema: z.object({
      query: z.string().describe("What to look for, in natural language."),
      ...searchFilters,
    }),
  },
  search_filter: {
    inputSchema: z.object({
      ...searchFilters,
      identifier: z
        .string()
        .optional()
        .describe("Exact legal identifier: case number, Aktenzeichen, HRB, statute reference."),
      party: z.string().optional().describe("A party's exact canonical name, or a party id."),
    }),
  },
  get_document: {
    inputSchema: z.object({
      document_id: z.string().describe("From citations[].document.id in a search result."),
      version_id: z.string().optional(),
      offset: z.number().int().min(0).optional().describe("Continue from content_page.next_offset."),
    }),
  },
  find_related_documents: {
    inputSchema: z.object({
      document_id: z.string().describe("The document whose relations to trace."),
      include_same_matter: z.boolean().optional(),
      limit: z.number().int().min(1).max(50).optional(),
      offset: z.number().int().min(0).optional().describe("Continue from page.next_offset."),
    }),
  },
  list_matters: {
    inputSchema: z.object({
      limit: z.number().int().min(1).max(50).optional(),
      offset: z.number().int().min(0).optional().describe("Continue from page.next_offset."),
    }),
  },
} as const;

interface McpToolResult {
  content?: Array<{ type: string; text?: string }>;
  structuredContent?: Record<string, unknown>;
  isError?: boolean;
}

/**
 * One MCP tool call over plain JSON-RPC.
 *
 * The preview panel needs `download_document` and `get_document`, and it needs
 * them outside a model turn. Going through the AI SDK's client to get there
 * would mean constructing a fake tool-call context; the endpoint is mounted
 * stateless, so a single POST is the whole protocol.
 *
 * Using the same tools the model uses is also the honest version of this demo:
 * the preview is not a privileged side door into the blob store, it is the
 * capability the appliance issues, audited in the same ledger, expiring on the
 * same clock.
 */
export async function callMcpTool(
  name: string,
  args: Record<string, unknown>,
): Promise<McpToolResult> {
  const response = await fetch(mcpUrl(), {
    method: "POST",
    headers: {
      ...identityHeaders(),
      "content-type": "application/json",
      accept: "application/json, text/event-stream",
    },
    body: JSON.stringify({
      jsonrpc: "2.0",
      id: 1,
      method: "tools/call",
      params: { name, arguments: args },
    }),
    cache: "no-store",
  });
  if (!response.ok) {
    throw new Error(`MCP ${name} failed: ${response.status}`);
  }
  const payload = (await response.json()) as {
    result?: McpToolResult;
    error?: { message?: string };
  };
  if (payload.error) {
    throw new Error(payload.error.message ?? `MCP ${name} returned an error`);
  }
  const result = payload.result ?? {};
  if (result.isError) {
    const detail = result.content?.find((item) => item.type === "text")?.text;
    throw new Error(detail ?? `MCP ${name} returned an error`);
  }
  return result;
}
