"""
Generate images with web AI chats (Gemini / ChatGPT / Grok) by driving a real Chrome
through CDP, with YOUR logged-in accounts. No API keys, no passwords stored.

How it works (same approach as google-flow-browser-mcp):
  - If a Chrome with remote debugging is already running on the CDP port (default 9222,
    e.g. the Flow MCP browser), we reuse it and its sign-ins.
  - Otherwise we launch Chrome with our own persistent profile (vidgen/browser-profile).
    Sign in once to gemini.google.com / chatgpt.com / grok.com in that window.
  - Each generation opens a NEW tab, types the prompt, waits for the image, saves the bytes
    and closes the tab. On failure the tab stays open and a screenshot goes to vidgen/data/.

These sites' DOM is not a public API: every step tries several selectors, so when a site
changes, adjust the SITES table below.
"""
import os
import re
import subprocess
import threading
import time
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
PROFILE_DIR = HERE / "browser-profile"
DEBUG_DIR = HERE / "data"
LOCK = threading.Lock()  # one browser generation at a time

BRAVE = r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"
BROWSERS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

SITES = {
    "gemini": dict(
        label="Gemini (web)",
        url="https://gemini.google.com/app",
        login_re=r"accounts\.google\.com",
        input=['rich-textarea [contenteditable="true"]', 'div.ql-editor[contenteditable="true"]',
               'div[contenteditable="true"][role="textbox"]', "textarea"],
        send_re=r"^(send|gửi)|send message|gửi tin nhắn|gửi lời nhắc",
        stop_re=r"^(stop|dừng|ngừng)",
        response="model-response",
        download_re=r"full.?size|kích thước đầy đủ|^download|tải.*xuống",
        prompt="Generate an image: {prompt}. {aspect}. No text, no watermark. "
               "Only return the image, no explanation.",
    ),
    "chatgpt": dict(
        label="ChatGPT (web)",
        url="https://chatgpt.com/",
        login_re=r"auth\.openai\.com|/auth/login",
        input=["#prompt-textarea", 'div[contenteditable="true"].ProseMirror', "textarea"],
        send_selector='[data-testid="send-button"], button[aria-label*="Send" i]',
        send_re=r"^send|send prompt|gửi",
        stop_selector='[data-testid="stop-button"], button[aria-label*="Stop" i]',
        stop_re=r"^stop|dừng|ngừng",
        response='[data-message-author-role="assistant"], [data-markdown-text-style="assistant-message"], article',
        logged_out_re=r"^(log in|đăng nhập)$",
        download_re=r"download|tải xuống",
        prompt="Create an image: {prompt}. {aspect}. No text, no watermark. "
               "Do not ask questions, just generate the image.",
    ),
    "grok": dict(
        label="Grok (web)",
        url="https://grok.com/",
        login_re=r"accounts\.x\.ai|/sign-in",
        input=['div.tiptap[contenteditable="true"]', 'div[contenteditable="true"]',
               'textarea[aria-label*="Grok" i]', "textarea"],
        send_selector='button[type="submit"], button[aria-label*="Submit" i]',
        send_re=r"^(submit|send|gửi)",
        stop_re=r"^(stop|dừng|ngừng)",
        response='[class*="message-bubble"], [class*="response"], main',
        prompt="Generate an image: {prompt}. {aspect}. No text, no watermark.",
    ),
}


# ----------------------------------------------------------------- browser
def find_browser(configured=None):
    for p in [configured] + BROWSERS:
        if p and Path(p).exists():
            return p
    raise RuntimeError("Không tìm thấy Chrome/Edge. Đặt BROWSER_PATH trong tab ⚙.")


def cdp_alive(port):
    try:
        return requests.get(f"http://127.0.0.1:{port}/json/version", timeout=2).ok
    except requests.RequestException:
        return False


SITE_KEYS = ("BROWSER_PATH", "CDP_PORT", "USER_DATA_DIR", "PROFILE")


def cfg(keys, site=None):
    """Browser for a site: <SITE>_BROWSER_PATH / _CDP_PORT / _USER_DATA_DIR / _PROFILE (e.g. CHATGPT_CDP_PORT),
    falling back to the shared BROWSER_* settings. Each browser needs its own CDP port and user-data dir."""
    def get(name, shared):
        own = keys.get(f"{site.upper()}_{name}") if site else None
        return own or keys.get(shared)
    own_port = site and keys.get(f"{site.upper()}_CDP_PORT")
    return dict(port=int(get("CDP_PORT", "BROWSER_CDP_PORT") or 9222),
                browser_path=get("BROWSER_PATH", "BROWSER_PATH") or None,
                user_data_dir=(keys.get(f"{site.upper()}_USER_DATA_DIR") or str(HERE / f"browser-profile-{site}"))
                if own_port else (keys.get("BROWSER_USER_DATA_DIR") or str(PROFILE_DIR)),
                profile=(keys.get(f"{site.upper()}_PROFILE") if own_port else get("PROFILE", "BROWSER_PROFILE")) or None)


def status(keys):
    """Which browser each AI site uses and whether it is running."""
    out = {}
    for s in SITES:
        c = cfg(keys, s)
        out[s] = dict(port=c["port"], alive=cdp_alive(c["port"]), browser=c["browser_path"] or "auto (Chrome/Edge)",
                      profile=c["user_data_dir"] + (f" › {c['profile']}" if c["profile"] else ""))
    return out


def ensure_browser(port=9222, browser_path=None, open_urls=(), user_data_dir=None, profile=None):
    """Start the browser with CDP + persistent profile unless one is already listening."""
    if cdp_alive(port):
        return "reused"
    user_data_dir = user_data_dir or str(PROFILE_DIR)
    Path(user_data_dir).mkdir(parents=True, exist_ok=True)
    args = [find_browser(browser_path), f"--remote-debugging-port={port}", f"--user-data-dir={user_data_dir}",
            *([f"--profile-directory={profile}"] if profile else []),
            "--no-first-run", "--no-default-browser-check", "--disable-blink-features=AutomationControlled",
            "--window-size=1500,1000", *open_urls]
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:  # break away from the server's job object so the browser survives server restarts
        subprocess.Popen(args, creationflags=flags | getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    except OSError:
        subprocess.Popen(args, creationflags=flags, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    for _ in range(30):
        if cdp_alive(port):
            return "launched"
        time.sleep(1)
    raise RuntimeError(f"Trình duyệt không mở được cổng CDP {port}. Đóng hết cửa sổ Edge/Chrome đang dùng "
                       f"profile này (kể cả Startup Boost / icon khay hệ thống) rồi thử lại.")


def open_login_tabs(keys, sites=None):
    """Open each site in the browser it is configured to use, so the user can sign in once."""
    from playwright.sync_api import sync_playwright
    groups = {}
    for s in sites or SITES:
        c = cfg(keys, s)
        groups.setdefault(c["port"], (c, []))[1].append(SITES[s]["url"])
    res = {}
    for port, (c, urls) in groups.items():
        how = ensure_browser(port, c["browser_path"], urls, c["user_data_dir"], c["profile"])
        if how == "reused":
            with sync_playwright() as p:
                ctx = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}").contexts[0]
                for u in urls:
                    pg = ctx.new_page()
                    pg.goto(u, wait_until="domcontentloaded")
                    pg.bring_to_front()
        res[port] = how
    return res


# ----------------------------------------------------------------- page helpers
def _shot(page, step):
    try:
        DEBUG_DIR.mkdir(exist_ok=True)
        f = DEBUG_DIR / f"{int(time.time())}-{step}.png"
        page.screenshot(path=str(f))
        return f
    except Exception:  # noqa: BLE001
        return None


def _fail(page, step, msg):
    f = _shot(page, step)
    raise RuntimeError(msg + (f" (ảnh chụp: {f})" if f else ""))


def _find_button(page, pattern, selector=None, scope=None):
    scope = scope or page
    if selector:
        loc = scope.locator(selector)
        for i in range(loc.count()):
            b = loc.nth(i)
            if b.is_visible():
                return b
    rx = re.compile(pattern, re.I)
    loc = scope.locator('button, [role="button"], a[download]')
    for i in range(min(loc.count(), 300)):
        b = loc.nth(i)
        try:
            if not b.is_visible():
                continue
            label = " | ".join(filter(None, [b.get_attribute("aria-label"), b.get_attribute("mattooltip"),
                                             b.get_attribute("data-tooltip"), (b.inner_text() or "").strip()[:40]]))
            if rx.search(label):
                return b
        except Exception:  # noqa: BLE001
            continue
    return None


def _generating(page, site):
    return _find_button(page, site["stop_re"], site.get("stop_selector")) is not None


def _result_images(page, site, before_srcs):
    """Large images inside responses that were not on the page before we sent the prompt."""
    return page.evaluate("""([sel, before]) => {
        const seen = new Set(before);
        const scopes = [...document.querySelectorAll(sel)];
        const imgs = (scopes.length ? scopes : [document.body]).flatMap(s => [...s.querySelectorAll('img')]);
        const out = [], uniq = new Set();
        for (const im of imgs) {
          const src = im.currentSrc || im.src;
          if (!src || seen.has(src) || uniq.has(src)) continue;
          if (!im.complete || im.naturalWidth < 400 || im.naturalHeight < 250) continue;
          uniq.add(src); out.push({src, w: im.naturalWidth, h: im.naturalHeight});
        }
        return out;
    }""", [site["response"], before_srcs])


def _last_text(page, site, n=300):
    try:
        return page.evaluate("""([sel, n]) => { const r = [...document.querySelectorAll(sel)].pop();
            return (r?.innerText || '').trim().slice(0, n); }""", [site["response"], n])
    except Exception:  # noqa: BLE001
        return ""


def _all_img_srcs(page):
    return page.evaluate("() => [...document.images].map(i => i.currentSrc || i.src)")


def _download_full(page, site, src):
    """Click the site's own 'download full size' button for this image (best quality)."""
    if not site.get("download_re"):
        return None
    img = page.locator(f'img[src="{src}"]').first
    resp = page.locator(site["response"].split(",")[0].strip(), has=img).last
    scope = resp if resp.count() else page
    img.scroll_into_view_if_needed()
    img.hover()
    page.wait_for_timeout(700)
    btn = _find_button(page, site["download_re"], scope=scope)
    if not btn:
        return None
    with page.expect_download(timeout=90000) as dl:
        btn.click()
    tmp = DEBUG_DIR / "last_download"
    DEBUG_DIR.mkdir(exist_ok=True)
    dl.value.save_as(str(tmp))
    data = tmp.read_bytes()
    return data if len(data) > 5000 else None


def _save(page, ctx, src, captured, site=None):
    """Get the image bytes: the site's full-size download button, then a '=s0' URL variant,
    then the response the tab already received, else refetch."""
    if site:
        try:
            data = _download_full(page, site, src)
            if data:
                return data
        except Exception as e:  # noqa: BLE001
            print("download button failed:", e)
    if src.startswith("http") and re.search(r"=s\d+[^/]*$", src):
        try:
            r = ctx.request.get(re.sub(r"=s\d+[^/]*$", "=s0", src), headers={"Referer": page.url})
            if r.ok and len(r.body()) > 5000:
                return r.body()
        except Exception:  # noqa: BLE001
            pass
    key = src.split("=s")[0].split("?")[0]
    for u, resp in reversed(captured):
        if u == src or u.split("=s")[0].split("?")[0] == key:
            try:
                b = resp.body()
                if b and len(b) > 5000:
                    return b
            except Exception:  # noqa: BLE001
                pass
    if src.startswith("http"):
        for u in [src, re.sub(r"=s\d+[^/]*$", "=s0", src)]:
            try:
                r = ctx.request.get(u, headers={"Referer": page.url})
                if r.ok and len(r.body()) > 5000:
                    return r.body()
            except Exception:  # noqa: BLE001
                pass
    if src.startswith(("blob:", "data:")):
        import base64
        b64 = page.evaluate("""async u => { const b = await (await fetch(u)).blob();
            return await new Promise(r => { const f = new FileReader(); f.onload = () => r(String(f.result).split(',')[1]); f.readAsDataURL(b); }); }""", src)
        return base64.b64decode(b64)
    # last resort: screenshot of the displayed image
    return page.locator(f'img[src="{src}"]').first.screenshot()


# ----------------------------------------------------------------- main entry
def _open_and_send(ctx, site_id, text):
    """New tab on the site, type `text`, send. Returns (page, captured_image_responses, img_srcs_before)."""
    site = SITES[site_id]
    page = ctx.new_page()
    captured = []
    page.on("response", lambda r: captured.append((r.url, r))
            if r.request.resource_type in ("image", "fetch", "xhr") and r.ok
            and "image" in (r.headers.get("content-type") or "") else None)
    page.goto(site["url"], wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(2500)
    if re.search(site["login_re"], page.url) or (
            site.get("logged_out_re") and _find_button(page, site["logged_out_re"]) is not None):
        _fail(page, f"{site_id}-login", f"{site['label']}: chưa đăng nhập. Tab ⚙ → 'Mở trình duyệt + 3 trang để đăng nhập', "
                                        f"đăng nhập {site['label']} trong cửa sổ đó rồi thử lại.")
    box = None
    for sel in site["input"]:
        loc = page.locator(sel).first
        try:
            loc.wait_for(state="visible", timeout=8000 if box is None else 1000)
            box = loc
            break
        except Exception:  # noqa: BLE001
            continue
    if box is None:
        _fail(page, f"{site_id}-input", f"{site['label']}: không thấy ô nhập (có thể chưa đăng nhập).")
    before = _all_img_srcs(page)
    box.click()
    page.keyboard.insert_text(text)
    page.wait_for_timeout(800)
    for _ in range(20):
        b = _find_button(page, site["send_re"], site.get("send_selector"))
        if b and b.is_enabled() and b.get_attribute("aria-disabled") != "true":
            b.click()
            break
        page.wait_for_timeout(500)
    else:
        page.keyboard.press("Enter")
    return page, captured, before


def _answer_text(page, site):
    """Last assistant answer. If it contains a code block, return the code verbatim (keeps markdown)."""
    return page.evaluate("""sel => { const r = [...document.querySelectorAll(sel)].pop(); if (!r) return '';
        const codes = [...r.querySelectorAll('[data-chatgpt-copy-code]')].map(c => (c.getAttribute('data-chatgpt-copy-code') || '').trim())
            .concat([...r.querySelectorAll('pre code, pre, code-block code')].map(c => c.innerText.trim()))
            .filter(t => t.length > 200);
        return codes.length ? codes.sort((a, b) => b.length - a.length)[0] : (r.innerText || '').trim(); }""",
                         site["response"])


def ask(site_id, prompt, port=9222, browser_path=None, user_data_dir=None, profile=None, timeout=600, log=print):
    """Send a text prompt to a web AI (ChatGPT / Gemini / Grok) and return its text answer."""
    from playwright.sync_api import sync_playwright
    site = SITES[site_id]
    with LOCK:
        how = ensure_browser(port, browser_path, (), user_data_dir, profile)
        log(f"  browser: {how} (CDP {port}) → {site['label']}")
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            page, _, _ = _open_and_send(ctx, site_id, prompt)
            log("  prompt sent, waiting for the answer…")
            start, last, stable_since = time.time(), "", None
            while time.time() - start < timeout:
                page.wait_for_timeout(2500)
                txt = _answer_text(page, site)
                busy = _generating(page, site)
                if txt and not busy and txt == last:
                    stable_since = stable_since or time.time()
                    if time.time() - stable_since > 5:
                        break
                else:
                    stable_since = None
                if not txt and not busy and time.time() - start > 120:
                    _fail(page, f"{site_id}-answer", f"{site['label']}: sau 2 phút vẫn chưa trả lời "
                                                     f"(chưa đăng nhập, hết lượt, hoặc có hộp thoại chặn).")
                if txt != last and len(txt) // 2000 != len(last) // 2000:
                    log(f"  … {len(txt)} ký tự")
                last = txt
            if not last:
                _fail(page, f"{site_id}-answer", f"{site['label']}: không có câu trả lời (hết {timeout}s).")
            log(f"  got {len(last)} chars in {time.time() - start:.0f}s")
            page.close()
            # Gemini prefixes answers with a screen-reader label ("Gemini said" / "Gemini đã nói")
            return re.sub(r"^\s*(gemini|chatgpt|grok)\s+(said|đã nói)\s*:?\s*\n+", "", last, flags=re.I)


ASPECTS = {"16:9": "Wide landscape 16:9 aspect ratio", "4:3": "Landscape 4:3 aspect ratio",
           "1:1": "Square 1:1 aspect ratio", "3:4": "Vertical portrait 3:4 aspect ratio",
           "9:16": "Tall vertical 9:16 aspect ratio (phone screen)"}


def generate(site_id, prompt, port=9222, browser_path=None, user_data_dir=None, profile=None, timeout=300, log=print,
             raw=False, aspect="16:9"):
    """Return image bytes generated by the given web AI for `prompt`.
    raw=True sends the prompt as-is (no "16:9, no text" template), e.g. for thumbnails with a title."""
    from playwright.sync_api import sync_playwright
    site = SITES[site_id]
    with LOCK:
        how = ensure_browser(port, browser_path, (), user_data_dir, profile)
        log(f"  browser: {how} (CDP {port}) → {site['label']}")
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            text = prompt if raw else site["prompt"].format(prompt=prompt, aspect=ASPECTS.get(aspect, ASPECTS["16:9"]))
            page, captured, before = _open_and_send(ctx, site_id, text)
            log("  prompt sent, waiting for image…")

            start, images, stable_since, last_n, idle_since = time.time(), [], None, -1, None
            while time.time() - start < timeout:
                page.wait_for_timeout(2500)
                images = _result_images(page, site, before)
                busy = _generating(page, site)
                if images and not busy:
                    if len(images) == last_n:
                        stable_since = stable_since or time.time()
                        if time.time() - stable_since > 4:
                            break
                    else:
                        stable_since = None
                last_n = len(images)
                # answered (not busy) but no image for a while -> it replied with text only
                if not images and not busy and time.time() - start > 20:
                    idle_since = idle_since or time.time()
                    if time.time() - idle_since > 30:
                        break
                else:
                    idle_since = None
            if not images:
                said = _last_text(page, site)
                _fail(page, f"{site_id}-result", f"{site['label']}: không trả về ảnh"
                      + (f'. Trả lời: "{said}"' if said else f" (hết {timeout}s)") + ".")
            # prefer the image closest to the requested shape, then the biggest
            want = {"16:9": 16 / 9, "4:3": 4 / 3, "1:1": 1, "3:4": 3 / 4, "9:16": 9 / 16}.get(aspect, 16 / 9)
            best = sorted(images, key=lambda i: (-abs(i["w"] / i["h"] - want), i["w"] * i["h"]), reverse=True)[0]
            data = _save(page, ctx, best["src"], captured, site)
            try:
                from io import BytesIO
                from PIL import Image
                w, h = Image.open(BytesIO(data)).size
            except Exception:  # noqa: BLE001
                w, h = best["w"], best["h"]
            log(f"  got image {w}x{h} in {time.time() - start:.0f}s")
            page.close()
            return data
