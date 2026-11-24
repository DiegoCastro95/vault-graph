#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
vault-graph: a local knowledge graph for Markdown / Obsidian vaults, built for AI agents.

An LLM agent that has to answer questions about a vault of a few hundred notes has two bad
options: read everything (thousands of tokens per question) or grep blindly. vault-graph builds
an index of the vault *without calling any LLM* (wikilinks, frontmatter, headings, tags, tasks)
and answers queries with the smallest useful excerpt: which notes match, which section to open,
and only those lines.

Usage (from the vault root, or pass --vault PATH):
  vault-graph search "question"            ranked notes and sections that answer it
  vault-graph note "Note name"             summary, outline with line ranges, links in/out
  vault-graph section "Note" "Heading"     only the requested section (--tail N, --lines A-B, --numbers)
  vault-graph tasks [--match X] [--until tomorrow] [--overdue]
  vault-graph recent [--days 7]            notes touched in the last days
  vault-graph changed [--since YYYY-MM-DD] what changed between index updates
  vault-graph path "A" "B"                 shortest chain of links between two notes
  vault-graph audit                        broken links, duplicate names, bad frontmatter, orphans
  vault-graph report                       overview: folders, hubs, communities, giant notes
  vault-graph update [--force]             re-index (every other command already does it)

Notes are never modified and nothing is deleted: the only files written live in the cache
directory (default <vault>/.vault-graph/). Standard library only, Python 3.8+.
"""
import argparse
import datetime
import hashlib
import json
import math
import os
import re
import sys
import time
import unicodedata
from collections import Counter, defaultdict, deque

__version__ = "0.1.0"

INDEX_FORMAT = 2
CHARS_PER_TOKEN = 3.5  # rough estimate used everywhere a token count is shown
DEFAULT_BUDGET = 1200  # approximate tokens per query answer

DEFAULT_CONFIG = {
    # "en" or "pt": picks stop words and a light stemmer. "none" disables stemming.
    "language": "en",
    # folders skipped entirely (any folder starting with "." is always skipped)
    "ignore_folders": ["node_modules"],
    # notes under these prefixes are templates: not reported as broken links/orphans, low search weight
    "template_folders": ["Templates/", "templates/", "_templates/"],
    # {"substring of path": weight} to push noisy folders down in search (1.0 = neutral)
    "low_weight_folders": {},
    # if set, `tasks` only looks here unless --folder or --all is given
    "tasks_folders": [],
    # frontmatter keys whose plain-text values count as links to a note (e.g. area: Finance)
    "link_properties": ["area", "areas", "project", "projects"],
    # frontmatter keys used as the one-line summary of a note, first match wins
    "summary_properties": ["summary", "description", "abstract"],
    # `audit` lists notes without a summary property under these prefixes
    "require_summary_in": [],
    # `audit` does not report notes under these prefixes as orphans
    "orphan_ignore_folders": [],
    # `audit` flags open tasks whose due date is older than this many days
    "overdue_days": 14,
}

ROOT = ""
CACHE = ""
CFG = dict(DEFAULT_CONFIG)

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def configure(vault=None, cache_dir=None):
    """Point the module at a vault and load <cache>/config.json (optional)."""
    global ROOT, CACHE, CFG
    ROOT = os.path.abspath(vault or os.getcwd())
    CACHE = os.path.abspath(cache_dir) if cache_dir else os.path.join(ROOT, ".vault-graph")
    CFG = json.loads(json.dumps(DEFAULT_CONFIG))
    user = load_json(os.path.join(CACHE, "config.json"), {})
    if isinstance(user, dict):
        for k, v in user.items():
            if k in DEFAULT_CONFIG:
                CFG[k] = v
    _stop_cache.clear()


def f_index():
    return os.path.join(CACHE, "index.json")


def f_terms():
    return os.path.join(CACHE, "terms.json")


def f_report():
    return os.path.join(CACHE, "REPORT.md")


def f_changes():
    return os.path.join(CACHE, "changes.jsonl")


# --------------------------------------------------------------------------- text

def est_tokens(n_chars):
    return int(round(n_chars / CHARS_PER_TOKEN))


def fmt_tokens(n):
    if n >= 1_000_000:
        return "~%.1fM tok" % (n / 1e6)
    return "~%.1fk tok" % (n / 1000.0) if n >= 1000 else "~%d tok" % n


def strip_accents(s):
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def norm(s):
    """For search: no accents, lower case."""
    return strip_accents(s or "").casefold().strip()


def link_key(s):
    """To resolve [[links]] like Obsidian does: case-insensitive, accent-sensitive."""
    return unicodedata.normalize("NFC", s or "").casefold().strip()


_STOP_EN = """a an the and or but if of to in on at by for with without from into over under about as is
are was were be been being am do does did have has had having it its this that these those i you he she
we they me him her us them my your his our their not no yes so than then there here when where which who
whom what why how all any each more most other some such only own same too very can will just should
would could may might also etc via per vs""".split()

_STOP_PT = """a o as os um uma uns umas de da do das dos em no na nos nas num numa por pelo pela pelos
pelas para pra pro com sem sobre entre e ou que se ao aos à às é ser foi era sao são esta está estao
estão isso isto esse essa esses essas este aquele aquela meu minha meus minhas seu sua seus suas eu
ele ela eles elas voce você nos nós mais menos muito ja já ainda tambem também onde qual quais quando
como porque quem nao não sim tem ter tinha tenho vai vou ver la lá aqui ali dia hoje ate até me te lhe
tudo todo toda todos todas cada mesmo mesma coisa coisas algo agora entao então""".split()

_stop_cache = {}


def stop_words():
    lang = CFG.get("language", "en")
    if lang not in _stop_cache:
        words = set(_STOP_EN)
        if lang in ("pt", "none"):
            words |= set(_STOP_PT)
        _stop_cache[lang] = {norm(w) for w in words}
    return _stop_cache[lang]


_PT_SUF1 = ("mente", "acoes", "icoes", "coes", "soes", "oes", "aes", "ais", "eis", "ois", "ns", "res",
            "zes", "les", "is", "es", "s")
_PT_SUF2 = ("cao", "sao", "ao", "al", "ar", "er", "ir", "ia", "io", "a", "o", "e")


def _stem_pt(w):
    """Light Portuguese stemmer: plural and gender, so 'constitucionais' matches 'constitucional'."""
    if len(w) <= 4 or w.isdigit():
        return w
    for s in _PT_SUF1:
        if w.endswith(s) and len(w) - len(s) >= 4:
            w = w[: -len(s)]
            break
    for s in _PT_SUF2:
        if w.endswith(s) and len(w) - len(s) >= 4:
            w = w[: -len(s)]
            break
    return w


def _stem_en(w):
    """Very light English stemmer: plurals and -ing/-ed. Prefix matching covers the rest."""
    if len(w) <= 3 or w.isdigit():
        return w
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    if w.endswith(("sses", "xes", "zes", "ches", "shes")):
        return w[:-2]
    if w.endswith("ing") and len(w) > 5:
        return w[:-3]
    if w.endswith("ed") and len(w) > 4:
        return w[:-2]
    if w.endswith("s") and not w.endswith(("ss", "us", "is")):
        return w[:-1]
    return w


def stem(w):
    lang = CFG.get("language", "en")
    if lang == "pt":
        return _stem_pt(w)
    if lang == "none":
        return w
    return _stem_en(w)


def terms(text, minimum=2, digits=True):
    stop = stop_words()
    out = []
    for w in re.findall(r"[a-z0-9]+", norm(text)):
        if w in stop:
            continue
        if w.isdigit():
            if digits:
                out.append(w)
            continue
        if len(w) < minimum:
            continue
        out.append(stem(w))
    return out


def clean_md(s):
    s = re.sub(r"\[\[([^\]|]+?)\\?\|([^\]]+)\]\]", r"\2", s)
    s = re.sub(r"\[\[([^\]]+)\]\]", lambda m: m.group(1).split("#")[0] or m.group(1), s)
    s = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", s)
    s = re.sub(r"(\*\*|__|==|~~|`)", "", s)
    s = re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def as_list(v):
    if v is None or v == "":
        return []
    if isinstance(v, list):
        return v
    return [v]


# ------------------------------------------------------------------- extraction

RE_HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)[ \t]*(?:[ \t]#+)?[ \t]*$")
RE_FENCE = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")
RE_INLINE_CODE = re.compile(r"(`+)(?:(?!\1).)+?\1")
RE_LINK = re.compile(r"(!?)\[\[([^\[\]\n]+?)\]\]")
RE_TAG = re.compile(r"(?<![\w/&#:='\"(\[\]`.\-])#([^\s#\[\]{}(),.;:!?\"'`|<>*=+\\]+)")
RE_TASK = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+\[(.)\]\s+(.*)$")
RE_DUE = re.compile(r"(?:due\s*::|(?:due|deadline|\U0001F4C5)\s*:?)\s*(\d{4}-\d{2}-\d{2})", re.I)
RE_FM_KEY = re.compile(r"^([^\s:#\-][^:]*?):(?:\s+(.*)|\s*)$")


def read_frontmatter(lines):
    """Return (dict, first_body_line, problems). No PyYAML: `key: value` and lists only."""
    if not lines or lines[0].strip() != "---":
        return {}, 0, []
    end = None
    for i in range(1, min(len(lines), 200)):
        if lines[i].strip() in ("---", "..."):
            end = i
            break
    if end is None:
        return {}, 0, []
    fm, problems, key = {}, [], None
    for i in range(1, end):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = RE_FM_KEY.match(line) if not line[:1].isspace() else None
        if m:
            key = m.group(1).strip()
            val = (m.group(2) or "").strip()
            if val == "":
                fm[key] = []
                continue
            if val.startswith("[") and val.endswith("]") and not val.startswith("[["):
                fm[key] = [x.strip().strip("\"'") for x in val[1:-1].split(",") if x.strip()]
            else:
                if len(val) >= 2 and val[0] in "\"'" and val[-1] == val[0]:
                    val = val[1:-1].replace('\\"', '"')
                elif re.search(r":\s", val) and not val.startswith(("[[", "http")):
                    problems.append("L%d %s: value has ':' without quotes" % (i + 1, key))
                fm[key] = val
        else:
            m2 = re.match(r"^\s+-\s+(.*)$", line)
            if m2 and key is not None:
                v = m2.group(1).strip().strip("\"'")
                cur = fm.get(key)
                if not isinstance(cur, list):
                    fm[key] = [cur] if cur not in (None, "") else []
                fm[key].append(v)
    return fm, end + 1, problems


def summary_of(fm, lines, start):
    for k in CFG["summary_properties"]:
        v = fm.get(k)
        if isinstance(v, str) and v.strip():
            return clean_md(v)[:220]
    fenced = False
    for line in lines[start: start + 80]:
        s = line.strip()
        if s.startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if fenced or not s:
            continue
        if s.startswith(("#", "|", "![", "---", "- [", "%%", "<")):
            continue
        if s.startswith(">"):
            s = s.lstrip("> ").strip()
            s = re.sub(r"^\[![^\]]*\][+-]?\s*", "", s)
            if not s:
                continue
        t = clean_md(s)
        if len(t) >= 25:
            return t[:220]
    return ""


def split_link(content):
    """'Note#Heading|alias' -> (note, anchor)."""
    content = content.replace("\\|", "|")
    target = content.split("|", 1)[0]
    anchor = ""
    if "#" in target:
        target, anchor = target.split("#", 1)
    return target.strip(), anchor.strip()


def extract(rel, text):
    """Parse one note. Returns (record, term_counter).

    Record keys (kept short because the index holds one per note):
      n name, fm frontmatter, fmp frontmatter problems, sm summary,
      sec sections [level, title, first_line, last_line, chars], lk links
      [target, anchor, kind(l|e), line, where(fm|c)], tg tags {tag: count},
      tf tasks [line, open(1|0), text, section, due], fl lines with inline fields (a::b),
      nl number of lines, ch number of characters.
    """
    if text.startswith("﻿"):
        text = text[1:]
    lines = text.splitlines()
    fm, start, probs = read_frontmatter(lines)
    pref = [0]
    for line in lines:
        pref.append(pref[-1] + len(line) + 1)

    headings, links, tasks = [], [], []
    tags = Counter()
    fields = 0
    fence = None
    cur_heading = ""

    for i in range(1, max(start - 1, 1)):
        for m in RE_LINK.finditer(lines[i]):
            target, anchor = split_link(m.group(2))
            links.append([target, anchor, "e" if m.group(1) else "l", i + 1, "fm"])
    for t in as_list(fm.get("tags")) + as_list(fm.get("tag")):
        if isinstance(t, str) and t.strip():
            tags[t.strip().lstrip("#")] += 1

    for i in range(start, len(lines)):
        line = lines[i]
        mc = RE_FENCE.match(line)
        if fence:
            if mc and mc.group(1)[0] == fence[0] and len(mc.group(1)) >= fence[1] and not mc.group(2).strip():
                fence = None
            continue
        if mc and not (mc.group(1)[0] == "`" and "`" in mc.group(2)):
            fence = (mc.group(1)[0], len(mc.group(1)))
            continue
        mh = RE_HEADING.match(line)
        if mh:
            txt = clean_md(mh.group(2))[:160]
            headings.append([len(mh.group(1)), txt, i + 1])
            cur_heading = txt
        s = RE_INLINE_CODE.sub(" ", line)
        for m in RE_LINK.finditer(s):
            target, anchor = split_link(m.group(2))
            links.append([target, anchor, "e" if m.group(1) else "l", i + 1, "c"])
        if not mh:
            for m in RE_TAG.finditer(s):
                tg = m.group(1).rstrip("/")
                if re.search(r"[^\d_\-/]", tg) and not re.fullmatch(r"[0-9A-Fa-f]{3}(?:[0-9A-Fa-f]{3})?", tg):
                    tags[tg] += 1  # hex colours such as #4E1A45 are not tags
        mt = RE_TASK.match(line)
        if mt:
            txt = mt.group(2).strip()
            md = RE_DUE.search(txt)
            tasks.append([i + 1, 1 if mt.group(1) in (" ", "/") else 0, txt[:260],
                          cur_heading[:80], md.group(1) if md else ""])
        if "::" in s and not s.lstrip().startswith("|") and "http" not in s:
            fields += 1

    # section ranges: each heading runs until the next heading of the same or higher level
    n = len(lines)
    sections = [None] * len(headings)
    stack = []
    for k, (level, txt, ln) in enumerate(headings):
        while stack and headings[stack[-1]][0] >= level:
            j = stack.pop()
            sections[j] = [headings[j][0], headings[j][1], headings[j][2], ln - 1]
        stack.append(k)
    while stack:
        j = stack.pop()
        sections[j] = [headings[j][0], headings[j][1], headings[j][2], n]
    for s in sections:
        s.append(pref[s[3]] - pref[s[2] - 1])  # characters in the section

    fm_short = {}
    for k, v in fm.items():
        if isinstance(v, list):
            fm_short[k] = [str(x)[:80] for x in v[:20]]
        else:
            fm_short[k] = str(v)[:200]

    body = "\n".join(lines[start:])
    counts = Counter(terms(body, minimum=3, digits=False))

    return {
        "n": os.path.basename(rel)[:-3],
        "fm": fm_short,
        "fmp": probs,
        "sm": summary_of(fm, lines, start),
        "sec": sections,
        "lk": links,
        "tg": dict(tags),
        "tf": tasks,
        "fl": fields,
        "nl": n,
        "ch": len(text),
    }, counts


# --------------------------------------------------------------- scan and cache

def scan():
    """List the notes (relative path with '/') -> (mtime, size)."""
    ignore = set(CFG["ignore_folders"])
    found = {}
    for dp, dn, fn in os.walk(ROOT):
        dn[:] = sorted(d for d in dn if not d.startswith(".") and d not in ignore)
        for f in fn:
            if not f.lower().endswith(".md"):
                continue
            ab = os.path.join(dp, f)
            try:
                st = os.stat(ab)
            except OSError:
                continue
            rel = os.path.relpath(ab, ROOT).replace(os.sep, "/")
            found[rel] = (round(st.st_mtime, 3), st.st_size)
    return found


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    # Written in place (no temp file + rename): renames over synced folders can be blocked.
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))


def config_signature():
    return hashlib.sha1(json.dumps(CFG, sort_keys=True, default=list).encode("utf-8")).hexdigest()[:10]


def update_index(force=False, quiet=False):
    os.makedirs(CACHE, exist_ok=True)
    idx = load_json(f_index(), {})
    sig = config_signature()
    if idx.get("format") != INDEX_FORMAT or idx.get("sig") != sig:
        idx, force = {}, True
    tdata = load_json(f_terms(), {})
    prev = idx.get("notes", {})
    prev_terms = tdata.get("notes", {}) if isinstance(tdata, dict) else {}

    found = scan()
    if not force and len(prev) >= 50 and len(found) < 0.6 * len(prev):
        print("[vault-graph] WARNING: found %d notes but the index has %d. The folder looks incomplete "
              "(cloud sync still running?). Keeping the old index; use 'update --force' if this is intended."
              % (len(found), len(prev)), file=sys.stderr)
        return idx

    new, changed = [], []
    notes, term_rows = {}, {}
    read_count = 0
    for rel, (mt, sz) in found.items():
        old = prev.get(rel)
        if not force and old and old.get("mt") == mt and old.get("sz") == sz and rel in prev_terms:
            notes[rel] = old
            term_rows[rel] = prev_terms[rel]
            continue
        try:
            with open(os.path.join(ROOT, rel), "rb") as f:
                raw = f.read()
        except OSError:
            continue
        read_count += 1
        h = hashlib.sha1(raw).hexdigest()[:16]
        if not force and old and old.get("h") == h and rel in prev_terms:
            old["mt"], old["sz"] = mt, sz
            notes[rel] = old
            term_rows[rel] = prev_terms[rel]
            continue
        rec, counts = extract(rel, raw.decode("utf-8", errors="replace"))
        rec.update({"mt": mt, "sz": sz, "h": h})
        notes[rel] = rec
        term_rows[rel] = " ".join("%s:%d" % (t, c) for t, c in counts.items())
        if not old:
            new.append(rel)
        elif old.get("h") != h:
            changed.append(rel)  # a forced re-index of identical text is not a change
    removed = [p for p in prev if p not in notes]

    if not (new or changed or removed) and not force and idx.get("edges") is not None:
        if not quiet:
            print_summary(idx, read_count, new, changed, removed)
        return idx

    idx = {"format": INDEX_FORMAT, "sig": sig, "ts": time.time(), "notes": notes}
    build_graph(idx)
    save_json(f_index(), idx)
    save_json(f_terms(), {"format": INDEX_FORMAT, "notes": term_rows})
    with open(f_report(), "w", encoding="utf-8") as f:
        f.write(build_report(idx))
    if new or changed or removed:
        log_changes(new, changed, removed)
    if not quiet:
        print_summary(idx, read_count, new, changed, removed)
    return idx


def print_summary(idx, read_count, new, changed, removed):
    print("[vault-graph] %d notes · %d read · %d new · %d changed · %d removed · %d links · %d broken links"
          % (len(idx["notes"]), read_count, len(new), len(changed), len(removed),
             len(idx["edges"]), len(idx["broken"])))


def log_changes(new, changed, removed):
    row = json.dumps({"ts": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
                      "new": new[:200], "changed": changed[:200], "removed": removed[:200]},
                     ensure_ascii=False)
    try:
        if os.path.exists(f_changes()) and os.path.getsize(f_changes()) > 1_000_000:
            with open(f_changes(), "r", encoding="utf-8") as f:
                rest = f.readlines()[-1000:]
            with open(f_changes(), "w", encoding="utf-8") as f:
                f.writelines(rest)
        with open(f_changes(), "a", encoding="utf-8") as f:
            f.write(row + "\n")
    except OSError:
        pass


# ------------------------------------------------------------------------ graph

def is_template(p):
    return p.startswith(tuple(CFG["template_folders"]))


def top_folder(p):
    return p.split("/", 1)[0] if "/" in p else "(root)"


def build_graph(idx):
    notes = idx["notes"]
    paths = sorted(notes)
    pos = {p: i for i, p in enumerate(paths)}
    by_name, by_alias, by_path = defaultdict(list), defaultdict(list), {}
    for p in paths:
        r = notes[p]
        by_name[link_key(r["n"])].append(p)
        by_path[link_key(p[:-3])] = p
        for a in as_list(r["fm"].get("aliases")) + as_list(r["fm"].get("alias")):
            if isinstance(a, str) and a.strip():
                by_alias[link_key(a)].append(p)

    def resolve(target, origin):
        t = target.strip().replace("\\", "/")
        if not t:
            return None, False
        if t.lower().endswith(".md"):
            t = t[:-3]
        k = link_key(t)
        if "/" in t:
            k2 = k.lstrip("./")
            if k2 in by_path:
                cands = [by_path[k2]]
            else:
                cands = [p for kk, p in by_path.items() if kk.endswith("/" + k2)]
        else:
            cands = by_name.get(k) or by_alias.get(k) or []
        if not cands:
            return None, False
        if len(cands) == 1:
            return cands[0], False
        origin_dir = os.path.dirname(origin)
        same = [c for c in cands if os.path.dirname(c) == origin_dir]
        pick = same[0] if same else sorted(cands, key=lambda c: (c.count("/"), len(c)))[0]
        return pick, True

    link_props = [x for x in CFG["link_properties"]]
    edges, broken, seen = [], [], set()
    for p in paths:
        r = notes[p]
        lks = list(r["lk"])
        for key in link_props:
            for a in as_list(r["fm"].get(key)):
                if isinstance(a, str) and a.strip() and "[[" not in a:
                    lks.append([a.strip(), "", "a", 1, "fm"])
        for target, anchor, kind, line, where in lks:
            if "{{" in target or "<%" in target:
                continue  # template placeholders
            if not target.strip():
                continue  # [[#Heading]] points at the note itself
            dest, amb = resolve(target, p)
            if dest is None:
                if kind == "a":
                    continue  # a frontmatter value that is not a note name is just a label
                ext = os.path.splitext(target)[1].lower()
                if re.match(r"^\.[a-z0-9]{2,5}$", ext) and ext != ".md":
                    continue  # attachment (pdf, png, ...)
                if not is_template(p):
                    broken.append([pos[p], target[:120], line])
                continue
            if dest == p:
                continue
            edge_key = (pos[p], pos[dest], kind)
            if edge_key in seen:
                continue
            seen.add(edge_key)
            edges.append([pos[p], pos[dest], kind, line, 1 if amb else 0])

    idx["paths"] = paths
    idx["edges"] = edges
    idx["broken"] = broken

    # communities (Louvain) on the undirected graph
    adj = defaultdict(lambda: defaultdict(float))
    for a, b, kind, _, _ in edges:
        adj[a][b] += 1.0
        adj[b][a] += 1.0
    comm = louvain({u: dict(v) for u, v in adj.items()})
    groups = defaultdict(list)
    for u, c in comm.items():
        groups[c].append(u)
    degree = Counter()
    for u, nb in adj.items():
        degree[u] = len(nb)
    ordered = sorted(groups.values(), key=lambda ms: (-len(ms), min(ms)))
    com, label = {}, {}
    for cid, ms in enumerate(ordered):
        for u in ms:
            com[u] = cid
        label[cid] = community_label(ms, paths, notes, degree)
    idx["com"] = {str(u): c for u, c in com.items()}
    idx["label"] = {str(c): r for c, r in label.items()}
    idx["degree"] = {str(u): g for u, g in degree.items()}


def community_label(members, paths, notes, degree):
    folders = Counter(top_folder(paths[u]) for u in members)
    core = sorted(members, key=lambda u: (-degree[u], paths[u]))
    names = [notes[paths[u]]["n"] for u in core][:2]
    return "%s · %s" % (" / ".join(names), folders.most_common(1)[0][0])


def louvain(adj, max_levels=10):
    """Louvain community detection, standard library only. adj is symmetric {u: {v: weight}}."""
    G = {u: dict(nb) for u, nb in adj.items()}
    members = {u: [u] for u in G}
    for _ in range(max_levels):
        part, moved = _louvain_level(G)
        if not moved:
            break
        agg = defaultdict(lambda: defaultdict(float))
        for u, nb in G.items():
            for v, w in nb.items():
                agg[part[u]][part[v]] += w
        new_members = defaultdict(list)
        for u in G:
            new_members[part[u]].extend(members[u])
        G = {c: dict(nb) for c, nb in agg.items()}
        for c in new_members:
            G.setdefault(c, {})
        members = new_members
    final = {}
    for c, ms in members.items():
        for m in ms:
            final[m] = c
    return final


def _louvain_level(G):
    m2 = sum(w for u in G for w in G[u].values())
    if m2 == 0:
        return {u: u for u in G}, False
    k = {u: sum(G[u].values()) for u in G}
    part = {u: u for u in G}
    tot = dict(k)
    moved = False
    order = sorted(G, key=str)
    for _ in range(50):
        improved = False
        for u in order:
            cu = part[u]
            wc = defaultdict(float)
            for v, w in G[u].items():
                if v != u:
                    wc[part[v]] += w
            tot[cu] -= k[u]
            best, best_gain = cu, wc.get(cu, 0.0) - tot[cu] * k[u] / m2
            for c, w in wc.items():
                gain = w - tot[c] * k[u] / m2
                if gain > best_gain + 1e-12:
                    best, best_gain = c, gain
            tot[best] += k[u]
            if best != cu:
                part[u] = best
                improved = moved = True
        if not improved:
            break
    return part, moved


# -------------------------------------------------------------------- utilities

class Output:
    """Collects the answer and cuts it at a token budget."""

    def __init__(self, budget):
        self.limit = int(budget * CHARS_PER_TOKEN)
        self.parts, self.used, self.cut = [], 0, 0

    def add(self, s=""):
        if self.used + len(s) + 1 > self.limit:
            self.cut += 1
            return False
        self.parts.append(s)
        self.used += len(s) + 1
        return True

    def print(self, hint=""):
        print("\n".join(self.parts))
        if self.cut:
            print("... (%d lines cut by the ~%d tok budget; use --budget N or narrow the query%s)"
                  % (self.cut, int(self.limit / CHARS_PER_TOKEN), hint))


def load(fast=False):
    if fast and os.path.exists(f_index()):
        idx = load_json(f_index(), {})
        if idx.get("format") == INDEX_FORMAT:
            return idx
    return update_index(quiet=True)


def neighbours(idx):
    out_, in_ = defaultdict(list), defaultdict(list)
    for a, b, kind, line, amb in idx["edges"]:
        out_[a].append((b, kind, line, amb))
        in_[b].append((a, kind, line, amb))
    return out_, in_


def find_note(idx, name, exact=False):
    """Resolve a name (or path) to a note. Returns (path, candidates)."""
    paths = idx["paths"]
    notes = idx["notes"]
    target = name.strip().replace("\\", "/")
    if target.lower().endswith(".md"):
        target = target[:-3]
    k = link_key(target)
    hits = [p for p in paths if link_key(p[:-3]) == k or link_key(notes[p]["n"]) == k
            or link_key(p[:-3]).endswith("/" + k)]
    if len(hits) == 1:
        return hits[0], []
    if len(hits) > 1:
        return None, hits
    if exact:
        return None, []
    for p in paths:
        for a in as_list(notes[p]["fm"].get("aliases")):
            if isinstance(a, str) and link_key(a) == k:
                return p, []
    kn = norm(target)
    contains = [p for p in paths if kn in norm(notes[p]["n"])]
    if len(contains) == 1:
        return contains[0], []
    if contains:
        return None, sorted(contains, key=len)
    q = set(terms(target))
    if q:
        scored = []
        for p in paths:
            t = set(terms(notes[p]["n"]))
            inter = len(q & t)
            if inter:
                scored.append((inter / len(q | t), p))
        scored.sort(reverse=True)
        if scored and scored[0][0] >= 0.5 and (len(scored) == 1 or scored[0][0] > scored[1][0]):
            return scored[0][1], []
        return None, [p for _, p in scored[:8]]
    return None, []


def fail_note(name, cands):
    if cands:
        print("Ambiguous or approximate name: \"%s\". Candidates:" % name)
        for c in cands[:12]:
            print("  - " + c)
        sys.exit(3)
    print("No note named \"%s\". Try: vault-graph search \"%s\"" % (name, name))
    sys.exit(4)


def today():
    return datetime.date.today()


def date_arg(s):
    s = (s or "").strip().lower()
    if s in ("today", ""):
        return today()
    if s == "tomorrow":
        return today() + datetime.timedelta(days=1)
    if s.startswith("+") and s[1:].isdigit():
        return today() + datetime.timedelta(days=int(s[1:]))
    return datetime.date.fromisoformat(s)


def short_info(r):
    bits = []
    for k in ("type", "status", "date", "updated"):
        v = r["fm"].get(k)
        if isinstance(v, str) and v:
            bits.append(v if k in ("type", "status") else "%s %s" % (k, v))
    return " · ".join(bits)


def read_lines(rel):
    with open(os.path.join(ROOT, rel), "r", encoding="utf-8", errors="replace") as f:
        t = f.read()
    if t.startswith("﻿"):
        t = t[1:]
    return t.splitlines()


# ----------------------------------------------------------------------- search

def folder_weight(p, r=None):
    if r is not None and str(r["fm"].get("type", "")).lower() in ("index", "moc", "dashboard", "hub"):
        return 0.6
    low = p.lower()
    for fragment, w in CFG["low_weight_folders"].items():
        if fragment.lower() in low:
            return float(w)
    if is_template(p):
        return 0.3
    return 1.0


def cmd_search(a):
    idx = load(a.fast)
    notes, paths = idx["notes"], idx["paths"]
    q = list(dict.fromkeys(terms(a.query)))
    if not q:
        print("The query has no useful terms.")
        return
    only = norm(a.folder) if a.folder else ""

    # metadata fields: name/alias, summary, tags, headings, frontmatter, folder
    fields = {}
    df = Counter()
    for p in paths:
        r = notes[p]
        f = {
            "name": set(terms(r["n"] + " " + " ".join(str(x) for x in as_list(r["fm"].get("aliases"))))),
            "summary": set(terms(r["sm"])),
            "tags": set(terms(" ".join(t.replace("/", " ").replace("-", " ") for t in r["tg"]))),
            "headings": set(terms(" ".join(s[1] for s in r["sec"][:400]))),
            "fm": set(terms(" ".join(" ".join(as_list(v)) if isinstance(v, list) else str(v)
                                     for k, v in r["fm"].items() if k not in ("summary", "tags", "aliases")))),
            "folder": set(terms(os.path.dirname(p))),
        }
        fields[p] = f
        for t in set().union(*f.values()):
            df[t] += 1
    N = max(1, len(paths))
    weights = {"name": 6.0, "summary": 4.0, "tags": 3.0, "headings": 2.5, "fm": 2.0, "folder": 1.5}

    def match(qt, bag):
        if qt in bag:
            return True
        if len(qt) >= 5:
            return any(w.startswith(qt) or (len(w) >= 5 and qt.startswith(w)) for w in bag)
        return False

    idf = {t: math.log(1 + N / (1 + df.get(t, 0))) for t in q}

    # body text (BM25-like) from the term index
    tdata = load_json(f_terms(), {}).get("notes", {})
    body = defaultdict(float)
    size = {p: max(1, notes[p]["ch"]) for p in paths}
    avg = sum(size.values()) / N
    tf_body = defaultdict(dict)
    for p, s in tdata.items():
        if p not in notes:
            continue
        for pair in s.split(" "):
            if not pair:
                continue
            t, _, c = pair.rpartition(":")
            for qt in q:
                if t == qt or (len(qt) >= 5 and t.startswith(qt)):
                    tf_body[qt][p] = tf_body[qt].get(p, 0) + int(c)
    for qt, d in tf_body.items():
        idf_b = math.log(1 + (N - len(d) + 0.5) / (len(d) + 0.5))
        for p, tf in d.items():
            body[p] += idf_b * (tf * 2.2) / (tf + 1.2 * (0.25 + 0.75 * size[p] / avg))

    scored = []
    for p in paths:
        if only and only not in norm(p):
            continue
        f = fields[p]
        s, matched = 0.0, set()
        for qt in q:
            best = 0.0
            for name, bag in f.items():
                if weights[name] > best and match(qt, bag):
                    best = weights[name]
            if best:
                s += best * idf[qt]
                matched.add(qt)
            elif p in tf_body.get(qt, {}):
                matched.add(qt)
        s += 1.6 * body.get(p, 0.0)
        if not s:
            continue
        coverage = len(matched) / len(q)
        s *= coverage ** 1.7 * folder_weight(p, notes[p])
        if len(q) > 1 and f["name"] and all(match(qt, f["name"]) for qt in q):
            s *= 1.5
        scored.append((s, p))
    scored.sort(reverse=True)
    if not scored:
        print("Nothing found for: %s" % a.query)
        return

    out_, in_ = neighbours(idx)
    pos = {p: i for i, p in enumerate(paths)}
    out = Output(a.budget)
    out.add("Search: %s  (terms: %s)" % (a.query, ", ".join(q)))
    for k, (s, p) in enumerate(scored[: a.max]):
        r = notes[p]
        out.add("")
        info = short_info(r)
        ok = out.add("[%d] %s  (%s · %d lines%s)" % (k + 1, p, fmt_tokens(est_tokens(r["ch"])), r["nl"],
                                                    (" · " + info) if info else ""))
        if not ok:
            break
        if r["sm"]:
            out.add("    summary: " + r["sm"][:180])
        # sections whose heading matches the query
        hits = []
        for sec in r["sec"]:
            ts = set(terms(sec[1]))
            n_ok = sum(1 for qt in q if match(qt, ts))
            if n_ok:
                hits.append((n_ok, -sec[0], sec))
        if any(h[2][0] > 1 for h in hits):
            hits = [h for h in hits if h[2][0] > 1]
        hits.sort(key=lambda x: (-x[0], x[1]))
        if hits:
            out.add("    sections: " + " · ".join('"%s" L%d-%d (%s)' % (
                h[2][1][:60], h[2][2], h[2][3], fmt_tokens(est_tokens(h[2][4]))) for h in hits[:3]))
        # body excerpts (top notes only: this reads the file)
        if k < a.excerpts and (not hits or k < 2):
            try:
                lines = read_lines(p)
                body_start = read_frontmatter(lines)[1]
                cand = []
                for i, line in enumerate(lines):
                    if i < body_start or line.lstrip().startswith("#"):
                        continue  # headings are already shown under "sections"
                    ts = set(terms(line, minimum=3))
                    n_ok = sum(1 for qt in q if match(qt, ts))
                    if n_ok:
                        cand.append((n_ok, -i, i))
                cand.sort(reverse=True)
                for n_ok, _, i in cand[:2]:
                    out.add("    L%d: %s" % (i + 1, clean_md(lines[i])[:150]))
            except OSError:
                pass
        if k < 3:
            i = pos[p]
            nb = [paths[b] for b, *_ in out_.get(i, [])] + [paths[b] for b, *_ in in_.get(i, [])]
            if nb:
                names = list(dict.fromkeys(notes[v]["n"] for v in nb))
                out.add("    linked to: " + ", ".join(names[:8]) + (" (+%d)" % (len(names) - 8) if len(names) > 8 else ""))
    out.add("")
    out.add('Next: vault-graph section "<note>" "<heading>"  or  vault-graph note "<note>"')
    out.print()


# ------------------------------------------------------------------------- note

def cmd_note(a):
    idx = load(a.fast)
    p, cands = find_note(idx, a.name, exact=a.exact)
    if not p:
        fail_note(a.name, cands)
    notes, paths = idx["notes"], idx["paths"]
    r = notes[p]
    pos = paths.index(p)
    out_, in_ = neighbours(idx)
    out = Output(a.budget)
    out.add("Note: %s" % r["n"])
    out.add("Path: %s  (%s · %d lines · %d KB)" % (p, fmt_tokens(est_tokens(r["ch"])), r["nl"], r["sz"] // 1024))
    c = idx["com"].get(str(pos))
    if c is not None:
        out.add("Community %s: %s" % (c, idx["label"].get(str(c), "")))
    props = {k: v for k, v in r["fm"].items() if k != "summary"}
    if props:
        out.add("Properties: " + "; ".join("%s=%s" % (k, ",".join(v) if isinstance(v, list) else v)
                                           for k, v in list(props.items())[:10])[:400])
    if r["sm"]:
        out.add("Summary: " + r["sm"])
    if r["tg"]:
        out.add("Tags: " + ", ".join("#" + t for t in sorted(r["tg"], key=lambda t: -r["tg"][t])[:10]))
    open_tasks = [t for t in r["tf"] if t[1]]
    extras = []
    if r["tf"]:
        extras.append("%d open / %d done tasks" % (len(open_tasks), len(r["tf"]) - len(open_tasks)))
    if r["fl"]:
        extras.append("%d lines with '::' (inline fields)" % r["fl"])
    if r["fmp"]:
        extras.append("frontmatter: " + "; ".join(r["fmp"][:3]))
    if extras:
        out.add(" · ".join(extras))
    out.add("")
    secs = r["sec"]
    if secs:
        max_level = 6
        if len(secs) > a.max_sections:
            for lv in range(1, 7):
                if sum(1 for s in secs if s[0] <= lv) > a.max_sections:
                    max_level = max(1, lv - 1)
                    break
        shown = [s for s in secs if s[0] <= max_level][: a.max_sections]
        out.add("Outline (%d sections%s):" % (len(secs), "" if len(shown) == len(secs)
                                              else ", showing down to level %d" % max_level))
        for s in shown:
            if not out.add("%s%s  L%d-%d %s" % ("  " * (s[0] - 1), s[1][:90], s[2], s[3], fmt_tokens(est_tokens(s[4])))):
                break
    else:
        out.add("No headings (running text).")
    out.add("")
    to_ = list(dict.fromkeys(paths[b] for b, *_ in out_.get(pos, [])))
    from_ = list(dict.fromkeys(paths[b] for b, *_ in in_.get(pos, [])))
    if to_:
        out.add("Links to (%d): %s" % (len(to_), ", ".join(notes[x]["n"] for x in to_[:25]) + (" ..." if len(to_) > 25 else "")))
    if from_:
        out.add("Linked from (%d): %s" % (len(from_), ", ".join(notes[x]["n"] for x in from_[:25]) + (" ..." if len(from_) > 25 else "")))
    qb = [q for q in idx["broken"] if q[0] == pos]
    if qb:
        out.add("Broken links here: " + ", ".join("[[%s]] L%d" % (q[1], q[2]) for q in qb[:8]))
    out.print()


# ---------------------------------------------------------------------- section

def find_section(r, title):
    k = norm(title).lstrip("#").strip()
    secs = r["sec"]
    for test in (lambda s: norm(s[1]) == k,
                 lambda s: norm(s[1]).startswith(k),
                 lambda s: k in norm(s[1])):
        hit = [s for s in secs if test(s)]
        if len(hit) == 1:
            return hit[0], []
        if hit:
            return None, hit
    q = set(terms(title))
    scored = sorted(((len(q & set(terms(s[1]))), s) for s in secs), key=lambda x: -x[0])
    scored = [x for x in scored if x[0]]
    if scored and (len(scored) == 1 or scored[0][0] > scored[1][0]):
        return scored[0][1], []
    return None, [x[1] for x in scored[:10]]


def cmd_section(a):
    idx = load(a.fast)
    p, cands = find_note(idx, a.note)
    if not p:
        fail_note(a.note, cands)
    r = idx["notes"][p]
    lines = read_lines(p)
    if a.lines:
        first, _, last = a.lines.partition("-")
        first, last = int(first), int(last or first)
        head = "%s  L%d-%d" % (p, first, last)
    elif a.title:
        sec, cands = find_section(r, a.title)
        if not sec:
            if cands:
                print("Ambiguous heading in %s. Candidates:" % p)
                for s in cands[:12]:
                    print('  "%s"  L%d-%d %s' % (s[1], s[2], s[3], fmt_tokens(est_tokens(s[4]))))
                sys.exit(3)
            print('Heading "%s" not found in %s. See: vault-graph note "%s"' % (a.title, p, r["n"]))
            sys.exit(4)
        first, last = sec[2], sec[3]
        head = '%s  ›  "%s"  L%d-%d (%s)' % (p, sec[1], first, last, fmt_tokens(est_tokens(sec[4])))
        if not a.all and not a.tail and est_tokens(sec[4]) > a.limit:
            subs = [s for s in r["sec"] if s[2] > first and s[3] <= last and s[0] == sec[0] + 1]
            print(head)
            print("Large section (%s > limit %d). Subsections:" % (fmt_tokens(est_tokens(sec[4])), a.limit))
            for s in subs[:40]:
                print('  "%s"  L%d-%d %s' % (s[1], s[2], s[3], fmt_tokens(est_tokens(s[4]))))
            print("Use a subheading, --lines A-B, --tail N or --all.")
            return
    else:
        first, last = 1, len(lines)
        head = "%s  (whole note, %s)" % (p, fmt_tokens(est_tokens(r["ch"])))
        if not a.all and not a.tail and est_tokens(r["ch"]) > a.limit:
            print(head)
            print("Large note. Pick a section (vault-graph note \"%s\"), --lines, --tail N or --all." % r["n"])
            return
    span = list(range(max(1, first), min(last, len(lines)) + 1))
    if a.tail:
        non_empty = [i for i in span if lines[i - 1].strip()]
        span = non_empty[-a.tail:]
        head += "  · last %d non-empty lines" % a.tail
    print(head)
    for i in span:
        print(("%5d| " % i if a.numbers else "") + lines[i - 1])


# ------------------------------------------------------------------------ tasks

def cmd_tasks(a):
    idx = load(a.fast)
    notes = idx["notes"]
    until = date_arg(a.until) if a.until else None
    today_ = today()
    match = norm(a.match) if a.match else ""
    folder = norm(a.folder) if a.folder else ""
    scope = tuple(norm(x) for x in CFG["tasks_folders"])
    items = []
    for p in idx["paths"]:
        if is_template(p):
            continue
        np_ = norm(p)
        if folder:
            if folder not in np_:
                continue
        elif scope and not a.all and not np_.startswith(scope):
            continue
        r = notes[p]
        areas = " ".join(norm(x) for k in CFG["link_properties"] for x in as_list(r["fm"].get(k)))
        for ln, is_open, txt, sec, due in r["tf"]:
            if not is_open and not a.done:
                continue
            if match and match not in norm(sec) and match not in areas and match not in np_ and match not in norm(txt):
                continue
            if a.overdue and not (due and due < today_.isoformat()):
                continue
            if until and not (due and due <= until.isoformat()):
                continue
            items.append((due or "9999", p, sec, ln, is_open, txt))
    items.sort(key=lambda x: (x[0] if (until or a.overdue) else "", x[1], x[3]))
    out = Output(a.budget)
    out.add("Tasks %s: %d%s" % ("open" if not a.done else "(open and done)", len(items),
                                (" · due until %s" % until.isoformat()) if until else (" · overdue" if a.overdue else "")))
    group = None
    for due, p, sec, ln, is_open, txt in items:
        g = (p, sec)
        if g != group:
            if not out.add("## %s  — %s" % (sec or "(no heading)", p)):
                break
            group = g
        late = ""
        if due != "9999" and due < today_.isoformat():
            late = " [OVERDUE %dd]" % (today_ - datetime.date.fromisoformat(due)).days
        if not out.add("- [%s] %s%s  (L%d)" % (" " if is_open else "x", clean_md(txt)[:200], late, ln)):
            break
    out.print(", --match X or --folder P")


# ------------------------------------------------------------- recent / changed

def cmd_recent(a):
    idx = load(a.fast)
    notes = idx["notes"]
    cutoff = time.time() - a.days * 86400
    only = norm(a.folder) if a.folder else ""
    rows = sorted(((r["mt"], p) for p, r in notes.items() if r["mt"] >= cutoff and (not only or only in norm(p))),
                  reverse=True)
    out = Output(a.budget)
    out.add("Notes modified in the last %d days: %d" % (a.days, len(rows)))
    for mt, p in rows:
        d = datetime.datetime.fromtimestamp(mt).strftime("%Y-%m-%d %H:%M")
        r = notes[p]
        if not out.add("- %s  %s%s" % (d, p, ("  — " + r["sm"][:100]) if r["sm"] else "")):
            break
    out.print()


def cmd_changed(a):
    load(a.fast)
    if not os.path.exists(f_changes()):
        print("No change history yet.")
        return
    with open(f_changes(), "r", encoding="utf-8") as f:
        rows = [json.loads(line) for line in f if line.strip()]
    if a.since:
        rows = [r for r in rows if r["ts"][:10] >= a.since]
    else:
        rows = rows[-a.last:]
    out = Output(a.budget)
    for r in rows:
        out.add("%s: %d new, %d changed, %d removed" % (r["ts"], len(r["new"]), len(r["changed"]), len(r["removed"])))
        for k in ("new", "changed", "removed"):
            for p in r[k][:15]:
                if not out.add("  %s %s" % ({"new": "+", "changed": "~", "removed": "-"}[k], p)):
                    break
    out.print()


# ------------------------------------------------------------------------- path

def cmd_path(a):
    idx = load(a.fast)
    pa, ca = find_note(idx, a.a)
    if not pa:
        fail_note(a.a, ca)
    pb, cb = find_note(idx, a.b)
    if not pb:
        fail_note(a.b, cb)
    paths, notes = idx["paths"], idx["notes"]
    ia, ib = paths.index(pa), paths.index(pb)
    adj = defaultdict(list)
    for x, y, kind, ln, amb in idx["edges"]:
        adj[x].append((y, "->", kind))
        adj[y].append((x, "<-", kind))
    prev = {ia: None}
    queue = deque([ia])
    while queue:
        u = queue.popleft()
        if u == ib:
            break
        for v, d, t in adj[u]:
            if v not in prev:
                prev[v] = (u, d, t)
                queue.append(v)
    if ib not in prev:
        print("No path between %s and %s." % (notes[pa]["n"], notes[pb]["n"]))
        return
    seq, u = [], ib
    while prev[u]:
        before, d, t = prev[u]
        seq.append((before, d, t, u))
        u = before
    seq.reverse()
    kind_name = {"l": "link", "e": "embed", "a": "property"}
    txt = notes[pa]["n"]
    for before, d, t, u in seq:
        rel = kind_name.get(t, t)
        txt += (" --%s--> " % rel if d == "->" else " <--%s-- " % rel) + notes[paths[u]]["n"]
    print("Path (%d steps): %s" % (len(seq), txt))


# ------------------------------------------------------------------------ audit

def audit(idx):
    notes, paths = idx["notes"], idx["paths"]
    res = {}
    by_target = defaultdict(list)
    for i, target, ln in idx["broken"]:
        by_target[target].append((paths[i], ln))
    res["broken"] = sorted(by_target.items(), key=lambda x: (-len(x[1]), x[0]))
    names = defaultdict(list)
    for p in paths:
        names[link_key(notes[p]["n"])].append(p)
    res["duplicates"] = [(k, v) for k, v in sorted(names.items()) if len(v) > 1]
    res["bad_frontmatter"] = [(p, notes[p]["fmp"]) for p in paths if notes[p]["fmp"]]
    need = tuple(CFG["require_summary_in"])
    res["no_summary"] = [p for p in paths if need and p.startswith(need) and not notes[p]["sm"]]
    degree = Counter()
    for x, y, *_ in idx["edges"]:
        degree[x] += 1
        degree[y] += 1
    skip = tuple(CFG["orphan_ignore_folders"])
    res["orphans"] = [p for i, p in enumerate(paths)
                      if not degree[i] and not is_template(p) and not (skip and p.startswith(skip))]
    t = today()
    overdue = []
    for p in paths:
        if is_template(p):
            continue
        for ln, is_open, txt, sec, due in notes[p]["tf"]:
            if is_open and due and (t - datetime.date.fromisoformat(due)).days > CFG["overdue_days"]:
                overdue.append((due, p, clean_md(txt)[:120], ln))
    res["overdue"] = sorted(overdue)
    res["root"] = [p for p in paths if "/" not in p]
    return res


def cmd_audit(a):
    idx = load(a.fast)
    r = audit(idx)
    out = Output(a.budget if not a.all else 10 ** 7)
    total = sum(len(v) for _, v in r["broken"])
    out.add("VAULT AUDIT — %s · %d notes" % (today().isoformat(), len(idx["paths"])))
    out.add("")
    out.add("Broken links: %d occurrences, %d distinct targets" % (total, len(r["broken"])))
    for target, occ in r["broken"][:25]:
        out.add("  [[%s]] ×%d — %s" % (target or "(empty)", len(occ),
                                       ", ".join("%s L%d" % (os.path.basename(p)[:-3], ln) for p, ln in occ[:3])))
    out.add("Duplicate note names (ambiguous [[links]]): %d" % len(r["duplicates"]))
    for k, ps in r["duplicates"][:20]:
        out.add("  %s: %s" % (k, " | ".join(ps)))
    out.add("Frontmatter problems: %d" % len(r["bad_frontmatter"]))
    for p, pr in r["bad_frontmatter"][:15]:
        out.add("  %s: %s" % (p, "; ".join(pr)))
    if CFG["require_summary_in"]:
        out.add("Notes without a summary property: %d" % len(r["no_summary"]))
        for p in r["no_summary"][:15]:
            out.add("  " + p)
    out.add("Open tasks overdue by more than %d days: %d" % (CFG["overdue_days"], len(r["overdue"])))
    for due, p, txt, ln in r["overdue"][:15]:
        out.add("  %s %s (L%d): %s" % (due, os.path.basename(p)[:-3], ln, txt))
    out.add("Notes at the vault root: " + (", ".join(r["root"]) or "none"))
    out.add("Orphan notes (no links in or out, templates excluded): %d" % len(r["orphans"]))
    by_folder = Counter(top_folder(p) for p in r["orphans"])
    out.add("  by folder: " + ", ".join("%s %d" % kv for kv in by_folder.most_common()))
    out.print(" or --all")


# ----------------------------------------------------------------------- report

def build_report(idx):
    notes, paths = idx["notes"], idx["paths"]
    L = []
    total = sum(r["ch"] for r in notes.values())
    L.append("# Vault graph report")
    L.append("")
    L.append("Generated %s by vault-graph (no LLM). Do not edit: it is rebuilt on every update."
             % datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))
    L.append("")
    L.append("- %d notes · %d links · %d broken links · total text %s (reading everything would cost this)"
             % (len(paths), len(idx["edges"]), len(idx["broken"]), fmt_tokens(est_tokens(total))))
    amb = sum(1 for e in idx["edges"] if e[4])
    L.append("- link confidence: %d EXTRACTED (explicit link) · %d AMBIGUOUS (duplicate name, Obsidian picks one)"
             % (len(idx["edges"]) - amb, amb))
    L.append("")
    L.append("## Folders")
    folders = defaultdict(lambda: [0, 0])
    for p, r in notes.items():
        folders[top_folder(p)][0] += 1
        folders[top_folder(p)][1] += r["ch"]
    for name, (n, ch) in sorted(folders.items(), key=lambda x: -x[1][1]):
        L.append("- %s: %d notes, %s" % (name, n, fmt_tokens(est_tokens(ch))))
    L.append("")
    L.append("## Hub notes (most connected)")
    degree = {int(k): v for k, v in idx["degree"].items()}
    for i in sorted(degree, key=lambda i: -degree[i])[:12]:
        L.append("- %s — %d neighbours — `%s`" % (notes[paths[i]]["n"], degree[i], paths[i]))
    L.append("")
    L.append("## Communities (groups of notes densely linked to each other)")
    sizes = Counter(idx["com"].values())
    for cid, n in sorted(sizes.items(), key=lambda x: -x[1])[:15]:
        if n < 3:
            break
        L.append("- C%s (%d notes): %s" % (cid, n, idx["label"][str(cid)]))
    unlinked = len(paths) - len(idx["com"])
    L.append("- %d notes with no links at all" % unlinked)
    L.append("")
    L.append("## Bridges between folders (links that cross folders)")
    bridges = Counter()
    for a, b, *_ in idx["edges"]:
        fa, fb = top_folder(paths[a]), top_folder(paths[b])
        if fa != fb:
            bridges[tuple(sorted((fa, fb)))] += 1
    for (fa, fb), n in bridges.most_common(10):
        L.append("- %s ↔ %s: %d" % (fa, fb, n))
    L.append("")
    L.append("## Giant notes (never read whole: use `section`)")
    for p in sorted(paths, key=lambda p: -notes[p]["ch"])[:15]:
        r = notes[p]
        L.append("- %s — %d sections — `%s`" % (fmt_tokens(est_tokens(r["ch"])), len(r["sec"]), p))
    L.append("")
    L.append("## Most used tags")
    tg = Counter()
    for r in notes.values():
        for t in r["tg"]:
            tg[t] += 1
    L.append(", ".join("#%s (%d)" % kv for kv in tg.most_common(20)))
    L.append("")
    L.append("## How to query (from the vault root)")
    L.append('`vault-graph search "question"` · `note "Name"` · `section "Note" "Heading"` · '
             '`tasks --match X` · `recent` · `path "A" "B"` · `audit`')
    return "\n".join(L) + "\n"


def cmd_report(a):
    idx = load(a.fast)
    if not os.path.exists(f_report()):
        with open(f_report(), "w", encoding="utf-8") as f:
            f.write(build_report(idx))
    with open(f_report(), "r", encoding="utf-8") as f:
        print(f.read())


def cmd_update(a):
    t = time.time()
    update_index(force=a.force)
    print("[vault-graph] done in %.1fs" % (time.time() - t))


# ------------------------------------------------------------------------- main

def build_parser():
    ap = argparse.ArgumentParser(prog="vault-graph",
                                 description="Local knowledge graph for Markdown/Obsidian vaults (no LLM).")
    ap.add_argument("--version", action="version", version="vault-graph " + __version__)
    ap.add_argument("--vault", help="vault root (default: current directory)")
    ap.add_argument("--cache-dir", help="where index files live (default: <vault>/.vault-graph)")
    sub = ap.add_subparsers(dest="cmd")

    def new(name, func, help_):
        sp = sub.add_parser(name, help=help_)
        sp.set_defaults(func=func)
        sp.add_argument("--budget", type=int, default=DEFAULT_BUDGET, help="approximate token limit of the answer")
        sp.add_argument("--fast", action="store_true", help="do not re-index first (use the index as it is)")
        return sp

    sp = new("search", cmd_search, "notes and sections that answer a question")
    sp.add_argument("query")
    sp.add_argument("--folder", help="restrict to paths containing this text (e.g. 'Projects')")
    sp.add_argument("--max", type=int, default=8)
    sp.add_argument("--excerpts", type=int, default=4, help="how many top notes also get body excerpts")

    sp = new("note", cmd_note, "summary, outline with line ranges and links of a note")
    sp.add_argument("name")
    sp.add_argument("--max-sections", type=int, default=45)
    sp.add_argument("--exact", action="store_true", help="exact name only (exit code 4 = no such note)")

    sp = new("section", cmd_section, "print only one section (or line range) of a note")
    sp.add_argument("note")
    sp.add_argument("title", nargs="?")
    sp.add_argument("--tail", type=int, help="only the last N non-empty lines (to append at the end)")
    sp.add_argument("--lines", help="line range A-B")
    sp.add_argument("--numbers", action="store_true", help="number the lines")
    sp.add_argument("--all", action="store_true", help="print even if large")
    sp.add_argument("--limit", type=int, default=3000, help="tokens above which a subsection is requested")

    sp = new("tasks", cmd_tasks, "open tasks ([ ] items), optionally with due dates")
    sp.add_argument("--match", help="text in the heading, path, text or area/project properties")
    sp.add_argument("--folder")
    sp.add_argument("--until", help="today, tomorrow, +N or YYYY-MM-DD (only tasks with a due date)")
    sp.add_argument("--overdue", action="store_true")
    sp.add_argument("--done", action="store_true", help="include completed tasks")
    sp.add_argument("--all", action="store_true", help="ignore the tasks_folders setting")

    sp = new("recent", cmd_recent, "notes modified in the last days")
    sp.add_argument("--days", type=int, default=7)
    sp.add_argument("--folder")

    sp = new("changed", cmd_changed, "history of changes between index updates")
    sp.add_argument("--since")
    sp.add_argument("--last", type=int, default=5)

    sp = new("path", cmd_path, "shortest chain of links between two notes")
    sp.add_argument("a")
    sp.add_argument("b")

    sp = new("audit", cmd_audit, "vault health: broken links, duplicates, frontmatter, orphans")
    sp.add_argument("--all", action="store_true")

    new("report", cmd_report, "overview of the vault")

    sp = sub.add_parser("update", help="re-index the vault (incremental)")
    sp.set_defaults(func=cmd_update)
    sp.add_argument("--force", action="store_true")
    return ap


def main(argv=None):
    ap = build_parser()
    a = ap.parse_args(argv)
    if not getattr(a, "func", None):
        ap.print_help()
        return 0
    configure(a.vault, a.cache_dir)
    if not os.path.isdir(ROOT):
        print("Vault folder not found: %s" % ROOT, file=sys.stderr)
        return 2
    a.func(a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
