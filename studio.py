"""Step ⓪ of the pipeline: topic ideas -> voice-over script -> voices (Kokoro TTS, local & free)."""
import json
import re
import subprocess
import time
from pathlib import Path

import requests

import vidgen

HERE = Path(__file__).resolve().parent
MODELS = HERE / "models"
IDEAS_FILE = HERE / "data" / "ideas.json"
KOKORO_FILES = {"kokoro-v1.0.onnx": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx",
                "voices-v1.0.bin": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin"}
VOICES = {  # Kokoro English voices (b = British, a = American; f = female, m = male)
    "bf_emma": "🇬🇧 Emma — nữ, nhẹ nhàng", "bf_isabella": "🇬🇧 Isabella — nữ, rõ ràng", "bf_alice": "🇬🇧 Alice — nữ",
    "bf_lily": "🇬🇧 Lily — nữ, trẻ", "bm_fable": "🇬🇧 Fable — nam, kể chuyện", "bm_george": "🇬🇧 George — nam, trầm",
    "bm_lewis": "🇬🇧 Lewis — nam", "bm_daniel": "🇬🇧 Daniel — nam",
    "af_heart": "🇺🇸 Heart — nữ, ấm (chất lượng cao nhất)", "af_bella": "🇺🇸 Bella — nữ, truyền cảm", "af_nicole": "🇺🇸 Nicole — nữ, thì thầm",
    "af_sarah": "🇺🇸 Sarah — nữ", "af_sky": "🇺🇸 Sky — nữ", "af_nova": "🇺🇸 Nova — nữ", "af_aoede": "🇺🇸 Aoede — nữ",
    "af_kore": "🇺🇸 Kore — nữ", "af_jessica": "🇺🇸 Jessica — nữ", "af_river": "🇺🇸 River — nữ", "af_alloy": "🇺🇸 Alloy — nữ",
    "am_michael": "🇺🇸 Michael — nam, trầm ấm", "am_fenrir": "🇺🇸 Fenrir — nam, mạnh", "am_puck": "🇺🇸 Puck — nam, vui",
    "am_onyx": "🇺🇸 Onyx — nam, trầm", "am_eric": "🇺🇸 Eric — nam", "am_liam": "🇺🇸 Liam — nam", "am_adam": "🇺🇸 Adam — nam",
    "am_echo": "🇺🇸 Echo — nam",
}
VOICE_DEFAULTS = dict(voice="bf_emma", voice2="", mix=0.3, lang="auto", speed=1.0, sentence_pause=0.25,
                      clause_pause=0.1, pause_scale=1.0, pad_start=0.3, pad_end=0.6, loudness=-16, lexicon="")


def voice_cfg(proj: Path = None, override=None):
    """Voice settings: defaults <- project's voice.json <- override (from the UI)."""
    cfg = dict(VOICE_DEFAULTS)
    if proj and (proj / "voice.json").exists():
        cfg.update(json.loads((proj / "voice.json").read_text(encoding="utf-8")))
    cfg.update({k: v for k, v in (override or {}).items() if k in VOICE_DEFAULTS and v is not None})
    for k in ("mix", "speed", "sentence_pause", "clause_pause", "pause_scale", "pad_start", "pad_end", "loudness"):
        cfg[k] = float(cfg[k])
    if cfg["voice"] not in VOICES or (cfg["voice2"] and cfg["voice2"] not in VOICES):
        raise ValueError("unknown voice")
    cfg["speed"] = min(2.0, max(0.5, cfg["speed"]))
    return cfg


def save_voice_cfg(proj: Path, cfg):
    (proj / "voice.json").write_text(json.dumps({k: cfg[k] for k in VOICE_DEFAULTS}, indent=2, ensure_ascii=False),
                                     encoding="utf-8")

# ----------------------------------------------------------------- prompts
IDEAS_PROMPT = """Suggest {n} fruits (or fruit-like plants) that would make a strong 8-10 minute YouTube history documentary \
in the style "The history of <fruit>" for an English-speaking audience.
Prefer fruits that are getting attention right now (health trends, social media, news, supermarkets, recent YouTube interest) \
AND have a surprising, story-rich history. Use web search if you can.
Do NOT suggest any of these (already made): {done}.
Return ONLY one code block containing a JSON array, no other text:
```json
[{{"fruit": "...", "title": "catchy YouTube title", "hook": "one-sentence surprising hook", "why_now": "why it is trending now"}}]
```"""

RESEARCH_PROMPT = """I want to make an 8-10 minute YouTube history documentary about this fruit: "{fruit}",
in the style "The history of <fruit>" for an English-speaking audience (calm British narration, illustrated).

These are the competing long videos already on YouTube (views | minutes | date | channel | title):
{competitors}

Use web search to research "{fruit}": its history, legends, surprising facts, why people care about it now
(health trends, news, social media, supermarkets), and what the competitors above already cover.
Then answer in JSON:
- "fruit": the fruit's common English name
- "hook": one surprising sentence that makes people want to watch (English)
- "why_now": why it gets attention now (Vietnamese)
- "covered": what the competitors already cover and how (Vietnamese, 1-2 sentences)
- "angle": the unique story angle WE should take that competitors miss (Vietnamese, 1-2 sentences)
- "titles": 3 different catchy titles for our angle (English), each at most {tmax} characters
- "hashtags": 2-3 hashtags, the first is the fruit itself (e.g. "#monkfruit #foodhistory")
RULE: each title + " " + hashtags must be at most 100 characters in total.
Return ONLY one code block with a JSON array of one object, no other text:
```json
[{{"fruit": "...", "hook": "...", "why_now": "...", "covered": "...", "angle": "...", "titles": ["...", "...", "..."], "hashtags": "#a #b"}}]
```"""


def fit_title(title, hashtags, limit=100):
    """Keep title + ' ' + hashtags within limit: drop hashtags from the end first, then shorten the title."""
    title, hs = (title or "").strip(), (hashtags or "").split()
    while hs and len(f"{title} {' '.join(hs)}") > limit:
        hs.pop()
    if len(title) > limit:
        title = title[:limit].rsplit(" ", 1)[0]
    return title, " ".join(hs)


SCRIPT_PROMPT = """Write the voice-over script for a calm, British-narrated YouTube history documentary: "{title}" (topic: {topic}).

LENGTH: about {minutes} minutes in total, exactly {parts} parts, each part 120-150 words (about 55 seconds of speech).
STORY ARC: part 1 = a hook / mystery that makes people stay; then origins, names and legends, traditional uses, \
how it grows, the science, how it spread around the world, modern life; last part = legacy, ending with: \
"Thanks for watching. In our next episode, we'll uncover the story of another fruit with a surprising past."
STYLE: warm spoken British English, short clear sentences, vivid but factual. Write numbers and years as words \
(it will be read by text-to-speech). No lists, no headings inside parts, no emojis, no stage directions. \
Be historically accurate; when something is only legend, say so ("tradition holds", "historians are unsure").
PAUSES: put [pause 0.3s] for a light pause, [pause 0.5s] for a dramatic pause, [pause 1s] when the idea changes. \
Each part needs 3 to 7 [pause 1s] markers, about every 2-3 sentences (each one becomes a new picture in the video).

OUTPUT: ONLY one markdown code block, nothing before or after it, in exactly this format:
```markdown
# {topic}: voice-over

## Part 01. <Part title> (0:00–0:50) · ~110 words

<text with [pause ...] markers>

## Part 02. <Part title> (0:50–1:45) · ~120 words

<text>
```"""


# ----------------------------------------------------------------- text AI over official APIs (no browser)
API_SITES = {"api_gemini": "GEMINI_API_KEY", "api_openai": "OPENAI_API_KEY"}


def ask_api(site, prompt, keys, log=print):
    t = time.time()
    if site == "api_gemini":
        model = keys.get("GEMINI_TEXT_MODEL") or "gemini-2.5-flash"
        log(f"  Gemini API ({model})…")
        r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                          params={"key": keys["GEMINI_API_KEY"]}, timeout=600,
                          json={"contents": [{"parts": [{"text": prompt}]}],
                                "tools": [{"google_search": {}}]})
        if not r.ok:
            raise RuntimeError(f"Gemini API {r.status_code}: {r.text[:300]}")
        parts = r.json()["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts)
    elif site == "api_openai":
        model = keys.get("OPENAI_TEXT_MODEL") or "gpt-4.1-mini"
        log(f"  OpenAI API ({model})…")
        r = requests.post("https://api.openai.com/v1/chat/completions", timeout=600,
                          headers={"Authorization": f"Bearer {keys['OPENAI_API_KEY']}"},
                          json={"model": model, "messages": [{"role": "user", "content": prompt}]})
        if not r.ok:
            raise RuntimeError(f"OpenAI API {r.status_code}: {r.text[:300]}")
        text = r.json()["choices"][0]["message"]["content"]
    else:
        raise ValueError(site)
    log(f"  got {len(text)} chars in {time.time() - t:.0f}s")
    m = re.search(r"```(?:\w+)?\n(.*?)```", text, re.S)  # answer is asked to be one code block
    return m.group(1) if m and len(m.group(1)) > 200 else text


def ask(site, prompt, keys, log=print):
    """Text answer from a web AI (browser) or an API, depending on `site`."""
    if site in API_SITES:
        if not keys.get(API_SITES[site]):
            raise RuntimeError(f"cần {API_SITES[site]}")
        return ask_api(site, prompt, keys, log)
    import browser_ai
    return browser_ai.ask(site, prompt, log=log, **browser_ai.cfg(keys, site))


SCENE_PROMPT = """You are the picture researcher for a YouTube history documentary: "{title}" (topic: {topic}).
Each scene below is shown on screen while the narrator says its text. Decide what the viewer should SEE.
Use web search when useful to check what the real people, places, plants, objects and periods looked like.

For every scene return:
- "id": the scene id, unchanged
- "query": 2-5 English keywords to find a REAL photo, artwork or historical image on Wikimedia Commons / stock sites \
(concrete nouns, species, place names, objects; no verbs, no abstract words)
- "prompts": exactly n image prompts (n is given per scene) for an AI image generator. Each prompt must stand alone with \
FULL context: main subject, place, period or year, people's appearance and clothing, setting, lighting, camera angle. \
Do NOT write the art style, aspect ratio or "no text" - the app adds these itself (style: {style}). \
For events before photography say which period look fits (antique painting, old engraving, sepia photo). \
Historically and botanically accurate (e.g. what the real fruit, tree and tools look like). No text, no logos, \
no modern objects in historical scenes. When n > 1, make the pictures clearly different (subject, angle or distance).

Video context: {context}

Scenes (id | part | n | narration):
{scenes}

Return ONLY one code block containing a JSON array, no other text:
```json
[{{"id": "p01_s01", "query": "...", "prompts": ["..."]}}]
```"""


SUGGEST_PROMPT = """You are the picture researcher for a YouTube history documentary: "{title}" (topic: {topic}).
Part {part}: "{part_title}". The narrator says this while ONE scene is on screen:

"{text}"

(Before it: "{prev}" / After it: "{next}")

Suggest {n} different pictures that would illustrate exactly this passage well. Use web search to check what real
places, people, plants and objects look like, and prefer things that exist as real photos or historical images.
For each picture give:
- "title": short title in Vietnamese
- "desc": one sentence in Vietnamese describing what is in the picture
- "query": 3-6 English keywords to find it as a REAL photo/artwork on Wikimedia Commons or stock sites
- "short": the 2-3 most important of those keywords (free photo archives need short queries)
- "prompt": an English prompt for an AI image generator with FULL context (subject, place, period/year, people and
  clothing, setting, lighting, camera angle), historically accurate; do NOT write art style, aspect ratio or "no text".
Order them from best to least good.
Return ONLY one code block with a JSON array, no other text:
```json
[{{"title": "...", "desc": "...", "query": "...", "short": "...", "prompt": "..."}}]
```"""


SEGMENT_PROMPT = """You are the picture editor for a YouTube history documentary: "{title}" (topic: {topic}).
Part {part}: "{part_title}". This narration is ONE scene, about {secs:.0f} seconds on screen:

"{text}"

(Before it: "{prev}" / After it: "{next}")

Split the narration into {lo} to {hi} consecutive segments, one per distinct visual idea, so the picture changes when
the idea changes. Each segment needs at least 20 words (about 8 seconds); prefer FEWER, longer segments. Copy the original words EXACTLY and in order,
covering the whole narration with no gaps. If it is really one idea, return a single segment.
Use web search when useful to check what the real places, people, plants and objects look like.
For each segment give:
- "text": the exact words of that segment
- "title": short title in Vietnamese
- "desc": one sentence in Vietnamese: what the picture shows
- "queries": 2-3 SHORT English searches (2-3 words each) that a free photo archive can actually match. The FIRST one
  shows what happens in the picture (e.g. "scientist microscope", "medieval market"), then the main subject
  (e.g. "sea buckthorn berries"). Never pile everything into one search - nobody has a photo of all of it.
- "query": 3-6 English keywords describing the picture
- "short": the 2-3 most important of those keywords
- "prompt": an English prompt for an AI image generator illustrating exactly that segment, with FULL context
  (subject, place, period/year, people and clothing, setting, lighting, camera angle), historically accurate;
  do NOT write art style, aspect ratio or "no text".
Return ONLY one code block with a JSON array, no other text:
```json
[{{"text": "...", "title": "...", "desc": "...", "queries": ["...", "..."], "query": "...", "short": "...", "prompt": "..."}}]
```"""


def segment_times(rows, scene, voice_path, min_len=2.5):
    """Fractions (0..1 of the scene) where each segment starts: by word count, snapped to a real pause in the voice.
    Segments shorter than min_len seconds are merged into the previous one."""
    start, end = scene["start"], scene["end"]
    dur = max(end - start, 0.1)
    counts = [max(1, len((r.get("text") or "").split())) for r in rows]
    total = sum(counts)
    sil = vidgen.silences(voice_path) if voice_path and Path(voice_path).exists() else []
    mids = [(a + b) / 2 for a, b in sil if start + 1.5 < (a + b) / 2 < end - 1.5]
    fr, acc = [0.0], 0
    for c in counts[:-1]:
        acc += c
        t = start + dur * acc / total
        near = min(mids, key=lambda m: abs(m - t), default=None)
        if near is not None and abs(near - t) < 1.5:
            t = near
        fr.append((t - start) / dur)
    keep_rows, keep_fr = [rows[0]], [0.0]
    for r, f in zip(rows[1:], fr[1:]):
        if (f - keep_fr[-1]) * dur < min_len:  # too short: glue its words onto the previous segment
            keep_rows[-1] = {**keep_rows[-1], "text": f'{keep_rows[-1].get("text", "")} {r.get("text", "")}'.strip()}
            continue
        keep_rows.append(r)
        keep_fr.append(f)
    if len(keep_fr) > 1 and (1 - keep_fr[-1]) * dur < min_len:
        last = keep_rows.pop()
        keep_fr.pop()
        keep_rows[-1] = {**keep_rows[-1], "text": f'{keep_rows[-1].get("text", "")} {last.get("text", "")}'.strip()}
    return keep_rows, [round(f, 4) for f in keep_fr]


def real_candidates(row, keys, free, sources, per=4, want=8):
    """Real photos for one picture idea. Searches the AI's short queries in order (action first, then subject);
    results keep that order (best fit first inside each search), so the picture of what HAPPENS comes first.
    Long queries are only shortened as a last resort."""
    words = (row.get("query") or "").split()
    tries = [q for q in (row.get("queries") or []) if isinstance(q, str)]
    tries += [row.get("short", ""), row.get("query", "")]
    out, seen = [], set()

    def run(q):
        got = []
        for src in free[:3]:
            try:
                for c in sources.search(src, q, per, keys)[:per]:
                    if c.get("url") not in seen:
                        seen.add(c.get("url"))
                        c["q"] = q
                        got.append(c)
            except Exception:  # noqa: BLE001
                pass
        return sources.rank(got)

    for q in dict.fromkeys(t.strip() for t in tries if t and t.strip()):
        out += run(q)
        if len(out) >= want and sum(1 for c in out if c.get("fit", 0) >= 0.9) >= 3:
            break
    if not out:  # nothing at all: shorten the long query
        for q in dict.fromkeys(" ".join(words[:n]) for n in (3, 2) if len(words) > n):
            out += run(q)
            if out:
                break
    return out[:want]


def scene_context(plan, sid):
    """(part, scene, previous text, next text) for a scene id."""
    flat = [(pt, s) for pt in plan["parts"] for s in pt["scenes"]]
    for i, (pt, s) in enumerate(flat):
        if s["id"] == sid:
            prev = flat[i - 1][1]["text"] if i else ""
            nxt = flat[i + 1][1]["text"] if i + 1 < len(flat) else ""
            return pt, s, prev[-300:], nxt[:300]
    raise KeyError(sid)


def scene_batches(plan, pr, only_ids=None, size=18):
    """[(scene lines text, {id: n})] in batches small enough for one chat answer."""
    rows = []
    for part in plan["parts"]:
        for s in part["scenes"]:
            if only_ids and s["id"] not in only_ids:
                continue
            n = max(1, int(pr.get("shots", {}).get(s["id"], 1)))
            rows.append((s["id"], f'{s["id"]} | Part {part["num"]}: {part["title"]} | n={n} | {s["text"]}', n))
    return [("\n".join(r[1] for r in rows[i:i + size]), {r[0]: r[2] for r in rows[i:i + size]})
            for i in range(0, len(rows), size)]


def parse_json_block(text):
    m = re.search(r"\[\s*{.*}\s*\]", text, re.S)
    if not m:
        raise ValueError("AI không trả về danh sách JSON: " + text[:200])
    return json.loads(m.group(0))


def normalize_script(text):
    """Clean an AI answer into the vidgen script format."""
    text = re.sub(r"^```(?:markdown|md)?\s*|\s*```\s*$", "", text.strip())
    lines = []
    for ln in text.splitlines():
        s = ln.strip()
        # headings sometimes lose their '##' when copied from a rendered answer
        if re.match(r"^(#+\s*)?\**\s*Part\s+\d+\s*[.:]", s, re.I):
            s = re.sub(r"^#+\s*|\*", "", s).strip()
            s = re.sub(r"^Part\s+(\d+)\s*[.:]\s*", lambda m: f"## Part {int(m.group(1)):02d}. ", s, flags=re.I)
        s = re.sub(r"\[\s*pause\s*([\d.]+)\s*s?\s*\]", r"[pause \1s]", s, flags=re.I)
        lines.append(s)
    return "\n".join(lines).strip() + "\n"


def done_topics(root: Path):
    return [p.name for p in root.iterdir() if p.is_dir() and (p / "texts").exists()]


# ----------------------------------------------------------------- YouTube competition check (optional key)
def _plain(s):
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFKD", s.lower()) if not unicodedata.combining(c))


def _seconds(iso):
    m = re.match(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso or "")
    d, hh, mm, ss = (int(x or 0) for x in m.groups()) if m else (0, 0, 0, 0)
    return d * 86400 + hh * 3600 + mm * 60 + ss


def youtube_check(topic, key):
    """Competition for a long-form '<topic> history' video.
    demand = views of the best relevant long video; competition = how many relevant long videos already did well."""
    r = requests.get("https://www.googleapis.com/youtube/v3/search", timeout=20, params=dict(
        key=key, part="snippet", type="video", q=f'"{topic}" fruit history', order="relevance", maxResults=25,
        relevanceLanguage="en"))
    r.raise_for_status()
    items = r.json().get("items", [])
    ids = [i["id"]["videoId"] for i in items]
    meta = {}
    if ids:
        s = requests.get("https://www.googleapis.com/youtube/v3/videos", timeout=20,
                         params=dict(key=key, part="statistics,contentDetails", id=",".join(ids)))
        s.raise_for_status()
        meta = {v["id"]: (int(v["statistics"].get("viewCount", 0)), _seconds(v["contentDetails"].get("duration")))
                for v in s.json().get("items", [])}
    words = [w for w in re.findall(r"\w+", _plain(topic)) if len(w) > 2] or [_plain(topic)]
    vids = []
    for i in items:
        import html
        vid, title = i["id"]["videoId"], html.unescape(i["snippet"]["title"])
        views, secs = meta.get(vid, (0, 0))
        vids.append({"title": title, "channel": html.unescape(i["snippet"]["channelTitle"]), "url": f"https://youtu.be/{vid}",
                     "views": views, "minutes": round(secs / 60, 1), "date": i["snippet"]["publishedAt"][:10],
                     "long": secs >= 180 and "#short" not in _plain(title),
                     "relevant": any(w in _plain(title) for w in words)})
    rel_long = sorted((v for v in vids if v["long"] and v["relevant"]), key=lambda v: -v["views"])
    top = rel_long[0]["views"] if rel_long else 0
    strong = sum(1 for v in rel_long if v["views"] >= 100_000)
    demand = "cao" if top >= 300_000 else "vừa" if top >= 20_000 else "thấp"
    comp = "cao" if strong >= 4 else "vừa" if strong >= 1 else "thấp"
    if demand == "thấp":
        verdict = ("🟡", "Ít người xem chủ đề này dạng dài — dễ chen nhưng chưa chắc có view")
    elif comp == "cao":
        verdict = ("🔴", f"Đã có {strong} video dài >100K view — khó chen, cần góc kể khác hẳn")
    elif comp == "vừa":
        verdict = ("🟢", f"Có nhu cầu, mới {strong} video mạnh — làm hay hơn là có cơ hội")
    else:
        verdict = ("🟢", "Có người xem nhưng chưa ai làm tốt — cơ hội tốt")
    return {"topic": topic, "demand": demand, "competition": comp, "verdict": verdict, "long_count": len(rel_long),
            "strong": strong, "top_long_views": top, "videos": rel_long[:3], "competitors": rel_long[:12],
            "shorts_top": max((v["views"] for v in vids if not v["long"] and v["relevant"]), default=0)}


# ----------------------------------------------------------------- TTS (Kokoro, local)
_KOKORO = None


def ensure_models(log=print):
    MODELS.mkdir(exist_ok=True)
    for name, url in KOKORO_FILES.items():
        f = MODELS / name
        if f.exists() and f.stat().st_size > 1_000_000:
            continue
        log(f"  tải mô hình giọng {name} (một lần)…")
        with requests.get(url, stream=True, timeout=60) as r:
            r.raise_for_status()
            tmp = f.with_suffix(".part")
            with open(tmp, "wb") as fh:
                for chunk in r.iter_content(1 << 20):
                    fh.write(chunk)
            tmp.replace(f)


def kokoro(log=print):
    global _KOKORO
    if _KOKORO is None:
        ensure_models(log)
        from kokoro_onnx import Kokoro
        _KOKORO = Kokoro(str(MODELS / "kokoro-v1.0.onnx"), str(MODELS / "voices-v1.0.bin"))
    return _KOKORO


def chunks_of(part):
    """[(text, pause_after_seconds)] split at the [pause] markers."""
    out, cur = [], []
    for kind, val in part["tokens"]:
        if kind == "w":
            cur.append(val)
        else:
            if cur:
                out.append((" ".join(cur), float(val)))
            elif out:
                out[-1] = (out[-1][0], out[-1][1] + float(val))
            cur = []
    if cur:
        out.append((" ".join(cur), 0.0))
    return out


def apply_lexicon(text, lexicon):
    """'word = how to say it' lines -> plain text replacements (whole words, any case)."""
    for line in (lexicon or "").splitlines():
        if "=" in line:
            a, b = (x.strip() for x in line.split("=", 1))
            if a and b:
                text = re.sub(rf"(?<!\w){re.escape(a)}(?!\w)", b, text, flags=re.I)
    return text


def synth(chunks, cfg):
    import numpy as np
    k = kokoro()
    style = cfg["voice"]
    if cfg.get("voice2") and cfg["mix"] > 0:
        a, b = k.get_voice_style(cfg["voice"]), k.get_voice_style(cfg["voice2"])
        style = (a * (1 - cfg["mix"]) + b * cfg["mix"]).astype(np.float32)
    lang = cfg["lang"] if cfg["lang"] != "auto" else ("en-gb" if cfg["voice"].startswith("b") else "en-us")
    sr = 24000
    pieces = [np.zeros(int(sr * cfg["pad_start"]), dtype=np.float32)]
    for text, pause in chunks:
        audio, sr = k.create(apply_lexicon(text, cfg["lexicon"]), voice=style, speed=cfg["speed"], lang=lang,
                             sentence_pause=cfg["sentence_pause"], clause_pause=cfg["clause_pause"])
        pieces += [audio, np.zeros(int(sr * (pause * cfg["pause_scale"] + 0.08)), dtype=np.float32)]
    pieces.append(np.zeros(int(sr * cfg["pad_end"]), dtype=np.float32))
    return np.concatenate(pieces), sr


def write_m4a(wav, sr, dest: Path, loudness=-16):
    import soundfile as sf
    tmp = dest.with_suffix(".tmp.wav")
    sf.write(tmp, wav, sr)
    r = subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(tmp),
                        "-af", f"loudnorm=I={loudness:g}:TP=-1.5:LRA=11", "-ar", "48000", "-c:a", "aac", "-b:a", "192k", str(dest)],
                       capture_output=True, text=True)
    tmp.unlink(missing_ok=True)
    if r.returncode:
        raise RuntimeError(r.stderr[-500:])


def make_voices(proj: Path, cfg, parts=None, log=print):
    """Read the script and write voices/part N.m4a for each part. Existing voices are moved to voices/_old/."""
    md = next((proj / "texts").glob("*.md"), None)
    if not md:
        raise RuntimeError("chưa có kịch bản")
    script = vidgen.parse_script(md)
    vdir = proj / "voices"
    vdir.mkdir(exist_ok=True)
    kokoro(log)
    for part in script:
        if parts and part["num"] not in parts:
            continue
        t = time.time()
        for old in vdir.glob("*"):
            if old.is_file() and re.search(rf"part\s*0*{part['num']}\b", old.stem, re.I):
                (vdir / "_old").mkdir(exist_ok=True)
                old.replace(vdir / "_old" / f"{int(time.time())}_{old.name}")
        wav, sr = synth(chunks_of(part), cfg)
        write_m4a(wav, sr, vdir / f"part {part['num']}.m4a", cfg["loudness"])
        log(f"  part {part['num']}: {len(wav) / sr:.0f}s giọng ({time.time() - t:.0f}s) (done in)")


def sample(cfg, text, dest: Path):
    import soundfile as sf
    wav, sr = synth([(text, 0.2)], {**cfg, "pad_start": 0.1, "pad_end": 0.2})
    sf.write(dest, wav, sr)
