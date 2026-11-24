"""Trend Radar — daily digest of rising open-source repos.

Sources: GitHub Trending, Trendshift, Hacker News front page (via Algolia).
Stores a daily snapshot in SQLite, computes signals from history, groups repos
into themes, and writes digests/YYYY-MM-DD.html + .md (and digests/index.html).

Usage:
    python radar.py              # run for today (UTC)
    python radar.py --no-enrich  # skip GitHub API enrichment
Optional env: GITHUB_TOKEN (raises the GitHub API limit from 60 to 5000/hour).
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "radar.db"
OUT_DIR = ROOT / "digests"
TEMPLATE = ROOT / "template.html"
UA = {"User-Agent": "Mozilla/5.0"}  # generic: crawled sites should not learn who runs this
TIMEOUT = 30


# ---------------------------------------------------------------- model
@dataclass
class Repo:
    slug: str                      # "owner/name", lower-cased key is slug.lower()
    desc: str = ""
    lang: str = ""
    stars_total: int | None = None
    stars_today: dict[str, int] = field(default_factory=dict)   # per source
    ranks: dict[str, int] = field(default_factory=dict)         # per source
    mentions: set[str] = field(default_factory=set)             # X / Hacker News / Reddit
    tags: set[str] = field(default_factory=set)
    sources: set[str] = field(default_factory=set)              # gh / ts / hn
    hn: dict | None = None
    # filled later
    theme: str = ""
    signals: list[str] = field(default_factory=list)
    hist: list[int] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return self.slug.lower()

    @property
    def momentum(self) -> int:
        return max(self.stars_today.values(), default=0)


def _int(text: str | None) -> int | None:
    if not text:
        return None
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None


def _get(url: str) -> str:
    r = requests.get(url, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    return r.text


# ---------------------------------------------------------------- sources
def fetch_github() -> list[Repo]:
    soup = BeautifulSoup(_get("https://github.com/trending?since=daily"), "html.parser")
    repos = []
    for i, row in enumerate(soup.select("article.Box-row"), 1):
        a = row.select_one("h2 a[href]")
        if not a:
            continue
        slug = a["href"].strip("/")
        desc_el = row.select_one("p")
        lang_el = row.select_one('[itemprop="programmingLanguage"]')
        stars_el = row.select_one('a[href$="/stargazers"]')
        today = 0
        for span in row.select("span"):
            m = re.search(r"([\d,]+)\s+stars?\s+today", span.get_text(" ", strip=True))
            if m:
                today = _int(m.group(1)) or 0
        repos.append(Repo(
            slug=slug,
            desc=desc_el.get_text(" ", strip=True) if desc_el else "",
            lang=lang_el.get_text(strip=True) if lang_el else "",
            stars_total=_int(stars_el.get_text()) if stars_el else None,
            stars_today={"gh": today},
            ranks={"gh": i},
            sources={"gh"},
        ))
    return repos


_TS_NOISE = re.compile(r"^(New|\d{4}|Mentioned on|Gained today|Like .*|Bookmark .*|\d+)$")


def fetch_trendshift() -> list[Repo]:
    soup = BeautifulSoup(_get("https://trendshift.io/"), "html.parser")
    repos, seen = [], set()
    links = [a for a in soup.find_all("a", href=True)
             if re.fullmatch(r"/repositories/\d+", a["href"]) and "/" in a.get_text()]
    for a in links:
        slug = a.get_text(strip=True)
        if slug.lower() in seen:
            continue
        seen.add(slug.lower())
        card = a.parent.parent.parent
        parts = [p for p in card.get_text("|", strip=True).split("|") if p]
        # Ranks 6+ print the rank before the name (1-5 use a medal image), and some
        # cards add a small second number after the stars — take the first number after the name.
        after = parts[parts.index(slug) + 1:] if slug in parts else parts[1:]
        gained = next((_int(p) for p in after if re.fullmatch(r"[\d,.]+k?", p)), 0)
        if after and after[0].endswith("k"):  # e.g. "1.2k"
            gained = int(float(after[0][:-1]) * 1000)
        mentions = set()
        for el in card.find_all(True):
            for attr in ("title", "aria-label"):
                v = el.get(attr) or ""
                if v.startswith("Mentioned on "):
                    mentions.update(s.strip() for s in v[len("Mentioned on "):].split(","))
        tags = {t.get_text(strip=True).lstrip("#") for t in card.find_all("a", href=re.compile(r"^/topics/"))}
        desc_parts = [p for p in after if p != slug and not _TS_NOISE.match(p)
                      and p.lstrip("#") not in tags and p not in mentions]
        repos.append(Repo(
            slug=slug,
            desc=max(desc_parts, key=len) if desc_parts else "",
            stars_today={"ts": gained or 0},
            ranks={"ts": len(repos) + 1},
            mentions=mentions,
            tags=tags,
            sources={"ts"},
        ))
    return repos


def fetch_hn() -> list[Repo]:
    data = requests.get("https://hn.algolia.com/api/v1/search",
                        params={"tags": "front_page", "hitsPerPage": 60},
                        headers=UA, timeout=TIMEOUT).json()
    repos = []
    for i, hit in enumerate(data.get("hits", []), 1):
        m = re.match(r"https?://(?:www\.)?github\.com/([\w.-]+)/([\w.-]+)", hit.get("url") or "")
        if not m:
            continue
        slug = f"{m.group(1)}/{m.group(2).removesuffix('.git')}"
        repos.append(Repo(
            slug=slug,
            desc=hit.get("title", ""),
            ranks={"hn": i},
            mentions={"Hacker News"},
            sources={"hn"},
            hn={"points": hit.get("points"), "comments": hit.get("num_comments"),
                "id": hit.get("objectID")},
        ))
    return repos


def merge(*lists: list[Repo]) -> dict[str, Repo]:
    out: dict[str, Repo] = {}
    for lst in lists:
        for r in lst:
            cur = out.get(r.key)
            if not cur:
                out[r.key] = r
                continue
            # GitHub's description beats Trendshift's beats an HN title.
            if r.sources & {"gh"} or (not cur.desc) or ("hn" in cur.sources and len(cur.sources) == 1):
                cur.desc = r.desc or cur.desc
            cur.lang = cur.lang or r.lang
            cur.stars_total = cur.stars_total or r.stars_total
            cur.stars_today.update(r.stars_today)
            cur.ranks.update(r.ranks)
            cur.mentions |= r.mentions
            cur.tags |= r.tags
            cur.sources |= r.sources
            cur.hn = cur.hn or r.hn
    return out


# ---------------------------------------------------------------- storage
SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots(
  date TEXT, repo TEXT, source TEXT, stars_today INTEGER, rank INTEGER,
  PRIMARY KEY(date, repo, source));
CREATE TABLE IF NOT EXISTS repos(
  repo TEXT PRIMARY KEY, slug TEXT, first_seen TEXT, desc TEXT, lang TEXT,
  stars_total INTEGER, topics TEXT, license TEXT, created_at TEXT, enriched INTEGER DEFAULT 0);
"""


def open_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.executescript(SCHEMA)
    cols = {c[1] for c in db.execute("PRAGMA table_info(repos)")}
    if "theme" not in cols:  # databases created before themes were stored
        db.execute("ALTER TABLE repos ADD COLUMN theme TEXT")
    return db


def store(db: sqlite3.Connection, day: str, repos: dict[str, Repo]) -> None:
    db.execute("DELETE FROM snapshots WHERE date=?", (day,))  # re-runs are idempotent
    for r in repos.values():
        for src in r.sources:
            db.execute("INSERT INTO snapshots VALUES(?,?,?,?,?)",
                       (day, r.key, src, r.stars_today.get(src), r.ranks.get(src)))
        db.execute("""INSERT INTO repos(repo, slug, first_seen, desc, lang, stars_total)
                      VALUES(?,?,?,?,?,?)
                      ON CONFLICT(repo) DO UPDATE SET
                        desc=excluded.desc, lang=COALESCE(NULLIF(excluded.lang,''), repos.lang),
                        stars_total=COALESCE(excluded.stars_total, repos.stars_total),
                        first_seen=MIN(repos.first_seen, excluded.first_seen)""",
                   (r.key, r.slug, day, r.desc, r.lang, r.stars_total))
    db.commit()


def enrich(db: sqlite3.Connection, repos: dict[str, Repo], limit: int = 15) -> int:
    """Fill language/topics/license for repos never enriched (GitHub REST API)."""
    headers = dict(UA, Accept="application/vnd.github+json")
    if os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    todo = [k for (k,) in db.execute("SELECT repo FROM repos WHERE enriched=0")
            if k in repos][:limit]
    done = 0
    for key in todo:
        r = repos[key]
        try:
            resp = requests.get(f"https://api.github.com/repos/{r.slug}", headers=headers, timeout=TIMEOUT)
        except requests.RequestException:
            break
        if resp.status_code == 403:  # rate limited; try again next run
            break
        if resp.status_code != 200:
            db.execute("UPDATE repos SET enriched=-1 WHERE repo=?", (key,))
            continue
        j = resp.json()
        topics = j.get("topics") or []
        db.execute("""UPDATE repos SET lang=COALESCE(NULLIF(lang,''), ?), stars_total=?, topics=?,
                      license=?, created_at=?, enriched=1 WHERE repo=?""",
                   (j.get("language"), j.get("stargazers_count"), json.dumps(topics),
                    (j.get("license") or {}).get("spdx_id"), j.get("created_at"), key))
        r.lang = r.lang or (j.get("language") or "")
        r.stars_total = j.get("stargazers_count") or r.stars_total
        if not r.desc and j.get("description"):
            r.desc = j["description"]
        done += 1
    db.commit()
    # Pull stored enrichment into today's objects (covers repos enriched on earlier days).
    for key, lang, topics, total in db.execute("SELECT repo, lang, topics, stars_total FROM repos"):
        if key in repos:
            r = repos[key]
            r.lang = r.lang or (lang or "")
            r.stars_total = r.stars_total or total
            r.topics = json.loads(topics) if topics else []
    return done


# ---------------------------------------------------------------- signals
def compute_signals(db: sqlite3.Connection, day: str, repos: dict[str, Repo]) -> int:
    dates = [d for (d,) in db.execute(
        "SELECT DISTINCT date FROM snapshots WHERE date<=? ORDER BY date DESC LIMIT 7", (day,))]
    dates.reverse()
    history_days = len(dates)
    for key, r in repos.items():
        rows = db.execute("SELECT date, source, stars_today FROM snapshots WHERE repo=? AND date<=?",
                          (key, day)).fetchall()
        by_date: dict[str, int] = {}
        gh_dates, ts_dates = set(), set()
        for d, src, st in rows:
            by_date[d] = max(by_date.get(d, 0), st or 0)
            (gh_dates if src == "gh" else ts_dates if src == "ts" else set()).add(d)
        r.hist = [by_date.get(d, 0) for d in dates]
        prior = [d for d in by_date if d < day]
        if history_days > 1 and not prior:
            r.signals.append("new")
        last3 = r.hist[-3:]
        if len(last3) == 3 and all(last3) and last3[0] < last3[1] < last3[2]:
            r.signals.append("hot")
        if "gh" in r.sources and gh_dates == {day} and any(d < day for d in ts_dates):
            r.signals.append("cross")
        if sum(1 for v in r.hist if v) >= 5:
            r.signals.append("sticky")
        if len(r.sources) >= 2:
            r.signals.append("multi")
    return history_days


# ---------------------------------------------------------------- themes
THEMES: list[tuple[str, list[str]]] = [
    # Keywords match at word starts ("agent" matches "agents", "ble" won't match "table").
    ("Agent skills & plugins", ["skill", "plugin", "slash command", "claude code", "mcp", "copilot", "ai-skills"]),
    ("Agent orchestration", ["agent-native", "orchestrat", "fleet of", "coding agent", "control claude", "multi-agent"]),
    ("Web data & scraping", ["scrap", "crawl", "web data", "data providers", "downloader", "download manager"]),
    ("AI video & media", ["video", "image", "music", "voice", "clip", "motion", "photo", "try-on"]),
    ("LLMs & inference", ["llm", "inference", "train", "transformer", "gpu", "vram"]),
    ("Security & privacy", ["security", "pentest", "red-team", "vulnerab", "spoof", "exploit", "nfc", "ble"]),
    ("Self-hosted alternatives", ["self-hosted", "alternative", "remote desktop", "office"]),
    ("Dev tools & infra", ["ci/cd", "nginx", "monitor", "observab", "cli", "terminal", "database", "docker", "error tracking"]),
    ("Finance & trading", ["trading", "stock", "crypto", "finance", "metatrader"]),
]
_THEME_RE = [(name, [re.compile(r"(?<![a-z0-9])" + re.escape(w)) for w in words]) for name, words in THEMES]


def assign_theme(r: Repo) -> str:
    text = " ".join([r.slug, r.desc, " ".join(r.tags), " ".join(r.topics)]).lower()
    best, best_hits = "Other", 0
    for name, pats in _THEME_RE:  # ties go to the earlier (more specific) theme
        hits = sum(1 for p in pats if p.search(text))
        if hits > best_hits:
            best, best_hits = name, hits
    return best


# ---------------------------------------------------------------- output
SRC_LABEL = {"gh": "GitHub", "ts": "Trendshift", "hn": "Hacker News"}


def build_payload(day: str, repos: dict[str, Repo], counts: dict[str, int], history_days: int) -> dict:
    ranked = sorted(repos.values(), key=lambda r: (len(r.sources) > 1, r.momentum,
                                                   (r.hn or {}).get("points") or 0), reverse=True)
    themes: dict[str, list[Repo]] = {}
    for r in ranked:
        themes.setdefault(r.theme, []).append(r)
    theme_list = sorted(themes.items(), key=lambda kv: (kv[0] != "Other", sum(x.momentum for x in kv[1])),
                        reverse=True)
    max_mom = max((sum(x.momentum for x in v) for _, v in theme_list), default=1) or 1
    gh = [r for r in repos.values() if "gh" in r.sources]
    top_theme_gh = max(({t: sum(1 for r in gh if r.theme == t) for t, _ in theme_list}).items(),
                       key=lambda kv: kv[1], default=("", 0))
    return {
        "date": day,
        "history_days": history_days,
        "generated": dt.datetime.now(dt.timezone.utc).strftime("%H:%M UTC"),
        "counts": counts,
        "stats": {
            "unique": len(repos),
            "multi": sum(1 for r in repos.values() if len(r.sources) > 1),
            "new": sum(1 for r in repos.values() if "new" in r.signals),
            "top_theme": top_theme_gh[0],
            "top_theme_share": round(100 * top_theme_gh[1] / len(gh)) if gh else 0,
        },
        "themes": [{"name": t, "count": len(v), "width": round(100 * sum(x.momentum for x in v) / max_mom),
                    "examples": [x.slug.split("/")[1] for x in v[:5]]} for t, v in theme_list if t != "Other"],
        "repos": [{
            "slug": r.slug, "desc": r.desc, "lang": r.lang, "today": r.momentum,
            "today_by": r.stars_today, "total": r.stars_total, "src": sorted(r.sources),
            "sig": r.signals, "tags": sorted(r.tags)[:3], "mentions": sorted(r.mentions),
            "theme": r.theme, "hist": r.hist, "hn": r.hn,
        } for r in ranked],
    }


def write_html(payload: dict, prev_day: str | None, next_day: str | None) -> Path:
    tpl = TEMPLATE.read_text(encoding="utf-8")
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    def link(d: str | None) -> str | None:
        return f"{d}.html" if d and (OUT_DIR / f"{d}.html").exists() else None
    nav = {"prev": link(prev_day), "next": link(next_day)}
    page = (tpl.replace("/*__DATA__*/null", data)
               .replace("/*__NAV__*/null", json.dumps(nav))
               .replace("__TITLE__", html.escape(f"Trend Radar — {payload['date']}")))
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{payload['date']}.html"
    path.write_text(page, encoding="utf-8")
    if next_day is None:  # index.html always mirrors the latest digest
        (OUT_DIR / "index.html").write_text(page, encoding="utf-8")
    return path


def write_md(payload: dict) -> Path:
    lines = [f"# Trend Radar — {payload['date']}", ""]
    s = payload["stats"]
    lines += [f"{s['unique']} repos · {s['multi']} on 2+ sources · {s['new']} new · "
              f"top GitHub theme: {s['top_theme']} ({s['top_theme_share']}%)", "", "## Themes", ""]
    for t in payload["themes"]:
        lines.append(f"- **{t['name']}** ({t['count']}): {', '.join(t['examples'])}")
    lines += ["", "## Watch list", ""]
    for i, r in enumerate(payload["repos"][:20], 1):
        badges = " ".join(f"`{x.upper()}`" for x in r["sig"])
        src = "/".join(SRC_LABEL[x] for x in r["src"])
        lines.append(f"{i}. [{r['slug']}](https://github.com/{r['slug']}) +{r['today']:,} today "
                     f"· {src} {badges}  \n   {r['desc']}")
    path = OUT_DIR / f"{payload['date']}.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="override snapshot date (YYYY-MM-DD), for testing")
    ap.add_argument("--no-enrich", action="store_true")
    args = ap.parse_args()
    day = args.date or dt.datetime.now(dt.timezone.utc).date().isoformat()

    results, counts = {}, {}
    for name, fn in (("gh", fetch_github), ("ts", fetch_trendshift), ("hn", fetch_hn)):
        try:
            results[name] = fn()
        except Exception as e:  # one failing source shouldn't kill the digest
            print(f"[warn] {SRC_LABEL[name]} failed: {e}", file=sys.stderr)
            results[name] = []
        counts[name] = len(results[name])
        print(f"{SRC_LABEL[name]:<12} {counts[name]:>3} repos")
    if not any(counts.values()):
        print("All sources failed; nothing written.", file=sys.stderr)
        return 1

    repos = merge(results["gh"], results["ts"], results["hn"])
    db = open_db()
    store(db, day, repos)
    if not args.no_enrich:
        print(f"Enriched     {enrich(db, repos):>3} repos via GitHub API")
    history_days = compute_signals(db, day, repos)
    for r in repos.values():
        r.theme = assign_theme(r)
        db.execute("UPDATE repos SET theme=? WHERE repo=?", (r.theme, r.key))
    db.commit()

    all_dates = [d for (d,) in db.execute("SELECT DISTINCT date FROM snapshots ORDER BY date")]
    i = all_dates.index(day)
    prev_day = all_dates[i - 1] if i > 0 else None
    next_day = all_dates[i + 1] if i + 1 < len(all_dates) else None

    payload = build_payload(day, repos, counts, history_days)
    html_path, md_path = write_html(payload, prev_day, next_day), write_md(payload)
    print(f"Wrote {html_path.name} and {md_path.name} in {OUT_DIR} "
          f"({len(repos)} repos, {history_days} day(s) of history)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
