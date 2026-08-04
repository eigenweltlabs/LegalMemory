// Sub-path deployments need the base prefix on links the docs author by hand.
//
// Astro rewrites the links it generates itself, so the sidebar, the logo and
// every asset already point at /docs/..., but a `/product/pipeline/` typed into
// a Markdown file is emitted verbatim and lands on the origin root, where the
// demo answers instead of the docs. Prefixing here keeps the pages themselves
// base-agnostic: content stays written against the site root and builds
// correctly whether or not a base is configured.

/** Prefix a site-root URL with the base path, leaving everything else alone. */
export function withBase(url, base) {
  const prefix = (base ?? "").replace(/\/+$/, "");
  if (!prefix) return url;
  // Anchors, external and protocol-relative URLs, and anything not anchored to
  // the site root already resolve on their own.
  if (!url.startsWith("/") || url.startsWith("//")) return url;
  // Already prefixed. Nothing under the site root is named after the base, and
  // the dev server hands the same objects back on every reload, so this keeps a
  // second pass from stacking /docs/docs.
  if (url === prefix || url.startsWith(prefix + "/")) return url;
  return prefix + url;
}

/** Walk every node in an mdast tree, children first or last, order irrelevant. */
function walk(node, visitor) {
  visitor(node);
  for (const child of node.children ?? []) walk(child, visitor);
}

/**
 * Rewrite site-root links and images in Markdown and MDX bodies.
 *
 * Runs once per file, so URLs cannot pick up the prefix twice.
 */
export function remarkBaseLinks({ base }) {
  if (!base) return () => {};

  return (tree) => {
    walk(tree, (node) => {
      if (node.type === "link" || node.type === "definition" || node.type === "image") {
        if (typeof node.url === "string") node.url = withBase(node.url, base);
      }
    });
  };
}
