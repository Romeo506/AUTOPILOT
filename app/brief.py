"""Rule-based client-brief analysis (offline, free, deterministic).

This is pattern matching, not a language model: it extracts what the text states
(formats, lengths, counts, budget, deadline, caption needs) and warns about
common scam / low-quality signals. It never invents requirements that are not in
the text; anything it cannot read stays None and must be filled in by the user.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

PLATFORMS = {
    "tiktok": r"tik\s?-?tok",
    "youtube_shorts": r"youtube\s+shorts?|\byt\s+shorts?|\bshorts\b",
    "instagram_reels": r"\breels?\b|instagram",
    "facebook": r"facebook|\bfb\b",
    "linkedin": r"linkedin",
    "x_twitter": r"\btwitter\b|\bx\.com\b|\bon x\b",
    "youtube": r"youtube|\byt\b",
}
PRESET_FOR = {"tiktok": "tiktok", "youtube_shorts": "youtube_shorts", "instagram_reels": "instagram_reels",
              "linkedin": "square", "facebook": "instagram_reels", "x_twitter": "landscape"}
VERTICAL = {"tiktok", "youtube_shorts", "instagram_reels"}

CONTENT_TYPES = ["podcast", "interview", "webinar", "livestream", "live stream", "stream", "gaming",
                 "lecture", "tutorial", "vlog", "keynote", "sermon", "course", "talk show", "long-form", "long form"]

# Things we do not implement. Reported so the user is never told a feature exists when it does not.
UNSUPPORTED = [
    (r"word[- ]by[- ]word|karaoke|animated (captions|subtitles)|highlight(ed)? words|hormozi",
     "Exact word-by-word highlighting is not supported. Short pop-in captions are available, but their timing is estimated unless real word timestamps exist."),
    (r"face[- ]?track|auto[- ]?reframe|speaker[- ]?track|follow the speaker",
     "Face/speaker tracking is not supported: framing is centre-crop or blurred-background fit."),
    (r"\bb[- ]?roll\b|stock footage|\bemoji|sound effects?|\bsfx\b|\bmeme",
     "B-roll, emoji, SFX and meme overlays are not generated; you would add these in another editor."),
    (r"background music|royalty[- ]free music|add music",
     "Background music is not added automatically."),
    (r"translat|dubb?(ed|ing)|multi[- ]?language|voice[- ]?over",
     "Translation, dubbing and voice-over are not supported."),
    (r"logo|watermark|brand(ed|ing) (template|kit|colors?)|intro|outro",
     "Logo overlay works if you upload the client's logo on the project. Branded templates and intros/outros are not added automatically."),
    (r"thumbnail",
     "Thumbnails are not generated."),
]

RED_FLAGS = [
    ("upfront_fee", "high", r"(pay|send|deposit)\w*\s+(a\s+|an\s+|the\s+)?(?:[$€£]\s?\d[\d,]*\s*)?(registration|training|starter|upfront|up-front|security|joining|onboarding)?\s*(fee|deposit)|buy (a |the )?(course|software|equipment|license)|upfront (payment|fee)|you (will )?(need|have) to pay",
     "Asks you to pay money first. Legitimate clients do not charge freelancers."),
    ("payment_method", "high", r"gift ?cards?|crypto|bitcoin|usdt|western union|cashier'?s? check|wire (me|the)|send (me )?money|overpay",
     "Unusual payment method or request to move money. Common scam pattern."),
    ("unpaid", "high", r"\bunpaid\b|for exposure|volunteer|no pay\b|free (trial|test|sample|work)|test project (is )?unpaid",
     "Unpaid work or free test requested."),
    ("rev_share", "medium", r"rev(enue)?[- ]?share( only)?|commission[- ]only|pay after (the )?(views|monetiz)|performance[- ]based only|paid (once|when) (it|videos?) (earns?|monetiz)",
     "Payment depends on future views/revenue rather than a fixed fee."),
    ("off_platform", "medium", r"(whatsapp|telegram|signal)\s+only|move (to|over to) (whatsapp|telegram)|text me on (whatsapp|telegram)|contact (me )?(only )?(on|via) (whatsapp|telegram)",
     "Asks to move off the platform immediately (often breaks marketplace rules and removes buyer/seller protection)."),
    ("too_good", "medium", r"easy money|guaranteed (income|earnings)|earn \$?\d[\d,]* (a|per) day|no experience (needed|required).{0,40}\$\d{3,}",
     "Income promise that sounds too good to be true."),
]

_CUR = {"$": "USD", "usd": "USD", "us$": "USD", "dollars": "USD", "dollar": "USD", "€": "EUR", "eur": "EUR", "euro": "EUR", "euros": "EUR",
        "£": "GBP", "gbp": "GBP", "pkr": "PKR", "inr": "INR", "cad": "CAD", "aud": "AUD", "aed": "AED"}
_UNIT_SEC = {"s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1, "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60}
_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _num(s: str) -> float:
    return float(s.replace(",", ""))


def _durations(t: str) -> dict:
    out = {"min_seconds": None, "max_seconds": None, "target_seconds": None}
    u = r"(seconds?|secs?|minutes?|mins?|s|m)\b"
    m = re.search(rf"(\d{{1,3}})\s*(?:-|–|to)\s*(\d{{1,3}})\s*[- ]?{u}", t)
    if m:
        k = _UNIT_SEC[m.group(3)]
        out["min_seconds"], out["max_seconds"] = int(m.group(1)) * k, int(m.group(2)) * k
        return out
    m = re.search(rf"(?:under|less than|max(?:imum)?(?: of)?|up to|no (?:longer|more) than|not (?:longer|more) than|below|<)\s*(\d{{1,3}})\s*[- ]?{u}", t)
    if m:
        out["max_seconds"] = int(m.group(1)) * _UNIT_SEC[m.group(2)]
        return out
    m = re.search(rf"(\d{{1,3}})\s*[- ]?{u}\s*(?:long\s*)?(?:clips?|videos?|shorts?|reels?|tiktoks?|highlights?|cuts?)", t)
    if m:
        out["target_seconds"] = out["max_seconds"] = int(m.group(1)) * _UNIT_SEC[m.group(2)]
    return out


def _budget(t: str) -> dict | None:
    sym = r"(\$|us\$|€|£|usd|eur|gbp|pkr|inr|cad|aud|aed)\s?"
    amt = r"(\d[\d,]*(?:\.\d+)?)"
    per = r"(?:\s*(?:per|/|a|each)\s*(video|clip|short|reel|hour|hr|month|week|minute|min|project|edit))?"
    m = re.search(rf"{sym}{amt}(?:\s*(?:-|–|to)\s*(?:{sym})?{amt})?{per}", t)
    cur = None
    if m:
        cur, lo, hi, unit = m.group(1), _num(m.group(2)), m.group(4), m.group(5)
        hi = _num(hi) if hi else None
    else:
        m = re.search(rf"{amt}\s*(usd|eur|gbp|pkr|inr|dollars|euros)\b{per}", t)
        if not m:
            return None
        lo, cur, unit, hi = _num(m.group(1)), m.group(2), m.group(3), None
    return {"amount": lo, "amount_max": hi, "currency": _CUR.get(cur.strip().lower(), "USD"),
            "per": unit.replace("hr", "hour").replace("min", "minute") if unit else None}


def _deadline(t: str, now: datetime) -> tuple[str | None, bool]:
    urgent = bool(re.search(r"\basap\b|\burgent(ly)?\b|right away|immediately", t))
    m = re.search(r"\b(20\d\d)-(\d\d)-(\d\d)\b", t)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date().isoformat(), urgent
        except ValueError:
            pass
    m = re.search(r"(?:within|in|inside|after)\s+(\d{1,3})\s*(hours?|hrs?|days?|weeks?)", t) or \
        re.search(r"(\d{1,3})[- ]\s*(hour|hr|day|week)\s+(?:turnaround|delivery|deadline)", t)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = timedelta(hours=n) if unit.startswith(("h")) else timedelta(days=n) if unit.startswith("d") else timedelta(weeks=n)
        return (now + delta).date().isoformat(), urgent
    m = re.search(r"(?:by|before|due|until|on)\s+(?:this\s+|next\s+)?(monday|tuesday|wednesday|thursday|friday|saturday|sunday)", t)
    if m:
        target = _WEEKDAYS.index(m.group(1))
        ahead = (target - now.weekday()) % 7 or 7
        return (now + timedelta(days=ahead)).date().isoformat(), urgent
    if re.search(r"\btomorrow\b", t):
        return (now + timedelta(days=1)).date().isoformat(), urgent
    if re.search(r"\btoday\b|tonight", t):
        return now.date().isoformat(), urgent
    return None, urgent


def analyze_brief(text: str, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    raw = text or ""
    t = raw.lower()

    platforms = [k for k, pat in PLATFORMS.items() if re.search(pat, t)]
    if "youtube_shorts" in platforms and "youtube" in platforms:
        platforms.remove("youtube")
    presets = []
    for p in platforms:
        pr = PRESET_FOR.get(p)
        if pr and pr not in presets:
            presets.append(pr)

    if any(p in VERTICAL for p in platforms) or re.search(r"vertical|9:16|portrait", t):
        aspect = "9:16"
    elif re.search(r"\b1:1\b|square", t):
        aspect = "1:1"
    elif re.search(r"\b4:5\b", t):
        aspect = "4:5"
    elif re.search(r"16:9|landscape|horizontal", t):
        aspect = "16:9"
    elif re.search(r"\bclips?\b|short[- ]form|highlights?", t):
        aspect = "9:16"  # short-form clipping default; flagged as an assumption below
    else:
        aspect = None
    assumed_aspect = bool(aspect == "9:16" and not any(p in VERTICAL for p in platforms)
                          and not re.search(r"vertical|9:16|portrait", t))

    dur = _durations(t)

    count, recurring = None, None
    m = re.search(r"(?<![\d:$\-–])(\d{1,3})(?![\d\-–])\s*(?:\+\s*)?(?!(?:seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?|months?)\b)(?:[a-z0-9:\-]+\s+){0,2}?(?:short|clip|reel|tiktok|video|highlight|cut|edit)s?\b", t)
    if m:
        count = int(m.group(1))
    if re.search(r"(?<!earn )(?<!\d )per (week|month|day)|weekly|monthly|daily|every (week|month)|ongoing|long[- ]term|retainer", t):
        recurring = True

    captions = bool(re.search(r"captions?|subtitles?|\bsubs\b|closed caption|\bcc\b", t))
    deadline, urgent = _deadline(t, now)

    links = re.findall(r"https?://[^\s)>\]\"']+", raw)
    link_info = []
    for u in links[:10]:
        host = (urlparse(u).hostname or "").lower()
        kind = ("youtube" if "youtu" in host else "google_drive" if "drive.google" in host or "docs.google" in host
                else "dropbox" if "dropbox" in host else "vimeo" if "vimeo" in host else "twitch" if "twitch" in host else "other")
        link_info.append({"url": u, "kind": kind})
    content = [c for c in CONTENT_TYPES if c in t]

    unsupported = [msg for pat, msg in UNSUPPORTED if re.search(pat, t)]
    flags = [{"code": c, "severity": sev, "text": msg} for c, sev, pat, msg in RED_FLAGS if re.search(pat, t)]

    budget = _budget(t)
    questions = []
    if not budget:
        questions.append("No budget is stated - ask the client before quoting.")
    if not deadline:
        questions.append("No deadline is stated - agree one in writing.")
    if not links and not re.search(r"attach|upload|dropbox|drive|send you|will send|provide", t):
        questions.append("How will you receive the source video?")
    if count is None:
        questions.append("How many clips are required?")
    if dur["max_seconds"] is None:
        questions.append("What clip length do they want?")
    if "revision" not in t:
        questions.append("How many revision rounds are included?")
    if captions is False:
        questions.append("Do they want captions? (Most short-form clients do.)")

    requirements = {"aspect": aspect, "min_seconds": dur["min_seconds"], "max_seconds": dur["max_seconds"],
                    "captions": captions, "count": count, "max_mb": None}

    lines = []
    if platforms: lines.append("Platforms: " + ", ".join(platforms))
    if aspect: lines.append(f"Format: {aspect}" + (" (assumed - confirm with client)" if assumed_aspect else ""))
    if dur["max_seconds"]: lines.append(f"Length: {'%ss-' % dur['min_seconds'] if dur['min_seconds'] else 'up to '}{dur['max_seconds']}s")
    if count: lines.append(f"Quantity: {count}" + (" (recurring)" if recurring else ""))
    lines.append("Captions: " + ("requested" if captions else "not mentioned"))
    if budget:
        b = f"{budget['currency']} {budget['amount']:g}" + (f"-{budget['amount_max']:g}" if budget["amount_max"] else "")
        lines.append("Budget: " + b + (f" per {budget['per']}" if budget["per"] else ""))
    if deadline: lines.append(f"Deadline: {deadline}" + (" (urgent)" if urgent else ""))
    elif urgent: lines.append("Marked urgent, no date given")

    return {"platforms": platforms, "suggested_presets": presets, "aspect": aspect, "aspect_assumed": assumed_aspect,
            **dur, "count": count, "recurring": bool(recurring), "captions": captions, "budget": budget,
            "deadline": deadline, "urgent": urgent, "links": link_info, "content_types": content,
            "unsupported": unsupported, "red_flags": flags, "questions": questions,
            "requirements": requirements, "summary": lines}
