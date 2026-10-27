# Security

Memory files, SQLite databases, delivery queues, snapshots and local configuration
can contain private conversations. They are excluded from version control and
must not be attached to public issues. Reproduce bugs with a temporary database
and synthetic messages. Never include API keys or a copied Harness profile.

For a suspected vulnerability, use the repository's private vulnerability
reporting feature when enabled. If it is unavailable, report only a minimal
description without exploit details or private data in an issue and ask the
maintainer for a private channel. Do not publish credentials or user data.

MemoryDreaming is a local trusted-host plugin. Model-supplied text and historical
memory do not grant approval rights. Optional model weights must be installed
locally; remote model code is disabled. See docs/open-problems.md for capability
limits and outstanding product acceptance work.
