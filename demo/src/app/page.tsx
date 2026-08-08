import { UserButton } from "@clerk/nextjs";

import { principals } from "@/lib/appliance";
import { authEnabled } from "@/lib/auth";
import { McpModal } from "@/components/mcp-modal";
import { Workspace } from "@/components/workspace";

export const dynamic = "force-dynamic";

/**
 * Everything this demo is not.
 *
 * The demo answers one question well and then a visitor wants the rest: how it
 * works, what it costs them to run, and whether the source is real. Hard-coded
 * rather than configurable — these are the product's own addresses, and a demo
 * running somewhere else still points at them.
 */
const NAV_LINKS = [
  { label: "Docs", href: "https://legalmemory.eigenweltlabs.com/docs" },
  { label: "GitHub", href: "https://github.com/eigenweltlabs/LegalMemory" },
  { label: "Website", href: "https://eigenweltlabs.com/legalmemory" },
] as const;

/**
 * The whole application.
 *
 * Minimal chrome on purpose: one black bar carrying the wordmark and the
 * identity everything below it is resolved against, then the working surface.
 * There is no marketing copy here — the product page does that, and a demo that
 * repeats it is a demo somebody has to scroll past to reach the thing they came
 * to see.
 */
export default function Page() {
  const identity = principals();

  return (
    <div className="flex h-full flex-col">
      {/* Black owns the chrome, exactly as it owns the hero on the product
          page, and nothing below this bar is allowed to be black again.

          On a phone the bar keeps the wordmark, the way into MCP and the
          identity, and gives up everything that is a signpost rather than a
          fact: the eyebrow, the divider, the three links out. Those are for
          somebody who has finished with the demo, and on four hundred pixels
          they would be in front of somebody who has not started. */}
      <header className="flex h-14 flex-none items-center gap-3 bg-[var(--lm-black)] px-4 text-[var(--lm-ink)] sm:h-16 sm:gap-5 sm:px-6">
        <span
          className="text-[17px] font-black text-[var(--lm-orange)] sm:text-[20px]"
          style={{
            // The wordmark is the product's face and it is set in the widest
            // optical width available. Geist has no expanded cut, so the system
            // stack carries it here exactly as it does on the product page.
            fontFamily:
              '".SF NS", -apple-system, BlinkMacSystemFont, "SF Pro Display", "Helvetica Neue", sans-serif',
            fontStretch: "200%",
            fontVariationSettings: '"wdth" 200, "wght" 900, "opsz" 144',
            letterSpacing: "-0.035em",
          }}
        >
          LegalMemory
        </span>

        <span className="hidden h-4 w-px bg-white/15 sm:block" aria-hidden />

        <span className="hidden font-mono text-[10.5px] tracking-[0.11em] text-white/40 uppercase sm:inline">
          Demo
        </span>

        {/* Where to go after the demo has done its job. Quiet, in the chrome's
            own mono, and never competing with the wordmark — somebody who wants
            the docs or the source will look for them, and somebody who does not
            should not have three links in their way. */}
        <nav className="ml-6 hidden items-center gap-5 md:flex">
          {NAV_LINKS.map((link) => (
            <a
              key={link.href}
              href={link.href}
              target="_blank"
              rel="noopener noreferrer"
              className="font-mono text-[10.5px] tracking-[0.11em] text-white/40 uppercase transition-colors duration-[var(--lm-dur-fast)] hover:text-[var(--lm-orange-hi)]"
            >
              {link.label}
            </a>
          ))}
        </nav>

        {/* The identity is in the chrome because it is the variable everything
            on screen depends on: this is one lawyer's estate, not the firm's. */}
        <div className="ml-auto flex min-w-0 items-center gap-2 sm:gap-2.5">
          {/* The demo republishes the appliance's MCP server, and a lawyer
              connecting their own tool to it is a better demonstration than
              this page is — so the way in sits in the chrome, not in a doc. */}
          <McpModal authEnabled={authEnabled} />

          <span className="ml-1 hidden font-mono text-[10.5px] tracking-[0.11em] text-white/40 uppercase lg:inline">
            Signed in as
          </span>
          {/* Truncated rather than wrapped or dropped: a firm's principal list
              is longer than a phone is wide, and which identity this is stays
              the one fact the bar cannot do without. */}
          <span
            title={identity}
            className="max-w-[42vw] truncate rounded-full border border-white/15 bg-white/5 px-2.5 py-[5px] font-mono text-[11.5px] text-white/75 sm:max-w-none sm:px-3"
          >
            {identity}
          </span>
          {/* Two different identities, deliberately not conflated. The pill is
              the appliance principal every document is resolved against; this
              is the human who got past the door. */}
          {authEnabled && (
            <div className="ml-1.5 flex items-center">
              <UserButton
                appearance={{ elements: { userButtonAvatarBox: "size-7" } }}
              />
            </div>
          )}
        </div>
      </header>

      <main className="min-h-0 flex-1">
        <Workspace />
      </main>
    </div>
  );
}
