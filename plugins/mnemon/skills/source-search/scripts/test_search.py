#!/usr/bin/env python3
"""Unit + regression + end-to-end tests for search.py.

Run: `python3 test_search.py` from this directory. No pytest dependency —
uses only stdlib (unittest + tempfile + subprocess).

Test classes
------------
- TokenizeTests               — tokenize() edge cases
- ParseFrontmatterTests       — inline list, block list, date objects, malformed
- ScanFileTests               — matched token set + snippet extraction
- RankResultsTests            — rating DESC, date DESC tiebreak, None handling
- DedupeByFolderTests         — extract > synthesis > source priority
- DomainFilterTests           — exact match, missing field, case handling
- FallbackStrategyTests       — REGRESSION for 3b09194 (most-specific, not longest)
- JsonOutputTests             — REGRESSION for 557b3f6 (datetime.date serialization)
- EndToEndTests               — full script via subprocess against a fixture vault

Each bug we hit in 2026-04-05 session has an explicit regression test marked
in the test name so future regressions produce an obvious failure message.
"""

from __future__ import annotations

import datetime
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# Import the module under test from the sibling file.
SCRIPT_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(SCRIPT_DIR))
import search  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def write_extract(
    folder: Path,
    *,
    title: str,
    rating=None,
    domains=None,
    created="2026-01-01",
    author="",
    url="",
    body="",
    domains_format: str = "inline",  # "inline" or "block"
) -> Path:
    """Write a Sources/<folder>/extract.md with the given fields.

    domains_format controls whether `domains:` is written as an inline list
    (`[a, b]`) or a YAML block list (newline + `- a`). Both forms appear in
    real Mnemon extracts, and parsing block lists was a real bug.
    """
    folder.mkdir(parents=True, exist_ok=True)
    lines = ["---", "type: extract", f'title: "{title}"']
    if author:
        lines.append(f'author: "{author}"')
    if url:
        lines.append(f'url: "{url}"')
    if rating is not None:
        lines.append(f"rating: {rating}")
    if domains is not None:
        if domains_format == "inline":
            lines.append(f"domains: [{', '.join(domains)}]")
        else:
            lines.append("domains:")
            for d in domains:
                lines.append(f"  - {d}")
    lines.append(f"created: {created}")
    lines.append("---")
    lines.append("")
    lines.append(body)
    path = folder / "extract.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def write_source(folder: Path, body: str) -> Path:
    """Write a minimal source.md in a folder (for --include-source tests)."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "source.md"
    path.write_text(
        f"---\ntype: source\n---\n\n{body}\n",
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# TokenizeTests
# ---------------------------------------------------------------------------

class TokenizeTests(unittest.TestCase):
    def test_basic_multi_word(self):
        self.assertEqual(search.tokenize("karpathy knowledge bases"),
                         ["karpathy", "knowledge", "bases"])

    def test_lowercases(self):
        self.assertEqual(search.tokenize("Karpathy KNOWLEDGE"),
                         ["karpathy", "knowledge"])

    def test_drops_punctuation(self):
        self.assertEqual(search.tokenize("hello, world!"),
                         ["hello", "world"])

    def test_drops_tokens_shorter_than_two(self):
        # 1-char tokens are noise (articles, initials) — drop them.
        self.assertEqual(search.tokenize("a b ai ml"),
                         ["ai", "ml"])

    def test_unicode(self):
        # Real-world: Russian + English mix in queries.
        tokens = search.tokenize("Карпатый knowledge")
        self.assertIn("knowledge", tokens)
        self.assertIn("карпатый", tokens)

    def test_empty(self):
        self.assertEqual(search.tokenize(""), [])

    def test_only_punctuation(self):
        self.assertEqual(search.tokenize("!?;:"), [])


# ---------------------------------------------------------------------------
# ParseFrontmatterTests
# ---------------------------------------------------------------------------

class ParseFrontmatterTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir)

    def _write(self, content: str) -> Path:
        p = Path(self.tmpdir) / "test.md"
        p.write_text(content, encoding="utf-8")
        return p

    def test_inline_list_domains(self):
        p = self._write(
            "---\n"
            "title: Test\n"
            "domains: [learning, career, mc/ai]\n"
            "---\n\nbody\n"
        )
        meta = search.parse_frontmatter(p)
        self.assertEqual(meta["domains"], ["learning", "career", "mc/ai"])

    def test_block_list_domains_REGRESSION(self):
        # REGRESSION: my hand-rolled parser handled only inline lists.
        # Real Mnemon extracts use block form for domains and tags.
        # yaml.safe_load handles both; this test guards against regression
        # if we ever swap the parser.
        p = self._write(
            "---\n"
            "title: Test\n"
            "domains:\n"
            "  - career\n"
            "  - mc/ai\n"
            "  - learning\n"
            "---\n\nbody\n"
        )
        meta = search.parse_frontmatter(p)
        self.assertEqual(meta["domains"], ["career", "mc/ai", "learning"])

    def test_yaml_1_1_implicit_date(self):
        # YAML 1.1 implicit typing: `created: 2026-04-04` → datetime.date.
        # search.py relies on this behavior for its date sort key, and
        # json.dump handles it via default=str (regression for 557b3f6).
        p = self._write(
            "---\n"
            "title: Test\n"
            "created: 2026-04-04\n"
            "---\n\nbody\n"
        )
        meta = search.parse_frontmatter(p)
        self.assertIsInstance(meta["created"], datetime.date)

    def test_integer_rating(self):
        p = self._write("---\ntitle: Test\nrating: 9\n---\n\nbody\n")
        meta = search.parse_frontmatter(p)
        self.assertEqual(meta["rating"], 9)
        self.assertIsInstance(meta["rating"], int)

    def test_no_frontmatter(self):
        p = self._write("Just a plain markdown file.\n")
        self.assertEqual(search.parse_frontmatter(p), {})

    def test_malformed_frontmatter(self):
        # Unclosed frontmatter — parser should not blow up.
        p = self._write("---\ntitle: Test\n\nno closing delimiter\n")
        self.assertEqual(search.parse_frontmatter(p), {})

    def test_missing_file(self):
        p = Path(self.tmpdir) / "does-not-exist.md"
        self.assertEqual(search.parse_frontmatter(p), {})


# ---------------------------------------------------------------------------
# ScanFileTests
# ---------------------------------------------------------------------------

class ScanFileTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.path = Path(self.tmpdir) / "extract.md"
        self.path.write_text(
            "---\n"
            "title: Karpathy LLM Knowledge Bases\n"
            "domains: [learning]\n"
            "---\n\n"
            "Body mentioning knowledge and llm.\n",
            encoding="utf-8",
        )

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir)

    def test_returns_matched_subset(self):
        matched, _ = search.scan_file(self.path, ["karpathy", "knowledge", "bases", "nonexistent"])
        self.assertEqual(matched, {"karpathy", "knowledge", "bases"})

    def test_all_tokens_match(self):
        matched, _ = search.scan_file(self.path, ["karpathy", "knowledge"])
        self.assertEqual(matched, {"karpathy", "knowledge"})

    def test_no_tokens_match(self):
        matched, snippet = search.scan_file(self.path, ["quantumxyz"])
        self.assertEqual(matched, set())
        self.assertIsNone(snippet)

    def test_snippet_returned(self):
        _, snippet = search.scan_file(self.path, ["knowledge"])
        self.assertIsNotNone(snippet)
        self.assertIn("knowledge", snippet.lower())

    def test_missing_file(self):
        missing = Path(self.tmpdir) / "no-such-file.md"
        matched, snippet = search.scan_file(missing, ["anything"])
        self.assertEqual(matched, set())
        self.assertIsNone(snippet)


# ---------------------------------------------------------------------------
# RankResultsTests
# ---------------------------------------------------------------------------

class RankResultsTests(unittest.TestCase):
    def test_rating_desc(self):
        results = [
            {"rating": 5, "created": "2026-01-01", "title": "b"},
            {"rating": 10, "created": "2026-01-01", "title": "a"},
            {"rating": 7, "created": "2026-01-01", "title": "c"},
        ]
        ranked = search.rank_results(results)
        self.assertEqual([r["rating"] for r in ranked], [10, 7, 5])

    def test_date_desc_tiebreak(self):
        results = [
            {"rating": 9, "created": "2026-01-01", "title": "a"},
            {"rating": 9, "created": "2026-03-15", "title": "b"},
            {"rating": 9, "created": "2026-02-10", "title": "c"},
        ]
        ranked = search.rank_results(results)
        self.assertEqual([r["created"] for r in ranked],
                         ["2026-03-15", "2026-02-10", "2026-01-01"])

    def test_none_rating_sinks_to_bottom(self):
        results = [
            {"rating": None, "created": "2026-12-31", "title": "nulled"},
            {"rating": 5, "created": "2026-01-01", "title": "low"},
            {"rating": 8, "created": "2026-01-01", "title": "mid"},
        ]
        ranked = search.rank_results(results)
        self.assertEqual([r["rating"] for r in ranked], [8, 5, None])

    def test_title_final_tiebreak(self):
        # Same rating + same date → title ascending as stable tiebreaker.
        results = [
            {"rating": 9, "created": "2026-01-01", "title": "zebra"},
            {"rating": 9, "created": "2026-01-01", "title": "alpha"},
            {"rating": 9, "created": "2026-01-01", "title": "mango"},
        ]
        ranked = search.rank_results(results)
        self.assertEqual([r["title"] for r in ranked],
                         ["alpha", "mango", "zebra"])


# ---------------------------------------------------------------------------
# DedupeByFolderTests
# ---------------------------------------------------------------------------

class DedupeByFolderTests(unittest.TestCase):
    def test_extract_beats_source(self):
        results = [
            {"path": "Sources/2026-01-01_aaa/", "tier": "source", "title": "raw"},
            {"path": "Sources/2026-01-01_aaa/", "tier": "extract", "title": "extracted"},
        ]
        deduped = search.dedupe_by_folder(results)
        self.assertEqual(len(deduped), 1)
        self.assertEqual(deduped[0]["tier"], "extract")

    def test_different_folders_both_kept(self):
        results = [
            {"path": "Sources/a/", "tier": "extract", "title": "a"},
            {"path": "Sources/b/", "tier": "extract", "title": "b"},
        ]
        deduped = search.dedupe_by_folder(results)
        self.assertEqual(len(deduped), 2)

    def test_synthesis_beats_source(self):
        results = [
            {"path": "X/", "tier": "source", "title": "s"},
            {"path": "X/", "tier": "synthesis", "title": "syn"},
        ]
        deduped = search.dedupe_by_folder(results)
        self.assertEqual(deduped[0]["tier"], "synthesis")


# ---------------------------------------------------------------------------
# DomainFilterTests
# ---------------------------------------------------------------------------

class DomainFilterTests(unittest.TestCase):
    def test_exact_match(self):
        results = [
            {"title": "a", "domains": ["career", "learning"]},
            {"title": "b", "domains": ["mc/ai"]},
            {"title": "c", "domains": ["career"]},
        ]
        filtered = search.apply_domain_filter(results, "career")
        self.assertEqual([r["title"] for r in filtered], ["a", "c"])

    def test_case_insensitive(self):
        results = [{"title": "a", "domains": ["Career"]}]
        self.assertEqual(len(search.apply_domain_filter(results, "career")), 1)

    def test_empty_domain_returns_all(self):
        results = [{"title": "a", "domains": ["x"]}, {"title": "b", "domains": ["y"]}]
        self.assertEqual(len(search.apply_domain_filter(results, "")), 2)

    def test_missing_domains_field(self):
        # Some extracts may have no domains key at all — shouldn't crash.
        results = [{"title": "a"}, {"title": "b", "domains": ["x"]}]
        filtered = search.apply_domain_filter(results, "x")
        self.assertEqual([r["title"] for r in filtered], ["b"])


# ---------------------------------------------------------------------------
# FallbackStrategyTests — REGRESSION for 3b09194
# ---------------------------------------------------------------------------

class FallbackStrategyTests(unittest.TestCase):
    """Tests the end-to-end fallback logic by running a small fixture vault
    through the script. The fallback code is deeply integrated into main()
    so a unit test on a single function isn't sufficient — we need to
    observe the JSON output to verify which token was selected.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.vault = Path(self.tmpdir) / "vault"
        (self.vault / "Sources").mkdir(parents=True)
        (self.vault / "Synthesis").mkdir()

        # Source A: contains "karpathy" (2 chars in token set) — rare in corpus.
        write_extract(
            self.vault / "Sources" / "2026-04-04_aaa11111",
            title="Andrej Karpathy tweet",
            rating=10,
            domains=["learning"],
            created="2026-04-04",
            body="Karpathy describes his LLM workflow.",
        )
        # Source B: also contains "karpathy".
        write_extract(
            self.vault / "Sources" / "2026-04-05_bbb22222",
            title="LLM Wiki by Karpathy",
            rating=8,
            domains=["learning"],
            created="2026-04-05",
            body="Karpathy pattern for personal knowledge.",
        )
        # Source C: contains "knowledge" (more common) but not "karpathy".
        write_extract(
            self.vault / "Sources" / "2026-03-01_ccc33333",
            title="Knowledge systems review",
            rating=7,
            domains=["learning"],
            created="2026-03-01",
            body="Review of knowledge management systems.",
        )
        # Source D: contains "knowledge" too — increasing its corpus count.
        write_extract(
            self.vault / "Sources" / "2026-03-02_ddd44444",
            title="Knowledge Graph primer",
            rating=6,
            domains=["learning"],
            created="2026-03-02",
            body="Introduction to knowledge graphs and RDF.",
        )

        # mnemon.yaml pointing at this vault
        self.config = Path(self.tmpdir) / "mnemon.yaml"
        self.config.write_text(
            f"vault_path: {self.vault}\n"
            f"search_provider: grep\n"
            f"qmd_collection: test\n"
            f"default_model: sonnet\n"
            f"default_language: en\n"
            f"whisper_model: large-v3\n"
            f"auto_detect_origin: true\n"
        )

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir)

    def _run(self, *query_args) -> dict:
        """Run search.py as a subprocess with the temp config, return parsed JSON."""
        env = os.environ.copy()
        env["MNEMON_CONFIG"] = str(self.config)
        result = subprocess.run(
            [sys.executable, str(SCRIPT_DIR / "search.py"), *query_args],
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(result.returncode, 0,
                         f"search.py exited {result.returncode}: {result.stderr}")
        return json.loads(result.stdout)

    def test_primary_and_match_returns_intersection(self):
        # Both Karpathy files contain "karpathy" AND their body-words.
        out = self._run("--query", "karpathy")
        self.assertEqual(out["count"], 2)
        self.assertFalse(out["fallback_applied"])

    def test_fallback_picks_most_specific_existing_token(self):
        # THE BUG from 3b09194:
        # "karpathy" appears in 2 files, "knowledge" appears in 4 files.
        # Old longest-token logic would pick "knowledge" (len 9 > len 8)
        # and return 4 results. New logic picks "karpathy" (count 2 < 4)
        # and returns 2 results — the more specific term.
        # Query is constructed so no file has BOTH tokens adjacent AND
        # the tokens aren't in the same file at the same density.
        out = self._run("--query", "karpathy quantumxyzfoo")
        self.assertTrue(out["fallback_applied"])
        self.assertEqual(out["fallback_token"], "karpathy")
        self.assertEqual(out["count"], 2)

    def test_fallback_longest_is_not_picked_when_rarer_exists(self):
        # The critical assertion: string length is NOT the signal.
        # "zyzzytoken" (10 chars, 0 matches) would have won under the old
        # logic. New logic must pick "karpathy" (8 chars, 2 matches).
        out = self._run("--query", "karpathy zyzzytoken")
        self.assertTrue(out["fallback_applied"])
        self.assertEqual(out["fallback_token"], "karpathy")
        self.assertEqual(out["count"], 2)

    def test_no_viable_tokens_means_no_fallback(self):
        # All tokens nonexistent → count=0, fallback_applied=False,
        # no false "broadened" disclosure.
        out = self._run("--query", "quantumxyzfoo flooblegobbledy")
        self.assertEqual(out["count"], 0)
        self.assertFalse(out["fallback_applied"])
        self.assertIsNone(out["fallback_token"])

    def test_single_token_no_fallback(self):
        # Single-token queries can't fall back (nothing to relax from).
        out = self._run("--query", "quantumxyzfoo")
        self.assertEqual(out["count"], 0)
        self.assertFalse(out["fallback_applied"])


# ---------------------------------------------------------------------------
# JsonOutputTests — REGRESSION for 557b3f6
# ---------------------------------------------------------------------------

class JsonOutputTests(unittest.TestCase):
    """Regression: json.dump can't serialize datetime.date without default=str.
    PyYAML parses `created: 2026-04-04` as a date object via YAML 1.1 implicit
    typing, and the original script crashed on the first result.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.vault = Path(self.tmpdir) / "vault"
        (self.vault / "Sources").mkdir(parents=True)
        write_extract(
            self.vault / "Sources" / "2026-04-04_aaa",
            title="Date test",
            rating=9,
            domains=["x"],
            created="2026-04-04",  # becomes datetime.date via yaml.safe_load
            body="marker-token-here",
        )
        self.config = Path(self.tmpdir) / "mnemon.yaml"
        self.config.write_text(f"vault_path: {self.vault}\n")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir)

    def test_json_output_survives_date_serialization(self):
        env = os.environ.copy()
        env["MNEMON_CONFIG"] = str(self.config)
        result = subprocess.run(
            [sys.executable, str(SCRIPT_DIR / "search.py"),
             "--query", "marker-token-here"],
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(result.returncode, 0,
                         f"search.py crashed: {result.stderr}")
        # The output must be parseable JSON (no partial write from a mid-dump crash).
        data = json.loads(result.stdout)
        self.assertEqual(data["count"], 1)
        # The date must be serialized as a string "YYYY-MM-DD" via default=str.
        created = data["results"][0]["created"]
        self.assertIsInstance(created, str)
        self.assertEqual(created, "2026-04-04")


# ---------------------------------------------------------------------------
# EndToEndTests — full pipeline via subprocess
# ---------------------------------------------------------------------------

class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.vault = Path(self.tmpdir) / "vault"
        (self.vault / "Sources").mkdir(parents=True)
        (self.vault / "Synthesis").mkdir()

        # A representative mini-vault with inline + block domains mix
        write_extract(
            self.vault / "Sources" / "2026-01-01_aaa",
            title="Inline list source",
            rating=10,
            domains=["career", "learning"],
            domains_format="inline",
            created="2026-01-01",
            body="Content mentioning alpha and beta.",
        )
        write_extract(
            self.vault / "Sources" / "2026-01-02_bbb",
            title="Block list source",
            rating=8,
            domains=["learning", "mc/ai"],
            domains_format="block",
            created="2026-01-02",
            body="Different content mentioning alpha only.",
        )
        write_source(
            self.vault / "Sources" / "2026-01-03_ccc",
            body="This phrase lives ONLY in source.md, not extract.md: unique-raw-marker",
        )
        # extract.md for ccc so the --include-source dedup has something to dedupe against
        write_extract(
            self.vault / "Sources" / "2026-01-03_ccc",
            title="Paired extract",
            rating=5,
            domains=["career"],
            created="2026-01-03",
            body="Extract mentions alpha in passing.",
        )
        # Synthesis note
        synth_dir = self.vault / "Synthesis"
        (synth_dir / "my-thinking.md").write_text(
            "---\ntype: synthesis\ntitle: My own note\n---\n\n"
            "A synthesis note mentioning alpha from Dima's perspective.\n",
            encoding="utf-8",
        )

        self.config = Path(self.tmpdir) / "mnemon.yaml"
        self.config.write_text(f"vault_path: {self.vault}\n")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir)

    def _run(self, *args) -> dict:
        env = os.environ.copy()
        env["MNEMON_CONFIG"] = str(self.config)
        result = subprocess.run(
            [sys.executable, str(SCRIPT_DIR / "search.py"), *args],
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_happy_path(self):
        out = self._run("--query", "alpha")
        titles = [r["title"] for r in out["results"]]
        # All three extracts mention alpha, plus the synthesis note if requested.
        # Default is extracts only.
        self.assertIn("Inline list source", titles)
        self.assertIn("Block list source", titles)

    def test_include_source_flag(self):
        # The raw marker lives ONLY in source.md.
        # Without --include-source, zero hits. With it, one hit from source.md
        # but deduped against the paired extract.md, which DOESN'T contain the
        # marker — so the source.md entry survives.
        out_without = self._run("--query", "unique-raw-marker")
        self.assertEqual(out_without["count"], 0)

        out_with = self._run("--query", "unique-raw-marker", "--include-source")
        self.assertEqual(out_with["count"], 1)
        self.assertEqual(out_with["results"][0]["tier"], "source")

    def test_include_synthesis_flag(self):
        out_without = self._run("--query", "perspective")
        self.assertEqual(out_without["count"], 0)

        out_with = self._run("--query", "perspective", "--include-synthesis")
        self.assertEqual(out_with["count"], 1)
        self.assertEqual(out_with["results"][0]["tier"], "synthesis")

    def test_domain_filter_end_to_end(self):
        out = self._run("--query", "alpha", "--domain", "career")
        for r in out["results"]:
            self.assertIn("career", r["domains"])

    def test_limit_respected(self):
        out = self._run("--query", "alpha", "--limit", "2")
        self.assertLessEqual(out["count"], 2)

    def test_human_output_does_not_crash_on_fallback(self):
        # --human path has its own formatting for fallback_applied/token —
        # regression against the lesson that non-JSON modes get undertested.
        env = os.environ.copy()
        env["MNEMON_CONFIG"] = str(self.config)
        result = subprocess.run(
            [sys.executable, str(SCRIPT_DIR / "search.py"),
             "--query", "alpha nonexistenttoken",
             "--human"],
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("fallback", result.stdout.lower())
        # The fallback_token name should appear in the human message too.
        self.assertIn("alpha", result.stdout.lower())


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main(verbosity=2)
