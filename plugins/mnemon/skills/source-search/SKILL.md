---
name: source-search
description: Search the Mnemon knowledge library (Knowledge vault) for sources, extracts, and synthesis notes. Use whenever the user asks "what do I know about X", "find sources about", "search knowledge", "have I saved anything about", references a topic they might have captured, or when you need to check for prior context before answering a substantive question. Prefer this over manual Grep on the vault — it handles tokenization, tier selection, domain filtering, and rating-based ranking in one call. Hybrid by default: keyword (rating-ranked) plus qmd vector-semantic recall that surfaces conceptually-related notes even without keyword overlap, returned as two blocks; the semantic layer is fail-open and degrades to keyword-only if qmd is unavailable.
---

# /source-search — Search the Mnemon Knowledge Library

Searches the Knowledge vault for sources matching a query. **Hybrid by default**: a keyword layer (rating-ranked) and a qmd vector-semantic layer (score-ranked) run together and come back as two blocks — `results` (keyword) and `semantic.results` (meaning-based). The semantic layer is a strict enhancement: it requires `search_provider: qmd` in `mnemon.yaml` and is fully fail-open — if qmd is missing, broken, or its index is empty, the search returns keyword results and reports `semantic.available: false` with a reason. You never need to handle a crash.

## How to run

All the logic lives in a bundled Python script. Call it via Bash:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/source-search/scripts/search.py" \
  --query "<query>" \
  [--engine auto|keyword|semantic|deep] \
  [--domain <domain>] \
  [--limit <n>] \
  [--include-source] \
  [--include-synthesis]
```

The script prints a JSON object to stdout. Parse it and format the results for the user (see "Output format" below).

### Parameters

| Flag | Default | Purpose |
|---|---|---|
| `--query` | required | The search query. The keyword layer tokenizes it into lowercase words of ≥2 chars and matches AND across tokens (a file must contain all of them). The semantic layer uses the raw query string for vector recall. |
| `--engine` | `auto` | `auto` = keyword + fast vector semantic (both blocks). `keyword` = keyword only (no qmd call). `semantic` = vector only (no keyword block). `deep` = keyword + qmd hybrid `query` with LLM expansion + rerank (higher recall, **~10s slower**). See "Choosing the engine". |
| `--domain` | none | Filter by one domain tag from the extract frontmatter, e.g. `career`, `learning`, `mc/ai`. Applies to the keyword block. |
| `--limit` | 10 | Max number of results to return. |
| `--include-source` | off | Also search `source.md` (raw captured content), not just `extract.md`. Use when the user is looking for a specific phrase that might only appear in the raw source. |
| `--include-synthesis` | off | Also search the `Synthesis/` directory (Dima's own notes). Use when the user might be looking for their own writing, not just captured material. |
| `--human` | off | Print human-readable output instead of JSON — only for manual CLI testing, don't pass this from the skill. |

### Choosing the engine

Default to **`auto`** — it almost always does the right thing (a deterministic keyword floor plus fast semantic recall). Override only when the query shape clearly calls for it:

- **Leave it `auto`** for ordinary "what do I know about X" / "find sources about Y" lookups.
- **`--engine semantic`** when the user asks conceptually and exact words are unlikely to match — "что я думал про то, как агенты учатся из опыта", "anything about models improving themselves". Vector recall finds notes that never use the user's phrasing.
- **`--engine keyword`** for precise lookups where you want only literal matches — a known title, a person's name, a quoted phrase, an `--domain` filter sweep. Also use it if you must avoid the qmd call entirely.
- **`--engine deep`** only when `auto` came back thin and the topic matters — it runs qmd's hybrid `query` (LLM query-expansion + reranking) for higher recall, at ~10s extra latency. Don't reach for it by default.

You don't need to pre-check whether qmd is healthy — just request the engine you want. If qmd can't serve it, the script falls back to keyword and tells you via `semantic.available`/`reason` (see below). The choice is yours to make per query; the script guarantees it never fails because of qmd.

### Multi-word queries

Pass the whole query as a single `--query` argument. The script tokenizes it and matches by AND (all tokens must appear in the same file). You do NOT need to construct separate grep calls. Examples:

- `--query "karpathy knowledge bases"` → finds files containing *karpathy* AND *knowledge* AND *bases*
- `--query "organizational design"` → finds files containing both words
- `--query "AI agents"` → same

### Fallback behavior

If zero files match all tokens (AND intersection is empty), the script automatically relaxes to the most specific token that actually exists in the corpus — the one with the smallest nonzero match count — and returns those results with `"fallback_applied": true` and `"fallback_token": "<token>"` in the JSON. If none of the tokens appear in any file, `fallback_applied` stays `false` and `count` is `0`.

Tell the user when fallback fires, using the `fallback_token` value from the JSON:

> *"No file matched all of your search terms. Showing results for `<fallback_token>` only — the most specific term I could find in the library."*

Do not guess which token was used — read it from the JSON.

### Zero results

If `count == 0` even after fallback, report: *"No sources found for '<query>'. Try broader terms or use `/source-status` to see what's in the library."*

## Output format

The script returns JSON shaped like:

```json
{
  "query": "reinforcement learning",
  "tokens": ["reinforcement", "learning"],
  "engine": "auto",
  "fallback_applied": false,
  "fallback_token": null,
  "count": 2,
  "results": [
    {
      "path": "Sources/2026-06-14_bbe4be4a/",
      "file": "extract.md",
      "tier": "extract",
      "title": "RL Environments and how to build them (Unsloth × NVIDIA)",
      "author": "Unsloth × NVIDIA",
      "url": "https://unsloth.ai/blog/rl-environments",
      "domains": ["mc/ai", "mc/engineering", "learning"],
      "tags": ["reinforcement-learning", "rlvr", "grpo"],
      "rating": 8,
      "created": "2026-06-14",
      "snippet": "tags: [reinforcement-learning, rl-environments, rlvr, grpo]..."
    }
  ],
  "semantic": {
    "available": true,
    "reason": null,
    "index_stale_days": null,
    "count": 1,
    "results": [
      {
        "path": "Sources/2026-05-31_b6552fcb/",
        "file": "extract.md",
        "tier": "extract",
        "title": "Decision making with data — Sergey Levine",
        "author": "Thinking About Thinking",
        "url": "https://www.youtube.com/watch?v=...",
        "domains": ["mc/ai", "mc/engineering"],
        "tags": ["offline-rl", "autonomous-agents"],
        "rating": 7,
        "created": "2026-05-31",
        "snippet": "machine learning reinforcement learning ... deep learning methods",
        "engine": "semantic",
        "score": 0.68
      }
    ]
  }
}
```

`results` is the keyword block (rating-ranked); `semantic.results` is the vector block (score-ranked), already de-duplicated against the keyword block so it shows only what meaning-based recall *adds*. Semantic results carry an extra `engine: "semantic"` and a `score` (0–1).

Format it for the user with the two blocks kept visually distinct:

```
Found 2 results for "reinforcement learning":

1. **RL Environments and how to build them (Unsloth × NVIDIA)** · rating 8
   by Unsloth × NVIDIA · 2026-06-14 · domains: mc/ai, mc/engineering, learning
   https://unsloth.ai/blog/rl-environments
   → tags: [reinforcement-learning, rlvr, grpo]...
   path: Sources/2026-06-14_bbe4be4a/

Semantically related (also worth a look):

1. **Decision making with data — Sergey Levine** · rating 7 · score 0.68
   by Thinking About Thinking · 2026-05-31 · domains: mc/ai, mc/engineering
   → machine learning reinforcement learning ... deep learning methods
   path: Sources/2026-05-31_b6552fcb/
```

Lead with title + rating — rating is Dima's primary triage signal, so it should be visible immediately. Include the wikilink-friendly path at the end so he can jump to the note. Show `score` on semantic results so he can see confidence.

**Handling the semantic block:**

- `semantic.available: true` with results → render the "Semantically related" section. Skip the section entirely if it's empty (don't print an empty header).
- `semantic.index_stale_days` > 7 → add a one-line note: *"(semantic index is N days stale — run `qmd update && qmd embed` to include recent captures)"*. Recent notes may be missing from vector recall until then.
- `semantic.available: false` → just present the keyword results. Only mention the reason if the user explicitly asked for semantic/conceptual search (e.g. they chose `--engine semantic`): then say *"Semantic search is unavailable (`<reason>`), so these are keyword matches only."* For ordinary `auto` lookups, stay silent about it — keyword results are the answer.

## When to broaden the search

If the first pass returns few or zero results and the user seemed to expect more, try:

1. Re-run with `--engine semantic` (or `deep`) — meaning-based recall catches notes that don't share the user's wording. This is the first thing to try for conceptual topics.
2. Re-run with `--include-source` — the phrase might be in the raw capture, not the summary
3. Re-run with `--include-synthesis` — Dima might have written about it himself
4. Re-run with a simpler / broader query — strip modifiers, keep the topic word

Don't do all of these silently — tell the user what you're broadening and why.

## What this skill does not do

- **Write** anything to the vault. This is a read-only skill. To add a source, use `/source-add`.
- **Read full extract content.** Results include a snippet and metadata. If the user wants to read a full extract, use the `Read` tool on the returned path.
- **Cross-vault federation.** Only searches the Knowledge vault (whatever `vault_path` points to in `~/Mnemon/mnemon.yaml`). Multi-vault search is a v3 concern — see `Projects/Mnemon/specs/v2-learnings.md` Thread 3.
- **Reader-Context framing.** Results are raw metadata + snippets, not personalized narrative. See v2-learnings Thread 2.
