import type { Metadata, Viewport } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import { ClerkProvider } from "@clerk/nextjs";

import { TooltipProvider } from "@/components/ui/tooltip";
import { authEnabled } from "@/lib/auth";

import "./globals.css";

// The two faces the product site self-hosts. Loading them here, rather than
// from a stylesheet, keeps the demo's font requests inside its own origin —
// which matters for something a firm is meant to be able to run air-gapped.
const geistSans = Geist({ variable: "--font-geist-sans", subsets: ["latin"] });
const geistMono = Geist_Mono({ variable: "--font-geist-mono", subsets: ["latin"] });

export const metadata: Metadata = {
  title: "LegalMemory",
  description: "Ask questions about the firm's indexed documents.",
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  // The app is exactly one viewport tall with nothing scrolling behind it, so
  // the default keyboard behaviour — resize the visual viewport, leave the
  // layout alone — hides the composer under the keyboard with no way to scroll
  // it back. `resizes-content` shortens the app instead, which is what puts the
  // input directly above the keys. No maximum-scale: zoom is somebody's only
  // way to read a scanned exhibit.
  interactiveWidget: "resizes-content",
  // The chrome above the page is the header's black, not the paper below it.
  themeColor: "#000000",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  const page = (
    <html
      lang="en"
      className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}
    >
      {/* No height utility here: the body is `100dvh` in globals.css, and a
          utility class would win over it and hand back the `100%` that a mobile
          browser measures against a viewport it is not currently showing. */}
      <body>
        <TooltipProvider delayDuration={300}>{children}</TooltipProvider>
      </body>
    </html>
  );

  // Mounted only when credentials exist. ClerkProvider without a publishable
  // key renders a hard error page, so an open deployment must not have one in
  // the tree at all.
  return authEnabled ? (
    <ClerkProvider
      // Sign-in happens here, not on Clerk's hosted portal on another origin.
      // Without these, redirectToSignIn() sends people to accounts.<domain>,
      // where none of the branding below applies.
      signInUrl="/sign-in"
      signUpUrl="/sign-up"
      signInFallbackRedirectUrl="/"
      signUpFallbackRedirectUrl="/"
      appearance={{
        // The sign-in box is the first thing anyone sees on a shared URL, so it
        // is the product's orange rather than Clerk's default indigo.
        variables: {
          colorPrimary: "#e95700",
          borderRadius: "0.75rem",
          fontFamily: "var(--font-geist-sans), ui-sans-serif, system-ui, sans-serif",
        },
      }}
    >
      {page}
    </ClerkProvider>
  ) : (
    page
  );
}
