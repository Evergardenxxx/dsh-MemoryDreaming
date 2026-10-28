# MemoryDreaming

A local long-term memory engine and native DeepSeek Harness plugin for companion characters. It preserves identity, relationships, shared history, and unfinished commitments across conversations.

[中文](README.md) · [Architecture](docs/architecture-overview.md) · [Quick start, Chinese](docs/quickstart.md) · [Roadmap](ROADMAP.md)

[![CI](https://github.com/Evergardenxxx/dsh-MemoryDreaming/actions/workflows/ci.yml/badge.svg)](https://github.com/Evergardenxxx/dsh-MemoryDreaming/actions/workflows/ci.yml)

[Source repository](https://github.com/Evergardenxxx/dsh-MemoryDreaming) · [Issue tracker](https://github.com/Evergardenxxx/dsh-MemoryDreaming/issues)

**Experimental, runnable release:** engine 0.30.0, native plugin 0.17.0, database schema 27. The engine, native integration, and first desktop management panel are implemented. Real DeepSeek extraction quality, long-term role stability, and the full current desktop/model workflow still need acceptance testing.

![MemoryDreaming architecture](docs/assets/architecture.svg)

## What it provides

- Controlled identity and relationship memory, plus working, episodic, semantic, and procedural memory.
- Source records, revision checks, scoped retrieval, corrections, and local forgetting propagation.
- Conversation-specific opt-in. Windows are inactive until an explicit user command enables them.
- Batched sleep review, fictional dreams, reversible recall adjustments, and wake recovery.
- Source-backed autobiography, commitments, companionship preferences, approved expression changes, and private migration archives.
- A native Harness sidebar panel for browsing memories and sources, reviewing proposals, confirming corrections/forgetting, first-role setup, and window/sleep status.
- Bounded memory injection through native Cordis hooks and a local Python JSONL bridge. The Harness plugin does not register MCP tools.

## Try the engine

From the repository root, with Python 3.10 or newer:

```bash
python -X utf8 examples/host_lifecycle.py
```

This uses a temporary database and synthetic messages, closes the first runtime, then restores the role and original evidence in another conversation. It makes no external model call.

## Install the native plugin

From the source tree or an extracted offline bundle:

```powershell
python scripts/install.py --destination "D:\Apps\MemoryDreaming" --user u1 --character c1 --relationship r1
```

Choose a new directory outside the repository. Add the generated `plugin` directory through the Harness native plugin manager, then reload the app. Open **记忆管理** in the sidebar and use **绑定与诊断** to review the fixed binding and initialize an empty role. A [command-line setup example](docs/quickstart.md) remains available.

The panel can browse the configured role without an active conversation. Its window selector chooses a conversation, not another role or database. Window buttons submit real user commands through Harness; an accepted command is queued, not necessarily completed. Closing the panel stops polling and does not undo submitted operations. See the [desktop guide, Chinese](docs/desktop-management.md).

Send standalone user messages in the target conversation:

```text
/memory on
/memory preference {"key":"comfort","value":"Listen first, then ask whether I want advice"}
/memory check
/memory sleep
```

After sleep completes, send `/memory wake` in a new conversation. `/memory off` disables participation in the current conversation. Additional JSON commands and approval rules are documented in the [role guide](docs/long-term-role.md).

## Current limits

The default rule extractor has limited coverage. The optional DeepSeek API extractor is implemented, but real extraction quality has not been accepted. The panel can approve/reject existing core, change, autobiography, growth, and companionship proposals, and confirm structured corrections or forgetting with version checks. The role tab is read-only; complete role editing forms and graphical archive import/export remain to be built.

Automatic scene selection, reliable conversational candidate formation with convenient confirmation, automatic growth suggestions, and a timed notification transport remain incomplete. Proactivity currently provides a disabled-by-default authorization and delivery queue only. Roles remain fixed by plugin configuration; the panel cannot switch bindings.

Request counting is estimated by default. Current V4.1 online usage and multimodal counting remain unverified. Local forgetting cannot retract text already delivered to a model or erase external backups.

Version 0.30 passed 773 local engineering tests, 27 isolated paths using installed Harness runtime modules, and 12 browser interaction paths. The browser uses the real Python bridge and temporary SQLite storage, with deterministic host admission and model adapters. These tests do not validate the user's current Electron window, live model, or long-term companion behavior. See [desktop boundaries](docs/desktop-management.md) and [acceptance scope](docs/installed-harness-acceptance.md).

## Data and dependencies

The base Python engine uses the standard library and SQLite. Optional semantic models and tokenizer dependencies are separate. Offline bundles do not include Python, Node, language models, or semantic weights.

Storage is local. Sleep review sends saved conversation fragments to the current Harness model; optional DeepSeek extraction sends user events to the configured API. Local storage alone does not imply offline processing.

Private continuity archives contain conversations and a private HMAC recovery key. Keep them out of public issues and repositories. Configuration is listed in [.env.example](.env.example); the engine does not load that file automatically.

## Contribute

```bash
python -m pip install --editable .
python -m unittest discover -s tests -q
```

Native development/tests also require Node.js; CI uses Node 22. Read [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md). Licensed under [MIT](LICENSE). The detailed documentation is currently primarily in Chinese.
