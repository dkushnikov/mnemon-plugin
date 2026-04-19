---
name: source-add
description: Add a source to your Mnemon knowledge library — articles, YouTube videos, podcasts, books, ideas. Captures content and generates a structured extract with personal context framing. Use when the user provides a URL to save, says "add source", "capture this", "save this article/video/podcast", or wants to process any content into their knowledge library.
---

# /source-add — Capture a Source

Add a source to the knowledge library. The gateway fetches content, applies an extraction template, and generates structured `source.md` + `extract.md` in the vault.

## Usage Examples

```
/source-add https://example.com/article
/source-add --url https://youtube.com/watch?v=abc123
/source-add --origin audio --url https://podcast.example.com/episode.mp3
/source-add --origin idea --title "My insight about X"
/source-add --origin book --title "Book Title" --author "Author Name"
/source-add --url https://spa-site.com/ --render     # JS-heavy SPA: pre-render via headless Chrome
/source-add --file ~/Downloads/paper.pdf             # local PDF: auto-detected, Read tool extracts
/source-add --url https://arxiv.org/pdf/2401.12345.pdf   # remote PDF: auto-detected, downloaded to /tmp
```

## Parameters

| Parameter | Required | Default | Description |
|-----------|----------|---------|-------------|
| `--url <url>` | For URL sources | — | Source URL (YouTube / .pdf auto-detected) |
| `--file <path>` | For local files | — | Local file path (audio or PDF, auto-detected by extension) |
| `--origin <type>` | Auto-detected | From URL/file | `url`, `youtube`, `audio`, `pdf`, `text`, `book`, `idea` |
| `--title <title>` | For text/book/idea | Inferred | Source title |
| `--author <name>` | No | Inferred | Source author |
| `--source-type <type>` | No | From origin | `article`, `video`, `podcast`, `book`, `paper`, `idea`, `conversation` |
| `--intent <text>` | No | none | Why you're capturing this |
| `--context <ctx>` | No | personal | `personal` or `mc` |
| `--render` | No | off | Pre-render URL via Chrome headless (for SPAs / client-side-rendered pages). Requires Google Chrome or Chromium. |

## PDF capture

Both local PDFs (`--file paper.pdf`) and remote PDFs (`--url https://.../x.pdf`) auto-detect as `origin=pdf` (source_type=paper). Remote PDFs are downloaded to `/tmp` first; original URL stays canonical in frontmatter.

Extraction uses Claude Code's native `Read` tool — no pypdf/pdftotext preprocessing. This handles text, layout, tables, and images/OCR content that CLI tools would miss. No `--render` / `--whisper` needed.

Page count lands in frontmatter (`pages: N`). For PDFs with heavy image/figure content, the extract describes the visual layout where relevant.

## When to use `--render`

Plain HTTP fetch returns empty `<div id="root">` for client-side-rendered apps (React/Vue/Svelte SPAs). Symptoms when you should retry with `--render`:

- Extract is suspiciously short (hero/tagline only, no body content)
- Source is a landing page, docs site, or dashboard built as a SPA
- You see the page fine in browser but extract is mostly meta tags

Pass `--render` and Mnemon pre-renders via Chrome headless (~2-5s latency), keeping the URL canonical in frontmatter. Falls back with a clear error if Chrome/Chromium isn't installed.

Do NOT use `--render` reflexively — default fast path is fine for 90% of web content. Use when you see SPA symptoms.

## How to Execute

1. Build the command from user's input. If the user just provides a URL, that's sufficient — origin and source_type are auto-detected.

2. Call the gateway via the Bash tool:

```bash
${MNEMON_HOME:-~/Mnemon}/bin/knowledge-gateway.sh source-add --url "<url>" [--origin <origin>] [--title "<title>"] [--author "<author>"] [--intent "<intent>"]
```

3. Parse RESULT lines from the output:
   - `RESULT:path=Sources/<folder>/` — created source path
   - `RESULT:status=extracted` — extraction status  
   - `RESULT:title=<title>` — extracted title

4. Show the user a concise summary:

```
Added: <title>
Path: <path>
Status: <status>
```

5. If the gateway fails, show the error message. Common issues:
   - "No mnemon.yaml found" → run `${MNEMON_HOME:-~/Mnemon}/setup.sh <vault-path>` first
   - "Vault not found" → check vault_path in mnemon.yaml
   - Media extraction failed → check yt-dlp/whisper installation

## For text/idea origins

If the user wants to capture text or an idea, they need to provide the content. Ask them to paste it, then pipe it to the gateway:

```bash
echo "<pasted content>" | ${MNEMON_HOME:-~/Mnemon}/bin/knowledge-gateway.sh source-add --origin text --title "<title>"
```
