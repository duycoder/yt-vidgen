"""
Image source plugins. Each source is one function registered in SOURCES:
  kind="search": fn(query, n, keys) -> list[Candidate dict]
  kind="ai":     fn(prompt, keys)   -> bytes (image file content)

Candidate dict: {url, thumb, width, height, title, author, license, page, source}
To add a new source: write a function and add it to SOURCES.
"""
import base64
import html
import re

import requests

UA = {"User-Agent": "vidgen/0.2 (local narrated-video tool)"}


def _strip(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


# ----------------------------------------------------------------- free, no key
def wikimedia(query, n, keys):
    r = requests.get("https://commons.wikimedia.org/w/api.php", headers=UA, timeout=30, params=dict(
        action="query", generator="search", gsrsearch=f"{query} filetype:bitmap", gsrnamespace=6,
        gsrlimit=n, prop="imageinfo", iiprop="url|size|extmetadata", iiurlwidth=1920, format="json"))
    r.raise_for_status()
    pages = sorted(r.json().get("query", {}).get("pages", {}).values(), key=lambda p: p.get("index", 0))
    out = []
    for p in pages:
        ii = (p.get("imageinfo") or [{}])[0]
        em = ii.get("extmetadata", {})
        thumb = ii.get("thumburl") or ii.get("url")
        out.append(dict(url=thumb, thumb=thumb.replace("/1920px-", "/500px-") if "/1920px-" in thumb else thumb,
                        width=ii.get("width"), height=ii.get("height"),
                        title=p["title"].removeprefix("File:"), author=_strip(em.get("Artist", {}).get("value")),
                        license=_strip(em.get("LicenseShortName", {}).get("value")),
                        page=ii.get("descriptionurl"), source="wikimedia"))
    return out


def openverse(query, n, keys):
    r = requests.get("https://api.openverse.org/v1/images/", headers=UA, timeout=30,
                     params=dict(q=query, page_size=n, aspect_ratio="wide"))
    if r.status_code == 400:  # some filters unsupported -> retry plain
        r = requests.get("https://api.openverse.org/v1/images/", headers=UA, timeout=30, params=dict(q=query, page_size=n))
    r.raise_for_status()
    return [dict(url=x["url"], thumb=x.get("thumbnail") or x["url"], width=x.get("width"), height=x.get("height"),
                 title=x.get("title") or "", author=x.get("creator") or "",
                 license=f"CC {(x.get('license') or '').upper()} {x.get('license_version') or ''}".strip(),
                 page=x.get("foreign_landing_url"), source="openverse") for x in r.json().get("results", [])]


def wikipedia(query, n, keys):
    """Lead images of Wikipedia articles matching the query (great for history / places / species)."""
    r = requests.get("https://en.wikipedia.org/w/api.php", headers=UA, timeout=30, params=dict(
        action="query", generator="search", gsrsearch=query, gsrlimit=n, prop="pageimages|info",
        piprop="original|thumbnail", pithumbsize=500, inprop="url", format="json"))
    r.raise_for_status()
    pages = sorted(r.json().get("query", {}).get("pages", {}).values(), key=lambda p: p.get("index", 0))
    out = []
    for p in pages:
        o = p.get("original")
        if not o or o["source"].lower().endswith(".svg"):
            continue
        out.append(dict(url=o["source"], thumb=p.get("thumbnail", {}).get("source", o["source"]),
                        width=o.get("width"), height=o.get("height"), title=p["title"],
                        author="Wikipedia / Wikimedia Commons", license="see page (CC/PD)",
                        page=p.get("fullurl"), source="wikipedia"))
    return out


def nasa(query, n, keys):
    r = requests.get("https://images-api.nasa.gov/search", headers=UA, timeout=30,
                     params=dict(q=query, media_type="image", page_size=n))
    r.raise_for_status()
    out = []
    for it in r.json()["collection"]["items"][:n]:
        d, links = it["data"][0], it.get("links", [])
        thumb = next((l["href"] for l in links if l.get("rel") == "preview"), links[0]["href"] if links else None)
        if not thumb:
            continue
        big = thumb.replace("~thumb", "~large").replace("~small", "~large").replace("~medium", "~large")
        out.append(dict(url=big, thumb=thumb, width=None, height=None, title=d.get("title", ""),
                        author=d.get("photographer") or d.get("center") or "NASA", license="NASA (public domain)",
                        page=f"https://images.nasa.gov/details/{d.get('nasa_id')}", source="nasa"))
    return out


# ----------------------------------------------------------------- free with API key
def google_cse(query, n, keys):
    """Official Google Custom Search JSON API, image mode, Creative-Commons rights only."""
    out = []
    for start in range(1, min(n, 20) + 1, 10):  # API max 10 per call
        r = requests.get("https://www.googleapis.com/customsearch/v1", timeout=30, params=dict(
            key=keys["GOOGLE_CSE_KEY"], cx=keys.get("GOOGLE_CSE_CX", ""), q=query, searchType="image",
            imgSize="large", num=10, start=start,
            rights="cc_publicdomain|cc_attribute|cc_sharealike|cc_noncommercial|cc_nonderived"))
        r.raise_for_status()
        for x in r.json().get("items", []):
            im = x.get("image", {})
            out.append(dict(url=x["link"], thumb=im.get("thumbnailLink") or x["link"], width=im.get("width"),
                            height=im.get("height"), title=x.get("title", ""), author=x.get("displayLink", ""),
                            license="CC (Google rights filter) - verify", page=im.get("contextLink"),
                            source="google"))
        if len(r.json().get("items", [])) < 10:
            break
    return out[:n]


def flickr(query, n, keys):
    """Creative-Commons-licensed Flickr photos only (license ids 1-6, 9, 10)."""
    r = requests.get("https://api.flickr.com/services/rest/", timeout=30, params=dict(
        method="flickr.photos.search", api_key=keys["FLICKR_API_KEY"], text=query, per_page=n, sort="relevance",
        license="1,2,3,4,5,6,9,10", content_type=1, media="photos", format="json", nojsoncallback=1,
        extras="url_l,url_k,url_n,o_dims,owner_name,license"))
    r.raise_for_status()
    names = {"1": "CC BY-NC-SA", "2": "CC BY-NC", "3": "CC BY-NC-ND", "4": "CC BY", "5": "CC BY-SA",
             "6": "CC BY-ND", "9": "CC0", "10": "Public Domain"}
    out = []
    for x in r.json().get("photos", {}).get("photo", []):
        url = x.get("url_k") or x.get("url_l")
        if not url:
            continue
        w = int(x.get("width_k") or x.get("width_l") or 0) or None
        h = int(x.get("height_k") or x.get("height_l") or 0) or None
        out.append(dict(url=url, thumb=x.get("url_n") or url, width=w, height=h, title=x.get("title", ""),
                        author=x.get("ownername", ""), license=names.get(str(x.get("license")), "CC"),
                        page=f"https://www.flickr.com/photos/{x['owner']}/{x['id']}", source="flickr"))
    return out



def pexels(query, n, keys):
    r = requests.get("https://api.pexels.com/v1/search", timeout=30, headers={"Authorization": keys["PEXELS_API_KEY"]},
                     params=dict(query=query, per_page=n, orientation="landscape"))
    r.raise_for_status()
    return [dict(url=x["src"]["large2x"], thumb=x["src"]["medium"], width=x["width"], height=x["height"],
                 title=x.get("alt") or "", author=x["photographer"], license="Pexels License",
                 page=x["url"], source="pexels") for x in r.json().get("photos", [])]


def pixabay(query, n, keys):
    r = requests.get("https://pixabay.com/api/", timeout=30, params=dict(
        key=keys["PIXABAY_API_KEY"], q=query, per_page=max(3, n), orientation="horizontal", image_type="all"))
    r.raise_for_status()
    return [dict(url=x["largeImageURL"], thumb=x["webformatURL"], width=x["imageWidth"], height=x["imageHeight"],
                 title=x.get("tags", ""), author=x["user"], license="Pixabay License",
                 page=x["pageURL"], source="pixabay") for x in r.json().get("hits", [])]


def unsplash(query, n, keys):
    r = requests.get("https://api.unsplash.com/search/photos", timeout=30, params=dict(
        query=query, per_page=n, orientation="landscape", client_id=keys["UNSPLASH_ACCESS_KEY"]))
    r.raise_for_status()
    return [dict(url=x["urls"]["regular"].split("&w=")[0] + "&w=2400", thumb=x["urls"]["small"],
                 width=x["width"], height=x["height"], title=x.get("alt_description") or "",
                 author=x["user"]["name"], license="Unsplash License", page=x["links"]["html"],
                 source="unsplash") for x in r.json().get("results", [])]


# ----------------------------------------------------------------- AI generation
def openai_image(prompt, keys):
    r = requests.post("https://api.openai.com/v1/images/generations", timeout=300,
                      headers={"Authorization": f"Bearer {keys['OPENAI_API_KEY']}"},
                      json={"model": keys.get("OPENAI_IMAGE_MODEL") or "gpt-image-1",
                            "prompt": prompt, "size": "1536x1024", "n": 1})
    r.raise_for_status()
    d = r.json()["data"][0]
    return base64.b64decode(d["b64_json"]) if d.get("b64_json") else requests.get(d["url"], timeout=120).content


def gemini_image(prompt, keys):
    model = keys.get("GEMINI_IMAGE_MODEL") or "gemini-2.5-flash-image"
    r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                      params={"key": keys["GEMINI_API_KEY"]}, timeout=300,
                      json={"contents": [{"parts": [{"text": prompt + " Wide 16:9 landscape composition."}]}],
                            "generationConfig": {"responseModalities": ["IMAGE"],
                                                 "imageConfig": {"aspectRatio": "16:9"}}})
    r.raise_for_status()
    for part in r.json()["candidates"][0]["content"]["parts"]:
        if "inlineData" in part:
            return base64.b64decode(part["inlineData"]["data"])
    raise RuntimeError("Gemini returned no image")


def _web(site):
    def fn(prompt, keys):
        import browser_ai
        import vidgen
        return browser_ai.generate(site, prompt, log=vidgen.log, **browser_ai.cfg(keys))
    return fn


SOURCES = {
    "wikimedia": dict(label="Wikimedia Commons", kind="search", fn=wikimedia, key=None),
    "openverse": dict(label="Openverse (Flickr, CC)", kind="search", fn=openverse, key=None),
    "wikipedia": dict(label="Wikipedia (ảnh bài viết)", kind="search", fn=wikipedia, key=None),
    "nasa":      dict(label="NASA Image Library", kind="search", fn=nasa, key=None),
    "google":    dict(label="Google Images (API, chỉ ảnh CC)", kind="search", fn=google_cse, key="GOOGLE_CSE_KEY"),
    "flickr":    dict(label="Flickr (chỉ ảnh CC)", kind="search", fn=flickr, key="FLICKR_API_KEY"),
    "pexels":    dict(label="Pexels", kind="search", fn=pexels, key="PEXELS_API_KEY"),
    "pixabay":   dict(label="Pixabay", kind="search", fn=pixabay, key="PIXABAY_API_KEY"),
    "unsplash":  dict(label="Unsplash", kind="search", fn=unsplash, key="UNSPLASH_ACCESS_KEY"),
    "web_gemini":  dict(label="Gemini web (tài khoản của bạn)", kind="ai", fn=_web("gemini"), key=None, web=True),
    "web_chatgpt": dict(label="ChatGPT web (tài khoản của bạn)", kind="ai", fn=_web("chatgpt"), key=None, web=True),
    "web_grok":    dict(label="Grok web (tài khoản của bạn)", kind="ai", fn=_web("grok"), key=None, web=True),
    "openai":    dict(label="OpenAI API", kind="ai", fn=openai_image, key="OPENAI_API_KEY"),
    "gemini":    dict(label="Gemini API", kind="ai", fn=gemini_image, key="GEMINI_API_KEY"),
}
KEY_NAMES = ["GOOGLE_CSE_KEY", "GOOGLE_CSE_CX", "FLICKR_API_KEY", "PEXELS_API_KEY", "PIXABAY_API_KEY", "UNSPLASH_ACCESS_KEY", "OPENAI_API_KEY", "OPENAI_IMAGE_MODEL",
             "GEMINI_API_KEY", "GEMINI_IMAGE_MODEL", "BROWSER_CDP_PORT", "BROWSER_PATH",
             "BROWSER_USER_DATA_DIR", "BROWSER_PROFILE"]


def available(keys):
    return {k: dict(label=v["label"], kind=v["kind"], web=v.get("web", False),
                    enabled=not v["key"] or bool(keys.get(v["key"])), key=v["key"])
            for k, v in SOURCES.items()}


def search(source, query, n, keys):
    return SOURCES[source]["fn"](query, n, keys)


def generate(source, prompt, keys):
    return SOURCES[source]["fn"](prompt, keys)


STOP = set("""a an the of on in at to with and or for from by into over under near its it is are was were be this that
these those very small large single some up down out as like than then while beside next his her their our your
soft warm dramatic cinematic style mood light lighting close-up closeup concept background detail scene""".split())


def auto_query(prompt, max_words=5):
    """Short stock-photo query from a long descriptive prompt."""
    first = re.split(r"[,.:;]", prompt)[0]
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z'-]+", first) if w.lower() not in STOP]
    return " ".join(words[:max_words]) or prompt[:40]
