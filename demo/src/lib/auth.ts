/**
 * Whether this deployment is behind a login.
 *
 * Both keys or neither. Clerk needs a publishable key in the browser and a
 * secret key on the server, and a deployment holding one of them is not
 * half-protected — it is broken in a way that surfaces as a runtime error on
 * the first request rather than as a missing door.
 *
 * Absent, the demo runs open, which is what you want on a laptop. Present, every
 * route including the API and the document previews requires a signed-in user —
 * which is the point: a public demo URL that answers questions about a firm's
 * documents and streams model tokens is a bill and a data-exposure waiting for
 * whichever crawler finds it first.
 */
export const authEnabled = Boolean(
  process.env.CLERK_SECRET_KEY && process.env.NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY,
);

/**
 * Whether the MCP endpoint requires a token.
 *
 * Off by default, and deliberately separate from `authEnabled`. The two gates
 * protect different things: the pages run the chat, which spends model tokens
 * on every question, and that is what a public URL needs shielding from. The
 * MCP endpoint calls no model — it searches, reads documents and traverses
 * relations over a demo corpus — so requiring OAuth there costs every client an
 * authorization dance and buys nothing.
 *
 * It also unblocks the clients. MCP's zero-config story depends on dynamic
 * client registration, which not every authorization server offers; an open
 * endpoint connects everywhere, immediately, which is what a demo is for.
 *
 * Set DEMO_MCP_REQUIRE_AUTH=true on a deployment whose corpus is not synthetic.
 */
export const mcpAuthRequired =
  authEnabled && process.env.DEMO_MCP_REQUIRE_AUTH === "true";
