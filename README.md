# agent-usage-dashboard

**English** | [简体中文](README.zh-CN.md)

A local dashboard for **all your coding agents'** token usage — reads local session logs / databases from every agent on your machine, aggregates them, and visualizes token consumption, cost, and activity.

All data stays on your machine — the only network calls are optional read-only fetches: your own Cursor usage export and the public models.dev price list (see below). Nothing is uploaded anywhere.

## Supported Agents

| Agent | Source | Tokens | Cost |
| --- | --- | --- | --- |
| Kimi Code | `~/.kimi-code/sessions` via ccusage | ✅ | ✅ |
| Codex | `~/.codex/sessions` via ccusage | ✅ | ✅ |
| Claude Code / Gemini / OpenCode / Kilo / Pi / Amp / Droid / Goose / Qwen / Copilot / … | auto-detected by ccusage | ✅ | ✅ |
| CommandCode | `~/.commandcode/projects/**.jsonl` | ✅ | ✅ |
| DimCode (dimagent) | `~/.dimcode/v2/dimcode.sqlite` | ✅ | ✅ |
| Devin | `~/.local/share/devin/cli/sessions.db` | ✅ | ≈² |
| KimiX | `~/.kimix/sessions/**/updates.jsonl` | ✅ | ≈² |
| Cursor | cursor.com usage export (read-only, uses your local login) + `~/.cursor` local logs | ✅¹ | ≈² |
| Antigravity (IDE) | `~/.gemini/antigravity{,-ide}/conversations/*.db` (protobuf `gen_metadata` decoded offline) | ✅ | ≈² |
| Antigravity CLI (`agy`) | `~/.gemini/antigravity-cli/conversations/*.db` (same protobuf store) | ✅ | ≈² |

¹ Cursor does not record token counts in local logs. The collector fetches your own usage CSV from `cursor.com` using the login token stored locally by the Cursor app (read-only GET; set `USAGE_DASH_CURSOR_API=0` to disable, `USAGE_DASH_CURSOR_TIMEOUT` for seconds). If the token is expired or the network is down, it falls back to local activity counts. Subscription "Included" rows have no dollar cost, so cost shows `—`.

² Sources without recorded cost get an **estimated** cost marked `≈$`, computed from the [models.dev](https://models.dev) price list (the same open database OpenCode uses — `input`/`output`/`cache_read`/`cache_write` per MTok). The collector fetches `models.dev/api.json` once and caches it in `.cache/` for 24h (set `USAGE_DASH_PRICES=0` to disable, `USAGE_DASH_PRICES_TIMEOUT` for seconds). Internal models with no public price (e.g. `swe-2-*`, `composer-*`) are marked "Unpriced". When several providers list the same model, the first-party vendor's price wins. A row with both recorded and estimated cost shows their sum, prefixed with `≈` whenever any estimate is included. Estimates are a reference, not actual charges.

> `~/.claude/projects` on this machine only contains `dimcode-mirror` / `commandcode-mirror` files — those are skipped to avoid double counting; the authoritative sources are read instead.

## Features

- **Filter bar**: Agent / Model / Project selectors — all composable, global to every card and table. The Model picker is a collapsible tree grouped by **developer** (Anthropic / OpenAI / DeepSeek / …, resolved from models.dev metadata): click a developer to filter the whole group, or expand it to pick a single model. Variant model IDs (`provider/model`, `cursor-*`, `-expires-on-*`, date stamps, `[agent]` prefixes) are normalized so the same model is counted once
- **Agent usage table**: per-agent share, days, sessions, token breakdown and cost — click a row to filter
- **Project distribution**: per-project share across agents, days and sessions — click a row to filter; falls back to session-level attribution for sources without native project rows
- **Top stat strip**: total usage / net generation / cache hit rate / est. cost (USD), each with a delta vs the previous period of equal length
- **Activity heatmap**: GitHub-style, trailing one year (token / cost / activity modes), plus a weekday × hour activity grid for hour-granular sources
- **Distribution**: donut charts for Agent / Model / Project token share (top 5 + other)
- **Daily token breakdown**: stacked bars (fresh input / output / cache read / cache write); switches to hourly granularity when the selected range is exactly one day
- **Per-model usage table**: with agent attribution and USD cost
- **Monthly summary**, **session explorer** (per-agent, per-model, per-project, searchable, with last-activity date), and a **data sources** card
- **Time range filter**: today / 7 / 14 / 30 days / all / custom; all selections survive refresh via URL params (`a`, `m`, `pj`, `p`, `s`, `e`)
- **Bilingual**: 中文 / English toggle

## Requirements

- **Python 3** — the collector `collect.py` and server `serve.py` (standard library only)
- **Node.js** — only needed for `ccusage` (covers kimi / codex / opencode / kilo / claude / gemini / pi / …). If `node_modules/.bin/ccusage` is missing, those sources are skipped with a warning and everything else still works.

## Quick Start

```bash
npm install   # installs the pinned ccusage dependency (offline mode)
./start.sh    # starts the local server (127.0.0.1:8931) and opens the page
```

The "刷新数据" (refresh) button calls `refresh.sh` → `collect.py` to re-aggregate the latest logs; you can also run it manually:

```bash
./refresh.sh
```

## How It Works

1. `collect.py` fans out to every source: one `ccusage daily --by-agent` call for all ccusage-supported agents, plus native parsers for CommandCode / KimiX (JSONL), DimCode / Devin (SQLite, copied to tmp first so live WAL databases are read safely), Antigravity (SQLite + protobuf decode) and Cursor (cloud usage CSV + local transcripts);
2. Everything is normalized into per-`(date, agent)` token rows + per-session rows, then written atomically to `data.js` (`window.DASHBOARD_DATA`, mode 0600);
3. `serve.py` serves the page and exposes `/refresh`; `index.html` renders everything in pure front-end JS — no framework, no build step.

> `data.js` is your personal usage data. It is excluded by `.gitignore` and never committed.

## Files

| File | Purpose |
| --- | --- |
| `index.html` | The dashboard page (styles + rendering logic, all in one file) |
| `collect.py` | Multi-agent collector: ccusage + native parsers → `data.js` |
| `serve.py` | Local server: static hosting + `/refresh` endpoint |
| `refresh.sh` | Lock-safe wrapper around `collect.py` |
| `start.sh` | Starts the server and opens the browser (safe to re-run) |

## License

[MIT](LICENSE)
