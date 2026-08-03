import type { Metadata } from "next";
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

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  const page = (
    <html
      lang="en"
      className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}
    >
      <body className="h-full">
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
