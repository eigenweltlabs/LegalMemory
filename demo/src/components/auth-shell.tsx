/**
 * The door.
 *
 * Clerk's hosted Account Portal is a different origin, which means the product's
 * type, colour and wordmark stop at the redirect: someone following a link to
 * the demo meets a generic form on `accounts.…` and has to take on faith that it
 * belongs to what they clicked. Rendering sign-in inside the application keeps
 * the URL, the brand and the trust in one place.
 *
 * The composition is the product page's hero, reduced: black field, the wordmark
 * in the widest optical cut available, and nothing else competing with the one
 * thing there is to do here.
 */
import type { ReactNode } from "react";

/**
 * The wordmark, set exactly as the header and the product page set it.
 *
 * Geist ships no expanded cut, so the system stack carries the width axis. Kept
 * beside the header's copy rather than shared with it: this one is display-sized
 * and the header's is chrome, and a single component parameterised by size is
 * how both end up slightly wrong.
 */
function Wordmark() {
  return (
    <span
      className="text-[34px] leading-none font-black text-[var(--lm-orange)] sm:text-[42px]"
      style={{
        fontFamily:
          '".SF NS", -apple-system, BlinkMacSystemFont, "SF Pro Display", "Helvetica Neue", sans-serif',
        fontStretch: "200%",
        fontVariationSettings: '"wdth" 200, "wght" 900, "opsz" 144',
        letterSpacing: "-0.035em",
      }}
    >
      LegalMemory
    </span>
  );
}

export function AuthShell({
  title,
  children,
}: {
  title: string;
  children: ReactNode;
}) {
  return (
    // Two boxes rather than one, because the page behind this scrolls nowhere:
    // the outer one is the viewport and owns the scrollbar, the inner one is at
    // least a viewport tall so a short form sits centred. Centring the scroll
    // container itself would put the top of a tall form — the wordmark, and on
    // a phone in landscape the first field — above the top of the page, where
    // there is no way to scroll back to it.
    <div className="lm-scroll h-full overflow-y-auto bg-[var(--lm-black)]">
      <div className="flex min-h-full flex-col items-center justify-center px-6 py-10 sm:py-16">
        <div className="flex w-full max-w-[420px] flex-col items-center">
          <Wordmark />

          {/* This is not the product; it is an instance of it holding a
              synthetic corpus. Saying so under the wordmark costs one line and
              prevents the reading where someone signs in expecting their own
              firm's matters. */}
          <span className="mt-3 rounded-full border border-white/15 bg-white/5 px-3 py-[5px] font-mono text-[10.5px] tracking-[0.11em] text-white/50 uppercase">
            Demo
          </span>

          <p className="mt-5 text-center text-[13.5px] leading-relaxed text-white/45 sm:mt-6">
            {title}
          </p>

          <div className="mt-6 w-full sm:mt-8">{children}</div>

          <p className="mt-8 text-center font-mono text-[10.5px] tracking-[0.08em] text-white/25 uppercase sm:mt-10">
            Eigenwelt Labs
          </p>
        </div>
      </div>
    </div>
  );
}

/**
 * Clerk's card, wearing the product's surface instead of its own.
 *
 * Only the container is restyled — the fields, validation and error states are
 * Clerk's, because those are the parts that have to keep working when Clerk
 * changes them. Shared by both routes so sign-in and sign-up cannot drift.
 */
export const clerkCardAppearance = {
  elements: {
    rootBox: "w-full",
    cardBox: "w-full shadow-[0_18px_50px_-12px_rgba(0,0,0,0.7)]",
    card: "bg-[var(--lm-paper)] border border-white/10 rounded-[14px]",
    headerTitle: "text-[var(--lm-ink-900)] tracking-[-0.02em]",
    headerSubtitle: "text-[var(--lm-muted-2)]",
    socialButtonsBlockButton:
      "border-[var(--lm-line)] hover:bg-[var(--lm-paper-2)] transition-colors",
    formButtonPrimary:
      "bg-[var(--lm-orange)] hover:bg-[var(--lm-orange-hi)] text-white normal-case tracking-[-0.01em] shadow-none",
    footerActionLink: "text-[var(--lm-orange)] hover:text-[var(--lm-orange-hi)]",
    // Clerk's own badge, on a page that already says who made this.
    footer: "hidden",
  },
} as const;
