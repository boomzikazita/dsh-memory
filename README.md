# dsh-memory

Persistent memory system for [DeepSeek Harness](https://github.com/deepseek-ai) (dsh). Provides multi-channel recall, confirmed writes, an encrypted secrets vault, session transcription, and maintenance tooling as a dsh plugin.

> 记忆系统插件：RRF 多路召回 / 确认制写入 / Fernet 加密库 / 会话转录 / 维护工具。

## Features

- **RRF 五路召回 (five-way RRF recall)** — fuse evidence from L1 rules (MEMORY.md / USER.md), L2 session FTS5, L3 semantic (optional Honcho/MCP), L4 wiki, and L5 time-window sources into a single ranked evidence ledger with per-item trust labels.
- **确认制写入 (confirm-before-write)** — nothing mutates your memory directly: `memory_suggest` enqueues a proposal, `memory_confirm` / `memory_reject` decide it. Budget checks, sensitive-pattern scanning, and automatic `trigger:` tags guard what lands in L1.
- **Fernet 加密库 (encrypted vault)** — secrets live in a Fernet-authenticated encrypted store (`VAULT.md.enc`); plaintext appears only as `→ vault:<ref>` references.
- **会话转录 (session transcription)** — incrementally indexes dsh session logs into an FTS5 store and extracts correction signals for skill distillation.
- **维护工具 (maintenance)** — health checks, git backups, hit auditing, and an asset de-duplication checker.

## Tools

| Tool | Description |
| --- | --- |
| `memory_recall` | Five-way RRF recall with source labels and trust levels; `date=` for time-window queries, `deep=true` for deeper recall |
| `memory_graph` | Entity graph walk (curated relation edges + automatic co-occurrence edges) |
| `memory_suggest` | Enqueue a memory write proposal (track: memory / user / skill; lifecycle: rule / stable / situational / record) |
| `memory_confirm` | Accept a pending proposal into MEMORY.md / USER.md / skill |
| `memory_reject` | Reject a pending proposal |
| `memory_vault_get` | Read an encrypted vault entry |
| `memory_vault_set` | Write an encrypted vault entry |
| `memory_harvest` | Transcribe dsh session logs into the L2 FTS5 index and extract correction signals |
| `memory_health` | One-line-per-layer health report (L1 budgets, vault decryptability, L2 index, graph freshness, pending queue) |
| `memory_backup` | Git commit + push of the memory directory |
| `memory_notify` | Push a message to a Feishu/Lark chat (requires credentials, see below) |
| `check_assets` | De-duplication check before creating new scripts/skills/plugins/automations |

## Installation

```bash
dsh plugin --profile web add github:boomzikazita/dsh-memory
```

The plugin registers its tools and hooks (primer recall injection, write guard, turn-end transcription) automatically. Python scripts under `scripts/` are executed on demand by the Node host.

## Configuration

The bundle patch (`cordis.patch.yml`) mounts the plugin with defaults; override per profile, e.g.:

```yaml
- insert:
    - id: dsh-memory
      name: 'dsh-memory'
      config:
        memoryDir: '~/.dsh/memories'
        # scriptsDir: '~/.dsh/plugins/dsh-memory/scripts'
        # wikiRoot:  '~/wiki'            # env DSH_WIKI_ROOT also works
        # guardBlock: false
        # primerBudget: 800
        # primerTopK: 3
```

Key paths (all overridable):

| Setting | Default | Notes |
| --- | --- | --- |
| `memoryDir` | `~/.dsh/memories` | L1 files, FTS5 indexes, vault, outbox |
| `scriptsDir` | plugin `scripts/` dir | Python helper scripts |
| `wikiRoot` / `DSH_WIKI_ROOT` | `~/wiki` | Obsidian/wiki root used by the L4 channel (`raw/`, `_meta/`, `entities/` live under it) |

## Dependencies

- **Node side**: `@deepseek-ai/dsh-tools` and `@deepseek-ai/dsh-llm` (declared as peer dependencies, `>=0.1.0-rc.6`).
- **Python side** (optional, for recall/graph/vault/harvest features): Python 3 with the standard library; SQLite 3. The vault additionally requires `cryptography` (Fernet). If Python is missing, tools degrade gracefully with an error message.
- **Feishu/Lark push** (`memory_notify` / `scripts/feishu_push.py`): set `LARK_APP_ID` / `LARK_APP_SECRET` (env or `~/.env`) and optionally `LARK_DEFAULT_CHAT` for the default chat target.
- **L3 semantic recall** (optional): set `DSH_HONCHO_MCP_URL` / `DSH_HONCHO_MCP_TOKEN` to an MCP endpoint exposing `honcho_search`. Unset → L3 silently skipped.

## License

MIT — see [LICENSE](LICENSE). Copyright (c) 2026 boomzikazita.
