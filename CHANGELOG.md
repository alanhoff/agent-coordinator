# Changelog

All notable changes follow Semantic Versioning.

## 3.0.0

- Replaced project-local deployment with a current-user global installation and removed the v2 compatibility surface.
- Added recoverable, integrity-verified install/update/check/doctor commands and anonymous GitHub Release distribution.
- Added strict schema-v3 workflow state with atomic commits, revision and mutation fencing, controller takeover/resume, launch reconciliation, and eight dynamically routed roles.
- Added a read-only loopback dashboard with watch, interactive bounded replay, and self-contained report modes.
- Added deterministic package assembly and an independent closed-inventory release verifier.
- Raised the runtime requirement to Python 3.11 while retaining a standard-library-only runtime on Linux, macOS, and Windows.
