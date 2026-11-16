"""Turns a raw 16:9 section into a 1080x1920 short with burned-in captions."""
import os
import subprocess

W, H = 1080, 1920


def _ts(t):
    t = max(0.0, t)
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


def _clean(s):
    return s.replace("{", "(").replace("}", ")").replace("\\", "").replace("\n", " ")


def write_ass(path, words, duration, ecfg, credit=None):
    font, size = ecfg.get("font", "DejaVu Sans"), ecfg.get("font_size", 80)
    per = ecfg.get("caption_words", 3)
    cap_margin = 420 if ecfg.get("layout", "blur") == "blur" else 480
    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}", "WrapStyle: 0", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour,"
        " Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline,"
        " Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Cap,{font},{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,6,2,2,60,60,{cap_margin},1",
        f"Style: Credit,{font},{int(size * 0.7)},&H0000E5FF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,5,0,8,40,40,330,1",
        "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    if credit:
        lines.append(f"Dialogue: 0,{_ts(0)},{_ts(duration)},Credit,,0,0,0,,{_clean(credit)}")
    # group words into short chunks; break on pauses and sentence ends
    chunk = []
    for i, w in enumerate(words):
        if w["end"] <= 0 or w["start"] >= duration:
            continue
        chunk.append(w)
        nxt = words[i + 1] if i + 1 < len(words) else None
        pause = nxt is not None and nxt["start"] - w["end"] > 0.6
        if len(chunk) >= per or pause or w["word"][-1:] in ".?!" or nxt is None:
            start, end = chunk[0]["start"], min(duration, (nxt["start"] if nxt and not pause else chunk[-1]["end"] + 0.3))
            text = _clean(" ".join(c["word"] for c in chunk)).upper()
            lines.append(f"Dialogue: 1,{_ts(start)},{_ts(end)},Cap,,0,0,0,,{text}")
            chunk = []
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def render(src, out, offset, duration, words, ecfg, credit=None):
    """src: downloaded section; offset: where the clip starts inside src (seconds)."""
    work = os.path.dirname(os.path.abspath(out))
    ass_name = os.path.basename(out) + ".ass"
    write_ass(os.path.join(work, ass_name), words, duration, ecfg, credit if ecfg.get("show_credit", True) else None)
    if ecfg.get("layout", "blur") == "crop":
        vf = f"scale=-2:{H},crop={W}:{H},ass={ass_name}"
    else:
        zoom = float(ecfg.get("zoom", 1.35))
        fw = int(W * zoom) // 2 * 2
        vf = (f"split[a][b];[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
              f"boxblur=20:3,eq=brightness=-0.08[bg];[b]scale={fw}:-2,crop={W}:ih[fg];"
              f"[bg][fg]overlay=(W-w)/2:(H-h)/2,ass={ass_name}")
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{offset:.2f}", "-i", os.path.abspath(src),
           "-t", f"{duration:.2f}", "-filter_complex", vf,
           "-af", "loudnorm=I=-14:TP=-1.5:LRA=11", "-r", "30",
           "-c:v", "libx264", "-preset", ecfg.get("preset", "veryfast"), "-crf", str(ecfg.get("crf", 21)),
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart",
           os.path.abspath(out)]
    subprocess.run(cmd, check=True, cwd=work)
    os.remove(os.path.join(work, ass_name))
