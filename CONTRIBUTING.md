# Contributing

Thanks for improving Agent Pipline Compressor. Before opening a change, search existing [issues](https://github.com/DKotsyuba/agent-pipline-compressor/issues) and use the relevant template for bugs or ideas.

## Development setup

```bash
git clone https://github.com/DKotsyuba/agent-pipline-compressor.git
cd agent-pipline-compressor
python3 -m unittest discover -s tests -v
```

The project intentionally uses only the Python standard library. Keep changes small, add a regression test for behavior changes, and run:

```bash
python3 -m unittest discover -s tests -v
python3 benchmarks/compression_lab.py --no-rtk
git diff --check
```

## Issues and pull requests

Describe the problem, expected behavior, and a safe reproduction. Never attach raw tool output, recovery references, credentials, or other sensitive data. In a pull request, explain the user-visible effect and include tests, documentation, and changelog updates when applicable. The pull-request template is a compact reminder, not a substitute for review.

Conventional Commit-style messages (for example, `fix: preserve raw output on spool failure`) are recommended for readability. They do not by themselves promise automated version bumps.

## Versions and security

Maintainers own release versions, tags, and changelog entries. Do not change them speculatively in a feature or bug-fix PR; call out a needed release in the PR instead. Report potential vulnerabilities privately as described in [SECURITY.md](SECURITY.md), not in a public issue.
