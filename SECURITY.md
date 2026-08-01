# Security policy

LegalMemory's job is to enforce a law firm's confidentiality boundaries, so we
treat permission-model and identity issues as the highest-severity class of
bug.

## Reporting a vulnerability

Please **do not** open a public issue for security reports. Use GitHub's
[private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
on this repository ("Report a vulnerability" under the Security tab).

Include what you can: affected component, reproduction steps, and impact —
especially whether the issue can cross a permission boundary (a caller seeing
a document their principals do not allow).

We will acknowledge reports promptly, keep you informed of progress, and
credit reporters in release notes unless you prefer otherwise.

## Scope notes for deployments

- The compose file's default passwords and keys are **development-only**.
  Production deployments must set real secrets for every variable listed in
  the production checklist of the documentation.
- Port 8000 (the direct app port) accepts a development identity header in
  `trusted_header` mode and must never be exposed beyond the local machine;
  production entry is through the identity proxy.
- `KI_MCP_DEV_TRUSTED_HEADER` must remain off on any real deployment.
