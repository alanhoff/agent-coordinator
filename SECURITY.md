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

Schema-v8 evidence proof commands are authorized repository shell commands. They run at the repository
root with the controller's inherited environment, so they can read accessible environment variables and
create ordinary build, test, or cache artifacts. Do not place secrets in command strings or derive proof
commands from untrusted content without review, and do not print secrets because positive combined
output becomes durable node state. Commands must be repeatable and idempotent: successful
node completion and workflow closeout can execute them again, and shell side effects cannot be rolled
back when a mutation is rejected. The state owner limits each command to five minutes and 32 KiB of
combined output, validates UTF-8, and never reruns commands for a persisted mutation receipt.
