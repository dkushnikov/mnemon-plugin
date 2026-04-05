---
name: source-search
description: Search the Mnemon knowledge library (Knowledge vault) for sources, extracts, and synthesis notes. Use whenever the user asks "what do I know about X", "find sources about", "search knowledge", "have I saved anything about", references a topic they might have captured, or when you need to check for prior context before answering a substantive question. Prefer this over manual Grep on the vault — it handles tokenization, tier selection, domain filtering, and rating-based ranking in one call.
---

# /source-search — Search the Mnemon Knowledge Library

Searches the Knowledge vault for sources matching a query. Results are ranked by rating (higher is more useful to the user) then recency.

## How to run

All the logic lives in a bundled Python script. Call it via Bash:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/plugins/mnemon/skills/source-search/scripts/search.py" \
  --query "<query>" \
  [--domain <domain>] \
  [--limit <n>] \
  [--include-source] \
  [--include-synthesis]
```

The script prints a JSON object to stdout. Parse it and format the results for the user (see "Output format" below).

### Parameters

| Flag | Default | Purpose |
|---|---|---|
| `--query` | required | The search query. Tokenized into lowercase words of ≥2 chars; matching is AND across tokens (a file must contain all of them). |
| `--domain` | none | Filter by one domain tag from the extract frontmatter, e.g. `career`, `learning`, `mc/ai`. |
| `--limit` | 10 | Max number of results to return. |
| `--include-source` | off | Also search `source.md` (raw captured content), not just `extract.md`. Use when the user is looking for a specific phrase that might only appear in the raw source. |
| `--include-synthesis` | off | Also search the `Synthesis/` directory (Dima's own notes). Use when the user might be looking for their own writing, not just captured material. |
| `--human` | off | Print human-readable output instead of JSON — only for manual CLI testing, don't pass this from the skill. |

### Multi-word queries

Pass the whole query as a single `--query` argument. The script tokenizes it and matches by AND (all tokens must appear in the same file). You do NOT need to construct separate grep calls. Examples:

- `--query "karpathy knowledge bases"` → finds files containing *karpathy* AND *knowledge* AND *bases*
- `--query "organizational design"` → finds files containing both words
- `--query "AI agents"` → same

### Fallback behavior

If zero files match all tokens, the script automatically relaxes to the longest (most specific) token and returns those results with `"fallback_applied": true` in the JSON. Tell the user when this happens: *"No exact matches; showing results for `<seed-token>` only."*

### Zero results

If `count == 0` even after fallback, report: *"No sources found for '<query>'. Try broader terms or use `/source-status` to see what's in the library."*

## Output format

The script returns JSON shaped like:

```json
{
  "query": "karpathy knowledge bases",
  "tokens": ["karpathy", "knowledge", "bases"],
  "fallback_applied": false,
  "count": 2,
  "results": [
    {
      "path": "Sources/2026-04-04_5c78d63f/",
      "file": "extract.md",
      "tier": "extract",
      "title": "Andrej Karpathy — LLM Knowledge Bases (tweet, Apr 2026)",
      "author": "Andrej Karpathy",
      "url": "https://x.com/karpathy/status/...",
      "domains": ["learning", "career", "mc/ai"],
      "tags": ["knowledge-base", "llm", "obsidian"],
      "rating": 10,
      "created": "2026-04-04",
      "snippet": "Andrej Karpathy describes his personal workflow..."
    }
  ]
}
```

Format it for the user like this:

```
Found 2 results for "karpathy knowledge bases":

1. **Andrej Karpathy — LLM Knowledge Bases (tweet, Apr 2026)** · rating 10
   by Andrej Karpathy · 2026-04-04 · domains: learning, career, mc/ai
   https://x.com/karpathy/status/...
   → Andrej Karpathy describes his personal workflow...
   path: Sources/2026-04-04_5c78d63f/

2. **LLM Wiki** · rating 8
   ...
```

Lead with title + rating — rating is Dima's primary triage signal, so it should be visible immediately. Include the wikilink-friendly path at the end so he can jump to the note.

## When to broaden the search

If the first pass returns few or zero results and the user seemed to expect more, try:

1. Re-run with `--include-source` — the phrase might be in the raw capture, not the summary
2. Re-run with `--include-synthesis` — Dima might have written about it himself
3. Re-run with a simpler / broader query — strip modifiers, keep the topic word

Don't do all three silently — tell the user what you're broadening and why.

## What this skill does not do

- **Write** anything to the vault. This is a read-only skill. To add a source, use `/source-add`.
- **Read full extract content.** Results include a snippet and metadata. If the user wants to read a full extract, use the `Read` tool on the returned path.
- **Cross-vault federation.** Only searches the Knowledge vault (whatever `vault_path` points to in `~/Mnemon/mnemon.yaml`). Multi-vault search is a v3 concern — see `Projects/Mnemon/specs/v2-learnings.md` Thread 3.
- **Reader-Context framing.** Results are raw metadata + snippets, not personalized narrative. See v2-learnings Thread 2.
