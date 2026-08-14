/**
 * Everything this demo is not.
 *
 * The demo answers one question well and then a visitor wants the rest: how it
 * works, what it costs them to run, and whether the source is real. Hard-coded
 * rather than configurable — these are the product's own addresses, and a demo
 * running somewhere else still points at them.
 *
 * Shared between the workspace header and the sign-in door, because most
 * visitors never get past the door: a link that only exists after login is a
 * link most of them never see.
 */
export const NAV_LINKS = [
  { label: "Docs", href: "https://legalmemory.eigenweltlabs.com/docs" },
  { label: "GitHub", href: "https://github.com/eigenweltlabs/LegalMemory" },
  { label: "Website", href: "https://eigenweltlabs.com/legalmemory" },
] as const;
