"""Opportunity discovery from RSS/Atom feeds and manual imports, plus lead scoring and outreach drafts."""
from __future__ import annotations

import re
from urllib.parse import urljoin

import feedparser
import httpx

from .brief import analyze_brief
from .db import Database, now
from .security import UnsafeURL, check_url

POSITIVE = [
    (r"\bclipper\b|\bclipping\b|video clips?|\bclips\b", 25), (r"shorts|reels|tik\s?-?tok", 15),
    (r"video edit(or|ing)?|\beditor\b", 15), (r"highlights?|repurpos|short[- ]form", 15),
    (r"podcast|webinar|stream(er)?|interview", 8), (r"captions?|subtitles?", 5),
    (r"\bpaid\b|budget|per (video|clip|short)|\brate\b|[$€£]\s?\d|\busd\b|\bpkr\b", 15),
]
MAX_FEED_BYTES = 3 * 1024 * 1024
UA = "AUTOPILOT/2.0 (personal RSS reader; +local)"


def score_lead(title: str, summary: str = "") -> dict:
    text = f"{title}\n{summary}".lower()
    score, matched = 0, []
    for pat, w in POSITIVE:
        m = re.search(pat, text)
        if m:
            score += w
            matched.append(m.group(0).strip())
    flags = analyze_brief(text)["red_flags"]
    for f in flags:
        score -= 40 if f["severity"] == "high" else 20
    return {"score": max(0, min(100, score)), "matched": ", ".join(dict.fromkeys(matched)),
            "flags": ",".join(f["code"] for f in flags)}


def save_lead(db: Database, title: str, url: str | None, summary: str, source: str) -> int | None:
    s = score_lead(title, summary)
    _, n = db.run("INSERT OR IGNORE INTO leads(title,url,summary,source,created,score,matched,flags) VALUES(?,?,?,?,?,?,?,?)",
                  (title[:250], url, summary[:4000], source, now(), s["score"], s["matched"], s["flags"]))
    if not n:
        return None
    row = db.one("SELECT id FROM leads WHERE url IS ? ORDER BY id DESC", (url,)) if url else db.one("SELECT MAX(id) AS id FROM leads")
    return row["id"] if row else None


def fetch_feed(url: str, allow_private: bool = False, timeout: float = 15) -> bytes:
    """Fetch with a timeout, a size cap, and SSRF checks on every redirect hop."""
    current = check_url(url, allow_private)
    with httpx.Client(timeout=timeout, headers={"User-Agent": UA}, follow_redirects=False) as client:
        for _ in range(4):
            with client.stream("GET", current) as r:
                if r.is_redirect:
                    current = check_url(urljoin(current, r.headers.get("location", "")), allow_private)
                    continue
                r.raise_for_status()
                buf = bytearray()
                for chunk in r.iter_bytes():
                    buf += chunk
                    if len(buf) > MAX_FEED_BYTES:
                        raise ValueError("Feed is larger than 3 MB")
                return bytes(buf)
    raise ValueError("Too many redirects")


def scan_feeds(db: Database, allow_private: bool = False) -> dict:
    report, new_total = [], 0
    for f in db.all("SELECT * FROM feeds"):
        new, err = 0, ""
        try:
            parsed = feedparser.parse(fetch_feed(f["url"], allow_private))
            if parsed.bozo and not parsed.entries:
                raise ValueError("Not a valid RSS/Atom feed")
            for e in parsed.entries[:50]:
                link = str(e.get("link", ""))[:1000]
                if not link:
                    continue
                summary = re.sub(r"<[^>]+>", " ", str(e.get("summary", "")))
                summary = re.sub(r"\s+", " ", summary).strip()[:1500]
                if save_lead(db, str(e.get("title", "Untitled")), link, summary, f["url"]):
                    new += 1
        except (httpx.HTTPError, UnsafeURL, ValueError) as ex:
            err = str(ex)[:200]
        db.update("feeds", f["id"], last_checked=now(), last_error=err)
        new_total += new
        report.append({"feed": f["url"], "new": new, "error": err})
    return {"new_leads": new_total, "feeds_scanned": len(report), "feeds": report, "review_required": True}


def draft_outreach(title: str, text: str, business_name: str) -> dict:
    """Template-based draft. The user edits and approves it; nothing is sent automatically.
    It makes no price, deadline or delivery promise."""
    a = analyze_brief(f"{title}\n{text}")
    plat = {"tiktok": "TikTok", "youtube_shorts": "YouTube Shorts", "instagram_reels": "Instagram Reels", "linkedin": "LinkedIn",
            "facebook": "Facebook", "x_twitter": "X"}
    names = [plat[p] for p in a["platforms"] if p in plat]
    for_what = f"short-form clips for {', '.join(names)}" if names else "short-form video clips"
    qs = [q for q in a["questions"] if "Most short-form" not in q][:4]
    body = (f"Hi,\n\nI saw your post \"{title[:120]}\" and I'd be glad to help with {for_what}.\n\n"
            "I cut long videos into short clips with captions, correct framing for each platform and normalised audio, "
            "and I check every clip against your requirements before sending it.\n")
    if qs:
        body += "\nBefore I send a quote, could you confirm:\n" + "\n".join(f"- {q}" for q in qs) + "\n"
    body += f"\nThanks,\n{business_name}"
    return {"subject": f"Re: {title[:80]}", "body": body, "analysis": a}
