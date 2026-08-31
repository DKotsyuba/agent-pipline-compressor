# Security policy

## Supported versions

This is a pre-1.0 project. Security fixes are considered for the latest release on the `0.1.x` line and the current `main` branch. Older pre-release versions may require upgrading before a fix can be evaluated.

## Reporting a vulnerability

Please report suspected vulnerabilities privately through [GitHub Security Advisories](https://github.com/DKotsyuba/agent-pipline-compressor/security/advisories/new). Do not open a public issue and do not include raw tool output, recovery references, credentials, or private paths in the report.

Include affected version, host and Python version, a minimal non-sensitive reproduction, impact, and any suggested mitigation. Maintainers will confirm receipt through the advisory, investigate, and coordinate disclosure there.

## Security boundaries

The project is local-only and executes wrapped commands as validated argv without a shell. Its safety depends on preserving strict command allowlists, trusted executable checks, private raw-output storage, mandatory recovery before replacement, and fail-open behavior. Changes touching these boundaries need focused tests and careful review.
