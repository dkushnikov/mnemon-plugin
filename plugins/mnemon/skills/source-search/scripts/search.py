#!/usr/bin/env python3
"""search.py — search the Mnemon knowledge library.

Called by the mnemon:source-search skill. Reads the vault path and search
provider from ~/Mnemon/mnemon.yaml (via mnemon-config.sh conventions), then
searches Sources/extract.md (and optionally source.md and Synthesis/) for
sources matching the query. Results are ranked by rating DESC then date DESC.

Design notes
------------
- Engine: by default (`--engine auto`) the keyword layer below runs AND a
  semantic layer backed by qmd (vector search) runs; output carries two
  blocks — `results` (keyword, rating-ranked) and `semantic.results`
  (meaning-based, score-ranked, with keyword dupes removed). The semantic
  layer is a STRICT enhancement: gated on search_provider=qmd and fully
  fail-open — a missing/broken qmd or empty index degrades to keyword-only
  and says so. See the "qmd semantic layer" section. Override with
  `--engine keyword|semantic|deep` (deep = qmd hybrid 'query', slower).
- Tokenization: query is split into lowercase tokens >=2 chars long. We
  find files containing ALL tokens (AND semantics). This is what you want
  99% of the time — "karpathy knowledge bases" should match files that
  contain all three words, not the literal phrase.
- Fallback: if no file matches all tokens, we relax to the most *specific*
  token that exists in the corpus (fewest matching files, not longest
  string) and mark results with a `fallback` flag so the caller can tell
  the user we broadened the search.
- Frontmatter parsing: real YAML via PyYAML (yaml.safe_load) — handles the
  inline lists, block lists, and YAML-1.1 date scalars Mnemon extracts use.
- Output: JSON to stdout by default. The skill reads the JSON and formats
  it for the user. If you want human output for ad-hoc CLI use, pass
  --human.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

try:
    import yaml  # PyYAML — stdlib-adjacent; install with `pip3 install pyyaml` if missing
except ImportError:
    print(
        "ERROR: PyYAML is required. Install with: pip3 install pyyaml",
        file=sys.stderr,
    )
    sys.exit(2)


# ---------------------------------------------------------------------------
# Config loading — mirrors ~/Mnemon/bin/mnemon-config.sh resolution order
# ---------------------------------------------------------------------------

def load_mnemon_config() -> dict:
    """Resolve mnemon.yaml and parse the handful of keys we care about.

    Resolution order matches mnemon-config.sh and honors MNEMON_HOME,
    which is the env var Dima exports globally (see ~/.claude/CLAUDE.md):
      1. $MNEMON_CONFIG env var (explicit file path)
      2. ./mnemon.yaml in cwd
      3. $MNEMON_HOME/mnemon.yaml (default: ~/Mnemon/mnemon.yaml)

    We also fall back to $MNEMON_ROOT for backward compatibility with any
    script that was already setting it.
    """
    candidates = []
    if os.environ.get("MNEMON_CONFIG"):
        candidates.append(Path(os.environ["MNEMON_CONFIG"]))
    candidates.append(Path.cwd() / "mnemon.yaml")
    mnemon_home = (
        os.environ.get("MNEMON_HOME")
        or os.environ.get("MNEMON_ROOT")
        or str(Path.home() / "Mnemon")
    )
    candidates.append(Path(mnemon_home).expanduser() / "mnemon.yaml")

    config_file = next((p for p in candidates if p.is_file()), None)
    if config_file is None:
        raise RuntimeError(
            "No mnemon.yaml found. Run ~/Mnemon/setup.sh <vault-path> first."
        )

    cfg: dict = {}
    for line in config_file.read_text().splitlines():
        if not line or line.lstrip().startswith("#"):
            continue
        m = re.match(r"^([a-zA-Z_][a-zA-Z0-9_]*)\s*:\s*(.*)$", line)
        if not m:
            continue
        key, raw = m.group(1), m.group(2).strip()
        # strip inline comment
        raw = re.sub(r"\s+#.*$", "", raw).strip()
        # strip surrounding quotes
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
            raw = raw[1:-1]
        # expand ~
        if raw.startswith("~"):
            raw = str(Path(raw).expanduser())
        cfg[key] = raw

    if "vault_path" not in cfg:
        raise RuntimeError(f"vault_path not set in {config_file}")
    return cfg


# ---------------------------------------------------------------------------
# Frontmatter parsing — real YAML via PyYAML
# ---------------------------------------------------------------------------
#
# We use yaml.safe_load on the frontmatter block to handle the full set of
# YAML constructs Mnemon extracts use in practice: scalar strings (quoted or
# not), integers, inline lists `[a, b]`, block lists (`\n  - item`), and
# nested dicts. A hand-rolled parser kept failing on block lists, which are
# used in many extracts for `domains` and `tags` — the fields the skill
# ranks and displays.


def parse_frontmatter(path: Path) -> dict:
    """Extract frontmatter from a markdown file. Returns {} if absent or malformed."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    if not text.startswith("---"):
        return {}
    lines = text.split("\n")
    end_idx = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break
    if end_idx is None:
        return {}
    block = "\n".join(lines[1:end_idx])
    try:
        data = yaml.safe_load(block)
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------------------
# Tokenization
# ---------------------------------------------------------------------------

def tokenize(query: str) -> list[str]:
    """Lowercase, unicode-aware tokenization. Drops tokens shorter than 2 chars."""
    tokens = re.findall(r"\w+", query.lower(), flags=re.UNICODE)
    return [t for t in tokens if len(t) >= 2]


# ---------------------------------------------------------------------------
# Target collection and matching
# ---------------------------------------------------------------------------

def collect_targets(
    vault: Path, include_source: bool, include_synthesis: bool
) -> list[tuple[Path, str]]:
    """Return list of (file_path, tier) tuples to search."""
    targets: list[tuple[Path, str]] = []
    sources_dir = vault / "Sources"
    if sources_dir.is_dir():
        for extract in sorted(sources_dir.glob("*/extract.md")):
            targets.append((extract, "extract"))
        if include_source:
            for src in sorted(sources_dir.glob("*/source.md")):
                targets.append((src, "source"))
    if include_synthesis:
        synth_dir = vault / "Synthesis"
        if synth_dir.is_dir():
            for md in sorted(synth_dir.rglob("*.md")):
                targets.append((md, "synthesis"))
    return targets


def scan_file(path: Path, tokens: list[str]) -> tuple[set[str], str | None]:
    """Scan a file and return (set of matched tokens, snippet).

    Returns the *subset* of query tokens that appear in the file, not just a
    boolean "matched all" verdict. This lets the caller:

    1. Filter to files matching ALL tokens (primary results: intersection).
    2. If that's empty, fall back to files matching just the most specific
       token that actually exists in the corpus — using the per-token match
       counts computed naturally from this scan.

    Returns (empty set, None) if the file can't be read or matches nothing.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return set(), None
    text_lower = text.lower()
    matched = {t for t in tokens if t in text_lower}
    if not matched:
        return set(), None
    return matched, _extract_snippet(text, list(matched))


def _extract_snippet(text: str, tokens: list[str], width: int = 200) -> str | None:
    """Return the first non-frontmatter line containing any of the given tokens.

    Prefers the longest matched token for readability (more specific terms
    produce more interesting snippets), but will settle for any match.
    """
    if not tokens:
        return None
    seed = max(tokens, key=len)
    lines = text.split("\n")
    in_frontmatter = False
    fm_ended = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if i == 0 and stripped == "---":
            in_frontmatter = True
            continue
        if in_frontmatter:
            if stripped == "---":
                in_frontmatter = False
                fm_ended = True
            continue
        if seed in line.lower():
            snippet = stripped
            if len(snippet) > width:
                snippet = snippet[:width] + "…"
            return snippet
    # fallback: match anywhere (frontmatter hit — e.g. author field)
    for line in lines:
        if seed in line.lower():
            snippet = line.strip()
            if len(snippet) > width:
                snippet = snippet[:width] + "…"
            return snippet
    return None


# ---------------------------------------------------------------------------
# Result assembly
# ---------------------------------------------------------------------------

def assemble_result(path: Path, tier: str, snippet: str | None, vault: Path) -> dict:
    meta = parse_frontmatter(path)
    # folder path relative to vault (e.g. Sources/2026-04-04_5c78d63f/)
    try:
        rel = path.relative_to(vault)
        folder_rel = str(rel.parent) + "/"
    except ValueError:
        folder_rel = str(path.parent)
    return {
        "path": folder_rel,
        "file": path.name,
        "tier": tier,
        "title": meta.get("title") or path.stem,
        "author": meta.get("author") or "",
        "url": meta.get("url") or "",
        "domains": meta.get("domains") or [],
        "tags": meta.get("tags") or [],
        "rating": meta.get("rating") if isinstance(meta.get("rating"), int) else None,
        "created": meta.get("created") or meta.get("extracted") or "",
        "snippet": snippet or "",
    }


def rank_results(results: list[dict]) -> list[dict]:
    """Sort by rating DESC (None last), then created DESC, then title."""
    def key(r: dict):
        rating = r.get("rating")
        has_rating = 0 if rating is None else 1
        return (-has_rating, -(rating or 0), -_date_sort_key(r.get("created", "")), r.get("title", ""))
    return sorted(results, key=key)


def _date_sort_key(date_str: str) -> int:
    """Turn 'YYYY-MM-DD' into a sortable integer. Unknown dates sort last."""
    if not date_str:
        return 0
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", str(date_str))
    if not m:
        return 0
    return int(m.group(1)) * 10000 + int(m.group(2)) * 100 + int(m.group(3))


def apply_domain_filter(results: list[dict], domain: str) -> list[dict]:
    if not domain:
        return results
    needle = domain.lower()
    return [r for r in results if any(needle == d.lower() for d in r.get("domains", []))]


def dedupe_by_folder(results: list[dict]) -> list[dict]:
    """If both extract.md and source.md match the same folder, keep extract.md."""
    by_folder: dict[str, dict] = {}
    tier_priority = {"extract": 0, "synthesis": 1, "source": 2}
    for r in results:
        key = r["path"]
        if key not in by_folder:
            by_folder[key] = r
            continue
        current = by_folder[key]
        if tier_priority.get(r["tier"], 99) < tier_priority.get(current["tier"], 99):
            by_folder[key] = r
    return list(by_folder.values())


# ---------------------------------------------------------------------------
# qmd semantic layer (hybrid search)
# ---------------------------------------------------------------------------
#
# The keyword search above is the reliable floor: pure Python, no external
# deps, reads live files. The qmd layer is a strict *enhancement* — it adds
# vector-semantic recall ("find notes about this idea even if they don't
# contain the words"). It must NEVER be load-bearing: if qmd is missing,
# broken (e.g. ABI mismatch after a Node upgrade), or its index is empty, the
# search degrades silently to keyword-only and says so. qmd is invoked via the
# `qmd` name on PATH so the user's runtime wrapper (~/.local/bin/qmd) is
# honored.


def parse_qmd_json(stdout: str) -> list | None:
    """Parse `qmd ... --json` stdout into a list of raw hit dicts.

    Returns None when no JSON array can be recovered (signals "qmd output
    unusable" → caller degrades to keyword-only). Returns [] for a valid but
    empty result set. qmd writes clean JSON to stdout today; the first-'['
    recovery is defensive against future stdout preamble.
    """
    if not stdout:
        return None
    text = stdout.strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        idx = text.find("[")
        if idx == -1:
            return None
        try:
            data = json.loads(text[idx:])
        except json.JSONDecodeError:
            return None
    return data if isinstance(data, list) else None


_QMD_STALE_RE = re.compile(r"last updated (\d+) days? ago", re.IGNORECASE)


def parse_qmd_stale_days(stderr: str) -> int | None:
    """Extract qmd's 'Index last updated N days ago' tip from stderr.

    Returns the day count so the skill can warn that recent captures may be
    missing from the semantic index. None when no such tip is present.
    """
    if not stderr:
        return None
    m = _QMD_STALE_RE.search(stderr)
    return int(m.group(1)) if m else None


_QMD_HASH_RE = re.compile(r"[0-9a-f]{8}")


def resolve_qmd_file(qmd_path: str, vault: Path) -> Path | None:
    """Map a normalized qmd:// file path back to the real vault file to show.

    qmd normalizes paths (lowercase, '_'->'-'), so the name can't be
    reconstructed losslessly. Mnemon source folders are `YYYY-MM-DD_<hash8>`
    where hash8 is a unique 8-hex id; we key off that. Prefers the curated
    extract.md over the raw source.md (mirrors dedupe_by_folder's tier
    priority). Returns None for non-Sources hits or unknown hashes — the
    caller simply drops those, never crashes.
    """
    if not qmd_path:
        return None
    # Strip trailing " #docid" and ":line" without harming the "://" scheme.
    s = qmd_path.strip()
    s = re.sub(r"\s+#\w+$", "", s)
    s = re.sub(r":\d+$", "", s)

    folder_part = s.rsplit("/", 1)[0] if "/" in s else s
    folder_seg = folder_part.rsplit("/", 1)[-1]
    if "/sources/" not in s.lower() and not s.lower().startswith("sources/"):
        return None
    m = _QMD_HASH_RE.search(folder_seg)
    if not m:
        return None
    hash8 = m.group(0)

    sources_dir = vault / "Sources"
    if not sources_dir.is_dir():
        return None
    for folder in sources_dir.glob(f"*{hash8}"):
        if not folder.is_dir():
            continue
        extract = folder / "extract.md"
        if extract.is_file():
            return extract
        hit = folder / Path(s).name
        if hit.is_file():
            return hit
        mds = sorted(folder.glob("*.md"))
        return mds[0] if mds else None
    return None


def _clean_qmd_snippet(snippet: str, width: int = 200) -> str:
    """Turn qmd's diff-style snippet ('@@ -29,4 @@ ...\\nline\\nline') into a
    single readable line showing the semantically-matched passage."""
    if not snippet:
        return ""
    s = re.sub(r"@@[^@]*@@", "", snippet)
    s = " ".join(s.split())
    return (s[:width] + "…") if len(s) > width else s


def _qmd_on_path() -> bool:
    """Whether the `qmd` binary is resolvable on PATH (honors the user's
    wrapper at ~/.local/bin/qmd). Isolated for monkeypatching in tests."""
    return shutil.which("qmd") is not None


def _run_qmd(argv: list[str]) -> subprocess.CompletedProcess:
    """Thin, mockable shell-out to qmd. The ONLY impure point of the layer."""
    return subprocess.run(argv, capture_output=True, text=True)


def semantic_search(
    query: str,
    collection: str,
    vault: Path,
    limit: int,
    *,
    deep: bool = False,
    run=None,
) -> dict:
    """Run qmd vector search and map hits onto the same result shape as the
    keyword layer. STRICTLY fail-open: any missing binary / non-zero exit /
    unparseable output returns {available: False, reason, results: []} — never
    raises. `run` is an injectable runner (argv -> CompletedProcess) for tests.

    Returns: {available, reason, index_stale_days, results:[{...assemble_result
    fields..., engine:"semantic", score:float}]}.
    """
    result: dict = {"available": False, "reason": None,
                    "index_stale_days": None, "results": []}

    if not collection:
        result["reason"] = "no qmd_collection configured"
        return result

    if run is None:
        if not _qmd_on_path():
            result["reason"] = "qmd not installed"
            return result
        run = _run_qmd

    fetch_n = max(limit * 3, 15)
    subcmd = "query" if deep else "vsearch"
    argv = ["qmd", subcmd, query, "-c", collection,
            "--limit", str(fetch_n), "--json"]

    try:
        proc = run(argv)
    except Exception as e:  # FileNotFoundError, OSError, ... — never propagate
        result["reason"] = f"qmd invocation failed: {e}"
        return result

    if getattr(proc, "returncode", 1) != 0:
        first = (proc.stderr or "").strip().splitlines()
        detail = f": {first[0]}" if first else ""
        result["reason"] = f"qmd exited {proc.returncode}{detail}"
        return result

    hits = parse_qmd_json(proc.stdout or "")
    if hits is None:
        result["reason"] = "qmd output unparseable"
        return result

    result["index_stale_days"] = parse_qmd_stale_days(proc.stderr or "")

    by_folder: dict[str, dict] = {}
    for hit in hits:
        f = resolve_qmd_file(hit.get("file", ""), vault)
        if f is None:
            continue
        if f.name == "extract.md":
            tier = "extract"
        elif "Synthesis" in f.parts:
            tier = "synthesis"
        else:
            tier = "source"
        score = hit.get("score")
        rec = assemble_result(f, tier, _clean_qmd_snippet(hit.get("snippet", "")), vault)
        rec["engine"] = "semantic"
        rec["score"] = score
        key = rec["path"]
        if key not in by_folder or (score or 0) > (by_folder[key].get("score") or 0):
            by_folder[key] = rec

    ranked = sorted(by_folder.values(), key=lambda r: -(r.get("score") or 0))
    result["results"] = ranked[:limit]
    result["available"] = True
    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Search the Mnemon knowledge library.")
    parser.add_argument("--query", required=True, help="Search query (tokenized, AND semantics)")
    parser.add_argument("--domain", default="", help="Filter by domain tag (e.g. career, learning)")
    parser.add_argument("--limit", type=int, default=10, help="Max results (default: 10)")
    parser.add_argument("--include-source", action="store_true",
                        help="Also search raw source.md (default: only extract.md)")
    parser.add_argument("--include-synthesis", action="store_true",
                        help="Also search Synthesis/ directory")
    parser.add_argument("--human", action="store_true", help="Human-readable output instead of JSON")
    parser.add_argument(
        "--engine", choices=["auto", "keyword", "semantic", "deep"], default="auto",
        help="auto: keyword + fast vector semantic (default). keyword: keyword only. "
             "semantic: vector only. deep: keyword + qmd hybrid 'query' (expansion+rerank, slower). "
             "Semantic layers require search_provider=qmd; otherwise they degrade to keyword.")
    args = parser.parse_args()

    try:
        cfg = load_mnemon_config()
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    vault = Path(cfg["vault_path"])
    if not vault.is_dir():
        print(f"ERROR: vault not found at {vault}", file=sys.stderr)
        return 2

    tokens = tokenize(args.query)
    if not tokens:
        print("ERROR: query contains no usable tokens (>=2 chars)", file=sys.stderr)
        return 2

    targets = collect_targets(vault, args.include_source, args.include_synthesis)

    # Single pass: collect every file that matches at least one token, along
    # with the set of tokens it matched. Primary results are the subset where
    # matched == all tokens (AND semantics). Fallback uses per-token counts
    # computed from the same pass.
    all_hits: list[tuple[Path, str, set, str | None]] = []
    for path, tier in targets:
        matched, snippet = scan_file(path, tokens)
        if matched:
            all_hits.append((path, tier, matched, snippet))

    token_set = set(tokens)
    primary_hits = [(p, t, m, s) for p, t, m, s in all_hits if m == token_set]

    fallback_applied = False
    fallback_token: str | None = None
    if primary_hits:
        selected_hits = primary_hits
    elif len(tokens) > 1:
        # Fallback strategy: pick the most *specific* token that actually
        # exists in the corpus — not the longest string. Specificity here
        # means "appears in the fewest files, but at least one". This beats
        # the naive longest-token heuristic because string length is a poor
        # proxy for specificity (e.g. "karpathy" < "zyzzytoken" by chars,
        # but karpathy matches 2 files and zyzzytoken matches 0 — picking
        # the longest would return empty results).
        token_counts: dict[str, int] = {t: 0 for t in tokens}
        for _, _, m, _ in all_hits:
            for t in m:
                token_counts[t] += 1
        viable = {t: c for t, c in token_counts.items() if c > 0}
        if viable:
            # Smallest nonzero count = most specific. Ties broken by token
            # length DESC (prefer longer tokens as tiebreak, since they're
            # usually more meaningful when counts are equal).
            fallback_token = min(viable, key=lambda t: (viable[t], -len(t)))
            selected_hits = [h for h in all_hits if fallback_token in h[2]]
            fallback_applied = True
        else:
            selected_hits = []
    else:
        selected_hits = []

    results = [assemble_result(p, t, s, vault) for p, t, _, s in selected_hits]
    results = dedupe_by_folder(results)
    results = apply_domain_filter(results, args.domain)
    results = rank_results(results)
    results = results[: args.limit]

    run_keyword = args.engine in ("auto", "keyword", "deep")
    run_semantic = args.engine in ("auto", "semantic", "deep")

    # In pure-semantic mode the keyword block is suppressed (the scan above is
    # cheap and harmless; we just don't surface it).
    if not run_keyword:
        results, fallback_applied, fallback_token = [], False, None

    # --- Semantic (qmd) block: STRICT enhancement, gated on search_provider=qmd ---
    provider = (cfg.get("search_provider") or "").strip().lower()
    collection = cfg.get("qmd_collection") or ""
    if not run_semantic:
        semantic = {"available": False, "reason": f"not requested (engine={args.engine})",
                    "index_stale_days": None, "results": [], "count": 0}
    elif provider != "qmd":
        semantic = {"available": False,
                    "reason": f"semantic disabled (search_provider={provider or 'unset'})",
                    "index_stale_days": None, "results": [], "count": 0}
    else:
        semantic = semantic_search(args.query, collection, vault, args.limit,
                                   deep=(args.engine == "deep"))
        # Drop folders the keyword block already surfaced — the semantic block
        # shows only what meaning-based recall ADDS, not duplicates.
        kw_paths = {r["path"] for r in results}
        semantic["results"] = [r for r in semantic["results"] if r["path"] not in kw_paths]
        semantic.setdefault("count", 0)
        semantic["count"] = len(semantic["results"])

    output = {
        "query": args.query,
        "tokens": tokens,
        "engine": args.engine,
        "fallback_applied": fallback_applied,
        "fallback_token": fallback_token,
        "count": len(results),
        "results": results,
        "semantic": semantic,
    }

    if args.human:
        _print_human(output)
    else:
        # default=str handles types PyYAML produces that json doesn't know
        # about natively — notably datetime.date/datetime.datetime from
        # YAML 1.1 implicit typing of fields like `created: 2026-04-04`.
        # str(date) gives "2026-04-04" which is exactly what we want in
        # the JSON output consumed by the skill.
        json.dump(output, sys.stdout, ensure_ascii=False, indent=2, default=str)
        sys.stdout.write("\n")
    return 0


def _print_human_result(i: int, r: dict) -> None:
    rating = f"rating {r['rating']}" if r["rating"] is not None else "unrated"
    domains = ", ".join(r["domains"]) if r["domains"] else "—"
    score = f" · score {r['score']:.2f}" if r.get("score") is not None else ""
    print(f"{i}. {r['title']}{score}")
    if r["author"]:
        print(f"   by {r['author']}")
    print(f"   {rating} · {r['created']} · domains: {domains} · tier: {r['tier']}")
    if r["url"]:
        print(f"   {r['url']}")
    if r["snippet"]:
        print(f"   → {r['snippet']}")
    print(f"   path: {r['path']}")
    print()


def _print_human(output: dict) -> None:
    if output["count"] == 0:
        print(f"No keyword results for '{output['query']}'.")
    else:
        if output["fallback_applied"]:
            note = f" (fallback: no file matched all tokens; relaxed to '{output['fallback_token']}')"
        else:
            note = ""
        print(f"Found {output['count']} result(s) for '{output['query']}'{note}:\n")
        for i, r in enumerate(output["results"], 1):
            _print_human_result(i, r)

    sem = output.get("semantic") or {}
    stale = sem.get("index_stale_days")
    if sem.get("available") and sem.get("results"):
        warn = f" — ⚠ index {stale}d stale, run 'qmd update'" if stale and stale > 7 else ""
        print(f"Semantically related ({sem['count']}){warn}:\n")
        for i, r in enumerate(sem["results"], 1):
            _print_human_result(i, r)
    elif sem.get("reason") and output.get("engine") != "keyword":
        print(f"(semantic unavailable: {sem['reason']})")


if __name__ == "__main__":
    sys.exit(main())
