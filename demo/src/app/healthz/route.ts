/**
 * Liveness, for the container runtime and the edge.
 *
 * Deliberately not `/`. Behind a login `/` answers 307 to a Clerk-hosted
 * sign-in page, so a probe reading it either follows a redirect off the box or
 * reports the container unhealthy the moment authentication is switched on —
 * and a healthcheck that fails when the deployment is correct is worse than no
 * healthcheck at all.
 *
 * It answers for the process, not the index: the appliance has its own
 * `/healthz`, and conflating them means a database hiccup takes down the page
 * that would have explained it.
 */
export const dynamic = "force-dynamic";

export function GET() {
  return new Response("ok\n", {
    status: 200,
    headers: { "content-type": "text/plain", "cache-control": "no-store" },
  });
}
