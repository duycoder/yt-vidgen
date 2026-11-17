#!/usr/bin/env python3
"""
vidgen - turn a voice-over script (.md with [pause Xs] markers) + per-part audio
into a narrated video with illustrations and simple Ken Burns / crossfade effects.

Project folder layout:
    <project>/texts/*.md          script: "## Part 01. Title (...)" sections
    <project>/voices/part 1.m4a   audio per part (any ext, name contains "part N")
    <project>/pics/               images per scene: p01_s01.png / .jpg / .webp
    <project>/prompts.json        (generated) image prompt per scene - edit freely
    <project>/output/             (generated) plan.json + video.mp4

Usage:
    python vidgen.py plan   "<project>"
    python vidgen.py images "<project>" [--provider pollinations|openai|placeholder]
    python vidgen.py render "<project>" [--titles]
    python vidgen.py all    "<project>" [...]
"""
import argparse
import json
import os
import re
import subprocess
import sys
import textwrap
import time
import urllib.parse
from pathlib import Path

AUDIO_EXT = {".m4a", ".mp3", ".wav", ".aac", ".ogg", ".flac"}
IMG_EXT = [".png", ".jpg", ".jpeg", ".webp"]
DEFAULT_STYLE = ("cinematic documentary illustration, painterly digital art, soft natural light, "
                 "rich detail, muted earthy colours, 16:9, no text, no watermark")


def log(*a, **k):
    """Progress output; the GUI replaces this to capture logs."""
    print(*a, **k)


# --------------------------------------------------------------------------- parsing
def parse_script(md_path: Path):
    """Return list of parts: {num, title, tokens:[('w', word)|('p', secs)]}."""
    text = md_path.read_text(encoding="utf-8")
    parts = []
    for m in re.finditer(r"^##\s*Part\s*0*(\d+)\.?\s*(.*?)\s*$(.*?)(?=^##\s*Part|\Z)", text, re.M | re.S):
        num, header, body = int(m.group(1)), m.group(2), m.group(3)
        title = re.sub(r"\s*\(.*$", "", header).strip()
        body = body.split("\n---")[0].strip()
        tokens = []
        for piece in re.split(r"(\[pause\s*[\d.]+\s*s?\])", body, flags=re.I):
            pm = re.match(r"\[pause\s*([\d.]+)", piece, re.I)
            if pm:
                tokens.append(("p", float(pm.group(1))))
            else:
                tokens += [("w", w) for w in piece.split()]
        parts.append({"num": num, "title": title, "tokens": tokens})
    if not parts:
        raise RuntimeError(f"No '## Part NN.' sections found in {md_path}")
    return parts


def find_voice(voices_dir: Path, num: int):
    for f in sorted(voices_dir.iterdir()):
        if f.suffix.lower() in AUDIO_EXT:
            m = re.search(r"part\s*0*(\d+)", f.stem, re.I)
            if m and int(m.group(1)) == num:
                return f
    return None


# --------------------------------------------------------------------------- audio
def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", **kw)


def duration(path: Path) -> float:
    r = run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)])
    return float(r.stdout.strip())


def silences(path: Path, noise_db=-35, min_d=0.2):
    r = run(["ffmpeg", "-hide_banner", "-i", str(path), "-af",
             f"silencedetect=noise={noise_db}dB:d={min_d}", "-f", "null", "-"])
    out, start = [], None
    for line in r.stderr.splitlines():
        if "silence_start" in line:
            start = float(line.split("silence_start:")[1])
        elif "silence_end" in line and start is not None:
            end = float(line.split("silence_end:")[1].split("|")[0])
            out.append((start, end))
            start = None
    return out


def align_markers(tokens, dur, sil):
    """Estimate each pause marker's time, then snap it to a real detected silence."""
    words_per_sec = 2.6
    total_w = sum(1 for t in tokens if t[0] == "w")
    total_p = sum(t[1] for t in tokens if t[0] == "p")
    # crude: weight pauses in word-units so the proportional estimate is closer
    weight_total = total_w + total_p * words_per_sec
    markers, acc = [], 0.0
    for i, (kind, val) in enumerate(tokens):
        if kind == "w":
            acc += 1
        else:
            est = dur * (acc + val * words_per_sec / 2) / weight_total
            markers.append({"idx": i, "pause": val, "est": est})
            acc += val * words_per_sec
    min_len = lambda p: 0.7 if p >= 1 else (0.35 if p >= 0.5 else 0.2)
    last_t, used = 0.0, set()
    for mk in markers:
        best = None
        for j, (s, e) in enumerate(sil):
            if j in used or s < last_t or e - s < min_len(mk["pause"]) or e >= dur - 0.05:
                continue
            dist = abs((s + e) / 2 - mk["est"])
            if dist < 5 and (best is None or dist < best[0]):
                best = (dist, j)
        if best:
            s, e = sil[best[1]]
            used.add(best[1])
            mk.update(t=(s + e) / 2, sil=(round(s, 3), round(e, 3)), snapped=True)
        else:
            mk.update(t=max(mk["est"], last_t + 0.5), snapped=False)
        last_t = mk["t"]
    return markers


def build_scenes(part, dur, markers, sil, split_pause=1.0, max_scene=16.0, min_scene=4.0, min_merge=3.5):
    """Cut at markers with pause >= split_pause; split over-long scenes at the inner marker nearest the middle."""
    cuts = [m for m in markers if m["pause"] >= split_pause and m.get("snapped", True)]

    def refine(a_t, b_t, a_idx, b_idx):
        if b_t - a_t <= max_scene:
            return []
        mid = (a_t + b_t) / 2
        inner = [m for m in markers if a_idx < m["idx"] < b_idx and m.get("snapped")
                 and m["t"] - a_t >= min_scene and b_t - m["t"] >= min_scene]
        if not inner:
            return []
        m = min(inner, key=lambda m: abs(m["t"] - mid))
        return refine(a_t, m["t"], a_idx, m["idx"]) + [m] + refine(m["t"], b_t, m["idx"], b_idx)

    def split_by_silence(a, b):
        """No pause marker inside a long scene: cut at a natural silence near the middle,
        and cut the text at the sentence end closest to the proportional word position."""
        if b["t"] - a["t"] <= max_scene:
            return []
        cand = [(s, e) for s, e in sil if e - s >= 0.3 and (s + e) / 2 - a["t"] >= min_scene
                and b["t"] - (s + e) / 2 >= min_scene]
        if not cand:
            return []
        mid = (a["t"] + b["t"]) / 2
        s, e = min(cand, key=lambda se: abs((se[0] + se[1]) / 2 - mid))
        t = (s + e) / 2
        word_idx = [i for i in range(a["idx"] + 1, b["idx"]) if part["tokens"][i][0] == "w"]
        target = word_idx[0] + (len(word_idx)) * (t - a["t"]) / (b["t"] - a["t"])
        ends = [i for i in word_idx[:-1] if re.search(r"[.!?:;]$", part["tokens"][i][1])]
        if not ends:
            return []
        cut_i = min(ends, key=lambda i: abs(i - target))
        m = {"t": t, "idx": cut_i + 0.5}
        return split_by_silence(a, m) + [m] + split_by_silence(m, b)

    bounds = [{"t": 0.0, "idx": -1}] + cuts + [{"t": dur, "idx": len(part["tokens"])}]
    full = [bounds[0]]
    for a, b in zip(bounds, bounds[1:]):
        full += refine(a["t"], b["t"], a["idx"], b["idx"]) + [b]
    full2 = [full[0]]
    for a, b in zip(full, full[1:]):
        full2 += split_by_silence(a, b) + [b]
    # merge very short scenes into the following one
    full = [full2[0]] + [m for i, m in enumerate(full2[1:-1], 1)
                         if m["t"] - full2[i - 1]["t"] >= min_merge] + [full2[-1]]

    scenes = []
    for k, (a, b) in enumerate(zip(full, full[1:]), 1):
        lo, hi = int(a["idx"] + 1), int(b["idx"] + (0.5 if b["idx"] % 1 else 0))
        words = [v for kind, v in part["tokens"][lo:hi] if kind == "w"]
        scenes.append({
            "id": f"p{part['num']:02d}_s{k:02d}",
            "start": round(a["t"], 3), "end": round(b["t"], 3),
            "text": " ".join(words),
        })
    return scenes


# --------------------------------------------------------------------------- plan
def cmd_plan(proj: Path, args):
    md_files = sorted((proj / "texts").glob("*.md"))
    if not md_files:
        raise RuntimeError(f"No .md in {proj/'texts'}")
    parts = parse_script(md_files[0])
    plan = {"script": str(md_files[0].name), "parts": []}
    for part in parts:
        voice = find_voice(proj / "voices", part["num"])
        if not voice:
            log(f"  ! Part {part['num']}: no audio found, skipped")
            continue
        dur = duration(voice)
        sil = silences(voice)
        markers = align_markers(part["tokens"], dur, sil)
        scenes = build_scenes(part, dur, markers, sil, args.split_pause, args.max_scene)
        snapped = sum(m.get("snapped", False) for m in markers)
        log(f"  Part {part['num']:02d} {dur:6.1f}s  {len(scenes)} scenes  "
              f"(markers snapped to silence: {snapped}/{len(markers)})")
        plan["parts"].append({"num": part["num"], "title": part["title"], "voice": voice.name,
                              "duration": dur, "scenes": scenes})
    out = proj / "output"
    out.mkdir(exist_ok=True)
    (out / "plan.json").write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")

    # prompts.json: keep user edits, add new scenes
    pf = proj / "prompts.json"
    prompts = json.loads(pf.read_text(encoding="utf-8")) if pf.exists() else {}
    prompts.setdefault("style", DEFAULT_STYLE)
    prompts.setdefault("scenes", {})
    for p in plan["parts"]:
        for s in p["scenes"]:
            if s["id"] not in prompts["scenes"]:
                first = re.split(r"(?<=[.!?])\s", s["text"])
                prompts["scenes"][s["id"]] = " ".join(first[:2])[:300]
    pf.write_text(json.dumps(prompts, indent=2, ensure_ascii=False), encoding="utf-8")
    n = sum(len(p["scenes"]) for p in plan["parts"])
    log(f"Plan: {n} scenes -> {out/'plan.json'}\nPrompts -> {pf}  (edit these for better images)")


def load_plan(proj: Path):
    pf = proj / "output" / "plan.json"
    if not pf.exists():
        raise RuntimeError("Run 'plan' first.")
    return json.loads(pf.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- images
def find_image(pics: Path, sid: str):
    for ext in IMG_EXT:
        f = pics / f"{sid}{ext}"
        if f.exists():
            return f
    return None


def gen_pollinations(prompt, dest: Path, w, h, seed):
    import requests
    url = ("https://image.pollinations.ai/prompt/" + urllib.parse.quote(prompt)
           + f"?width={w}&height={h}&nologo=true&seed={seed}&model=flux")
    r = requests.get(url, timeout=180)
    r.raise_for_status()
    if not r.headers.get("content-type", "").startswith("image"):
        raise RuntimeError(f"not an image: {r.text[:200]}")
    dest.with_suffix(".jpg").write_bytes(r.content)


def gen_openai(prompt, dest: Path, w, h, seed):
    import base64, requests
    key = os.environ.get("OPENAI_API_KEY") or sys.exit("Set OPENAI_API_KEY")
    r = requests.post("https://api.openai.com/v1/images/generations", timeout=300,
                      headers={"Authorization": f"Bearer {key}"},
                      json={"model": os.environ.get("OPENAI_IMAGE_MODEL", "gpt-image-1"),
                            "prompt": prompt, "size": "1536x1024", "n": 1})
    r.raise_for_status()
    dest.with_suffix(".png").write_bytes(base64.b64decode(r.json()["data"][0]["b64_json"]))


def gen_placeholder(prompt, dest: Path, w, h, seed, label=""):
    from PIL import Image, ImageDraw, ImageFont
    import random
    rnd = random.Random(seed)
    c1 = tuple(rnd.randint(20, 90) for _ in range(3))
    c2 = tuple(min(255, v + rnd.randint(40, 110)) for v in c1)
    img = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(img)
    for y in range(h):
        t = y / h
        d.line([(0, y), (w, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(c1, c2)))
    try:
        font = ImageFont.truetype("arial.ttf", 54)
        small = ImageFont.truetype("arial.ttf", 36)
    except OSError:
        font = small = ImageFont.load_default()
    d.text((80, 70), label, font=small, fill=(255, 255, 255))
    d.multiline_text((80, h // 3), "\n".join(textwrap.wrap(prompt, 52)[:8]), font=font,
                     fill=(255, 255, 255), spacing=14)
    img.save(dest.with_suffix(".png"))


def cmd_images(proj: Path, args):
    plan = load_plan(proj)
    prompts = json.loads((proj / "prompts.json").read_text(encoding="utf-8"))
    pics = proj / "pics"
    pics.mkdir(exist_ok=True)
    gen = {"pollinations": gen_pollinations, "openai": gen_openai, "placeholder": gen_placeholder}[args.provider]
    for p in plan["parts"]:
        for s in p["scenes"]:
            sid = s["id"]
            if find_image(pics, sid) and not args.force:
                continue
            scene_prompt = prompts["scenes"].get(sid, s["text"])
            full = f"{scene_prompt}. {prompts.get('style', '')}"
            seed = int(re.sub(r"\D", "", sid))
            log(f"  {sid}: {scene_prompt[:80]}...", flush=True)
            for attempt in range(3):
                try:
                    if args.provider == "placeholder":
                        gen_placeholder(scene_prompt, pics / sid, args.width, args.height, seed, sid)
                    else:
                        gen(full, pics / sid, args.width, args.height, seed)
                    break
                except Exception as e:  # noqa: BLE001
                    log(f"    attempt {attempt+1} failed: {e}")
                    time.sleep(5 * (attempt + 1))
            else:
                log("    -> using placeholder")
                gen_placeholder(scene_prompt, pics / sid, args.width, args.height, seed, sid)


# --------------------------------------------------------------------------- render
EFFECTS = ["zoom_in", "pan_right", "zoom_out", "pan_left", "zoom_in_tl", "pan_up",
           "zoom_in_br", "pan_down", "zoom_out_tl", "zoom_in_drift"]
TRANSITIONS_SOFT = ["fade", "dissolve", "fadeblack", "smoothleft", "smoothright", "hblur", "fadegrays"]
TRANSITIONS_ALL = TRANSITIONS_SOFT + ["smoothup", "smoothdown", "circleopen", "circleclose", "wipeleft",
                                      "wiperight", "slideleft", "slideright", "radial", "zoomin", "diagtl",
                                      "diagbr", "horzopen", "vertopen", "squeezeh", "pixelize"]


def zoompan_expr(effect, frames, zmax, ax=0.5, ay=0.5):
    """ax/ay = zoom anchor (0..1): centre zooms aim at the framed focus instead of the middle."""
    N = max(frames - 1, 1)
    p = f"(on/{N})"
    cx, cy = f"(iw-iw/zoom)*{ax:.4f}", f"(ih-ih/zoom)*{ay:.4f}"
    zin, zout = f"1+{zmax-1}*{p}", f"{zmax}-{zmax-1}*{p}"
    return {
        "zoom_in":       (zin, cx, cy),
        "zoom_out":      (zout, cx, cy),
        "pan_right":     (f"{zmax}", f"(iw-iw/zoom)*{p}", cy),
        "pan_left":      (f"{zmax}", f"(iw-iw/zoom)*(1-{p})", cy),
        "pan_up":        (f"{zmax}", cx, f"(ih-ih/zoom)*(1-{p})"),
        "pan_down":      (f"{zmax}", cx, f"(ih-ih/zoom)*{p}"),
        "zoom_in_tl":    (zin, "(iw-iw/zoom)*0.3", "(ih-ih/zoom)*0.3"),
        "zoom_in_br":    (zin, "(iw-iw/zoom)*0.7", "(ih-ih/zoom)*0.7"),
        "zoom_out_tl":   (zout, "(iw-iw/zoom)*0.3", "(ih-ih/zoom)*0.3"),
        "zoom_in_drift": (zin, f"(iw-iw/zoom)*(0.35+0.3*{p})", cy),
        "static":        ("1.0001", cx, cy),
    }[effect]


def pick_effects(args, part_num, n, offset):
    """Per-scene Ken Burns effect and per-cut transition. Random modes are seeded per part,
    so re-rendering one part gives the same result; change --seed for a new shuffle."""
    import random
    rng = random.Random(f"{args.seed}-{part_num}")

    def no_repeat(pool, k):
        out = []
        for _ in range(k):
            choices = [x for x in pool if not out or x != out[-1]] or pool
            out.append(rng.choice(choices))
        return out

    mode = args.effects
    if mode == "cycle":
        eff = [EFFECTS[(offset + i) % len(EFFECTS)] for i in range(n)]
    elif mode == "random":
        eff = no_repeat(EFFECTS, n)
    else:
        eff = [mode] * n
    t = args.transition
    if t == "random_soft":
        trans = no_repeat(TRANSITIONS_SOFT, max(n - 1, 0))
    elif t == "random_all":
        trans = no_repeat(TRANSITIONS_ALL, max(n - 1, 0))
    else:
        trans = [t] * max(n - 1, 0)
    return eff, trans


def ff_escape(path: str) -> str:
    return path.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def shot_id(sid, k):
    """Image slot k (1-based) of a scene: p01_s02, p01_s02_2, p01_s02_3..."""
    return sid if k == 1 else f"{sid}_{k}"


def load_prompts(proj):
    pf = proj / "prompts.json"
    return json.loads(pf.read_text(encoding="utf-8")) if pf.exists() else {}


def shot_counts(proj):
    return load_prompts(proj).get("shots", {})


def default_framing(img_path, W, H):
    """No framing set: center crop for landscape images; portrait images are shown whole on a
    blurred background; slightly tall images keep their upper part (heads) instead of the middle."""
    try:
        from PIL import Image
        with Image.open(img_path) as im:
            w, h = im.size
        if w / h < 1.0:  # portrait: show the whole picture on a blurred copy (no cut heads)
            return {"fit": "contain"}
        if w / h < 1.25:  # near-square (e.g. 1:1, 5:4): keep the top, where heads usually are
            return {"fit": "cover", "fx": 0.5, "fy": 0.1}
    except Exception:  # noqa: BLE001
        pass
    return {"fit": "cover", "fx": 0.5, "fy": 0.5}


def shot_filter(i, effect, fr, W, H, FPS, d, frames, zoom):
    """ffmpeg filter for one image shot -> [v{i}] of exactly d seconds.
    fr = framing: fit cover (crop at fx/fy) | contain (whole image on a blurred copy) | scroll (pan the whole image)."""
    W2, H2 = W * 2, H * 2
    fit = fr.get("fit", "cover")
    tail = f"trim=duration={d:.3f},setpts=PTS-STARTPTS,format=yuv420p[v{i}]"
    if fit == "scroll":
        # move a frame-sized window across the whole image along its long axis
        back = fr.get("dir") == "back"
        p = f"min(1,t/{max(d, 0.1):.3f})"
        p = f"(1-{p})" if back else p
        return (f"[{i}:v]scale='if(gt(a,{W/H:.4f}),-2,{W2})':'if(gt(a,{W/H:.4f}),{H2},-2)',setsar=1,"
                f"loop=loop={frames}:size=1:start=0,setpts=N/{FPS}/TB,fps={FPS},"
                f"crop={W2}:{H2}:x='(iw-ow)*{p}':y='(ih-oh)*{p}',scale={W}:{H},{tail}")
    ax = ay = 0.5
    if fit == "cover":
        ax = min(1.0, max(0.0, float(fr.get("fx", 0.5))))
        ay = min(1.0, max(0.0, float(fr.get("fy", 0.5))))
    z, x, y = zoompan_expr(effect, frames, zoom, ax, ay)
    zp = f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={W}x{H}:fps={FPS},{tail}"
    if fit == "contain":
        return (f"[{i}:v]split[a{i}][b{i}];"
                f"[a{i}]scale={W2}:{H2}:force_original_aspect_ratio=increase,crop={W2}:{H2},boxblur=40:4,"
                f"eq=brightness=-0.12[bg{i}];"
                f"[b{i}]scale={W2}:{H2}:force_original_aspect_ratio=decrease[fg{i}];"
                f"[bg{i}][fg{i}]overlay=(W-w)/2:(H-h)/2,setsar=1,{zp}")
    fx = min(1.0, max(0.0, float(fr.get("fx", 0.5))))
    fy = min(1.0, max(0.0, float(fr.get("fy", 0.5))))
    return (f"[{i}:v]scale={W2}:{H2}:force_original_aspect_ratio=increase,"
            f"crop={W2}:{H2}:x='(iw-ow)*{fx:.4f}':y='(ih-oh)*{fy:.4f}',setsar=1,{zp}")


def expand_shots(proj, scenes, dur, XF):
    """Scenes -> shots. A scene with N images is split into N equal shots.
    Scene cuts keep their crossfade inside the voice silence (start XF/2 before its midpoint)."""
    counts = shot_counts(proj)
    out = []
    for k, s in enumerate(scenes):
        a = 0.0 if k == 0 else max(0.0, s["start"] - XF / 2)
        b = max(0.0, scenes[k + 1]["start"] - XF / 2) if k + 1 < len(scenes) else dur
        n_sh = max(1, min(8, int(counts.get(s["id"], 1))))
        for j in range(n_sh):
            out.append({"id": shot_id(s["id"], j + 1), "text": s["text"], "start": a + (b - a) * j / n_sh})
    return out


def part_timeline(proj, part, pics, args, effect_offset):
    """Everything the renderer decides for one part, without rendering:
    duration + shots [{id, text, start, end, effect, transition (into this shot), framing, image}].
    Used by the renderer, the instant browser preview and the skip-unchanged check."""
    W, H, XF = args.width, args.height, args.xfade
    dur = part["duration"] + args.gap
    shots = expand_shots(proj, part["scenes"], dur, XF)
    framing = load_prompts(proj).get("framing", {})
    n = len(shots)
    effects, transitions = pick_effects(args, part["num"], n, effect_offset)
    for i, sh in enumerate(shots):
        img = find_image(pics, sh["id"])
        fr = framing.get(sh["id"]) or (default_framing(img, W, H) if img else {"fit": "cover"})
        sh.update(end=shots[i + 1]["start"] if i + 1 < n else dur, image=img, framing=fr,
                  effect=fr.get("effect") or effects[i], transition=transitions[i - 1] if i else None)
    return dur, shots


def part_signature(proj, part, pics, args, effect_offset):
    """Hash of every input of a rendered part; unchanged hash -> the old part file can be reused."""
    import hashlib
    dur, shots = part_timeline(proj, part, pics, args, effect_offset)
    voice = proj / "voices" / part["voice"]
    keys = ("width", "height", "fps", "xfade", "zoom", "gap", "titles", "font", "preset", "crf")
    data = {"args": {k: getattr(args, k, None) for k in keys}, "dur": dur, "title": part["title"],
            "voice": [voice.stat().st_size, voice.stat().st_mtime] if voice.exists() else None,
            "shots": [[sh["id"], round(sh["start"], 3), sh["effect"], sh["transition"], sh["framing"],
                       [sh["image"].name, sh["image"].stat().st_size, sh["image"].stat().st_mtime] if sh["image"] else None]
                      for sh in shots]}
    return hashlib.sha1(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()


def render_part(proj, part, pics, tmp: Path, args, effect_offset, scene=None, out_name=None):
    """Render one part. scene="pNN_sMM" renders only that scene's time window (for previews)."""
    W, H, FPS, XF = args.width, args.height, args.fps, args.xfade
    dur = part["duration"] + args.gap
    shots = expand_shots(proj, part["scenes"], dur, XF)
    framing = load_prompts(proj).get("framing", {})
    n = len(shots)
    effects, transitions = pick_effects(args, part["num"], n, effect_offset)
    log("    effects: " + ", ".join(effects) + (" | transitions: " + ", ".join(transitions) if transitions else ""))
    starts = [sh["start"] for sh in shots]
    ends = starts[1:] + [dur]
    sel = list(range(n))
    if scene:
        sel = [i for i, sh in enumerate(shots) if sh["id"] == scene or sh["id"].startswith(scene + "_")]
        if not sel:
            raise ValueError(f"scene {scene} not in part {part['num']}")
    t0, t1 = starts[sel[0]], ends[sel[-1]]
    dur_full, dur = dur, t1 - t0
    inputs, chains = [], []
    for j, i in enumerate(sel):
        sh = shots[i]
        img = find_image(pics, sh["id"])
        if not img:
            gen_placeholder(sh["text"], pics / sh["id"], W, H, i, sh["id"])
            img = find_image(pics, sh["id"])
        d = ends[i] - starts[i] + (XF if i < sel[-1] else 0)
        frames = max(2, round(d * FPS))
        fr = framing.get(sh["id"]) or default_framing(img, W, H)
        effect = fr.get("effect") or effects[i]  # per-image effect overrides the render setting
        inputs += ["-i", str(img)]
        chains.append(shot_filter(j, effect, fr, W, H, FPS, d, frames, args.zoom))
    last = "v0"
    for j in range(1, len(sel)):
        i, out = sel[j], f"x{j}"
        chains.append(f"[{last}][v{j}]xfade=transition={transitions[i-1]}:duration={XF}:offset={starts[i]-t0:.3f}[{out}]")
        last = out
    fade = f"fade=t=in:st=0:d=0.5,fade=t=out:st={dur-0.6:.3f}:d=0.6"
    if args.titles and t0 == 0:
        tf = tmp / f"title_{part['num']:02d}.txt"
        tf.write_text(f"Part {part['num']}. {part['title']}", encoding="utf-8")
        font = ff_escape(args.font)
        fade += (f",drawtext=fontfile='{font}':textfile='{ff_escape(str(tf))}':fontsize={H//18}:"
                 f"fontcolor=white:borderw=2:bordercolor=black@0.5:box=1:boxcolor=black@0.45:boxborderw=18:x=(w-tw)/2:y=h*0.80:"
                 f"alpha='if(lt(t,0.6),t/0.6,if(lt(t,3.4),1,max(0,(4-t)/0.6)))':enable='lt(t,4)'")
    chains.append(f"[{last}]{fade}[vout]")
    cut = f"atrim=start={t0:.3f},asetpts=PTS-STARTPTS," if t0 > 0 else ""
    chains.append(f"[{len(sel)}:a]{cut}aresample=48000,apad=whole_dur={dur:.3f},afade=t=out:st={dur-0.4:.3f}:d=0.4[aout]")
    out_file = tmp / (out_name or f"part_{part['num']:02d}.mp4")
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *inputs,
           "-i", str(proj / "voices" / part["voice"]),
           "-filter_complex", ";".join(chains), "-map", "[vout]", "-map", "[aout]",
           "-t", f"{dur:.3f}", "-c:v", "libx264", "-preset", args.preset, "-crf", str(args.crf),
           "-r", str(FPS), "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", str(out_file)]
    r = run(cmd)
    if r.returncode:
        raise RuntimeError(f"ffmpeg failed on part {part['num']}:\n{r.stderr[-3000:]}")
    return out_file


def cmd_render(proj: Path, args):
    plan = load_plan(proj)
    pics, out = proj / "pics", proj / "output"
    tmp = out / "parts"
    tmp.mkdir(parents=True, exist_ok=True)
    pics.mkdir(exist_ok=True)
    only = set(args.parts or [])
    files, offset = [], 0
    for part in plan["parts"]:
        f = tmp / f"part_{part['num']:02d}.mp4"
        sig_file = f.with_suffix(".sig")
        sig = part_signature(proj, part, pics, args, offset)
        unchanged = f.exists() and sig_file.exists() and sig_file.read_text().strip() == sig
        if only and part["num"] not in only:
            pass
        elif unchanged and not only and not getattr(args, "force", False):
            log(f"  part {part['num']:02d}: không đổi -> giữ bản cũ (done in 0s)")
        else:
            t0 = time.time()
            log(f"  rendering part {part['num']:02d} ({len(part['scenes'])} scenes)...", flush=True)
            sig_file.unlink(missing_ok=True)
            render_part(proj, part, pics, tmp, args, offset)
            sig_file.write_text(sig)
            log(f"    done in {time.time()-t0:.0f}s")
        offset += len(part["scenes"])
        if f.exists():
            files.append(f)
    lst = tmp / "concat.txt"
    lst.write_text("".join(f"file '{f.name}'\n" for f in files), encoding="utf-8")
    final = out / args.output
    r = run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0",
             "-i", str(lst), "-c", "copy", "-movflags", "+faststart", str(final)])
    if r.returncode:
        raise RuntimeError(r.stderr)
    log(f"Video -> {final}  ({duration(final):.1f}s)")


# --------------------------------------------------------------------------- cli
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["plan", "images", "render", "all"])
    ap.add_argument("project", type=Path)
    ap.add_argument("--split-pause", type=float, default=1.0, help="pause length that starts a new scene")
    ap.add_argument("--max-scene", type=float, default=16.0, help="split scenes longer than this (s) at an inner pause")
    ap.add_argument("--provider", default="pollinations", choices=["pollinations", "openai", "placeholder"])
    ap.add_argument("--force", action="store_true", help="regenerate existing images")
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1080)
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--xfade", type=float, default=0.8, help="crossfade seconds between scenes")
    ap.add_argument("--transition", default="fade",
                    help="ffmpeg xfade transition (fade, dissolve, smoothleft...) or random_soft / random_all")
    ap.add_argument("--effects", default="cycle", help="Ken Burns per scene: cycle, random, or one effect name")
    ap.add_argument("--seed", type=int, default=1, help="shuffle seed for random effects/transitions")
    ap.add_argument("--zoom", type=float, default=1.15, help="Ken Burns max zoom")
    ap.add_argument("--gap", type=float, default=0.5, help="extra seconds after each part")
    ap.add_argument("--force", action="store_true", help="re-render every part even if nothing changed")
    ap.add_argument("--titles", action="store_true", help="show 'Part N. Title' at start of each part")
    ap.add_argument("--font", default="C:/Windows/Fonts/georgia.ttf")
    ap.add_argument("--parts", type=int, nargs="*", help="only (re)render these part numbers")
    ap.add_argument("--preset", default="medium")
    ap.add_argument("--crf", type=int, default=20)
    ap.add_argument("--output", default="video.mp4")
    args = ap.parse_args()
    proj = args.project.resolve()
    if args.command in ("plan", "all"):
        cmd_plan(proj, args)
    if args.command in ("images", "all"):
        cmd_images(proj, args)
    if args.command in ("render", "all"):
        cmd_render(proj, args)


if __name__ == "__main__":
    main()
