import subprocess

import pytest

from app import video as v


def frame(path, t):
    r = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(t), "-i", str(path), "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                       capture_output=True, check=True)
    return r.stdout


def test_probe(media):
    i = v.probe(media["video"])
    assert (i["width"], i["height"], i["has_audio"], i["vcodec"]) == (640, 360, True, "h264")
    assert 39.5 < i["duration"] < 40.5
    assert v.probe(media["silent"])["has_audio"] is False


def test_probe_rejects_non_video(tmp_path):
    p = tmp_path / "x.mp4"
    p.write_bytes(b"not a video at all")
    with pytest.raises(v.VideoError):
        v.probe(p)


def test_audio_levels_are_one_per_second(media):
    lv = v.audio_levels(media["video"])
    assert 39 <= len(lv) <= 42
    assert lv[20] > lv[5] + 15  # loud burst vs quiet background


def test_highlight_finds_the_known_loud_segment(media):
    lv = v.audio_levels(media["video"])
    hl = v.find_highlights(lv, clip_seconds=10, count=3)
    top = hl[0]
    assert top["start"] >= 12 and top["end"] <= 30, top  # inside/around the 15-27 s burst
    assert top["score"] > hl[-1]["score"]
    # non-overlapping, sorted by score
    spans = sorted((h["start"], h["end"]) for h in hl)
    assert all(a[1] <= b[0] for a, b in zip(spans, spans[1:]))


def test_highlights_edge_cases():
    assert v.find_highlights([-50] * 3) == []
    assert v.find_highlights([-50.0] * 60, 10, 3)  # flat audio must not crash
    assert len(v.find_highlights([-40.0 + (i % 7) for i in range(100)], 10, 50)) <= 50


def test_transcript_boosts_hook_segments():
    levels = [-30.0] * 100  # identical audio everywhere
    cues = [{"start": 60, "end": 70, "text": "Why is this the biggest mistake? Nobody tells you 3 secrets"}]
    hl = v.find_highlights(levels, 10, 1, cues=cues)
    assert 50 <= hl[0]["start"] <= 62 and "hook" in hl[0]["reason"] and hl[0]["title"].startswith("Why is this")


def test_scene_times_runs(media):
    assert isinstance(v.scene_times(media["video"]), list)


def test_analyze_source_requires_audio(media):
    with pytest.raises(v.VideoError, match="no audio"):
        v.analyze_source(media["silent"])
    r = v.analyze_source(media["video"], clip_seconds=10, count=2, use_scenes=False)
    assert r["highlights"] and r["used_scenes"] is False


def test_parse_srt_and_vtt():
    srt = "\ufeff1\r\n00:00:01,500 --> 00:00:03,000\r\n<i>Hello</i>\r\nworld\r\n\r\n2\r\n01:00:00,000 --> 01:00:02,250\r\nLate\r\n"
    c = v.parse_captions(srt)
    assert c[0] == {"start": 1.5, "end": 3.0, "text": "Hello world"} and c[1]["start"] == 3600
    vtt = "WEBVTT\n\n00:01.000 --> 00:02.500 align:start\nHi {\\an8}there\n\n"
    assert v.parse_captions(vtt)[0] == {"start": 1.0, "end": 2.5, "text": "Hi there"}
    with pytest.raises(v.VideoError):
        v.parse_captions("garbage")


def test_cues_for_clip_shift_and_clip():
    cues = [{"start": 5, "end": 15, "text": "a"}, {"start": 30, "end": 40, "text": "b"}]
    out = v.cues_for_clip(cues, 10, 20)
    assert out == [{"start": 0, "end": 5, "text": "a"}]


def test_ass_has_safe_zone_and_escapes():
    ass = v.make_ass([{"start": 0, "end": 1.5, "text": "Hi {x}\nthere"}], 1080, 1920)
    assert "PlayResY: 1920" in ass and "Dialogue: 0,0:00:00.00,0:00:01.50" in ass and "Hi (x)\\Nthere" in ass


@pytest.mark.parametrize("aspect,size", [("9:16", (1080, 1920)), ("1:1", (1080, 1080)), ("16:9", (1920, 1080)), ("4:5", (1080, 1350))])
def test_render_dimensions_and_qa(media, tmp_path, aspect, size):
    out = tmp_path / "o.mp4"
    meta = v.render_clip(media["video"], out, 15, 25, aspect=aspect)
    i = v.probe(out)
    assert (i["width"], i["height"]) == size and i["vcodec"] == "h264" and i["pix_fmt"] == "yuv420p" and i["has_audio"]
    assert abs(i["duration"] - 10) < 0.4 and meta["captioned"] is False
    qa = v.qa_check(out, {"aspect": aspect, "max_seconds": 60})
    assert qa["status"] == "pass", qa


def test_blur_fit_differs_from_crop(media, tmp_path):
    a, b = tmp_path / "a.mp4", tmp_path / "b.mp4"
    v.render_clip(media["video"], a, 15, 20, "9:16", "crop")
    v.render_clip(media["video"], b, 15, 20, "9:16", "blur")
    assert (v.probe(b)["width"], v.probe(b)["height"]) == (1080, 1920)
    assert frame(a, 2) != frame(b, 2)


def test_captions_are_really_burned_in(media, tmp_path):
    cues = v.cues_for_clip(v.parse_captions(media["srt"].read_text()), 15, 25)
    plain, capt = tmp_path / "p.mp4", tmp_path / "c.mp4"
    v.render_clip(media["video"], plain, 15, 25, "9:16")
    meta = v.render_clip(media["video"], capt, 15, 25, "9:16", cues=cues)
    assert meta["captioned"] is True
    assert frame(plain, 2.5) != frame(capt, 2.5)  # a cue is on screen at t=2.5 s of the clip
    # Lossy encoding changes pixels slightly everywhere, so compare mean absolute difference per band:
    # the caption band (lower third, above the platform UI zone) must change a lot; the top must not.
    w = 1080
    fp, fc = frame(plain, 2.5), frame(capt, 2.5)

    def mad(r0, r1):
        a, b = fp[r0 * w:r1 * w][::5], fc[r0 * w:r1 * w][::5]
        return sum(abs(x - y) for x, y in zip(a, b)) / len(a)
    assert mad(0, 600) < 0.5  # nothing burned into the top of the frame
    assert mad(1300, 1750) > 3


def test_render_without_audio_source(media, tmp_path):
    out = tmp_path / "s.mp4"
    v.render_clip(media["silent"], out, 0, 5, "1:1")
    i = v.probe(out)
    assert i["has_audio"] is False
    assert v.qa_check(out, {"aspect": "1:1"}, expect_audio=False)["status"] == "pass"


def test_render_validates_inputs(media, tmp_path):
    with pytest.raises(v.VideoError):
        v.render_clip(media["video"], tmp_path / "x.mp4", 0, 0.2)
    with pytest.raises(v.VideoError):
        v.render_clip(media["video"], tmp_path / "x.mp4", 100, 110)  # beyond end
    with pytest.raises(v.VideoError):
        v.render_clip(media["video"], tmp_path / "x.mp4", 0, 5, aspect="2:1")
    assert not (tmp_path / "x.mp4").exists()


def test_end_is_clamped_to_video_length(media, tmp_path):
    out = tmp_path / "e.mp4"
    meta = v.render_clip(media["video"], out, 35, 90, "9:16")
    assert 4 < meta["duration"] < 5.6


def names(qa, status):
    return {c["name"] for c in qa["checks"] if c["status"] == status}


def test_qa_catches_each_requirement_failure(media, tmp_path):
    out = tmp_path / "o.mp4"
    v.render_clip(media["video"], out, 15, 25, "9:16")
    qa = v.qa_check(out, {"aspect": "16:9", "max_seconds": 5, "min_seconds": 30, "captions": True, "max_mb": 0.001}, captioned=False)
    assert qa["status"] == "fail"
    assert {"Aspect ratio matches brief", "Within maximum length", "Meets minimum length", "File size limit", "Captions present"} <= names(qa, "fail")
    ok = v.qa_check(out, {"aspect": "9:16", "min_seconds": 8, "max_seconds": 12, "max_mb": 50})
    assert ok["status"] == "pass"


def test_qa_detects_silent_audio_and_black_video(media, tmp_path):
    out = tmp_path / "mute.mp4"
    v.render_clip(media["mute"], out, 0, 6, "9:16", normalize=False)
    assert "Audible audio" in names(v.qa_check(out, {}), "fail")
    black = tmp_path / "black.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=black:s=1080x1920:r=15:d=4", "-f", "lavfi", "-i", "sine=f=300:d=4",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(black)], check=True)
    assert "No black frames" in names(v.qa_check(black, {}), "fail")


def test_qa_flags_unreadable_and_wrong_codec(tmp_path, media):
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"xx")
    assert v.qa_check(bad, {})["status"] == "fail"
    low = tmp_path / "low.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(media["video"]), "-t", "3", "-vf", "scale=320:568", "-c:v", "mpeg4", "-c:a", "aac", str(low)], check=True)
    q = v.qa_check(low, {})
    assert {"Compatible codec (H.264, yuv420p)", "Resolution"} <= names(q, "warn")
