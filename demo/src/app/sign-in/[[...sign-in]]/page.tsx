import { SignIn } from "@clerk/nextjs";

import { AuthShell, clerkCardAppearance } from "@/components/auth-shell";

/**
 * Sign-in, in the application rather than on Clerk's hosted portal.
 *
 * A catch-all segment because Clerk routes its own sub-steps — factor two,
 * password reset, SSO callback — as path segments under this one. A plain
 * `page.tsx` serves the first screen and 404s on every step after it.
 */
export default function SignInPage() {
  return (
    <AuthShell title="Sign in to ask questions about the indexed corpus.">
      <SignIn appearance={clerkCardAppearance} />
    </AuthShell>
  );
}
