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
import studio
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
PLAN_DEFAULTS = dict(split_pause=1.0, max_scene=25.0, min_scene=12.0)


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


JOB_LABEL = {"plan": "phân cảnh", "generate": "tạo ảnh AI", "autofill": "auto-fill ảnh", "preview": "render nháp",
             "render": "render video", "ideas": "gợi ý chủ đề", "youtube": "kiểm tra YouTube", "script": "viết kịch bản",
             "prompts": "AI nghiên cứu cảnh", "suggest": "AI gợi ý ảnh", "segment": "chia ý", "voices": "tạo giọng đọc",
             "thumbnail": "tạo thumbnail"}


def start_job(kind, fn, key=None):
    """Background job. Heavy jobs run one at a time (LOCK); others wait in line and say so.
    key: the same key already running/waiting -> return that job instead of starting a duplicate."""
    if key:
        for j in JOBS.values():
            if j.get("key") == key and j["status"] == "running":
                return j
    jid = uuid.uuid4().hex[:8]
    job = JOBS[jid] = dict(id=jid, kind=kind, key=key, status="running", logs=[], progress=0.0, error=None,
                           started=False, cancel=False)

    def emit(*a, **k):
        line = " ".join(str(x) for x in a)
        job["logs"].append(line)
        try:
            print(line)
        except UnicodeEncodeError:
            print(line.encode("ascii", "replace").decode())

    def runner():
        if LOCK.locked():
            busy = [JOB_LABEL.get(j["kind"], j["kind"]) for j in JOBS.values()
                    if j["status"] == "running" and j.get("started")]
            emit(f"⏳ đang chờ {', '.join(busy) or 'việc khác'} xong…")
        with LOCK:
            if job["cancel"]:
                job["status"], job["error"] = "error", "đã huỷ"
                return
            job["started"] = True
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


@app.get("/api/status")
def status():
    running = [{"id": j["id"], "kind": j["kind"], "waiting": not j.get("started")}
               for j in JOBS.values() if j["status"] == "running"]
    return {"busy": bool(running), "running": running}


@app.post("/api/jobs/{jid}/cancel")
def cancel_job(jid: str):
    """Cancel a job that is still waiting in line (a running job finishes its current step)."""
    j = JOBS.get(jid)
    if not j:
        raise HTTPException(404, "no such job")
    j["cancel"] = True
    if not j.get("started") and j["status"] == "running":
        j["status"], j["error"] = "error", "đã huỷ"
    return {"ok": True, "started": j.get("started")}


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
                slots = slots_of(pr, s["id"])
                ft = pr.get("shot_times", {}).get(s["id"])
                ft = ft if ft and len(ft) == len(slots) else [j / len(slots) for j in range(len(slots))]
                for j, slot in enumerate(slots):
                    info = (dict(
                        text=pr.get("shot_text", {}).get(slot, ""), query=pr.get("shot_query", {}).get(slot, ""),
                        t0=round(s["start"] + ft[j] * (s["end"] - s["start"]), 2),
                        t1=round(s["start"] + (ft[j + 1] if j + 1 < len(ft) else 1) * (s["end"] - s["start"]), 2)))
                    im = vidgen.find_image(p / "pics", slot)
                    s["shots"].append(dict(id=slot, prompt=pr["scenes"].get(slot, "") if slot != s["id"] else s["prompt"],
                                           image=f"/files/{name}/pics/{im.name}?v={int(im.stat().st_mtime)}" if im else None,
                                           credit=cr.get(slot) if im else None,
                                           framing=pr.get("framing", {}).get(slot),
                                           auto=vidgen.default_framing(im, 1920, 1080) if im else None, **info))
    video = p / "output" / "video.mp4"
    return dict(name=name, texts=[t.name for t in texts], voices=[v.name for v in voices], parts=parts,
                parse_error=parse_error, plan=plan, style=pr["style"], aspect=pr.get("aspect", "16:9"),
                video=f"/files/{name}/output/video.mp4?v={int(video.stat().st_mtime)}" if video.exists() else None)


@app.post("/api/projects/{name}/upload")
async def upload(name: str, kind: str = Form(...), files: list[UploadFile] = File(...)):
    p = proj_dir(name)
    dest = p / ("texts" if kind == "text" else "voices")
    dest.mkdir(parents=True, exist_ok=True)
    if kind == "text":  # one script per project
        for old in dest.glob("*.md"):
            old.replace(old.with_suffix(".md.bak"))
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
        if old.name == "script.md" and old.read_text(encoding="utf-8") == text:
            return {"ok": True, "unchanged": True}
        old.replace(old.with_suffix(".md.bak"))  # replace(): overwrites an older .bak (rename() fails on Windows)
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
    d.setdefault("shot_times", {}).pop(sid, None)
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
    d.setdefault("shot_times", {}).pop(sid, None)
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
def generate(name: str, sid: str, source: str = Body(..., embed=True), aspect: str = Body(None, embed=True)):
    """AI generation can take minutes (web chats) -> background job."""
    p = proj_dir(name)
    if sources.SOURCES.get(source, {}).get("kind") != "ai":
        raise HTTPException(400, "unknown AI source")
    pr = prompts_of(p)
    aspect = aspect or pr.get("aspect") or "16:9"
    prompt = f"{prompt_for(pr, sid)}. {pr['style']}"
    if sid != base_sid(sid) and sid not in pr["scenes"]:  # extra slot without its own prompt: vary the framing
        prompt += " Different composition and camera angle from the previous shot."

    def work(job, emit):
        emit(f"{sid}: {source}, khổ {aspect}")
        emit(f"prompt: {prompt}")
        data = sources.generate(source, prompt, {**keys(), "_ASPECT": aspect})
        save_image(p, sid, data, {"source": source, "title": "AI generated", "license": "AI generated",
                                  "prompt": prompt})
        emit(f"{sid}: saved")

    return start_job("generate", work, key=f"gen:{name}:{sid}")


@app.get("/api/browser/status")
def browser_status():
    import browser_ai
    k = keys()
    c = browser_ai.cfg(k)
    return {"port": c["port"], "alive": browser_ai.cdp_alive(c["port"]),
            "browser": c["browser_path"] or "auto (Chrome/Edge)",
            "profile": c["user_data_dir"] + (f" › {c['profile']}" if c["profile"] else ""),
            "sites": browser_ai.status(k), "brave": browser_ai.BRAVE if Path(browser_ai.BRAVE).exists() else None}


@app.post("/api/browser/open")
def browser_open(sites: list = Body(None, embed=True)):
    import browser_ai
    try:
        how = browser_ai.open_login_tabs(keys(), sites)
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

    return start_job("autofill", work, key=f"autofill:{name}")


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

    return start_job("preview", work, key=f"preview:{name}:{fname}")


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

    return start_job("render", work, key=f"render:{name}")


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


THUMB_SITES = {"web_gemini": "gemini", "web_chatgpt": "chatgpt", "web_grok": "grok", "gemini": None, "openai": None}
THUMB_PROMPT = """Create a YouTube thumbnail, 16:9 landscape (1280x720), for a history documentary channel.
Video: "{title}" (topic: {topic}).
Follow this exact layout:
1. BACKGROUND (whole frame): a photorealistic close-up of {topic} - several whole pieces plus some cut open to show
   the inside, with fresh leaves, filling the frame, rich natural colors, soft daylight, shallow depth of field.
2. LEFT THIRD: {figure}, painted in the traditional art style of that culture and era (e.g. a Chinese ink-and-wash
   painting for an ancient Chinese monk), shown from the waist up as a clean cut-out over the photo, facing the
   viewer or the text. The painted figure contrasts with the photo background.
3. RIGHT TWO-THIRDS: the title in classic elegant SERIF capitals (Trajan / Cinzel style), white letters with a thin
   dark outline and soft dark shadow, centered in 3 stacked lines:
   {text_rule}
   Keep the text away from the bottom-right corner (YouTube puts the video length there).
Spell every word exactly, no other text anywhere. No logos, no watermark, no borders.{extra}"""


def thumbs_of(name: str):
    d = proj_dir(name) / "output" / "thumbnails"
    fs = sorted(d.glob("*.jpg"), key=lambda f: f.stat().st_mtime, reverse=True) if d.exists() else []
    return [{"name": f.name, "url": f"/files/{name}/output/thumbnails/{f.name}?v={int(f.stat().st_mtime)}",
             "kb": round(f.stat().st_size / 1024)} for f in fs]


@app.get("/api/projects/{name}/thumbnails")
def get_thumbnails(name: str):
    yt = read_json(proj_dir(name) / "youtube.json", {})
    return {"items": thumbs_of(name), "text": yt.get("thumb_text", ""), "idea": yt.get("thumb_idea", "")}


@app.post("/api/projects/{name}/thumbnail")
def make_thumbnail(name: str, opts: dict = Body(default={})):
    """🖼 AI makes a YouTube thumbnail WITH the hook text drawn on it -> output/thumbnails/*.jpg (1280x720, < 2 MB)."""
    p = proj_dir(name)
    site = opts.get("site", "web_gemini")
    if site not in THUMB_SITES:
        raise HTTPException(400, "site must be " + "/".join(THUMB_SITES))
    text, idea = (opts.get("text") or "").strip(), (opts.get("idea") or "").strip()
    yt = read_json(p / "youtube.json", {})
    yt.update(thumb_text=text, thumb_idea=idea)
    write_json(p / "youtube.json", yt)
    lines = [x.strip() for x in re.split(r"\s*[|/\n]\s*", text) if x.strip()] if text else []
    if len(lines) == 1:  # "THE SWEET HISTORY OF MONK FRUIT" -> split at the last " of "
        m = re.match(r"(?i)(.+?)\s+(of)\s+(.+)$", lines[0])
        lines = [m.group(1), m.group(2).lower(), m.group(3)] if m else lines
    if lines:
        rule = "\n   ".join(f'line {i + 1}: "{ln}"' for i, ln in enumerate(lines))
        rule += ("\n   The LAST line is the biggest (about twice the size); a short middle word like \"of\" is small"
                 " italic script.")
    else:
        rule = (f'line 1: "THE <ONE ADJECTIVE> HISTORY" (pick an intriguing adjective for this story, e.g. SWEET, DARK,'
                f' STRANGE, SECRET)\n   line 2: "of" in small italic script\n   line 3: "{p.name.upper()}" - the'
                f' biggest line, about twice the size of line 1')
    prompt = THUMB_PROMPT.format(
        title=yt.get("title") or f"The History of {p.name}", topic=p.name, text_rule=rule,
        figure=idea or f"the most important historical person in the story of {p.name} (pick one that fits the video)",
        extra="")
    n = max(1, min(4, int(opts.get("n") or 1)))

    def work(job, emit):
        from io import BytesIO
        from PIL import Image, ImageOps
        import browser_ai
        out = p / "output" / "thumbnails"
        out.mkdir(parents=True, exist_ok=True)
        for i in range(n):
            if job["cancel"]:
                break
            emit(f"thumbnail {i + 1}/{n}: {site}…")
            try:
                if THUMB_SITES[site]:
                    web = THUMB_SITES[site]
                    data = browser_ai.generate(web, prompt, log=emit, raw=True, aspect="16:9",
                                               **browser_ai.cfg(keys(), web))
                else:
                    data = sources.generate(site, prompt, {**keys(), "_ASPECT": "16:9"})
                img = ImageOps.fit(Image.open(BytesIO(data)).convert("RGB"), (1280, 720), Image.LANCZOS)
                f = out / f"thumb_{time.strftime('%Y%m%d_%H%M%S')}_{i + 1}.jpg"
                for q in (92, 85, 75):  # YouTube limit: 2 MB
                    img.save(f, "JPEG", quality=q, optimize=True)
                    if f.stat().st_size < 2_000_000:
                        break
                emit(f"✓ {f.name}")
            except Exception as e:  # noqa: BLE001
                emit(f"lỗi: {e}")
            job["progress"] = (i + 1) / n
        job["result"] = thumbs_of(name)

    return start_job("thumbnail", work, key=f"thumb:{name}")


@app.delete("/api/projects/{name}/thumbnails/{fname}")
def del_thumbnail(name: str, fname: str):
    f = proj_dir(name) / "output" / "thumbnails" / Path(fname).name
    if f.exists():
        f.unlink()
    return {"items": thumbs_of(name)}


@app.get("/api/projects/{name}/credits")
def get_credits(name: str):
    p = proj_dir(name)
    plan = read_json(p / "output" / "plan.json", None)
    if plan:
        write_credits(p, plan)
    f = p / "output" / "credits.txt"
    return {"text": f.read_text(encoding="utf-8") if f.exists() else ""}


# ----------------------------------------------------------------- ⓪ ideas / script / voices
AI_SITES = ("chatgpt", "gemini", "grok", "api_gemini", "api_openai")


@app.get("/api/ideas")
def get_ideas():
    k = keys()
    return {"ideas": read_json(studio.IDEAS_FILE, {}).get("ideas", []), "voices": studio.VOICES,
            "youtube_key": bool(k.get("YOUTUBE_API_KEY")), "done": studio.done_topics(ROOT),
            "api_keys": {s: bool(k.get(n)) for s, n in studio.API_SITES.items()}}


@app.post("/api/ideas")
def suggest_ideas(site: str = Body("gemini", embed=True), n: int = Body(12, embed=True)):
    if site not in AI_SITES:
        raise HTTPException(400, "site must be " + "/".join(AI_SITES))
    def work(job, emit):
        prompt = studio.IDEAS_PROMPT.format(n=n, done=", ".join(studio.done_topics(ROOT)) or "none")
        text = studio.ask(site, prompt, keys(), log=emit)
        ideas = studio.parse_json_block(text)
        write_json(studio.IDEAS_FILE, {"ideas": ideas, "site": site, "at": time.strftime("%Y-%m-%d %H:%M")})
        emit(f"{len(ideas)} gợi ý")

    return start_job("ideas", work, key="ideas")


@app.post("/api/ideas/youtube")
def ideas_youtube(topics: list = Body(..., embed=True)):
    key = keys().get("YOUTUBE_API_KEY")
    if not key:
        raise HTTPException(400, "cần YOUTUBE_API_KEY (tab ⚙)")
    data = read_json(studio.IDEAS_FILE, {"ideas": []})

    def work(job, emit):
        for i, t in enumerate(topics[:20]):
            emit(f"YouTube: {t}…")
            res = studio.youtube_check(t, key)
            for idea in data["ideas"]:
                if idea.get("fruit") == t:
                    idea["yt"] = res
            job["progress"] = (i + 1) / len(topics)
        write_json(studio.IDEAS_FILE, data)

    return start_job("youtube", work, key="youtube")


@app.post("/api/projects/{name}/script/generate")
def gen_script(name: str, opts: dict = Body(...)):
    site = opts.get("site", "chatgpt")
    if site not in AI_SITES:
        raise HTTPException(400, "site must be " + "/".join(AI_SITES))
    p = proj_dir(name.strip())
    topic = (opts.get("topic") or name).strip()
    prompt = studio.SCRIPT_PROMPT.format(topic=topic, title=opts.get("title") or f"The History of {topic}",
                                         parts=int(opts.get("parts") or 10), minutes=int(opts.get("minutes") or 9))
    def work(job, emit):
        for sub in ("texts", "voices", "pics"):
            (p / sub).mkdir(parents=True, exist_ok=True)
        text = studio.normalize_script(studio.ask(site, prompt, keys(), log=emit))
        for old in (p / "texts").glob("*.md"):
            old.replace(old.with_suffix(".md.bak"))
        f = p / "texts" / "script.md"
        f.write_text(text, encoding="utf-8")
        parts = vidgen.parse_script(f)
        if not parts:
            raise RuntimeError("AI trả lời sai định dạng (không thấy '## Part'). Xem texts/script.md, sửa tay hoặc tạo lại.")
        words = sum(sum(1 for k, _ in pt["tokens"] if k == "w") for pt in parts)
        emit(f"✓ {len(parts)} part, {words} từ (~{words / 150:.1f} phút đọc) → texts/script.md")
        yt = read_json(p / "youtube.json", {})
        if opts.get("title") and not yt.get("title"):
            write_json(p / "youtube.json", {**yt, "title": opts["title"]})

    return start_job("script", work, key=f"script:{name}")


@app.get("/api/projects/{name}/script")
def get_script(name: str):
    p = proj_dir(name)
    md = next((p / "texts").glob("*.md"), None) if (p / "texts").exists() else None
    return {"name": md.name if md else None, "text": md.read_text(encoding="utf-8") if md else ""}


@app.post("/api/projects/{name}/prompts/generate")
def gen_prompts(name: str, opts: dict = Body(default={})):
    """AI researches every scene and writes a search query + context-rich image prompt(s)."""
    p = proj_dir(name)
    site = opts.get("site", "gemini")
    if site not in AI_SITES:
        raise HTTPException(400, "site must be " + "/".join(AI_SITES))
    plan = read_json(p / "output" / "plan.json", None)
    if not plan:
        raise HTTPException(400, "run plan first")
    pr = prompts_of(p)
    only = None
    if opts.get("mode") == "missing":  # scenes that still have an empty image slot
        only = {s["id"] for part in plan["parts"] for s in part["scenes"]
                if any(not vidgen.find_image(p / "pics", sl) for sl in slots_of(pr, s["id"]))}
    yt = read_json(p / "youtube.json", {})
    title = yt.get("title") or f"The History of {p.name}"
    batches = studio.scene_batches(plan, pr, only)
    if not batches:
        raise HTTPException(400, "không có cảnh nào cần làm")

    def work(job, emit):
        bak = p / "prompts.json.bak"
        if (p / "prompts.json").exists():
            bak.write_text((p / "prompts.json").read_text(encoding="utf-8"), encoding="utf-8")
        done = 0
        for k, (lines, counts) in enumerate(batches):
            emit(f"Nhóm {k + 1}/{len(batches)}: {len(counts)} cảnh…")
            prompt = studio.SCENE_PROMPT.format(title=title, topic=p.name, style=pr["style"], scenes=lines,
                                                context=opts.get("context") or yt.get("description", "")[:600] or "-")
            rows = studio.parse_json_block(studio.ask(site, prompt, keys(), log=emit))
            d = prompts_of(p)  # re-read: the user may edit other scenes meanwhile
            for row in rows:
                sid = row.get("id")
                if sid not in counts:
                    continue
                if row.get("query"):
                    d["queries"][sid] = row["query"].strip()
                for j, txt in enumerate((row.get("prompts") or [])[:counts[sid]]):
                    d["scenes"][vidgen.shot_id(sid, j + 1)] = txt.strip()
                done += 1
            write_json(p / "prompts.json", d)
            job["progress"] = (k + 1) / len(batches)
        emit(f"✓ {done} cảnh có từ khoá + prompt mới (bản cũ: prompts.json.bak)")

    return start_job("prompts", work, key=f"prompts:{name}")


@app.get("/api/projects/{name}/scene/{sid}/suggest")
def get_suggest(name: str, sid: str):
    p = proj_dir(name)
    return read_json(p / "output" / "suggest" / f"{base_sid(sid)}.json", {"suggestions": []})


@app.post("/api/projects/{name}/scene/{sid}/suggest")
def suggest(name: str, sid: str, opts: dict = Body(default={})):
    """AI suggests N pictures for one scene (title/desc in Vietnamese, search query, AI prompt) + real photo candidates."""
    p = proj_dir(name)
    site = opts.get("site", "gemini")
    if site not in AI_SITES:
        raise HTTPException(400, "site must be " + "/".join(AI_SITES))
    plan = read_json(p / "output" / "plan.json", None)
    if not plan:
        raise HTTPException(400, "run plan first")
    base = base_sid(sid)
    try:
        part, scene, prev, nxt = studio.scene_context(plan, base)
    except KeyError:
        raise HTTPException(404, "scene not found")
    yt = read_json(p / "youtube.json", {})
    prompt = studio.SUGGEST_PROMPT.format(title=yt.get("title") or f"The History of {p.name}", topic=p.name,
                                          part=part["num"], part_title=part["title"], text=scene["text"],
                                          prev=prev, next=nxt, n=max(2, min(8, int(opts.get("n") or 5))))
    k = keys()
    free = [s for s, v in sources.available(k).items() if v["kind"] == "search" and v["enabled"]
            and s in ("wikimedia", "openverse", "pexels", "pixabay", "unsplash", "flickr", "google")]

    def work(job, emit):
        rows = studio.parse_json_block(studio.ask(site, prompt, k, log=emit))
        emit(f"{len(rows)} gợi ý — đang tìm ảnh thật cho từng gợi ý…")
        from concurrent.futures import ThreadPoolExecutor

        def find(row):
            return studio.real_candidates(row, k, free, sources)

        with ThreadPoolExecutor(6) as ex:
            for row, cands in zip(rows, ex.map(find, rows)):
                row["cands"] = cands
        res = {"sid": base, "site": site, "at": time.strftime("%Y-%m-%d %H:%M"), "suggestions": rows}
        write_json(p / "output" / "suggest" / f"{base}.json", res)
        job["result"] = res
        emit("✓ xong")

    return start_job("suggest", work, key=f"suggest:{name}:{base}")


def free_sources(k):
    return [s for s, v in sources.available(k).items() if v["kind"] == "search" and v["enabled"]
            and s in ("wikimedia", "openverse", "pexels", "pixabay", "unsplash", "flickr", "google")]


@app.get("/api/projects/{name}/scene/{sid}/segment")
def get_segment(name: str, sid: str):
    p = proj_dir(name)
    return read_json(p / "output" / "segments" / f"{base_sid(sid)}.json", {"segments": []})


def segment_scene(p: Path, base: str, site: str, k: dict, emit, plan=None):
    """AI splits one scene's narration into visual ideas -> image slots timed to the voice, with real-photo candidates.
    Saves prompts.json + output/segments/<base>.json and returns that result."""
    plan = plan or read_json(p / "output" / "plan.json", None)
    part, scene, prev, nxt = studio.scene_context(plan, base)
    secs = scene["end"] - scene["start"]
    hi = max(1, min(3, int(secs / 8)))  # one picture per ~8s or more
    yt = read_json(p / "youtube.json", {})
    prompt = studio.SEGMENT_PROMPT.format(title=yt.get("title") or f"The History of {p.name}", topic=p.name,
                                          part=part["num"], part_title=part["title"], text=scene["text"], secs=secs,
                                          prev=prev, next=nxt, lo=min(2, hi), hi=hi)
    rows = [r for r in studio.parse_json_block(studio.ask(site, prompt, k, log=emit)) if r.get("text")]
    if not rows:
        raise RuntimeError("AI không trả về đoạn nào")
    rows, fr = studio.segment_times(rows, scene, p / "voices" / part["voice"], min_len=6.0)
    emit(f"{base}: {len(rows)} ý: " + " | ".join(f"{f * secs:.1f}s" for f in fr))
    d = prompts_of(p)
    d.setdefault("shots", {})[base] = len(rows)
    d.setdefault("shot_times", {})[base] = fr
    for j, r in enumerate(rows):
        slot = vidgen.shot_id(base, j + 1)
        d["scenes"][slot] = r.get("prompt", "").strip()
        d.setdefault("shot_text", {})[slot] = r.get("text", "").strip()
        d.setdefault("shot_query", {})[slot] = ((r.get("queries") or [None])[0] or r.get("short") or r.get("query") or "").strip()
    d["queries"][base] = (rows[0].get("short") or rows[0].get("query") or d["queries"].get(base, "")).strip()
    write_json(p / "prompts.json", d)
    emit(f"{base}: đang tìm ảnh thật cho từng ý…")
    from concurrent.futures import ThreadPoolExecutor
    free = free_sources(k)
    with ThreadPoolExecutor(6) as ex:
        for r, c in zip(rows, ex.map(lambda r: studio.real_candidates(r, k, free, sources), rows)):
            r["cands"] = c
    res = {"sid": base, "site": site, "at": time.strftime("%Y-%m-%d %H:%M"),
           "segments": [{**r, "slot": vidgen.shot_id(base, j + 1), "t0": round(scene["start"] + fr[j] * secs, 2),
                         "t1": round(scene["start"] + (fr[j + 1] if j + 1 < len(fr) else 1) * secs, 2)}
                        for j, r in enumerate(rows)]}
    write_json(p / "output" / "segments" / f"{base}.json", res)
    return res


def autopick_segments(p: Path, res: dict, used: set, emit, overwrite=False):
    """Give every idea of a segmented scene the best real photo found for it (big landscape first, no repeats)."""
    n = 0
    for x in res.get("segments", []):
        if not overwrite and vidgen.find_image(p / "pics", x["slot"]):
            continue
        # cands are in relevance order (the AI's first search = what happens in the picture); take the first
        # reasonably sized one, so a big but off-topic photo of the subject does not win
        cands = [c for c in x.get("cands") or [] if c.get("url") not in used]
        cands.sort(key=lambda c: -(c.get("fit", 0) >= 0.5))
        for c in cands:
            try:
                save_image(p, x["slot"], download(c["url"]), {kk: c.get(kk) for kk in
                                                              ("source", "title", "author", "license", "page", "url")})
                used.add(c["url"])
                emit(f"{x['slot']}: ✓ [{c.get('source')}] {(c.get('title') or '')[:60]}")
                n += 1
                break
            except Exception as e:  # noqa: BLE001
                emit(f"{x['slot']}: bỏ qua 1 ảnh lỗi ({e})")
        else:
            emit(f"{x['slot']}: không có ảnh thật phù hợp — mở 🧩 để tìm thêm hoặc ✨ AI tạo")
    return n


@app.post("/api/projects/{name}/scene/{sid}/segment")
def segment(name: str, sid: str, opts: dict = Body(default={})):
    """🧩 AI splits the scene's narration into visual ideas -> one image slot per idea, timed to the narration;
    then each empty slot gets the best real photo found (auto_pick, on by default)."""
    p = proj_dir(name)
    site = opts.get("site", "gemini")
    if site not in AI_SITES:
        raise HTTPException(400, "site must be " + "/".join(AI_SITES))
    plan = read_json(p / "output" / "plan.json", None)
    if not plan:
        raise HTTPException(400, "run plan first")
    base = base_sid(sid)
    try:
        studio.scene_context(plan, base)
    except KeyError:
        raise HTTPException(404, "scene not found")

    def work(job, emit):
        res = segment_scene(p, base, site, keys(), emit, plan)
        if opts.get("auto_pick", True):
            autopick_segments(p, res, {c.get("url") for c in credits_of(p).values()}, emit)
        job["result"] = res
        emit("✓ xong")

    return start_job("segment", work, key=f"segment:{name}:{base}")


@app.post("/api/projects/{name}/segment_all")
def segment_all(name: str, opts: dict = Body(default={})):
    """🧩 for every scene: AI splits it into ideas (skips scenes already split unless redo=true),
    then fills each empty idea slot with the best real photo. Long job - can be cancelled between scenes."""
    p = proj_dir(name)
    site = opts.get("site", "gemini")
    if site not in AI_SITES:
        raise HTTPException(400, "site must be " + "/".join(AI_SITES))
    plan = read_json(p / "output" / "plan.json", None)
    if not plan:
        raise HTTPException(400, "run plan first")
    redo = bool(opts.get("redo"))
    ids = [s["id"] for pt in plan["parts"] for s in pt["scenes"]]

    def work(job, emit):
        k = keys()
        used = {c.get("url") for c in credits_of(p).values()}
        done = fail = picked = 0
        for i, base in enumerate(ids):
            if job["cancel"]:
                emit("đã dừng theo yêu cầu")
                break
            job["progress"] = i / len(ids)
            saved = p / "output" / "segments" / f"{base}.json"
            try:
                if saved.exists() and not redo:
                    res = read_json(saved, {"segments": []})
                else:
                    emit(f"── {base} ({i + 1}/{len(ids)})")
                    res = segment_scene(p, base, site, k, emit, plan)
                    done += 1
                picked += autopick_segments(p, res, used, emit)
            except Exception as e:  # noqa: BLE001
                fail += 1
                emit(f"{base}: lỗi — {e}")
        emit(f"✓ xong: chia {done} cảnh, tự chọn {picked} ảnh" + (f", {fail} cảnh lỗi (bấm chạy lại)" if fail else ""))

    return start_job("segment", work, key=f"segment_all:{name}")


@app.post("/api/projects/{name}/scene/{sid}/generate_all")
def generate_all(name: str, sid: str, opts: dict = Body(...)):
    """AI-generate the picture of every image slot of a scene (only the empty ones unless all=true)."""
    p = proj_dir(name)
    source = opts.get("source")
    if sources.SOURCES.get(source, {}).get("kind") != "ai":
        raise HTTPException(400, "unknown AI source")
    pr = prompts_of(p)
    base = base_sid(sid)
    aspect = opts.get("aspect") or pr.get("aspect") or "16:9"
    slots = [s for s in slots_of(pr, base) if opts.get("all") or not vidgen.find_image(p / "pics", s)]
    if not slots:
        raise HTTPException(400, "mọi ô ảnh đã có ảnh")

    def work(job, emit):
        for i, slot in enumerate(slots):
            prompt = f"{prompt_for(pr, slot)}. {pr['style']}"
            emit(f"{slot} ({i + 1}/{len(slots)}): {source}, khổ {aspect}")
            try:
                data = sources.generate(source, prompt, {**keys(), "_ASPECT": aspect})
                save_image(p, slot, data, {"source": source, "title": "AI generated", "license": "AI generated",
                                           "prompt": prompt})
                emit(f"{slot}: ✓")
            except Exception as e:  # noqa: BLE001
                emit(f"{slot}: lỗi — {e}")
            job["progress"] = (i + 1) / len(slots)

    return start_job("generate", work, key=f"gen:{name}:{sid}")


@app.put("/api/projects/{name}/aspect")
def put_aspect(name: str, aspect: str = Body(..., embed=True)):
    import browser_ai
    if aspect not in browser_ai.ASPECTS:
        raise HTTPException(400, "aspect: " + ", ".join(browser_ai.ASPECTS))
    p = proj_dir(name)
    d = prompts_of(p)
    d["aspect"] = aspect
    write_json(p / "prompts.json", d)
    return {"ok": True}


@app.get("/api/projects/{name}/voice")
def get_voice(name: str):
    p = proj_dir(name)
    md = next((p / "texts").glob("*.md"), None) if (p / "texts").exists() else None
    first = ""
    if md:
        parts = vidgen.parse_script(md)
        if parts:
            ch = studio.chunks_of(parts[0])
            first = ch[0][0] if ch else ""
    return {"cfg": studio.voice_cfg(p), "voices": studio.VOICES, "defaults": studio.VOICE_DEFAULTS, "first": first,
            "parts": len(vidgen.parse_script(md)) if md else 0}


@app.put("/api/projects/{name}/voice")
def put_voice(name: str, cfg: dict = Body(...)):
    p = proj_dir(name)
    try:
        c = studio.voice_cfg(p, cfg)
    except ValueError as e:
        raise HTTPException(400, str(e))
    studio.save_voice_cfg(p, c)
    return {"cfg": c}


@app.post("/api/projects/{name}/voices/generate")
def gen_voices(name: str, opts: dict = Body(default={})):
    p = proj_dir(name)
    try:
        cfg = studio.voice_cfg(p, opts)
    except ValueError as e:
        raise HTTPException(400, str(e))
    studio.save_voice_cfg(p, cfg)
    parts = [int(x) for x in (opts.get("parts") or [])] or None
    n = len(vidgen.parse_script(next((p / "texts").glob("*.md")))) if (p / "texts").exists() else 0
    if not n:
        raise HTTPException(400, "chưa có kịch bản")

    def work(job, emit):
        def tracking(*a, **k):
            emit(*a, **k)
            if a and "(done in)" in str(a[0]):
                job["progress"] = min(0.95, job["progress"] + 1 / (len(parts) if parts else n))
        mix = f" + {cfg['voice2']} {cfg['mix']:.0%}" if cfg["voice2"] and cfg["mix"] > 0 else ""
        emit(f"Giọng {cfg['voice']}{mix}, tốc độ {cfg['speed']}, pause x{cfg['pause_scale']}")
        studio.make_voices(p, cfg, parts, log=tracking)
        emit("Phân cảnh lại theo giọng mới…")
        vidgen.cmd_plan(p, SimpleNamespace(**PLAN_DEFAULTS))
        emit("✓ xong — sang tab ② để lấy ảnh")

    return start_job("voices", work, key=f"voices:{name}")


@app.post("/api/tts/sample")
def tts_sample(body: dict = Body(...)):
    try:
        cfg = studio.voice_cfg(None, body.get("cfg") or {})
    except ValueError as e:
        raise HTTPException(400, str(e))
    text = (body.get("text") or "Imagine a fruit sweeter than sugar.").strip()[:400]
    import hashlib
    key = hashlib.sha1(json.dumps([cfg, text], sort_keys=True).encode()).hexdigest()[:16]
    f = studio.HERE / "data" / "samples" / f"{key}.wav"
    if not f.exists():
        f.parent.mkdir(parents=True, exist_ok=True)
        studio.sample(cfg, text, f)
    return FileResponse(f, media_type="audio/wav")


@app.get("/")
def index():
    return FileResponse(HERE / "static" / "index.html")


app.mount("/files", StaticFiles(directory=ROOT), name="files")

class _QuietAccess:
    """Hide the browser's once-a-second job polling and image/video loads from the console log."""
    NOISE = ("/api/jobs/", '"GET /files/', '"GET /api/status')

    def filter(self, record):
        msg = record.getMessage()
        return not (any(n in msg for n in self.NOISE) and '" 200' in msg or '" 304' in msg or '" 206' in msg)


if __name__ == "__main__":
    import logging
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    logging.getLogger("uvicorn.access").addFilter(_QuietAccess())
    print("vidgen GUI -> http://127.0.0.1:8765")
    uvicorn.run(app, host="127.0.0.1", port=8765)
