"""Caption styles, hook text, crop focus and logo overlay: checked on real rendered pixels."""
import subprocess

import pytest

from app import video as v


def frame(path, t):
    return subprocess.run(["ffmpeg", "-v", "error", "-ss", str(t), "-i", str(path), "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                          capture_output=True, check=True).stdout


def mad(a, b, w, r0, r1, c0=0, c1=None):
    c1 = c1 or w
    tot = n = 0
    for y in range(r0, r1, 3):
        ra, rb = a[y * w + c0:y * w + c1][::3], b[y * w + c0:y * w + c1][::3]
        tot += sum(abs(x - z) for x, z in zip(ra, rb)); n += len(ra)
    return tot / n


def test_normalize_style_rejects_unknown_values():
    assert v.normalize_style(None)["caption_style"] == "classic"
    for bad in ({"caption_style": "neon"}, {"caption_pos": "left"}, {"logo_pos": "centre"}, {"crop_x": 2}, {"crop_x": "abc"}):
        with pytest.raises(v.VideoError):
            v.normalize_style(bad)
    assert v.normalize_style({"crop_x": "0.25", "logo": True})["logo"] is True


def test_chunk_cues_keeps_timing_and_words():
    cues = [{"start": 10.0, "end": 16.0, "text": "one two three four five six seven"}]
    ch = v.chunk_cues(cues, 3)
    assert " ".join(c["text"] for c in ch) == "one two three four five six seven"
    assert ch[0]["start"] == 10.0 and abs(ch[-1]["end"] - 16.0) < 1e-6
    assert all(a["end"] == pytest.approx(b["start"]) for a, b in zip(ch, ch[1:]))   # gapless
    assert len(ch[-1]["text"].split()) >= 2                                          # no one-word orphan
    assert v.chunk_cues([{"start": 0, "end": 1, "text": "   "}]) == []


def test_ass_variants():
    cue = [{"start": 0, "end": 2, "text": "alpha beta gamma delta epsilon zeta"}]
    classic = v.make_ass(cue, 1080, 1920)
    assert "Style: Default,Arial,70" in classic and ",2," in classic and classic.count("Dialogue: 0") == 1
    pop = v.make_ass(cue, 1080, 1920, {"caption_style": "pop"})
    assert "&H0000FFFF" in pop and pop.count("Dialogue: 0") == 2           # split into short phrases, yellow
    box = v.make_ass(cue, 1080, 1920, {"caption_style": "box", "caption_pos": "middle"})
    assert ",3," in box and ",5," in box                                       # opaque box, centre alignment
    hook = v.make_ass(cue, 1080, 1920, {"hook_text": "Wait for it {x}"})
    assert "Dialogue: 1,0:00:00.00,0:00:03.00,Hook" in hook and "Wait for it (x)" in hook


def test_pop_captions_burned_into_lower_area(media, tmp_path):
    cues = v.cues_for_clip(v.parse_captions(media["srt"].read_text()), 15, 25)
    plain, pop = tmp_path / "p.mp4", tmp_path / "q.mp4"
    v.render_clip(media["video"], plain, 15, 25, "9:16")
    meta = v.render_clip(media["video"], pop, 15, 25, "9:16", cues=cues, style={"caption_style": "pop", "caption_pos": "middle"})
    assert meta["captioned"] is True
    a, b = frame(plain, 2.0), frame(pop, 2.0)
    assert mad(a, b, 1080, 700, 1250) > 3          # middle band changed
    assert mad(a, b, 1080, 0, 400) < 0.6           # top untouched


def test_hook_text_shows_only_at_start_and_is_not_a_caption(media, tmp_path):
    plain, hook = tmp_path / "p.mp4", tmp_path / "h.mp4"
    v.render_clip(media["video"], plain, 15, 25, "9:16")
    meta = v.render_clip(media["video"], hook, 15, 25, "9:16", style={"hook_text": "WAIT FOR THE END"})
    assert meta["hook"] is True and meta["captioned"] is False       # hook must never satisfy a 'captions required' check
    early = mad(frame(plain, 1.0), frame(hook, 1.0), 1080, 80, 450)
    late = mad(frame(plain, 6.0), frame(hook, 6.0), 1080, 80, 450)
    assert early > 3 and late < 0.8


def test_crop_focus_moves_the_framing(media, tmp_path):
    left, right = tmp_path / "l.mp4", tmp_path / "r.mp4"
    v.render_clip(media["video"], left, 15, 20, "9:16", style={"crop_x": 0})
    v.render_clip(media["video"], right, 15, 20, "9:16", style={"crop_x": 1})
    assert mad(frame(left, 2), frame(right, 2), 1080, 200, 1700) > 8
    assert v.probe(left)["width"] == 1080 and v.probe(right)["height"] == 1920


def test_logo_overlay_only_in_requested_corner(media, tmp_path):
    logo = tmp_path / "logo.png"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=red:s=200x100", "-frames:v", "1", str(logo)], check=True)
    plain, withlogo = tmp_path / "p.mp4", tmp_path / "w.mp4"
    v.render_clip(media["video"], plain, 15, 20, "9:16")
    meta = v.render_clip(media["video"], withlogo, 15, 20, "9:16", style={"logo": True, "logo_pos": "tr"}, logo_path=logo)
    assert meta["logo"] is True
    a, b = frame(plain, 2), frame(withlogo, 2)
    assert mad(a, b, 1080, 30, 150, 800, 1050) > 20      # top-right corner changed a lot
    assert mad(a, b, 1080, 1500, 1800, 0, 300) < 0.6     # bottom-left untouched
    # logo requested but file missing -> silently no logo (and reported)
    none = tmp_path / "n.mp4"
    assert v.render_clip(media["video"], none, 15, 20, "9:16", style={"logo": True}, logo_path=tmp_path / "missing.png")["logo"] is False
