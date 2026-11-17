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
        stop_re=r"^(stop|dừng)",
        response="model-response",
        download_re=r"full.?size|kích thước đầy đủ|^download|tải.*xuống",
        prompt="Generate an image: {prompt}. Wide landscape 16:9 aspect ratio. No text, no watermark. "
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
        stop_re=r"^stop|dừng",
        response='[data-message-author-role="assistant"], article',
        download_re=r"download|tải xuống",
        prompt="Create an image: {prompt}. Wide landscape 16:9 (1536x1024). No text, no watermark. "
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
        stop_re=r"^(stop|dừng)",
        response='[class*="message-bubble"], [class*="response"], main',
        prompt="Generate an image: {prompt}. Wide landscape 16:9 aspect ratio. No text, no watermark.",
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


def cfg(keys):
    """Browser settings from vidgen/settings.json (same meaning as google-flow-browser-mcp's config)."""
    return dict(port=int(keys.get("BROWSER_CDP_PORT") or 9222),
                browser_path=keys.get("BROWSER_PATH") or None,
                user_data_dir=keys.get("BROWSER_USER_DATA_DIR") or str(PROFILE_DIR),
                profile=keys.get("BROWSER_PROFILE") or None)


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


def open_login_tabs(port=9222, browser_path=None, user_data_dir=None, profile=None):
    """Open the 3 sites so the user can sign in once."""
    urls = [s["url"] for s in SITES.values()]
    how = ensure_browser(port, browser_path, urls, user_data_dir, profile)
    if how == "reused":
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            ctx = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}").contexts[0]
            for u in urls:
                ctx.new_page().goto(u, wait_until="domcontentloaded")
    return how


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
def generate(site_id, prompt, port=9222, browser_path=None, user_data_dir=None, profile=None, timeout=300, log=print):
    """Return image bytes generated by the given web AI for `prompt`."""
    from playwright.sync_api import sync_playwright
    site = SITES[site_id]
    with LOCK:
        how = ensure_browser(port, browser_path, (), user_data_dir, profile)
        log(f"  browser: {how} (CDP {port}) → {site['label']}")
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            page = ctx.new_page()
            captured = []
            page.on("response", lambda r: captured.append((r.url, r))
                    if r.request.resource_type in ("image", "fetch", "xhr") and r.ok
                    and "image" in (r.headers.get("content-type") or "") else None)
            page.goto(site["url"], wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2500)
            if re.search(site["login_re"], page.url):
                _fail(page, f"{site_id}-login", f"{site['label']}: chưa đăng nhập. Đăng nhập trong cửa sổ Chrome rồi thử lại.")

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
            text = site["prompt"].format(prompt=prompt)
            box.click()
            page.keyboard.insert_text(text)
            page.wait_for_timeout(800)
            sent = False
            for _ in range(20):
                b = _find_button(page, site["send_re"], site.get("send_selector"))
                if b and b.is_enabled() and b.get_attribute("aria-disabled") != "true":
                    b.click()
                    sent = True
                    break
                page.wait_for_timeout(500)
            if not sent:
                page.keyboard.press("Enter")
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
            # prefer the widest landscape image
            best = sorted(images, key=lambda i: (i["w"] >= i["h"], i["w"] * i["h"]), reverse=True)[0]
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
