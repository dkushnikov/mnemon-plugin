# Mnemon Plugin for Claude Code

Slash commands for [Mnemon](https://github.com/dkushnikov/mnemon) — your personal library with an AI reader.

| Command | What it does |
|---------|-------------|
| `/source-add` | Capture articles, PDFs, YouTube, podcasts, ideas |
| `/source-search` | Search your knowledge library |
| `/source-status` | Dashboard of all sources |

## Install

Normally installed automatically by Mnemon's `setup.sh`. Manual install:

```bash
# Requires Mnemon (https://github.com/dkushnikov/mnemon) installed first
claude plugin marketplace add https://github.com/dkushnikov/mnemon-plugin
claude plugin install mnemon@mnemon-plugin
```

## Updating

```bash
claude plugin marketplace update mnemon-plugin
claude plugin update mnemon@mnemon-plugin
```

## Documentation

Full docs, configuration, and examples: **[Mnemon README](https://github.com/dkushnikov/mnemon)**.

Custom Mnemon path: set `MNEMON_HOME` if not at `~/Mnemon/`.
