import contextlib
import datetime
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

import vault_graph as vg  # noqa: E402

SAMPLE = os.path.join(REPO, "examples", "sample-vault")


def run(vault, cache, *args):
    """Run the CLI in-process. Returns (exit_code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = vg.main(["--vault", vault, "--cache-dir", cache, *args])
        except SystemExit as e:  # fail_note() and friends exit with codes 3/4
            code = e.code
    return code, out.getvalue(), err.getvalue()


class SampleVaultTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="vault-graph-test-")
        self.vault = os.path.join(self.tmp, "vault")
        self.cache = os.path.join(self.tmp, "cache")
        shutil.copytree(SAMPLE, self.vault)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def cli(self, *args):
        return run(self.vault, self.cache, *args)

    def index(self):
        with open(os.path.join(self.cache, "index.json"), encoding="utf-8") as f:
            return json.load(f)

    # ------------------------------------------------------------------ index

    def test_update_builds_index(self):
        code, out, _ = self.cli("update")
        self.assertEqual(code, 0)
        idx = self.index()
        self.assertEqual(len(idx["paths"]), 16)
        self.assertGreater(len(idx["edges"]), 20)
        self.assertEqual([b[1] for b in idx["broken"]], ["Competitor Teardown"])
        for name in ("index.json", "terms.json", "REPORT.md", "changes.jsonl"):
            self.assertTrue(os.path.exists(os.path.join(self.cache, name)), name)

    def test_notes_are_never_modified(self):
        before = {}
        for dp, _, fn in os.walk(self.vault):
            for f in fn:
                p = os.path.join(dp, f)
                with open(p, "rb") as fh:
                    before[p] = fh.read()
        for cmd in (["update"], ["search", "pricing"], ["audit"], ["report"], ["tasks"]):
            self.cli(*cmd)
        for p, data in before.items():
            with open(p, "rb") as fh:
                self.assertEqual(fh.read(), data, p)
        self.assertFalse(os.path.exists(os.path.join(self.vault, ".vault-graph")))

    def test_templates_do_not_produce_broken_links(self):
        self.cli("update")
        idx = self.index()
        sources = {idx["paths"][b[0]] for b in idx["broken"]}
        self.assertNotIn("Templates/Meeting.md", sources)

    def test_code_blocks_are_ignored(self):
        self.cli("update")
        idx = self.index()
        rec = idx["notes"]["Reference/Code Examples.md"]
        self.assertEqual([lk[0] for lk in rec["lk"]], ["Home"])  # not the links in code
        self.assertEqual([t[2] for t in rec["tf"]], ["A real task after the code blocks"])
        self.assertNotIn("not-a-tag", rec["tg"])
        self.assertEqual([s[1] for s in rec["sec"]], ["Code Examples", "After the fences"])

    def test_ambiguous_link_is_flagged(self):
        self.cli("update")
        idx = self.index()
        paths = idx["paths"]
        home = paths.index("Home.md")
        amb = [e for e in idx["edges"] if e[0] == home and e[4] == 1]
        self.assertEqual(len(amb), 1)
        self.assertTrue(paths[amb[0][1]].endswith("Notes.md"))

    def test_heading_anchors_and_aliases_resolve(self):
        self.cli("update")
        idx = self.index()
        paths = idx["paths"]
        brand = paths.index("Projects/Brand Guidelines.md")
        pricing = paths.index("Reference/Pricing Model.md")
        self.assertTrue(any(e[0] == brand and e[1] == pricing for e in idx["edges"]))
        # "client: [[Acme Corp]]" in frontmatter is a link too
        web = paths.index("Projects/Website Redesign.md")
        acme = paths.index("Clients/Acme Corp.md")
        self.assertTrue(any(e[0] == web and e[1] == acme for e in idx["edges"]))

    # ----------------------------------------------------------------- search

    def test_search_ranks_the_right_note_first(self):
        code, out, _ = self.cli("search", "fixed fee pricing")
        self.assertEqual(code, 0)
        first = [line for line in out.splitlines() if line.startswith("[1]")][0]
        self.assertIn("Reference/Pricing Model.md", first)
        self.assertIn("Fixed fee", out)

    def test_search_matches_aliases(self):
        _, out, _ = self.cli("search", "ACME Corporation")
        first = [line for line in out.splitlines() if line.startswith("[1]")][0]
        self.assertIn("Clients/Acme Corp.md", first)

    def test_search_respects_budget(self):
        _, out, _ = self.cli("search", "project client pricing", "--budget", "40")
        self.assertIn("cut by the", out)

    def test_search_no_useful_terms(self):
        _, out, _ = self.cli("search", "the of and")
        self.assertIn("no useful terms", out)

    def test_search_nothing_found(self):
        _, out, _ = self.cli("search", "zzzqqqxxx")
        self.assertIn("Nothing found", out)

    # ------------------------------------------------------------- note/section

    def test_note_outline_and_links(self):
        code, out, _ = self.cli("note", "Pricing Model")
        self.assertEqual(code, 0)
        self.assertIn("Outline", out)
        self.assertIn("Fixed fee", out)
        self.assertIn("Linked from", out)
        self.assertIn("Community", out)

    def test_note_ambiguous_and_missing(self):
        code, out, _ = self.cli("note", "Notes")
        self.assertEqual(code, 3)
        self.assertIn("Clients/Notes.md", out)
        code, out, _ = self.cli("note", "Nonexistent Thing", "--exact")
        self.assertEqual(code, 4)

    def test_section_returns_only_that_section(self):
        code, out, _ = self.cli("section", "Pricing Model", "Hourly")
        self.assertEqual(code, 0)
        self.assertIn("Standard rate is 90", out)
        self.assertNotIn("Fixed fees are quoted", out)

    def test_section_lines_tail_and_numbers(self):
        _, out, _ = self.cli("section", "Pricing Model", "--lines", "1-3", "--numbers", "--all")
        self.assertIn("    1| ---", out)
        _, out, _ = self.cli("section", "Pricing Model", "--tail", "2")
        body = out.splitlines()[1:]
        self.assertEqual(len(body), 2)
        self.assertIn("change requests", body[-1].lower())

    def test_section_refuses_large_without_flag(self):
        _, out, _ = self.cli("section", "Pricing Model", "--limit", "10")
        self.assertIn("Large note", out)
        _, out, _ = self.cli("section", "Pricing Model", "Fixed fee", "--limit", "10")
        self.assertIn("Large section", out)
        self.assertIn("What a fixed fee includes", out)

    # ------------------------------------------------------------------- tasks

    def test_tasks_open_and_overdue(self):
        _, out, _ = self.cli("tasks")
        self.assertIn("Tasks open: 13", out)
        self.assertNotIn("Collect stakeholder interviews", out)  # done
        self.assertNotIn("Print flyers", out)  # cancelled
        self.assertNotIn("Not a task", out)  # inside code fence
        _, out, _ = self.cli("tasks", "--overdue")
        self.assertIn("Tasks open: 2", out)
        self.assertIn("OVERDUE", out)

    def test_tasks_until_and_match(self):
        _, out, _ = self.cli("tasks", "--until", "+30")
        self.assertIn("Tasks open: 2", out)  # only the two with past due dates
        _, out, _ = self.cli("tasks", "--match", "wireframes")
        self.assertIn("Send revised wireframes", out)
        self.assertNotIn("invoice number follows", out)

    def test_tasks_dataview_due_filters_refresh_old_index(self):
        path = os.path.join(self.vault, "Inbox", "Dataview Tasks.md")
        text = ("# Dataview Tasks\n"
                "- [ ] bracket [due:: 2020-01-01]\n"
                "- [ ] parens (due:: 2020-01-02)\n"
                "- [ ] future [due:: 2999-01-01]\n"
                "- [ ] scheduled \U0001F4C6 2020-01-01\n"
                "- [ ] started \U0001F6EB 2020-01-01\n")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        # A format-1 index did not extract double-colon dates. Keep its file
        # hashes and timestamps intact so only format invalidation refreshes it.
        self.cli("update")
        old_index = self.index()
        old_index["format"] = 1
        for task in old_index["notes"]["Inbox/Dataview Tasks.md"]["tf"]:
            task[4] = ""
        for args in (("--until", "2020-01-02"), ("--overdue",)):
            with self.subTest(args=args):
                with open(os.path.join(self.cache, "index.json"), "w", encoding="utf-8") as f:
                    json.dump(old_index, f)
                code, out, err = self.cli("tasks", *args, "--match", "Dataview Tasks")
                self.assertEqual(code, 0)
                self.assertEqual(err, "")
                self.assertIn("Tasks open: 2", out)
                self.assertIn("bracket", out)
                self.assertIn("parens", out)
                for excluded in ("future", "scheduled", "started"):
                    self.assertNotIn(excluded, out)
                self.assertEqual(self.index()["format"], vg.INDEX_FORMAT)
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), text)

    def test_tasks_done_flag(self):
        _, out, _ = self.cli("tasks", "--done", "--match", "Kickoff")
        self.assertIn("[x] Priya shares analytics access", out)

    def test_tasks_folders_setting(self):
        os.makedirs(self.cache, exist_ok=True)
        with open(os.path.join(self.cache, "config.json"), "w", encoding="utf-8") as f:
            json.dump({"tasks_folders": ["Projects/"]}, f)
        _, out, _ = self.cli("tasks")
        self.assertNotIn("invoice number follows", out)
        self.assertIn("Send revised wireframes", out)
        _, out, _ = self.cli("tasks", "--all")
        self.assertIn("invoice number follows", out)

    # ------------------------------------------------------------ audit / path

    def test_audit_reports_problems(self):
        _, out, _ = self.cli("audit")
        self.assertIn("[[Competitor Teardown]]", out)
        self.assertIn("Clients/Notes.md | Projects/Notes.md", out)
        self.assertIn("Inbox/Bad Frontmatter.md", out)
        self.assertIn("Orphan notes (no links in or out, templates excluded): 1", out)
        self.assertIn("Open tasks overdue by more than 14 days: 2", out)

    def test_path_between_notes(self):
        _, out, _ = self.cli("path", "Jo Martins", "Brand Guidelines")
        self.assertIn("Path (1 steps)", out)
        _, out, _ = self.cli("path", "Loose Thought", "Home")
        self.assertIn("No path", out)

    def test_report_mentions_hubs_and_giants(self):
        _, out, _ = self.cli("report")
        self.assertIn("Hub notes", out)
        self.assertIn("Giant notes", out)
        self.assertIn("Pricing Model", out)

    # ------------------------------------------------- incremental and config

    def test_incremental_update_and_changed(self):
        self.cli("update")
        _, out, _ = self.cli("update")
        self.assertIn("0 read", out)
        path = os.path.join(self.vault, "Reference", "Pricing Model.md")
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n## Discounts\nNonprofits get ten percent off.\n")
        _, out, _ = self.cli("update")
        self.assertIn("1 changed", out)
        _, out, _ = self.cli("changed")
        self.assertIn("~ Reference/Pricing Model.md", out)
        _, out, _ = self.cli("search", "nonprofits discount")
        first = [line for line in out.splitlines() if line.startswith("[1]")][0]
        self.assertIn("Pricing Model", first)

    def test_new_and_removed_notes(self):
        self.cli("update")
        with open(os.path.join(self.vault, "Inbox", "New.md"), "w", encoding="utf-8") as f:
            f.write("# New\nLinks to [[Home]].\n")
        os.remove(os.path.join(self.vault, "Inbox", "Loose Thought.md"))
        _, out, _ = self.cli("update")
        self.assertIn("1 new", out)
        self.assertIn("1 removed", out)

    def test_config_change_triggers_reindex(self):
        self.cli("update")
        _, out, _ = self.cli("update")
        self.assertIn("0 read", out)
        with open(os.path.join(self.cache, "config.json"), "w", encoding="utf-8") as f:
            json.dump({"language": "pt"}, f)
        _, out, _ = self.cli("update")
        self.assertIn("16 read", out)

    def test_ignore_folders_config(self):
        os.makedirs(self.cache, exist_ok=True)
        with open(os.path.join(self.cache, "config.json"), "w", encoding="utf-8") as f:
            json.dump({"ignore_folders": ["Inbox", "node_modules"]}, f)
        self.cli("update")
        self.assertEqual(len(self.index()["paths"]), 14)

    def test_recent(self):
        _, out, _ = self.cli("recent", "--days", "1")
        self.assertIn("Notes modified in the last 1 days: 16", out)


class ShrinkGuardTest(unittest.TestCase):
    def test_keeps_old_index_when_folder_looks_incomplete(self):
        tmp = tempfile.mkdtemp(prefix="vault-graph-shrink-")
        try:
            vault, cache = os.path.join(tmp, "v"), os.path.join(tmp, "c")
            os.makedirs(vault)
            for i in range(60):
                with open(os.path.join(vault, "n%d.md" % i), "w", encoding="utf-8") as f:
                    f.write("# Note %d\nSome text here for note number %d.\n" % (i, i))
            run(vault, cache, "update")
            for i in range(40):
                os.remove(os.path.join(vault, "n%d.md" % i))
            _, _, err = run(vault, cache, "update")
            self.assertIn("WARNING", err)
            with open(os.path.join(cache, "index.json"), encoding="utf-8") as f:
                self.assertEqual(len(json.load(f)["paths"]), 60)
            run(vault, cache, "update", "--force")
            with open(os.path.join(cache, "index.json"), encoding="utf-8") as f:
                self.assertEqual(len(json.load(f)["paths"]), 20)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class UnitTest(unittest.TestCase):
    def setUp(self):
        vg.configure(tempfile.gettempdir(), os.path.join(tempfile.gettempdir(), "vg-unit-cache"))

    def test_frontmatter_scalars_lists_and_problems(self):
        text = """---
title: Plain
tags: [a, "b c", d]
aliases:
  - One
  - Two
quoted: "has: colon"
bad: has: colon
empty:
---
# Body
"""
        fm, start, problems = vg.read_frontmatter(text.splitlines())
        self.assertEqual(fm["title"], "Plain")
        self.assertEqual(fm["tags"], ["a", "b c", "d"])
        self.assertEqual(fm["aliases"], ["One", "Two"])
        self.assertEqual(fm["quoted"], "has: colon")
        self.assertEqual(fm["empty"], [])
        self.assertEqual(len(problems), 1)
        self.assertIn("bad", problems[0])
        self.assertEqual(start, 10)

    def test_no_frontmatter(self):
        self.assertEqual(vg.read_frontmatter(["# Title", "text"]), ({}, 0, []))
        self.assertEqual(vg.read_frontmatter(["---", "never closed"]), ({}, 0, []))

    def test_extract_tags_ignore_hex_colours_and_headings(self):
        rec, _ = vg.extract("x.md", "# Heading #notatag\nA #real-tag and colour #1F4E79 and #123.\n")
        self.assertEqual(set(rec["tg"]), {"real-tag"})

    def test_extract_task_due_dates(self):
        rec, _ = vg.extract("x.md", "- [ ] pay rent due 2026-03-01\n- [x] done\n- [/] doing \U0001F4C5 2026-04-02\n- [-] dropped\n")
        self.assertEqual([(t[1], t[4]) for t in rec["tf"]],
                         [(1, "2026-03-01"), (0, ""), (1, "2026-04-02"), (0, "")])

    def test_extract_task_dataview_due_dates(self):
        for field in ("[due:: 2026-03-01]", "(due:: 2026-03-01)",
                      "[DUE::2026-03-01]", "(due::2026-03-01)"):
            with self.subTest(field=field):
                rec, _ = vg.extract("x.md", "- [ ] task " + field + "\n")
                self.assertEqual(rec["tf"][0][4], "2026-03-01")
        for field in ("due 2026-03-01", "due: 2026-03-01",
                      "deadline 2026-03-01", "\U0001F4C5 2026-03-01"):
            with self.subTest(field=field):
                rec, _ = vg.extract("x.md", "- [ ] task " + field + "\n")
                self.assertEqual(rec["tf"][0][4], "2026-03-01")
        for field in ("\U0001F4C6 2026-03-01", "\U0001F6EB 2026-03-01"):
            with self.subTest(field=field):
                rec, _ = vg.extract("x.md", "- [ ] task " + field + "\n")
                self.assertEqual(rec["tf"][0][4], "")

    def test_section_ranges_nest_correctly(self):
        rec, _ = vg.extract("x.md", "# A\ntext\n## B\nmore\n## C\nend\n# D\nlast\n")
        ranges = {s[1]: (s[2], s[3]) for s in rec["sec"]}
        self.assertEqual(ranges["A"], (1, 6))
        self.assertEqual(ranges["B"], (3, 4))
        self.assertEqual(ranges["C"], (5, 6))
        self.assertEqual(ranges["D"], (7, 8))

    def test_wikilink_forms(self):
        rec, _ = vg.extract("x.md", "[[Note]] [[Note#Heading|alias]] ![[Embed]] [[folder/Deep Note]] [[#Self]]\n")
        got = [(lk[0], lk[1], lk[2]) for lk in rec["lk"]]
        self.assertEqual(got, [("Note", "", "l"), ("Note", "Heading", "l"), ("Embed", "", "e"),
                               ("folder/Deep Note", "", "l"), ("", "Self", "l")])

    def test_stemmers(self):
        vg.CFG["language"] = "en"
        self.assertEqual(vg.stem("queries"), "query")
        self.assertEqual(vg.stem("templates"), "template")
        self.assertEqual(vg.stem("boxes"), "box")
        self.assertEqual(vg.stem("class"), "class")
        vg.CFG["language"] = "pt"
        self.assertEqual(vg.stem("constitucionais"), vg.stem("constitucional"))
        vg.CFG["language"] = "en"

    def test_louvain_finds_two_communities(self):
        def clique(nodes):
            return {u: {v: 1.0 for v in nodes if v != u} for u in nodes}
        adj = {}
        adj.update(clique([0, 1, 2, 3]))
        adj.update(clique([4, 5, 6, 7]))
        adj[3][4] = 1.0
        adj[4][3] = 1.0
        comm = vg.louvain(adj)
        self.assertEqual(len(set(comm.values())), 2)
        self.assertEqual(len({comm[i] for i in (0, 1, 2, 3)}), 1)
        self.assertEqual(len({comm[i] for i in (4, 5, 6, 7)}), 1)
        self.assertEqual(set(comm), set(range(8)))

    def test_louvain_handles_empty_and_edgeless_graphs(self):
        self.assertEqual(vg.louvain({}), {})
        self.assertEqual(set(vg.louvain({1: {}, 2: {}})), {1, 2})

    def test_date_arg(self):
        today = datetime.date.today()
        self.assertEqual(vg.date_arg("today"), today)
        self.assertEqual(vg.date_arg("tomorrow"), today + datetime.timedelta(days=1))
        self.assertEqual(vg.date_arg("+7"), today + datetime.timedelta(days=7))
        self.assertEqual(vg.date_arg("2026-05-06"), datetime.date(2026, 5, 6))

    def test_output_budget(self):
        o = vg.Output(1)  # 3 characters of budget
        self.assertTrue(o.add("ab"))
        self.assertFalse(o.add("this does not fit"))
        self.assertEqual(o.cut, 1)

    def test_token_formatting(self):
        self.assertEqual(vg.fmt_tokens(950), "~950 tok")
        self.assertEqual(vg.fmt_tokens(2500), "~2.5k tok")


class CliTest(unittest.TestCase):
    def test_no_command_prints_help(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(vg.main([]), 0)
        self.assertIn("search", out.getvalue())

    def test_missing_vault_folder(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = vg.main(["--vault", os.path.join(tempfile.gettempdir(), "does-not-exist-xyz"), "update"])
        self.assertEqual(code, 2)
        self.assertIn("not found", err.getvalue())


if __name__ == "__main__":
    unittest.main()
