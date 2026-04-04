# Mnemon Plugin for Claude Code

Claude Code marketplace plugin for [Mnemon](https://github.com/dkushnikov/mnemon) — AI-powered personal knowledge extraction system.

## Install

```bash
# 1. Install Mnemon first
git clone https://github.com/dkushnikov/mnemon ~/Mnemon
cd ~/Mnemon && ./setup.sh ~/path/to/your/vault

# 2. Install this plugin
claude plugin marketplace add https://github.com/dkushnikov/mnemon-plugin
claude plugin install mnemon@mnemon-plugin
```

## Skills

| Skill | Description |
|-------|-------------|
| `/source-add` | Capture articles, videos, podcasts, books, ideas |
| `/source-search` | Search your knowledge library |
| `/source-status` | Dashboard of all sources |

## Custom Install Path

If Mnemon is not at `~/Mnemon/`, set `MNEMON_HOME`:

```bash
export MNEMON_HOME=~/path/to/mnemon
```

## Updating

```bash
claude plugin marketplace update mnemon-plugin
claude plugin update mnemon@mnemon-plugin
```
