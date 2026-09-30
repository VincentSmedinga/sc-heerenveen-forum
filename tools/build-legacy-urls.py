#!/usr/bin/env python3
"""Build data/legacy-urls.csv: the normalised list of old phpBB URLs (issue #22).

Sources
  wayback      Wayback Machine CDX API (fetched live)
  phpbb-dump   every real topic and every board that holds topics, read from the
               private phpBB dump (optional; pass --dump). The dump itself never
               enters the repo, only the ids derived from it.

Normalisation
  - host: lower-case, port and a leading "www." removed, https assumed
  - session and view parameters (sid, sd, sk, st, highlight, ...) removed
  - viewtopic.php: ?t=X[&start=N] (topic) or ?p=X (post)
  - viewforum.php: ?f=X[&start=N]
  - profile.php and memberlist.php ?mode=viewprofile&u=X: memberlist.php?mode=viewprofile&u=X
  - everything else (posting.php, login.php, search.php, ...): the path only. The query
    string is dropped and `id` stays empty

Usage
  tools/build-legacy-urls.py [--dump PATH] [--out data/legacy-urls.csv]
"""
import argparse
import collections
import csv
import html
import json
import re
import sys
import time
import urllib.request
from urllib.parse import parse_qsl, quote, urlsplit

CDX = "https://web.archive.org/cdx/search/cdx?url={}&output=json&collapse=urlkey&fl=original,mimetype"
# heerenveen.org/forum/* also returns www.heerenveen.org, so one query covers both.
QUERIES = ["sc.heerenveen.org/forum/*", "heerenveen.org/forum/*"]
CANONICAL_HOST = "sc.heerenveen.org"


def fetch_cdx(pattern):
    url = CDX.format(quote(pattern, safe="*/"))
    for attempt in range(4):
        try:
            with urllib.request.urlopen(url, timeout=300) as r:
                return json.load(r)[1:]
        except Exception as e:  # CDX answers 504 now and then
            print(f"  retry {attempt + 1} for {pattern}: {e}", file=sys.stderr)
            time.sleep(10 * (attempt + 1))
    sys.exit(f"CDX query failed: {pattern}")


def num(v):
    """Canonical form of a numeric parameter ("0042" -> "42"), or None if it isn't a number."""
    return str(int(v)) if v is not None and re.fullmatch(r"\d+", v) else None


def normalise(original, mime):
    """Return (url, type, id, start) or (None, reason, None, None)."""
    u = urlsplit(html.unescape(original))
    host = re.sub(r"^www\.", "", (u.hostname or "").lower())
    m = re.match(r"^/forum(?:/(.*))?$", u.path, re.I)
    if not m:
        return None, "outside-forum", None, None
    page = m.group(1) or ""
    q = dict(parse_qsl(u.query, keep_blank_values=True))
    base = f"https://{host}/forum/"

    def out(path, kind, id_="", start=""):
        return base + path, kind, id_, start

    if page in ("", "index.php"):
        c = num(q.get("c"))
        return out("index.php" if page else "", "index") if not c else out(f"index.php?c={c}", "index", c)
    if page == "viewtopic.php":
        p, t, start = num(q.get("p")), num(q.get("t")), num(q.get("start"))
        if p:
            return out(f"viewtopic.php?p={p}", "post", p)
        if t:
            s = f"&start={start}" if start and start != "0" else ""
            return out(f"viewtopic.php?t={t}{s}", "topic", t, start if s else "")
        return None, "invalid", None, None
    if page == "viewforum.php":
        f, start = num(q.get("f")), num(q.get("start"))
        if not f or "mark" in q:
            return None, "invalid" if not f else "action", None, None
        s = f"&start={start}" if start and start != "0" else ""
        return out(f"viewforum.php?f={f}{s}", "forum", f, start if s else "")
    if page in ("memberlist.php", "profile.php") and q.get("mode") == "viewprofile":
        uid = num(q.get("u"))
        if not uid:
            return None, "invalid", None, None
        return out(f"memberlist.php?mode=viewprofile&u={uid}", "profile", uid)
    if page in ("file.php", "download/file.php"):  # phpBB 3 serves attachments from download/
        if (att := num(q.get("id"))) is not None:
            return out(f"{page}?id={att}", "attachment", att)
        if q.get("avatar"):
            return out(f"{page}?avatar={quote(q['avatar'])}", "avatar", q["avatar"])
        return None, "invalid", None, None
    if page == "feed.php":
        f, t = num(q.get("f")), num(q.get("t"))
        qs = "&".join(x for x in (f and f"f={f}", t and f"t={t}") if x)
        return out("feed.php" + (f"?{qs}" if qs else ""), "feed", t or f or "")
    # Static resources (smilies, styles, images) don't need redirects.
    if re.search(r"\.(gif|jpe?g|png|css|js|ico|swf)$", page, re.I) or (mime or "").startswith(("image/", "text/css")):
        return None, "asset", None, None
    return out(page, "other")  # posting.php, login.php, search.php, privmsg.php, ...


# --- phpBB dump (optional) -------------------------------------------------

def read_statement(text, table):
    """Yield the rows of a multi-row `INSERT INTO table VALUES (...),(...);` as lists of str."""
    pre = f"INSERT INTO `{table}` VALUES\n"
    pos = 0
    while (k := text.find(pre, pos)) >= 0:
        i = k + len(pre)
        while True:
            assert text[i] == "(", (table, text[i:i + 60])
            i += 1
            row = []
            while True:
                if text[i] == "'":
                    j, buf = i + 1, []
                    while True:
                        c = text[j]
                        if c == "\\":
                            buf.append(text[j + 1]); j += 2
                        elif c == "'":
                            if text[j + 1] == "'":
                                buf.append("'"); j += 2
                            else:
                                break
                        else:
                            buf.append(c); j += 1
                    row.append("".join(buf)); i = j + 1
                else:
                    j = i
                    while text[j] not in ",)":
                        j += 1
                    row.append(text[i:j]); i = j
                if text[i] == ",":
                    i += 1; continue
                i += 1; break
            yield row
            if text[i] == ";":
                break
            i += 2  # ",\n"
        pos = i


def table_columns(text, table):
    body = text[text.index(f"CREATE TABLE `{table}` ("):]
    body = body[:body.index("\n)")]
    return re.findall(r"^\s+`(\w+)`", body, re.M)


def read_dump(path):
    text = open(path, encoding="utf8", errors="replace").read()

    def dicts(table):
        cols = table_columns(text, table)
        return (dict(zip(cols, r)) for r in read_statement(text, table))

    real_topics = [r for r in dicts("topics") if r["topic_moved_id"] == "0"]  # skip "moved" shadow rows
    topics = {r["topic_id"] for r in real_topics}
    with_topics = {r["forum_id"] for r in real_topics}
    forums = {r["forum_id"] for r in dicts("forums") if r["forum_type"] == "1"}  # every postable board
    populated = forums & with_topics  # boards that hold at least one real topic
    # Only ids are needed from posts; each row starts with "(post_id,topic_id,".
    start = text.index("INSERT INTO `posts` VALUES\n")
    end = text.index("/*!40000 ALTER TABLE `posts` ENABLE KEYS */;", start)  # post text can contain ";\n"
    posts = set(re.findall(r"^\((\d+),\d+,", text[start:end], re.M))
    return topics, forums, populated, posts


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dump", help="path to the private phpBB SQL dump")
    ap.add_argument("--out", default="data/legacy-urls.csv")
    args = ap.parse_args()

    rows = {}  # url -> dict
    skipped = collections.Counter()

    def add(url, kind, id_, start, source):
        r = rows.setdefault(url, {"url": url, "type": kind, "id": id_, "start": start, "source": set()})
        r["source"].add(source)

    for pattern in QUERIES:
        print(f"CDX {pattern}", file=sys.stderr)
        for original, mime in fetch_cdx(pattern):
            url, kind, id_, start = normalise(original, mime)
            if url is None:
                skipped[kind] += 1
            else:
                add(url, kind, id_, start, "wayback")

    known = None
    if args.dump:
        print("reading dump", file=sys.stderr)
        topics, forums, populated, posts = read_dump(args.dump)
        print(f"  {len(topics)} topics, {len(forums)} boards ({len(populated)} with topics), {len(posts)} posts", file=sys.stderr)
        known = {"topic": topics, "forum": forums, "post": posts}
        for t in topics:
            add(f"https://{CANONICAL_HOST}/forum/viewtopic.php?t={t}", "topic", t, "", "phpbb-dump")
        for f in populated:
            add(f"https://{CANONICAL_HOST}/forum/viewforum.php?f={f}", "forum", f, "", "phpbb-dump")

    fields = ["url", "type", "id", "start", "in_dump", "source"]
    with open(args.out, "w", newline="", encoding="utf8") as fh:
        w = csv.DictWriter(fh, fields, lineterminator="\n")
        w.writeheader()
        for r in sorted(rows.values(), key=lambda r: (r["type"], int(r["id"]) if r["id"].isdigit() else 0, int(r["start"] or 0), r["url"])):
            in_dump = ""
            if known and r["type"] in known:
                in_dump = "yes" if r["id"] in known[r["type"]] else "no"
            w.writerow({**r, "in_dump": in_dump, "source": ";".join(sorted(r["source"]))})

    by_type = collections.Counter(r["type"] for r in rows.values())
    print(f"wrote {len(rows)} urls to {args.out}: {dict(by_type)}", file=sys.stderr)
    print(f"skipped: {dict(skipped)}", file=sys.stderr)


if __name__ == "__main__":
    main()
