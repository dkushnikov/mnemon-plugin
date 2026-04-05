#!/usr/bin/env python3
"""search.py — search the Mnemon knowledge library.

Called by the mnemon:source-search skill. Reads the vault path and search
provider from ~/Mnemon/mnemon.yaml (via mnemon-config.sh conventions), then
searches Sources/extract.md (and optionally source.md and Synthesis/) for
sources matching the query. Results are ranked by rating DESC then date DESC.

Design notes
------------
- Tokenization: query is split into lowercase tokens >=2 chars long. We
  find files containing ALL tokens (AND semantics). This is what you want
  99% of the time — "karpathy knowledge bases" should match files that
  contain all three words, not the literal phrase.
- Fallback: if no file matches all tokens, we relax to just the longest
  (most specific) token and mark results with a `fallback` flag so the
  caller can tell the user we broadened the search.
- Frontmatter parsing: we parse only the subset Mnemon extracts use —
  scalar strings (quoted or not), integers, inline lists [a, b, c]. No
  PyYAML dependency — keeps the script portable across any machine with
  python3.
- Output: JSON to stdout by default. The skill reads the JSON and formats
  it for the user. If you want human output for ad-hoc CLI use, pass
  --human.
"""

from __future__ import annotations

import argparse
import json
import os
import re
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


def match_file(path: Path, tokens: list[str]) -> tuple[bool, str | None]:
    """Check whether the file contains ALL tokens. Return (matched, snippet)."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False, None
    text_lower = text.lower()
    if not all(t in text_lower for t in tokens):
        return False, None
    return True, _extract_snippet(text, tokens)


def _extract_snippet(text: str, tokens: list[str], width: int = 200) -> str | None:
    """Return the first non-frontmatter line containing the longest token."""
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
    results: list[dict] = []
    for path, tier in targets:
        matched, snippet = match_file(path, tokens)
        if matched:
            results.append(assemble_result(path, tier, snippet, vault))

    fallback_applied = False
    if not results and len(tokens) > 1:
        # Relax to longest token only
        seed = [max(tokens, key=len)]
        for path, tier in targets:
            matched, snippet = match_file(path, seed)
            if matched:
                results.append(assemble_result(path, tier, snippet, vault))
        fallback_applied = True

    results = dedupe_by_folder(results)
    results = apply_domain_filter(results, args.domain)
    results = rank_results(results)
    results = results[: args.limit]

    output = {
        "query": args.query,
        "tokens": tokens,
        "fallback_applied": fallback_applied,
        "count": len(results),
        "results": results,
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


def _print_human(output: dict) -> None:
    if output["count"] == 0:
        print(f"No sources found for '{output['query']}'.")
        return
    note = " (fallback: relaxed to longest token)" if output["fallback_applied"] else ""
    print(f"Found {output['count']} result(s) for '{output['query']}'{note}:\n")
    for i, r in enumerate(output["results"], 1):
        rating = f"rating {r['rating']}" if r["rating"] is not None else "unrated"
        domains = ", ".join(r["domains"]) if r["domains"] else "—"
        print(f"{i}. {r['title']}")
        if r["author"]:
            print(f"   by {r['author']}")
        print(f"   {rating} · {r['created']} · domains: {domains} · tier: {r['tier']}")
        if r["url"]:
            print(f"   {r['url']}")
        if r["snippet"]:
            print(f"   → {r['snippet']}")
        print(f"   path: {r['path']}")
        print()


if __name__ == "__main__":
    sys.exit(main())
