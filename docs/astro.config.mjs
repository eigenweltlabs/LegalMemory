import { defineConfig } from "astro/config";
import starlight from "@astrojs/starlight";

// The public URL the site is deployed under. Deployments set DOCS_SITE_URL;
// the appliance itself learns the same value through KI_DOCS_URL so the admin
// UI can link here.
const site = process.env.DOCS_SITE_URL || "https://docs.legalmemory.example";

export default defineConfig({
  site,
  integrations: [
    starlight({
      title: "LegalMemory",
      description:
        "Open-source, on-prem legal knowledge index: a continuously synced shadow index over a firm's document estate, exposed through MCP.",
      social: [
        {
          icon: "github",
          label: "GitHub",
          href: "https://github.com/eigenweltlabs/knowledge-index",
        },
      ],
      sidebar: [
        {
          label: "Getting started",
          items: [
            { label: "What is LegalMemory?", slug: "getting-started/introduction" },
            { label: "Quick start", slug: "getting-started/quickstart" },
          ],
        },
        {
          // One entry per page in the product's own sidebar, same order.
          label: "Product guide",
          items: [
            { label: "Overview", slug: "product/overview" },
            { label: "Connectors", slug: "product/connectors" },
            { label: "Insertion pipeline", slug: "product/pipeline" },
            { label: "Ontology", slug: "product/ontology" },
            { label: "Data", slug: "product/data" },
            { label: "Access control", slug: "product/access-control" },
            { label: "Sign-in", slug: "product/sign-in" },
            { label: "Models & services", slug: "product/models-and-services" },
            { label: "Costs", slug: "product/costs" },
            { label: "External access", slug: "product/external-access" },
            { label: "Activity", slug: "product/activity" },
            { label: "Backup", slug: "product/backup" },
          ],
        },
        {
          label: "Connectors",
          items: [
            { label: "Connecting a source", slug: "connectors" },
            { label: "SharePoint Online", slug: "connectors/sharepoint-online" },
            { label: "OneDrive", slug: "connectors/onedrive" },
            { label: "Google Drive", slug: "connectors/google-drive" },
            { label: "Clio", slug: "connectors/clio" },
            { label: "Local folders", slug: "connectors/local-folders" },
            { label: "Live events: Microsoft 365", slug: "connectors/microsoft-live-events" },
            { label: "Live events: Google Drive", slug: "connectors/google-drive-live-events" },
          ],
        },
        {
          label: "Concepts",
          items: [
            { label: "Architecture", slug: "concepts/architecture" },
            { label: "The data model", slug: "concepts/data-model" },
            { label: "How retrieval works", slug: "concepts/retrieval" },
            { label: "Design evidence", slug: "concepts/evidence" },
          ],
        },
        {
          label: "Operations",
          items: [
            { label: "Deployment & identity", slug: "operations/deployment" },
            { label: "Backup & restore", slug: "operations/backups" },
          ],
        },
        {
          label: "Development",
          items: [
            { label: "Benchmarks", slug: "development/benchmarks" },
            { label: "Plugin connectors", slug: "development/plugin-connectors" },
          ],
        },
      ],
    }),
  ],
});
