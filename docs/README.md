# LegalMemory documentation

The product documentation site, built with [Astro Starlight](https://starlight.astro.build/).
Content lives in `src/content/docs/`; the sidebar is defined in `astro.config.mjs`.

```bash
npm install
npm run dev      # http://localhost:4321
npm run build    # static site in dist/
```

Deploy `dist/` to any static host. Set `DOCS_SITE_URL` at build time to the
public URL of the deployment, and set the same URL as `KI_DOCS_URL` in the
appliance's `.env` so the admin UI links here (sidebar "Documentation" entry
and the per-connector setup panels).

The per-connector pages are linked from inside the product as
`<KI_DOCS_URL>/connectors/<connector-short-name-with-hyphens>/` — keep those
slugs stable.
