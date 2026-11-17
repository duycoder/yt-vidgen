"""
vidgen GUI - local web app.  Run:  python app.py   then open http://127.0.0.1:8765
Projects live as sub-folders of the parent directory (e.g. d:/Vibes/MMO/YT/monk fruit).
"""
import io
import sys
import json
import re
import threading
import time
import traceback
import uuid
from pathlib import Path
from types import SimpleNamespace

import requests
import uvicorn
from fastapi import Body, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image

import sources
import vidgen

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent  # projects folder
SETTINGS = HERE / "settings.json"
app = FastAPI(title="vidgen")

RENDER_DEFAULTS = dict(width=1920, height=1080, fps=30, xfade=0.8, transition="fade", effects="cycle", seed=1,
                       zoom=1.15, gap=0.5,
                       titles=False, font="C:/Windows/Fonts/georgia.ttf", parts=None, preset="medium",
                       crf=20, output="video.mp4", force=False)
PLAN_DEFAULTS = dict(split_pause=1.0, max_scene=16.0)


# ----------------------------------------------------------------- helpers
def keys():
    return json.loads(SETTINGS.read_text(encoding="utf-8")) if SETTINGS.exists() else {}


def proj_dir(name: str) -> Path:
    p = (ROOT / name).resolve()
    if p.parent != ROOT or not re.fullmatch(r"[\w .\-()]+", name):
        raise HTTPException(400, "bad project name")
    return p


def read_json(path: Path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def prompts_of(p: Path):
    d = read_json(p / "prompts.json", {})
    d.setdefault("style", vidgen.DEFAULT_STYLE)
    d.setdefault("scenes", {})
    d.setdefault("queries", {})
    return d


def base_sid(sid: str) -> str:
    """p01_s02_3 -> p01_s02 (scene id of an image slot)."""
    return re.sub(r"_\d+$", "", sid)


def prompt_for(pr, sid):
    return pr["scenes"].get(sid) or pr["scenes"].get(base_sid(sid), "")


def slots_of(pr, sid):
    return [vidgen.shot_id(sid, k) for k in range(1, max(1, int(pr.get("shots", {}).get(sid, 1))) + 1)]


def credits_of(p: Path):
    return read_json(p / "pics" / "credits.json", {})


def save_image(p: Path, sid: str, data: bytes, credit: dict):
    pics = p / "pics"
    pics.mkdir(exist_ok=True)
    img = Image.open(io.BytesIO(data)).convert("RGB")
    if img.width > 2560:
        img = img.resize((2560, round(img.height * 2560 / img.width)), Image.LANCZOS)
    for ext in vidgen.IMG_EXT:  # replace any previous image of this scene
        (pics / f"{sid}{ext}").unlink(missing_ok=True)
    img.save(pics / f"{sid}.jpg", quality=92)
    d = prompts_of(p)
    if d.get("framing", {}).pop(sid, None) is not None:  # new image -> old crop no longer applies
        write_json(p / "prompts.json", d)
    c = credits_of(p)
    c[sid] = credit
    write_json(pics / "credits.json", c)


BROWSER_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/130.0 Safari/537.36", "Accept": "image/avif,image/webp,image/*,*/*;q=0.8"}


def download(url: str) -> bytes:
    r = requests.get(url, headers=sources.UA, timeout=120)
    if r.status_code in (401, 403, 406, 429):  # some sites refuse non-browser clients for a pasted link
        r = requests.get(url, headers=BROWSER_UA, timeout=120)
    r.raise_for_status()
    if not r.headers.get("content-type", "").startswith("image"):
        raise RuntimeError(f"link không phải ảnh ({r.headers.get('content-type')}) - hãy copy địa chỉ HÌNH ẢNH, không phải trang")
    return r.content


# ----------------------------------------------------------------- background jobs
JOBS = {}
LOCK = threading.Lock()  # one heavy job at a time (vidgen.log is global)


def start_job(kind, fn):
    jid = uuid.uuid4().hex[:8]
    job = JOBS[jid] = dict(id=jid, kind=kind, status="running", logs=[], progress=0.0, error=None)

    def emit(*a, **k):
        line = " ".join(str(x) for x in a)
        job["logs"].append(line)
        try:
            print(line)
        except UnicodeEncodeError:
            print(line.encode("ascii", "replace").decode())

    def runner():
        with LOCK:
            vidgen.log = emit
            try:
                fn(job, emit)
                job["status"], job["progress"] = "done", 1.0
            except Exception as e:  # noqa: BLE001
                job["status"], job["error"] = "error", str(e)
                emit(traceback.format_exc())
            finally:
                vidgen.log = print

    threading.Thread(target=runner, daemon=True).start()
    return job


@app.get("/api/jobs/{jid}")
def job_status(jid: str, since: int = 0):
    j = JOBS.get(jid) or HTTPException(404)
    if isinstance(j, HTTPException):
        raise j
    return {**j, "logs": j["logs"][since:], "log_count": len(j["logs"])}


# ----------------------------------------------------------------- settings / sources
@app.get("/api/settings")
def get_settings():
    k = keys()
    return {"keys": {n: ("•••" + k[n][-4:] if k.get(n) and "KEY" in n else k.get(n, "")) for n in sources.KEY_NAMES},
            "sources": sources.available(k)}


@app.put("/api/settings")
def put_settings(data: dict = Body(...)):
    k = keys()
    for n, v in data.items():
        if n in sources.KEY_NAMES and not str(v).startswith("•••"):
            if v:
                k[n] = v.strip()
            else:
                k.pop(n, None)
    write_json(SETTINGS, k)
    return get_settings()


# ----------------------------------------------------------------- projects
@app.get("/api/projects")
def list_projects():
    return [d.name for d in sorted(ROOT.iterdir())
            if d.is_dir() and ((d / "texts").exists() or (d / "voices").exists())]


@app.post("/api/projects")
def create_project(name: str = Body(..., embed=True)):
    p = proj_dir(name.strip())
    for sub in ("texts", "voices", "pics"):
        (p / sub).mkdir(parents=True, exist_ok=True)
    return {"name": p.name}


@app.get("/api/projects/{name}")
def project_state(name: str):
    p = proj_dir(name)
    texts = sorted((p / "texts").glob("*.md")) if (p / "texts").exists() else []
    voices = [f for f in sorted((p / "voices").iterdir()) if f.suffix.lower() in vidgen.AUDIO_EXT] \
        if (p / "voices").exists() else []
    parts, parse_error = [], None
    if texts:
        try:
            for part in vidgen.parse_script(texts[0]):
                v = vidgen.find_voice(p / "voices", part["num"]) if voices else None
                parts.append(dict(num=part["num"], title=part["title"],
                                  words=sum(1 for t in part["tokens"] if t[0] == "w"),
                                  pauses=sum(1 for t in part["tokens"] if t[0] == "p"),
                                  voice=v.name if v else None,
                                  duration=round(vidgen.duration(v), 2) if v else None))
        except Exception as e:  # noqa: BLE001
            parse_error = str(e)
    plan = read_json(p / "output" / "plan.json", None)
    pr, cr = prompts_of(p), credits_of(p)
    if plan:
        for part in plan["parts"]:
            for s in part["scenes"]:
                img = vidgen.find_image(p / "pics", s["id"])
                s["prompt"] = pr["scenes"].get(s["id"], "")
                s["query"] = pr["queries"].get(s["id"]) or sources.auto_query(s["prompt"] or s["text"])
                s["image"] = f"/files/{name}/pics/{img.name}?v={int(img.stat().st_mtime)}" if img else None
                s["credit"] = cr.get(s["id"]) if img else None
                s["shots"] = []
                for slot in slots_of(pr, s["id"]):
                    im = vidgen.find_image(p / "pics", slot)
                    s["shots"].append(dict(id=slot, prompt=pr["scenes"].get(slot, "") if slot != s["id"] else s["prompt"],
                                           image=f"/files/{name}/pics/{im.name}?v={int(im.stat().st_mtime)}" if im else None,
                                           credit=cr.get(slot) if im else None,
                                           framing=pr.get("framing", {}).get(slot),
                                           auto=vidgen.default_framing(im, 1920, 1080) if im else None))
    video = p / "output" / "video.mp4"
    return dict(name=name, texts=[t.name for t in texts], voices=[v.name for v in voices], parts=parts,
                parse_error=parse_error, plan=plan, style=pr["style"],
                video=f"/files/{name}/output/video.mp4?v={int(video.stat().st_mtime)}" if video.exists() else None)


@app.post("/api/projects/{name}/upload")
async def upload(name: str, kind: str = Form(...), files: list[UploadFile] = File(...)):
    p = proj_dir(name)
    dest = p / ("texts" if kind == "text" else "voices")
    dest.mkdir(parents=True, exist_ok=True)
    if kind == "text":  # one script per project
        for old in dest.glob("*.md"):
            old.rename(old.with_suffix(".md.bak"))
    saved = []
    for f in files:
        fn = Path(f.filename).name
        if kind == "text" and not fn.endswith(".md"):
            fn = Path(fn).stem + ".md"
        (dest / fn).write_bytes(await f.read())
        saved.append(fn)
    return {"saved": saved}


@app.post("/api/projects/{name}/script")
def paste_script(name: str, text: str = Body(..., embed=True)):
    p = proj_dir(name)
    (p / "texts").mkdir(parents=True, exist_ok=True)
    for old in (p / "texts").glob("*.md"):
        old.rename(old.with_suffix(".md.bak"))
    (p / "texts" / "script.md").write_text(text, encoding="utf-8")
    return {"ok": True}


@app.post("/api/projects/{name}/plan")
def run_plan(name: str, opts: dict = Body(default={})):
    p = proj_dir(name)
    args = SimpleNamespace(**{**PLAN_DEFAULTS, **{k: v for k, v in opts.items() if k in PLAN_DEFAULTS}})
    return start_job("plan", lambda job, emit: vidgen.cmd_plan(p, args))


@app.put("/api/projects/{name}/style")
def put_style(name: str, style: str = Body(..., embed=True)):
    p = proj_dir(name)
    d = prompts_of(p)
    d["style"] = style
    write_json(p / "prompts.json", d)
    return {"ok": True}


@app.put("/api/projects/{name}/scene/{sid}")
def put_scene(name: str, sid: str, data: dict = Body(...)):
    p = proj_dir(name)
    d = prompts_of(p)
    if "prompt" in data:
        d["scenes"][sid] = data["prompt"]
    if "query" in data:
        d["queries"][sid] = data["query"]
    write_json(p / "prompts.json", d)
    return {"ok": True}


@app.put("/api/projects/{name}/scene/{sid}/shots")
def set_shots(name: str, sid: str, n: int = Body(..., embed=True)):
    p = proj_dir(name)
    d = prompts_of(p)
    d.setdefault("shots", {})[sid] = max(1, min(8, int(n)))
    write_json(p / "prompts.json", d)
    return {"ok": True, "n": d["shots"][sid]}


@app.put("/api/projects/{name}/scene/{sid}/framing")
def set_framing(name: str, sid: str, fr: dict = Body(...)):
    """fit: cover (fx, fy = crop position 0..1) | contain (blurred background) | scroll (dir: fwd/back);
    effect: optional Ken Burns override. Empty body = back to automatic."""
    p = proj_dir(name)
    d = prompts_of(p)
    clean = {k: fr[k] for k in ("fit", "fx", "fy", "effect", "dir") if fr.get(k) not in (None, "")}
    if clean.get("fit") not in (None, "cover", "contain", "scroll"):
        raise HTTPException(400, "fit must be cover, contain or scroll")
    if clean.get("effect") and clean["effect"] not in vidgen.EFFECTS + ["static"]:
        raise HTTPException(400, "unknown effect")
    if clean:
        d.setdefault("framing", {})[sid] = clean
    else:
        d.setdefault("framing", {}).pop(sid, None)
    write_json(p / "prompts.json", d)
    return {"ok": True, "framing": clean or None}


@app.delete("/api/projects/{name}/scene/{sid}/shot/{k}")
def remove_shot(name: str, sid: str, k: int):
    """Remove image slot k and shift the following slots down (keeps images, credits and prompts aligned)."""
    p = proj_dir(name)
    d, c = prompts_of(p), credits_of(p)
    n = max(1, int(d.get("shots", {}).get(sid, 1)))
    if n == 1:
        raise HTTPException(400, "cảnh phải có ít nhất 1 ảnh")
    pics = p / "pics"
    for ext in vidgen.IMG_EXT:
        (pics / f"{vidgen.shot_id(sid, k)}{ext}").unlink(missing_ok=True)
    c.pop(vidgen.shot_id(sid, k), None)
    d.setdefault("framing", {}).pop(vidgen.shot_id(sid, k), None)
    for j in range(k + 1, n + 1):
        src, dst = vidgen.shot_id(sid, j), vidgen.shot_id(sid, j - 1)
        img = vidgen.find_image(pics, src)
        if img:
            img.rename(pics / f"{dst}{img.suffix}")
        if src in c:
            c[dst] = c.pop(src)
        if src in d["scenes"] and j > 1:
            d["scenes"][dst] = d["scenes"].pop(src)
        fr = d.setdefault("framing", {})
        fr.pop(dst, None)
        if src in fr:
            fr[dst] = fr.pop(src)
    d["shots"][sid] = n - 1
    write_json(p / "prompts.json", d)
    write_json(pics / "credits.json", c)
    return {"ok": True}


@app.get("/api/search")
def search(source: str, q: str, n: int = 20):
    if source not in sources.SOURCES or sources.SOURCES[source]["kind"] != "search":
        raise HTTPException(400, "unknown search source")
    try:
        return sources.search(source, q, n, keys())
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"{source}: {e}")


@app.post("/api/projects/{name}/scene/{sid}/pick")
def pick(name: str, sid: str, cand: dict = Body(...)):
    p = proj_dir(name)
    try:
        save_image(p, sid, download(cand["url"]), {k: cand.get(k) for k in
                                                   ("source", "title", "author", "license", "page", "url")})
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"download failed: {e}")
    return {"ok": True}


@app.post("/api/projects/{name}/scene/{sid}/generate")
def generate(name: str, sid: str, source: str = Body(..., embed=True)):
    """AI generation can take minutes (web chats) -> background job."""
    p = proj_dir(name)
    if sources.SOURCES.get(source, {}).get("kind") != "ai":
        raise HTTPException(400, "unknown AI source")
    pr = prompts_of(p)
    prompt = f"{prompt_for(pr, sid)}. {pr['style']}"
    if sid != base_sid(sid) and sid not in pr["scenes"]:  # extra slot without its own prompt: vary the framing
        prompt += " Different composition and camera angle from the previous shot."

    def work(job, emit):
        emit(f"{sid}: generating with {source}")
        data = sources.generate(source, prompt, keys())
        save_image(p, sid, data, {"source": source, "title": "AI generated", "license": "AI generated",
                                  "prompt": prompt})
        emit(f"{sid}: saved")

    return start_job("generate", work)


@app.get("/api/browser/status")
def browser_status():
    import browser_ai
    c = browser_ai.cfg(keys())
    return {"port": c["port"], "alive": browser_ai.cdp_alive(c["port"]),
            "browser": c["browser_path"] or "auto (Chrome/Edge)",
            "profile": c["user_data_dir"] + (f" › {c['profile']}" if c["profile"] else "")}


@app.post("/api/browser/open")
def browser_open():
    import browser_ai
    try:
        how = browser_ai.open_login_tabs(**browser_ai.cfg(keys()))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, str(e))
    return {"how": how}


@app.post("/api/projects/{name}/scene/{sid}/upload")
async def upload_scene(name: str, sid: str, file: UploadFile = File(...)):
    save_image(proj_dir(name), sid, await file.read(), {"source": "upload", "title": file.filename})
    return {"ok": True}


@app.delete("/api/projects/{name}/scene/{sid}/image")
def delete_image(name: str, sid: str):
    p = proj_dir(name)
    for ext in vidgen.IMG_EXT:
        (p / "pics" / f"{sid}{ext}").unlink(missing_ok=True)
    c = credits_of(p)
    c.pop(sid, None)
    write_json(p / "pics" / "credits.json", c)
    return {"ok": True}


@app.post("/api/projects/{name}/autofill")
def autofill(name: str, opts: dict = Body(...)):
    """For every scene without an image, try the chosen sources in order; take the first good landscape image."""
    p = proj_dir(name)
    order, overwrite = opts.get("sources", ["wikimedia", "openverse"]), opts.get("overwrite", False)

    def work(job, emit):
        state = project_state(name)
        # every image slot of every scene (a scene can have several shots)
        slots = [(s, sh, j) for part in state["plan"]["parts"] for s in part["scenes"]
                 for j, sh in enumerate(s["shots"], 1)]
        used = {c.get("url") for c in credits_of(p).values()}
        k = keys()
        cache = {}  # (source, query) -> candidates, so extra slots of a scene take the next result
        for i, (s, sh, j) in enumerate(slots):
            job["progress"] = i / len(slots)
            if sh["image"] and not overwrite:
                continue
            done = False
            for src in order:
                meta = sources.SOURCES[src]
                try:
                    if meta["kind"] == "ai":
                        prompt = f"{sh['prompt'] or s['prompt']}. {state['style']}"
                        if j > 1 and not sh["prompt"]:
                            prompt += " Different composition and camera angle from the previous shot."
                        save_image(p, sh["id"], sources.generate(src, prompt, k),
                                   {"source": src, "title": "AI generated", "license": "AI generated"})
                        emit(f"{sh['id']}: AI image from {src}")
                        done = True
                        break
                    if (src, s["query"]) not in cache:
                        cache[(src, s["query"])] = sources.search(src, s["query"], 20, k)
                    good = [c for c in cache[(src, s["query"])] if c["url"] not in used and c.get("width")
                            and c.get("height") and c["width"] >= 1000 and c["width"] >= c["height"] * 1.15]
                    if not good:
                        continue
                    c = good[0]
                    save_image(p, sh["id"], download(c["url"]), {kk: c.get(kk) for kk in
                                                                 ("source", "title", "author", "license", "page", "url")})
                    used.add(c["url"])
                    emit(f"{sh['id']}: [{src}] {c['title'][:60]}")
                    done = True
                    break
                except Exception as e:  # noqa: BLE001
                    emit(f"{sh['id']}: {src} failed: {e}")
            if not done:
                emit(f"{sh['id']}: no image found for '{s['query']}' - edit the query and search manually")

    return start_job("autofill", work)


@app.post("/api/projects/{name}/preview")
def preview(name: str, opts: dict = Body(default={})):
    """Quick low-res render of one part or one scene into output/preview/ (never touches the real parts)."""
    p = proj_dir(name)
    plan = read_json(p / "output" / "plan.json", None)
    if not plan:
        raise HTTPException(400, "run plan first")
    scene = opts.get("scene")
    if scene and not re.fullmatch(r"p\d+_s\d+", scene):
        raise HTTPException(400, "bad scene id")
    num = int(opts.get("part") or (int(scene[1:].split("_")[0]) if scene else 0))
    idx = next((k for k, x in enumerate(plan["parts"]) if x["num"] == num), None)
    if idx is None:
        raise HTTPException(400, f"no part {num}")
    keep = {k: v for k, v in opts.items() if k in RENDER_DEFAULTS and k not in ("width", "height", "preset", "crf", "parts", "output")}
    args = SimpleNamespace(**{**RENDER_DEFAULTS, **keep, "width": 854, "height": 480, "fps": 25, "preset": "ultrafast", "crf": 28})
    offset = sum(len(x["scenes"]) for x in plan["parts"][:idx])
    out = p / "output" / "preview"
    out.mkdir(parents=True, exist_ok=True)
    fname = f"preview_{scene or f'p{num:02d}'}.mp4"

    def work(job, emit):
        t = time.time()
        emit(f"Xem thử {'cảnh ' + scene if scene else f'part {num}'} (480p)...")
        vidgen.render_part(p, plan["parts"][idx], p / "pics", out, args, offset, scene=scene, out_name=fname)
        emit(f"xong sau {time.time() - t:.0f}s")
        f = out / fname
        job["result"] = f"/files/{name}/output/preview/{fname}?v={int(f.stat().st_mtime)}"

    return start_job("preview", work)


@app.post("/api/projects/{name}/timeline")
def timeline(name: str, opts: dict = Body(default={})):
    """What the renderer would do for one part (shots, timings, effects, framing) -> instant browser preview."""
    p = proj_dir(name)
    plan = read_json(p / "output" / "plan.json", None)
    if not plan:
        raise HTTPException(400, "run plan first")
    num = int(opts.get("part") or 0)
    idx = next((k for k, x in enumerate(plan["parts"]) if x["num"] == num), None)
    if idx is None:
        raise HTTPException(400, f"no part {num}")
    args = SimpleNamespace(**{**RENDER_DEFAULTS, **{k: v for k, v in opts.items() if k in RENDER_DEFAULTS}})
    part = plan["parts"][idx]
    offset = sum(len(x["scenes"]) for x in plan["parts"][:idx])
    dur, shots = vidgen.part_timeline(p, part, p / "pics", args, offset)
    url = lambda f: f"/files/{name}/pics/{f.name}?v={int(f.stat().st_mtime)}" if f else None
    return {"num": num, "title": part["title"], "duration": dur, "xfade": args.xfade, "zoom": args.zoom,
            "titles": bool(args.titles), "voice": f"/files/{name}/voices/{part['voice']}",
            "shots": [{**{k: sh[k] for k in ("id", "text", "start", "end", "effect", "transition", "framing")},
                       "image": url(sh["image"])} for sh in shots]}


@app.post("/api/projects/{name}/render")
def render(name: str, opts: dict = Body(default={})):
    p = proj_dir(name)
    args = SimpleNamespace(**{**RENDER_DEFAULTS, **{k: v for k, v in opts.items() if k in RENDER_DEFAULTS}})
    plan = read_json(p / "output" / "plan.json", None)
    if not plan:
        raise HTTPException(400, "run plan first")
    total = len(plan["parts"]) if not args.parts else len(args.parts)

    def work(job, emit):
        def tracking(*a, **k):
            emit(*a, **k)
            if a and "done in" in str(a[0]):
                job["progress"] = min(0.98, job["progress"] + 1 / (total + 0.2))
        vidgen.log = tracking
        vidgen.cmd_render(p, args)
        write_credits(p, plan)

    return start_job("render", work)


def write_credits(p: Path, plan):
    c = credits_of(p)
    pr = prompts_of(p)
    lines, seen = ["Image credits:"], set()
    for part in plan["parts"]:
        for slot in [sl for s in part["scenes"] for sl in slots_of(pr, s["id"])]:
            x = c.get(slot)
            if not x or x.get("source") in ("upload", None) or x.get("url") in seen:
                continue
            seen.add(x.get("url"))
            if x.get("license") == "AI generated":
                continue
            lines.append(f"- {x.get('title', '')[:80]} by {x.get('author') or 'unknown'} "
                         f"({x.get('license') or x.get('source')}) {x.get('page') or ''}".strip())
    (p / "output" / "credits.txt").write_text("\n".join(lines), encoding="utf-8")


# ----------------------------------------------------------------- YouTube title / description / chapters
def ts(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600}:{sec // 60 % 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60}:{sec % 60:02d}"


def chapters_of(p: Path, plan, titles: dict):
    """Chapter start = sum of the real rendered part lengths before it (falls back to plan duration + gap)."""
    out, t, measured = [], 0.0, True
    for part in plan["parts"]:
        out.append({"num": part["num"], "t": t, "time": ts(t), "auto": part["title"],
                    "title": titles.get(str(part["num"])) or part["title"]})
        f = p / "output" / "parts" / f"part_{part['num']:02d}.mp4"
        if f.exists():
            t += vidgen.duration(f)
        else:
            measured = False
            t += part["duration"] + RENDER_DEFAULTS["gap"]
    return out, measured


def youtube_text(yt, chapters, credits):
    blocks = [yt.get("description", "").strip()]
    if chapters:
        blocks.append("Chapters:\n" + "\n".join(f"{c['time']} {c['title']}" for c in chapters))
    if yt.get("include_credits", True) and credits.strip():
        blocks.append(credits.strip())
    if yt.get("hashtags", "").strip():
        blocks.append(yt["hashtags"].strip())
    return "\n\n".join(b for b in blocks if b)


def compact_credits(p: Path, plan):
    """Short attribution lines for the YouTube description (5000-char limit): author, licence, link."""
    c, pr = credits_of(p), prompts_of(p)
    lines, seen = [], set()
    for part in plan["parts"]:
        for slot in [sl for s in part["scenes"] for sl in slots_of(pr, s["id"])]:
            x = c.get(slot)
            if not x or x.get("source") in ("upload", None) or x.get("license") == "AI generated":
                continue
            link = x.get("page") or x.get("url") or ""
            if link in seen:
                continue
            seen.add(link)
            who = x.get("author") or ""
            if not who or "Wikimedia" in who or who == "unknown":
                who = re.sub(r"\.(jpe?g|png|tiff?|gif|webp)$", "", (x.get("title") or "")[:45], flags=re.I)
            lic = x.get("license") or x.get("source") or ""
            lic = "CC/PD" if lic.startswith("see page") else lic
            who = who if len(who) <= 50 else who[:47].rstrip(" ,;") + "…"
            lines.append(f"- {who} ({lic or 'see link'}) {link}")
    return ("Image credits:\n" + "\n".join(lines)) if lines else ""


@app.get("/api/projects/{name}/youtube")
def get_youtube(name: str):
    p = proj_dir(name)
    plan = read_json(p / "output" / "plan.json", None)
    if not plan:
        raise HTTPException(400, "run plan first")
    yt = {"title": name, "description": "", "hashtags": "", "tags": "", "include_credits": True, "chapters": {},
          **read_json(p / "youtube.json", {})}
    chapters, measured = chapters_of(p, plan, yt["chapters"])
    credits = compact_credits(p, plan)
    return {**yt, "chapter_list": chapters, "measured": measured, "text": youtube_text(yt, chapters, credits)}


@app.put("/api/projects/{name}/youtube")
def put_youtube(name: str, body: dict = Body(...)):
    p = proj_dir(name)
    yt = read_json(p / "youtube.json", {})
    for k in ("title", "description", "hashtags", "tags", "include_credits", "chapters"):
        if k in body:
            yt[k] = body[k]
    write_json(p / "youtube.json", yt)
    return get_youtube(name)


@app.get("/api/projects/{name}/credits")
def get_credits(name: str):
    p = proj_dir(name)
    plan = read_json(p / "output" / "plan.json", None)
    if plan:
        write_credits(p, plan)
    f = p / "output" / "credits.txt"
    return {"text": f.read_text(encoding="utf-8") if f.exists() else ""}


@app.get("/")
def index():
    return FileResponse(HERE / "static" / "index.html")


app.mount("/files", StaticFiles(directory=ROOT), name="files")

if __name__ == "__main__":
    print("vidgen GUI -> http://127.0.0.1:8765")
    uvicorn.run(app, host="127.0.0.1", port=8765)
