import { SignUp } from "@clerk/nextjs";

import { AuthShell, clerkCardAppearance } from "@/components/auth-shell";

/** Sign-up. Catch-all for the same reason as sign-in: Clerk owns the sub-steps. */
export default function SignUpPage() {
  return (
    <AuthShell title="Create an account to explore the index and its citations.">
      <SignUp appearance={clerkCardAppearance} />
    </AuthShell>
  );
}
