"""aHi static news collector. Run only in GitHub Actions, not Blogger.
Only short source-provided excerpts are stored. No fabricated summaries.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse, urljoin
import hashlib, io, json, re, time
import requests
import feedparser
from PIL import Image, ImageOps

VN = timezone(timedelta(hours=7))
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "news"
FEEDS = [
    ("VnExpress", "https://vnexpress.net/rss/so-hoa.rss"),
    ("Tuổi Trẻ", "https://tuoitre.vn/rss/nhip-song-so.rss"),
    ("Thanh Niên", "https://thanhnien.vn/rss/cong-nghe.rss"),
    ("VietNamNet", "https://vietnamnet.vn/rss/cong-nghe.rss"),
    ("GenK", "https://genk.vn/rss/home.rss"),
]
NEWS_HOSTS = ("vnexpress.net", "tuoitre.vn", "thanhnien.vn", "vietnamnet.vn", "genk.vn")
IMAGE_HOSTS = ("vnecdn.net", "mediacdn.vn", "vnncdn.net", "vietnamnet.vn", "vnexpress.net")
PATTERNS = {
    "web": r"website|trang web|\bseo\b|blogger|wordpress|trình duyệt|chrome|firefox|tên miền|thiết kế web|google search",
    "ai": r"\bai\b|trí tuệ nhân tạo|chatgpt|gemini|openai|claude|copilot|chatbot|prompt|deepseek|mô hình ngôn ngữ",
    "code": r"lập trình|mã nguồn|javascript|python|github|tự động hóa|power automate|\bsql\b|\bvba\b|phần mềm|công cụ|tiện ích",
    "view": r"chuyển đổi số|cuộc sống số|xu hướng công nghệ|kinh tế số|việc làm|tương lai công nghệ|kỹ năng số|quyền riêng tư",
}
HEADERS = {"User-Agent": "aHiNews/1.0 (+https://www.ahi.id.vn)", "Accept": "*/*"}

class Markup(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text, self.images, self.meta = [], [], {}
        self.skip = 0
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("script", "style"): self.skip += 1
        if tag == "img" and a.get("src"): self.images.append(a["src"])
        if tag == "meta": self.meta[a.get("property", a.get("name", ""))] = a.get("content", "")
    def handle_endtag(self, tag):
        if tag in ("script", "style"): self.skip = max(0, self.skip-1)
    def handle_data(self, data):
        if not self.skip: self.text.append(data)

def markup(s):
    p = Markup(); p.feed(unescape(str(s or ""))); return p

def plain(s):
    return re.sub(r"\s+", " ", " ".join(markup(s).text)).strip()

def excerpt(s):
    s = plain(s)
    if len(s) <= 280: return s
    return s[:277].rsplit(" ", 1)[0] + "…"

def allowed(url, hosts):
    try:
        u = urlparse(url)
        return u.scheme == "https" and not u.username and not u.password and u.port in (None,443) and any(u.hostname == h or u.hostname.endswith("."+h) for h in hosts)
    except (ValueError, AttributeError): return False

def get(url, hosts, limit=2_000_000):
    # Revalidate every redirect; never act as an arbitrary URL proxy.
    for _ in range(4):
        if not allowed(url, hosts): raise ValueError("Host not allowed")
        with requests.get(url, headers=HEADERS, timeout=(5,12), allow_redirects=False, stream=True) as r:
            if r.status_code in (301,302,303,307,308):
                url = urljoin(url, r.headers["Location"]); continue
            r.raise_for_status()
            chunks, n = [], 0
            for chunk in r.iter_content(32768):
                n += len(chunk)
                if n > limit: raise ValueError("Response too large")
                chunks.append(chunk)
            return b"".join(chunks)
    raise ValueError("Too many redirects")

def rank(title, summary):
    scores = {k: len(re.findall(p, title.lower()))*3 + len(re.findall(p, summary.lower())) for k,p in PATTERNS.items()}
    topic = max(scores, key=scores.get)
    return (topic if scores[topic] >= 3 else "", min(sum(scores.values()),15)*2)

def read_feed(pair):
    name, url = pair
    try:
        parsed = feedparser.parse(get(url, NEWS_HOSTS))
        if not parsed.entries: raise ValueError("No RSS entries")
        items = []
        for e in parsed.entries[:80]:
            stamp = e.get("published_parsed") or e.get("updated_parsed")
            if not stamp: continue
            import calendar
            at = calendar.timegm(stamp)*1000
            title, link = plain(e.get("title", "")), e.get("link", "")
            if not title or not allowed(link, NEWS_HOSTS): continue
            desc = e.get("summary", "")
            summary = excerpt(desc)
            if summary.lower().startswith(title.lower()[:60]): summary = ""
            p = markup(desc)
            image = p.images[0] if p.images else ""
            topic, score = rank(title, summary)
            if topic: items.append(dict(title=title,url=link,at=at,topic=topic,score=score,summary=summary,image=image,source=name))
        print(f"OK {name}: {len(items)} relevant entries")
        return True, items
    except Exception as e:
        print(f"WARNING {name}: {type(e).__name__}: {e}")
        return False, []

def enrich(x):
    if len(x["summary"]) >= 45: return x
    try:
        p = markup(get(x["url"], NEWS_HOSTS, 1_500_000).decode("utf-8", "replace"))
        x["summary"] = excerpt(p.meta.get("og:description") or p.meta.get("description", ""))
        x["image"] = x.get("image") or p.meta.get("og:image", "")
    except Exception: pass
    return x

def thumbnail(x):
    src = x.get("image", "")
    if src.startswith("thumbs/"):
        return src if (OUT/src).is_file() else ""
    if not allowed(src, IMAGE_HOSTS): return ""
    name = hashlib.sha256(src.encode()).hexdigest()[:24]+".webp"
    dest = OUT/"thumbs"/name
    if dest.exists(): return "thumbs/"+name
    try:
        raw = get(src, IMAGE_HOSTS, 3_000_000)
        with Image.open(io.BytesIO(raw)) as im:
            if im.width*im.height > 24_000_000: return ""
            im = ImageOps.fit(im.convert("RGB"),(240,150),method=Image.Resampling.LANCZOS)
            im.save(dest,"WEBP",quality=78,method=4)
        return "thumbs/"+name
    except Exception: return ""

def write_json(path, value):
    text = json.dumps(value, ensure_ascii=False, indent=2)+"\n"
    if not path.exists() or path.read_text(encoding="utf-8") != text:
        temp = path.with_suffix(".tmp");temp.write_text(text,encoding="utf-8");temp.replace(path)

def run():
    OUT.mkdir(exist_ok=True);(OUT/"thumbs").mkdir(exist_ok=True)
    now = datetime.now(VN); low = now.date()-timedelta(days=7)
    results = list(ThreadPoolExecutor(max_workers=3).map(read_feed,FEEDS))
    if not any(ok for ok,_ in results):
        raise SystemExit("All sources failed. Existing JSON is untouched; inspect Actions logs.")
    pool = [x for _,items in results for x in items]
    for f in OUT.glob("????-??-??.json"):
        try: pool += json.loads(f.read_text(encoding="utf-8")).get("items",[])
        except (ValueError,OSError): pass
    days = {}
    for x in pool:
        dt = datetime.fromtimestamp(x["at"]/1000,VN)
        if low <= dt.date() <= now.date() and x["at"] <= now.timestamp()*1000+300000:
            days.setdefault(str(dt.date()),[]).append(x)
    budget = 12
    for day,items in sorted(days.items(),reverse=True):
        seen, unique = set(), []
        for x in sorted(items,key=lambda x:bool(x.get("summary")),reverse=True):
            k = re.sub(r"\W+","",x["title"].lower())
            if k not in seen: seen.add(k);unique.append(x)
        for x in sorted(unique,key=lambda x:x.get("score",0),reverse=True):
            if budget and len(x.get("summary",""))<45: enrich(x);budget-=1
        candidates = [x for x in unique if len(x.get("summary",""))>=45]
        candidates.sort(key=lambda x:(x.get("score",0)+(4 if x.get("summary") else 0),x["at"]),reverse=True)
        chosen = candidates[:5]
        if not chosen: continue
        for x in chosen:
            x["image"] = thumbnail(x)
            x.pop("sum",None)
        write_json(OUT/(day+".json"),{"date":day,"updatedAt":now.isoformat(),"items":chosen})
    files = []
    for f in sorted(OUT.glob("????-??-??.json"),reverse=True):
        if f.stem < str(low): f.unlink()
        else: files.append(f)
    used = set()
    for f in files:
        j = json.loads(f.read_text(encoding="utf-8"))
        used.update(x.get("image","") for x in j.get("items",[]))
    for f in (OUT/"thumbs").glob("*.webp"):
        if "thumbs/"+f.name not in used: f.unlink()
    write_json(OUT/"index.json",{"updatedAt":now.isoformat(),"timezone":"Asia/Ho_Chi_Minh","dates":[f.stem for f in files]})
    print(f"Published {len(files)} daily files. Historical dates accumulate from available RSS and previous runs.")

if __name__ == "__main__": run()
