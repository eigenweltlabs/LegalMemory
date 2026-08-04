import { defineRouteMiddleware } from "@astrojs/starlight/route-data";
import { withBase } from "./plugins/base-links.mjs";

// Hero action links need the same base prefix the Markdown bodies get, but they
// cannot come from the remark plugin: the collection loader parses frontmatter
// long before anything renders, and Starlight reads the hero straight off the
// parsed entry. Route data is the one place both are already resolved.
export const onRequest = defineRouteMiddleware((context) => {
  const actions = context.locals.starlightRoute.entry.data.hero?.actions;
  for (const action of actions ?? []) {
    action.link = withBase(action.link, import.meta.env.BASE_URL);
  }
});
