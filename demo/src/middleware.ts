import { clerkMiddleware } from "@clerk/nextjs/server";
import { NextResponse, type NextRequest } from "next/server";

import { authEnabled } from "@/lib/auth";

/**
 * The login gate, when there is one.
 *
 * `clerkMiddleware` throws at startup without keys, so the export is chosen
 * rather than the behaviour branched inside it: with no credentials this is a
 * pass-through and Clerk is never constructed, which is what lets the same
 * image run open on a laptop and closed on a public URL.
 *
 * Everything is protected, not just the pages. The expensive and disclosing
 * parts of this demo are `/api/chat`, which spends tokens, and `/api/preview`,
 * which streams a firm's documents. A gate over the UI alone leaves both of
 * them open to anyone who reads the network tab.
 */
/**
 * One line per request, so a client that says it cannot connect can be checked
 * against what actually arrived. Without this a failed integration is two
 * parties each certain the other is at fault: `next start` does not log
 * requests, so "we got nothing" and "we sent it" are equally unfalsifiable.
 *
 * Logs the method, path, and whether an Authorization header was present —
 * never the header itself.
 */
function trace(request: NextRequest) {
  const { pathname, search } = request.nextUrl;
  if (pathname.startsWith("/_next/")) return;
  const auth = request.headers.get("authorization");
  console.log(
    JSON.stringify({
      at: new Date().toISOString(),
      method: request.method,
      path: pathname + search,
      ua: request.headers.get("user-agent")?.slice(0, 80) ?? null,
      auth: auth ? `${auth.split(" ")[0]} (${auth.length} chars)` : null,
      origin: request.headers.get("origin"),
    }),
  );
}

/**
 * Paths that authenticate themselves and must not be gated by the session.
 *
 * `/mcp` takes an OAuth bearer and answers its own 401 carrying the
 * `resource_metadata` pointer a client needs to start sign-in; a middleware
 * redirect to a sign-in page would hand an MCP client an HTML document it
 * cannot read. The `.well-known` documents are read *before* any token exists
 * — gating them makes discovery impossible and OAuth can never begin.
 */
const selfAuthenticating = (pathname: string) =>
  // Liveness. Docker and the edge probe this before anyone has signed in, and a
  // 307 to a sign-in page reads as "unhealthy" to both.
  pathname === "/healthz" ||
  // The door itself. Gating it would redirect the sign-in page to the sign-in
  // page, which browsers report as a redirect loop rather than as a login.
  pathname.startsWith("/sign-in") ||
  pathname.startsWith("/sign-up") ||
  pathname === "/mcp" ||
  pathname.startsWith("/mcp/") ||
  pathname.startsWith("/.well-known/") ||
  // RFC 7591 registration is unauthenticated by design: a client posts here
  // precisely because it has no credentials yet. Behind the session gate it
  // would answer a sign-in redirect to a client that cannot read one, and
  // discovery would dead-end at the last step.
  pathname.startsWith("/oauth/");

export default authEnabled
  ? clerkMiddleware(async (auth, request) => {
      trace(request);
      if (selfAuthenticating(request.nextUrl.pathname)) return;
      const { userId } = await auth();
      if (userId) return;

      // A person gets a door; a script gets nothing. `auth.protect()` answers
      // 404 to both, which is right for the API and wrong for someone who was
      // sent the link and now believes the demo is down.
      if (request.nextUrl.pathname.startsWith("/api/")) {
        return new NextResponse(null, { status: 404 });
      }

      // Our own page, named explicitly rather than via `redirectToSignIn()`.
      // That helper resolves the destination from Clerk's own configuration,
      // which on a production instance is the hosted portal on
      // `accounts.<domain>` — a different origin, where none of this
      // application's branding applies. The `signInUrl` prop on ClerkProvider
      // does not change it: that is read in the browser, and this runs before
      // any of it is sent.
      // The return address is relative on purpose. Behind the edge this process
      // only knows the address it binds — `request.url` is `0.0.0.0:3000` — so
      // an absolute round trip sends someone who just signed in to a host that
      // exists nowhere. A path is the same destination without the guess.
      const signIn = request.nextUrl.clone();
      signIn.pathname = "/sign-in";
      signIn.search = "";
      const from = request.nextUrl.pathname + request.nextUrl.search;
      if (from !== "/") signIn.searchParams.set("redirect_url", from);
      return NextResponse.redirect(signIn);
    })
  : (request: NextRequest) => {
      trace(request);
      return NextResponse.next();
    };

export const config = {
  matcher: [
    // Everything except Next's own build output and static files — the usual
    // Clerk matcher, with the API explicitly included.
    "/((?!_next|[^?]*\\.(?:html?|css|js(?!on)|jpe?g|webp|png|gif|svg|ttf|woff2?|ico|csv|docx?|xlsx?|zip|webmanifest)).*)",
    "/(api|trpc)(.*)",
  ],
};
