# Security Policy

## Reporting a vulnerability

`learn-agent-harness` is an educational project, but we still take security
seriously. If you believe you have found a security issue, **please do not open
a public issue** — report it privately so it can be addressed before it is
disclosed.

### How to report

Email the maintainer at [sfyhdu@gmail.com](mailto:sfyhdu@gmail.com) with:

- A clear description of the issue
- Steps to reproduce it
- The affected file(s) / chapter(s)
- Any suggested remediation (optional)

Please allow a reasonable amount of time for a response. We will acknowledge
receipt and keep you informed of progress. We will not publicly disclose the
issue until a fix is available.

## Scope

Security reports are most relevant to:

- `harness_llm.py` — the shared model-access layer (handles API keys, HTTP
  requests, environment variables)
- Any chapter code that interacts with the filesystem or shell (e.g. the tool
  execution and permission pipeline in `s04_permission`, `s15_capability_seams`,
  `s18_full_harness`)

The chapter code is **deliberately simplified for teaching** and is not intended
to be used as-is in production. In particular, the local filesystem and shell
tools are not sandboxed (see [DESIGN.md §6](DESIGN.md) — "no real sandbox").

## Supported versions

Only the latest commit on the `main` branch is supported. There are no
versioned releases with security backports.

## Disclosure expectations

We follow a coordinated-disclosure process. Once a fix is merged, we will
credit the reporter in the fix unless they prefer to remain anonymous.
