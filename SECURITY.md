# Security policy

## Supported versions

The latest 3.x release receives security fixes. Version 2 and earlier are unsupported because version 3 replaces project-local installation and state behavior.

## Reporting a vulnerability

Use the repository's private GitHub Security Advisory reporting flow. Do not open a public issue for credential exposure, archive/path traversal, install ownership, controller fencing, dashboard authorization, or stored-content execution concerns.

Include the affected version, operating system, reproduction, impact, and whether any bearer or API credential may have been exposed. Do not include live secrets; revoke them first. Maintainers will acknowledge a report, coordinate validation and remediation privately, and publish an advisory when affected users have a safe update path.

## Security boundaries

Coordinator treats release bytes, existing user paths/configuration, persisted workflow state, HTTP requests, and stored display text as untrusted at their owning boundaries. It does not request GitHub credentials for end-user installation, does not send cookies or repository secrets with package downloads, and never stores controller bearer values in workflow state or dashboard output.
