from datetime import datetime, timezone

from app.brief import analyze_brief

MON = datetime(2026, 11, 23, tzinfo=timezone.utc)  # a Monday


def test_full_brief():
    a = analyze_brief("Need 10 TikTok and Instagram Reels clips from my podcast, under 60 seconds each, with captions. "
                      "Budget $25 per clip. Due by Friday. Source: https://youtu.be/abc", MON)
    assert a["platforms"] == ["tiktok", "instagram_reels"] and a["aspect"] == "9:16" and not a["aspect_assumed"]
    assert a["max_seconds"] == 60 and a["count"] == 10 and a["captions"] is True
    assert a["budget"] == {"amount": 25.0, "amount_max": None, "currency": "USD", "per": "clip"}
    assert a["deadline"] == "2026-11-27"
    assert a["links"][0]["kind"] == "youtube" and "podcast" in a["content_types"]
    assert a["requirements"]["max_seconds"] == 60 and a["requirements"]["captions"] is True


def test_range_budget_and_recurring():
    a = analyze_brief("15-30 second shorts, 5 videos per week, $150-200/month.", MON)
    assert (a["min_seconds"], a["max_seconds"]) == (15, 30) and a["count"] == 5 and a["recurring"]
    assert a["budget"]["amount"] == 150 and a["budget"]["amount_max"] == 200 and a["budget"]["per"] == "month"


def test_count_is_not_confused_with_duration():
    assert analyze_brief("I need 30 second clips of my stream")["count"] is None
    assert analyze_brief("3 square clips 1:1")["count"] == 3
    assert analyze_brief("Cut my 2 hour webinar into 3 clips")["count"] == 3


def test_relative_deadlines_and_other_currencies():
    a = analyze_brief("within 48 hours, PKR 5000", MON)
    assert a["deadline"] == "2026-11-25" and a["budget"]["currency"] == "PKR"
    assert analyze_brief("deadline 2026-12-31")["deadline"] == "2026-12-31"
    assert analyze_brief("ASAP please")["urgent"] is True


def test_red_flags_detected():
    a = analyze_brief("Unpaid test project. Pay a $20 registration fee. Telegram only. Earn $500 per day easy money. Gift cards accepted")
    codes = {f["code"] for f in a["red_flags"]}
    assert {"unpaid", "upfront_fee", "off_platform", "too_good", "payment_method"} <= codes
    assert analyze_brief("Clean job: 5 TikTok clips, $20 each, 2 revisions")["red_flags"] == []


def test_unsupported_features_are_reported_not_promised():
    a = analyze_brief("Word by word animated captions like Hormozi, add b-roll, background music, translate to Spanish, logo watermark, thumbnails")
    assert len(a["unsupported"]) >= 5


def test_missing_info_becomes_questions_not_guesses():
    a = analyze_brief("Edit my video")
    assert a["budget"] is None and a["deadline"] is None and a["count"] is None
    assert any("budget" in q.lower() for q in a["questions"])


def test_empty_input_is_safe():
    a = analyze_brief("")
    assert a["requirements"]["aspect"] is None
