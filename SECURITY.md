# Security

Memory files, SQLite databases, delivery queues, snapshots and local configuration
can contain private conversations. Default data directories and common private
file patterns are ignored by Git; custom filenames still need review. They
must not be attached to public issues. Reproduce bugs with a temporary database
and synthetic messages. Never include API keys or a copied Harness profile.

For a suspected vulnerability, use the repository's private vulnerability
reporting feature when enabled. If it is unavailable, report only a minimal
description without exploit details or private data in an issue and ask the
maintainer for a private channel. Do not publish credentials or user data.

Private continuity archives also include a private HMAC recovery key. They are
plaintext backups, not public manifests or encrypted exports. Protect them like
the database, and review Git history as well as the working tree before publishing.

Storage is local, but sleep review uses the configured Harness model and optional
DeepSeek extraction sends user events to the configured API. Do not assume local
storage implies offline processing. Forgetting cannot retract text already sent
to a model or erase independent external backups.

MemoryDreaming is a local trusted-host plugin. Model-supplied text and historical
memory do not grant approval rights. Optional model weights must be installed
locally; remote model code is disabled. See docs/open-problems.md for capability
limits and outstanding product acceptance work.
