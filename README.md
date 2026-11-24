# vault-graph

A local knowledge graph for Markdown and Obsidian vaults, built for AI agents.

An agent that answers questions about a vault has two bad options: read everything (thousands of
tokens per question) or grep blindly. `vault-graph` indexes the vault **without calling any LLM** and
answers each question with the smallest useful excerpt: which notes match, which section to open, and
only those lines.

- **Zero dependencies.** One Python file, standard library only, Python 3.8+.
- **No LLM, no network.** Links, frontmatter, headings, tags and tasks are already explicit in an
  Obsidian vault, so extraction is deterministic and free.
- **Incremental.** Files are re-read only when their hash changes. Every command refreshes the index
  first, so it is never stale.
- **Read-only.** Your notes are never modified. The only files written live in a cache folder
  (`<vault>/.vault-graph/` by default).
- **Token-budgeted answers.** Every command cuts its output at a budget (default ~1,200 tokens).

On the author's own vault (828 notes, roughly 3.9M tokens of text by the tool's estimate) a typical
`search` answers in about 1.2k tokens and a full re-index takes about 20 seconds; later updates only
read what changed.

## Install

```bash
git clone https://github.com/DiegoCastro95/vault-graph
cd vault-graph
python vault_graph.py --help          # run it directly, or:
pip install .                         # adds a `vault-graph` command
```

## Quick start

Run from the vault root, or pass `--vault PATH`:

```bash
vault-graph search "how do we price a fixed fee project"
```

```text
Search: how do we price a fixed fee project  (terms: price, fix, fee, project)

[1] Reference/Pricing Model.md  (~329 tok · 29 lines · reference · active)
    summary: How Fieldwork Studio prices work, covering hourly rates, fixed fees, retainers and change requests.
    sections: "What a fixed fee includes" L20-22 (~30 tok) · "Fixed fee" L15-22 (~104 tok)
    L16: Fixed fees are quoted from a scoped list of deliverables. The price is the estimated hours times the
    linked to: Invoice Checklist, Brand Guidelines, Website Redesign, Northwind Traders, Home, 2026-01-19 Design Review
```

Then read only the part you need:

```bash
vault-graph section "Pricing Model" "Hourly"
```

```text
Reference/Pricing Model.md  ›  "Hourly"  L11-14 (~53 tok)
## Hourly
Standard rate is 90 per hour for design and 110 per hour for front-end work. Time is tracked in
15-minute blocks. Used for exploratory work such as the [[Brand Guidelines]].
```

Try it on the bundled demo vault: `vault-graph --vault examples/sample-vault --cache-dir /tmp/vg search pricing`.

## Commands

| Command | What it does |
|---|---|
| `search "question"` | Ranked notes and sections. Scores name/alias, summary, tags, headings, frontmatter, folder and body text (BM25-like), then shows matching sections and excerpts. |
| `note "Name"` | One note: summary, properties, outline with line ranges and token sizes, links in and out, community. |
| `section "Note" "Heading"` | Prints only that section. Large sections list their subsections instead; `--lines A-B`, `--tail N` (to append at the end of a note) and `--numbers` are available. |
| `tasks` | Open `- [ ]` tasks across the vault, grouped by heading. Filters: `--match`, `--folder`, `--until tomorrow`, `--overdue`, `--done`. Due dates are read from `due 2026-03-01`, `deadline 2026-03-01`, `📅 2026-03-01`, `[due:: 2026-03-01]` or `(due:: 2026-03-01)`. |
| `recent --days 7` | Notes modified recently. |
| `changed` | What each index update added, changed or removed. |
| `path "A" "B"` | Shortest chain of links between two notes. |
| `audit` | Broken links, duplicate note names, invalid frontmatter, overdue tasks, notes at the root, orphans. |
| `report` | Overview: folders, hub notes, communities (Louvain), bridges between folders, giant notes, top tags. |
| `update [--force]` | Re-index. All other commands already do this; `--force` re-reads everything. |

Global options: `--vault PATH`, `--cache-dir PATH`. Per command: `--budget N` (approximate tokens) and
`--fast` (skip the refresh and use the index as is).

Exit codes: `3` = ambiguous note name, `4` = no such note (`note NAME --exact` is a cheap "does this
note exist?" check for scripts and agents).

## Configuration

Optional. Create `<cache-dir>/config.json` (by default `<vault>/.vault-graph/config.json`) with any of:

| Key | Default | Meaning |
|---|---|---|
| `language` | `"en"` | `"en"`, `"pt"` or `"none"`. Picks stop words and a light stemmer. |
| `ignore_folders` | `["node_modules"]` | Skipped entirely. Folders starting with `.` are always skipped. |
| `template_folders` | `["Templates/", "templates/", "_templates/"]` | Not reported as broken links or orphans, low search weight. |
| `low_weight_folders` | `{}` | `{"path fragment": 0.5}` pushes noisy folders down in search. |
| `tasks_folders` | `[]` | If set, `tasks` only looks here unless `--folder` or `--all` is given. |
| `link_properties` | `["area", "areas", "project", "projects"]` | Frontmatter keys whose values count as links to notes. |
| `summary_properties` | `["summary", "description", "abstract"]` | Frontmatter keys used as the one-line summary. |
| `require_summary_in` | `[]` | `audit` lists notes without a summary under these prefixes. |
| `orphan_ignore_folders` | `[]` | `audit` does not report orphans here. |
| `overdue_days` | `14` | `audit` flags open tasks overdue by more than this. |

Changing the configuration triggers a full re-index automatically.

## Using it with Claude Code (or any agent)

Tell the agent to query the graph before opening notes. A ready-made `CLAUDE.md` snippet and a skill
file are in [docs/claude-code.md](docs/claude-code.md) and [skills/vault-graph/SKILL.md](skills/vault-graph/SKILL.md).
Any agent that can run a shell command can use it; the output is plain text.

## How it works

1. **Scan** the vault for `.md` files and compare modification time and size with the cache.
2. **Extract** each changed note: frontmatter, headings (with line ranges), `[[wikilinks]]` and
   embeds (code blocks and inline code are skipped), tags, tasks, a one-line summary and term counts.
3. **Resolve links** the way Obsidian does (by name, alias or path; case-insensitive). When two notes
   share a name the link is marked **ambiguous** instead of silently picking one.
4. **Detect communities** with a standard-library Louvain implementation and label them by their most
   connected notes.
5. **Answer queries** from the index, reading a file only to print an excerpt.

## Limitations

- Lexical search only (no embeddings). It is fast and predictable, but it will not find synonyms.
- Only `[[wikilinks]]` become graph edges. Standard Markdown links `[text](note.md)` are not parsed yet.
- The frontmatter parser handles `key: value`, inline lists and `- item` lists, not full YAML.
- Tokenisation targets Latin-script languages. Stemming exists for English and Portuguese only.

## Development

```bash
python -m unittest discover -s tests -v
```

Tests run against `examples/sample-vault`, a small fictional vault that deliberately contains a
broken link, two notes with the same name, invalid frontmatter, an orphan, a template, overdue tasks
and code blocks full of fake links.

## Credits

Inspired by [Graphify](https://github.com/Graphify-Labs/graphify) (Apache-2.0): deterministic
extraction, hash-based caching, edge confidence, communities, hub notes and token-budgeted queries.
Graphify builds its graph by sending files to an LLM; `vault-graph` targets the narrower case of a
Markdown vault where the structure is already explicit, so no model is needed. The ideas were
reimplemented from scratch for that narrower case; Graphify itself is a different, much broader tool.

## License

[MIT](LICENSE)
