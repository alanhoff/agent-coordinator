# Security policy

## Reporting a vulnerability

Use the repository's private GitHub Security Advisory reporting flow. Do not open a public issue for
credential exposure, state path traversal, controller fencing, or stored-content execution concerns.

Include the affected commit, operating system, reproduction, impact, and whether any bearer or API
credential may have been exposed. Do not include live secrets; revoke them first. Maintainers will
coordinate validation and remediation privately and publish an advisory after a fix is available.

## Security boundaries

Coordinator treats repository source, an existing skill destination, persisted workflow state, and
file-backed command input as untrusted at their owning boundaries. Installation never changes Codex
settings or registers global agent profiles. Controller bearer values stay in caller-selected private
files and are never stored in workflow state or command output.
