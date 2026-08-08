import { convertToModelMessages, stepCountIs, streamText, type UIMessage } from "ai";
import { createOpenAICompatible } from "@ai-sdk/openai-compatible";

import { CHAT_TOOL_SCHEMAS, openMcpClient } from "@/lib/mcp";

export const maxDuration = 120;

/**
 * The model, by the name the gateway serves it under.
 *
 * OpenAI-compatible rather than a named provider: everything goes through
 * LiteLLM, so the demo carries no provider key, `DEMO_MODEL` is the only thing
 * that changes when the demo changes model, and the tokens it spends are
 * accounted in the same place as the appliance's own.
 */
function model() {
  const gateway = createOpenAICompatible({
    name: "legalmemory-gateway",
    baseURL: process.env.DEMO_MODEL_BASE_URL ?? "http://127.0.0.1:4000/v1",
    apiKey: process.env.DEMO_MODEL_API_KEY ?? "sk-lm-dev-only",
  });
  return gateway(process.env.DEMO_MODEL ?? "gemini-3.6-flash");
}

const SYSTEM_PROMPT = `You are the LegalMemory demo assistant. You answer questions about the documents indexed in this firm's LegalMemory appliance, and about nothing else.

## What you can do

Every fact you state about a document must come from a tool call in this conversation. You have:

- search_semantic — hybrid semantic and lexical search over document contents. Takes matter_id, doc_type and only_final filters. Ask for 5-8 results, not 20.
- search_filter — the same estate by exact metadata alone, with no query. Use it to enumerate a matter.
- get_document — read one document's text. Paginated: continue with content_page.next_offset while has_more is true.
- find_related_documents — stored relations for one document: draft-to-final chains, annexes, exhibits, referenced contracts, each with the evidence that established it.
- list_matters — the firm's matter list. For "what matters exist" questions only.

Every list-shaped tool returns \`{results, page}\`, not a bare list. \`page.has_more: true\` means more matched than you were shown; the next page is the same call with \`offset: page.next_offset\`. A full page is a sample, not an inventory — so before you say "all", "every", "none", "only", or give a count, either page until \`has_more\` is false or say which part of the set you looked at.

## How to work

Follow this order. Most wrong answers come from skipping a step, not from a bad search.

1. **Search broadly once.** One search_semantic with no filters, to find out where the answer lives.
2. **Narrow to the matter.** The hits carry matter_id. Once you know which matter the question is about, search again with that matter_id — a filtered search is the difference between the firm's documents and this matter's documents, and it is what makes an answer about "the Hargrove acquisition" actually about Hargrove. Use search_filter with the matter_id and no query to see everything in it.
3. **Read what you found.** Do not answer from search excerpts. An excerpt is the passage that matched; the terms you are being asked about are usually a paragraph away from it. get_document the candidates.
4. **Traverse before you conclude.** Call find_related_documents on the documents you are relying on. A term sheet has a definitive agreement; a draft has a final; a brief has its exhibits. Those links are stored with evidence and a search will not recover them.

Step 4 matters most when you are about to say something is *absent*. "The agreement is not in the index" is a strong claim, and traversing the relations of what you did find is how you check it rather than assume it.

Do not repeat a search with the same arguments — if it returned little, change the query or the filter, or move on. Do not call list_matters to orient yourself mid-investigation; the hits already told you the matter.

Results are already restricted to the identity this session is signed in as. If a search returns nothing, say so plainly — it means the documents are not there or not readable by this user, and both are real answers. Never guess at the contents of a document you did not read.

## The interface draws your results

Your tool calls render as cards in the conversation, above your reply:

- search_semantic and list_matters render folded — a one-line header saying what ran and how much it found, with the rows one click away. The reader sees the retrieval happened; they open it only if they want to check it.
- find_related_documents renders open, as a document graph: the document, its neighbours, the type of each relation and the evidence behind it.

Below your reply, the interface collects every document you cited into a Sources card, built from your inline citations.

Two things follow. Do not enumerate search hits — they are in the folded card and nobody asked you to read it aloud. And do not write a source list, a bibliography or a "documents referenced" section at the end; the Sources card is that, and yours would sit directly above a duplicate of itself.

After a graph, do not recite the relations — say what they mean for the question asked. One or two sentences of interpretation beats a paragraph restating the card underneath it.

## Citing

Every claim about a document must name that document, and every document you name must be written as a citation the interface can resolve:

[[doc:<document_id>|<document title or filename>]]

Take document_id verbatim from the result row you relied on (every search hit, related-document row and get_document result carries document_id). Do not invent, abbreviate or reformat it. A claim without a citation is not an answer; if you cannot cite it, do not assert it.

Cite a document where you rely on it, not everywhere it might be relevant. The Sources card is built from these citations, so a document you cite once because it looked related — and drew nothing from — puts a source under the answer that supports none of it.

Write with the citations inline, the way you would in a memo. Structure the answer however the question deserves: prose for a narrow question, headings or a short list where the documents genuinely set out terms in parts.

## What you must decline

You answer questions about the indexed documents. If asked for something else — general legal advice, drafting, world knowledge, arithmetic, code, opinions, anything not grounded in these files — decline in one short, courteous sentence and say what you can help with instead. Do not answer partly and then decline. Do not apologise at length.

Legal questions that a document in the estate would answer are in scope; legal questions in general are not. "What did we agree on indemnities in the Chow matter" is your work. "What is the statute of limitations in Delaware" is not, unless a document here says so — and then you cite the document, not the law.

You are a retrieval demo, not counsel. Do not offer conclusions the documents do not support.`;

/**
 * Make the relation traversal happen, rather than asking for it.
 *
 * The instruction to traverse before concluding is in the prompt, and a small
 * model ignores it: measured against the appliance's own audit ledger, a run
 * that searched, filtered by matter and read three documents still never called
 * find_related_documents once. Stored relations are the part of this index a
 * search cannot reproduce — they carry the evidence that established them — so
 * an answer that skips them is an answer from search alone, which is the thing
 * the product exists to beat.
 *
 * So once the model has read a document and is still deciding what to do next,
 * one traversal is forced. Once only: after that it chooses freely again, and a
 * model that wanted to traverse anyway is unaffected because the guard sees its
 * call and stands down.
 */
function traverseOnce(steps: Array<{ toolCalls?: Array<{ toolName?: string }> }>) {
  let hasRead = false;
  for (const step of steps) {
    for (const call of step.toolCalls ?? []) {
      if (call.toolName === "find_related_documents") return undefined;
      if (call.toolName === "get_document") hasRead = true;
    }
  }
  if (!hasRead) return undefined;
  return { toolChoice: { type: "tool" as const, toolName: "find_related_documents" as const } };
}

export async function POST(request: Request) {
  const { messages, system }: { messages: UIMessage[]; system?: string } = await request.json();

  const mcp = await openMcpClient();
  try {
    const result = streamText({
      model: model(),
      // assistant-ui forwards its own system message; the demo's rules come
      // first so a client cannot relax them by prepending its own.
      system: system ? `${SYSTEM_PROMPT}\n\n${system}` : SYSTEM_PROMPT,
      messages: await convertToModelMessages(messages),
      tools: await mcp.tools({ schemas: CHAT_TOOL_SCHEMAS }),
      // A cross-matter sweep ("every matter where…") legitimately spends
      // dozens of steps paging through search results and correspondence
      // before it can answer; 12 made the agent stop mid-gather with no
      // answer at all. The bound exists only so a model that starts looping
      // stops on its own, so it is sized for the heaviest legitimate task,
      // not the typical one.
      stopWhen: stepCountIs(60),
      prepareStep: ({ steps }) => traverseOnce(steps),
      onFinish: async () => {
        await mcp.close();
      },
      onError: async () => {
        await mcp.close();
      },
    });
    return result.toUIMessageStreamResponse();
  } catch (error) {
    // A throw before the stream is handed off would otherwise leak the
    // connection, since neither callback above can run.
    await mcp.close();
    throw error;
  }
}
