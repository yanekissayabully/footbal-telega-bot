import html
import io
import logging
import os
import json
import re
import sys
import time
from calendar import timegm
from pathlib import Path

import feedparser
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from PIL import Image

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHANNEL_ID = os.environ["CHANNEL_ID"]
POLL_MINUTES = float(os.getenv("POLL_MINUTES", "10"))
MAX_POSTS_PER_RUN = int(os.getenv("MAX_POSTS_PER_RUN", "3"))
MAX_AGE_HOURS = float(os.getenv("MAX_AGE_HOURS", "6"))
FOOTER = os.getenv("FOOTER", "").strip()
POST_WITHOUT_IMAGE = os.getenv("POST_WITHOUT_IMAGE", "0") == "1"

STATE_PATH = Path(__file__).with_name("seen.json")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"
CAPTION_LIMIT = 1024

# (url, нужен ли фильтр по футбольным словам — для лент про все виды спорта)
FEEDS = [
    ("https://ge.globo.com/rss/ge/futebol/", False),
    ("https://trivela.com.br/feed/", False),
    ("https://www.gazetaesportiva.com/feed/", True),
    ("https://www.torcedores.com/feed", True),
    ("https://www.placar.com.br/feed/", True),
]

FOOTBALL_RE = re.compile(
    r"futebol|brasileir[aã]o|libertadores|sul-americana|copa do brasil|sele[cç][aã]o|"
    r"flamengo|palmeiras|corinthians|s[aã]o paulo|santos|fluminense|vasco|botafogo|"
    r"gr[eê]mio|internacional|cruzeiro|atl[eé]tico|bahia|fortaleza|athletico|"
    r"champions|premier league|la liga|neymar|vin[ií]cius|gol\b|t[eé]cnico|rodada",
    re.I,
)

log = logging.getLogger("bot")
session = requests.Session()
session.headers["User-Agent"] = UA


class Seen:
    """Список уже обработанных новостей в JSON (хранится в репозитории)."""

    def __init__(self, path: Path):
        self.path = path
        try:
            self.data: dict[str, int] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {}

    def __contains__(self, uid: str) -> bool:
        return uid in self.data

    def __len__(self) -> int:
        return len(self.data)

    def add(self, uid: str) -> None:
        self.data.setdefault(uid, int(time.time()))
        self.save()

    def prune(self, days: int = 14) -> None:
        cutoff = time.time() - days * 86400
        self.data = {k: v for k, v in self.data.items() if v >= cutoff}
        self.save()

    def save(self) -> None:
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=0), encoding="utf-8")


def clean(text: str) -> str:
    text = re.sub(r"\s+", " ", BeautifulSoup(text or "", "html.parser").get_text(" ")).strip()
    # хвост WordPress: "O post ... apareceu primeiro em Site ."
    return re.sub(r"\s*O post .*? apareceu primeiro em .*$", "", text).strip()


def to_jpeg(data: bytes) -> bytes:
    im = Image.open(io.BytesIO(data)).convert("RGB")
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=90)
    return buf.getvalue()


def entry_image(entry) -> str | None:
    for key in ("media_content", "media_thumbnail"):
        for m in entry.get(key, []) or []:
            if m.get("url"):
                return m["url"]
    for enc in entry.get("enclosures", []) or []:
        if enc.get("type", "").startswith("image") and enc.get("href"):
            return enc["href"]
    parts = [entry.get("summary", "")] + [c.get("value", "") for c in entry.get("content", [])]
    for part in parts:
        img = BeautifulSoup(part, "html.parser").find("img")
        if img and img.get("src", "").startswith("http"):
            return img["src"]
    return None


def page_meta(url: str) -> tuple[str | None, str | None]:
    """og:image и og:description со страницы статьи."""
    try:
        r = session.get(url, timeout=15)
        r.raise_for_status()
    except requests.RequestException as e:
        log.warning("не открылась страница %s: %s", url, e)
        return None, None
    soup = BeautifulSoup(r.text, "html.parser")
    img = soup.find("meta", property="og:image")
    desc = soup.find("meta", property="og:description")
    return (img.get("content") if img else None), (desc.get("content") if desc else None)


def published_ts(entry) -> float:
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    return timegm(t) if t else time.time()


def fetch_candidates(seen: Seen) -> list[dict]:
    out = []
    cutoff = time.time() - MAX_AGE_HOURS * 3600
    for url, need_filter in FEEDS:
        try:
            r = session.get(url, timeout=20)
            r.raise_for_status()
            feed = feedparser.parse(r.content)
        except requests.RequestException as e:
            log.warning("лента %s недоступна: %s", url, e)
            continue
        for e in feed.entries:
            link = e.get("link")
            if not link:
                continue
            uid = e.get("id") or link
            if uid in seen:
                continue
            ts = published_ts(e)
            title = clean(e.get("title", ""))
            summary = clean(e.get("summary", ""))
            cats = " ".join(t.get("term", "") for t in e.get("tags", []) or [])
            if need_filter and not FOOTBALL_RE.search(f"{title} {summary} {cats}"):
                continue
            out.append({"uid": uid, "link": link, "title": title, "summary": summary,
                        "ts": ts, "fresh": ts >= cutoff, "image": entry_image(e)})
    return out


def build_caption(title: str, summary: str) -> str:
    tail = f"\n\n{FOOTER}" if FOOTER else ""
    head = f"<b>{html.escape(title)}</b>"
    room = CAPTION_LIMIT - len(head) - len(tail) - 4
    body = ""
    if summary and summary != title and room > 40:
        s = summary if len(summary) <= room else summary[: room - 1].rsplit(" ", 1)[0] + "…"
        body = "\n\n" + html.escape(s)
    return head + body + tail


def send(item: dict) -> bool:
    caption = build_caption(item["title"], item["summary"])
    api = f"https://api.telegram.org/bot{BOT_TOKEN}"
    if item["image"]:
        r = session.post(f"{api}/sendPhoto", timeout=30, data={
            "chat_id": CHANNEL_ID, "photo": item["image"], "caption": caption, "parse_mode": "HTML"})
        if r.ok:
            return True
        log.warning("sendPhoto по ссылке не прошёл (%s), качаю файл", r.text[:200])
        try:
            img = session.get(item["image"], timeout=20)
            img.raise_for_status()
            r = session.post(f"{api}/sendPhoto", timeout=60,
                             data={"chat_id": CHANNEL_ID, "caption": caption, "parse_mode": "HTML"},
                             files={"photo": ("image.jpg", to_jpeg(img.content))})
            if r.ok:
                return True
            log.warning("загрузка файлом не прошла: %s", r.text[:200])
        except (requests.RequestException, OSError) as e:
            log.warning("не скачалась/не сконвертировалась картинка: %s", e)
    if POST_WITHOUT_IMAGE:
        r = session.post(f"{api}/sendMessage", timeout=30, data={
            "chat_id": CHANNEL_ID, "text": caption, "parse_mode": "HTML"})
        if r.ok:
            return True
        log.error("sendMessage: %s", r.text[:300])
    return False


def run_once(seen: Seen) -> None:
    first_run = len(seen) == 0
    cands = fetch_candidates(seen)

    if first_run:
        # не заливаем канал старой лентой: всё, кроме самых свежих, помечаем как виденное
        cands.sort(key=lambda c: c["ts"], reverse=True)
        for c in cands[MAX_POSTS_PER_RUN:]:
            seen.add(c["uid"])
        cands = cands[:MAX_POSTS_PER_RUN]

    for c in cands:
        if not c["fresh"]:
            seen.add(c["uid"])
    fresh = sorted((c for c in cands if c["fresh"]), key=lambda c: c["ts"])

    posted = 0
    for c in fresh:
        if posted >= MAX_POSTS_PER_RUN:
            break
        if not c["image"] or not c["summary"]:
            img, desc = page_meta(c["link"])
            c["image"] = c["image"] or img
            c["summary"] = c["summary"] or clean(desc or "")
        if not c["image"] and not POST_WITHOUT_IMAGE:
            log.info("нет картинки, пропускаю: %s", c["title"])
            seen.add(c["uid"])
            continue
        if send(c):
            log.info("опубликовано: %s", c["title"])
            posted += 1
            seen.add(c["uid"])
            time.sleep(3)
        else:
            log.error("не удалось отправить, повторю в следующий раз: %s", c["title"])

    seen.prune()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    seen = Seen(STATE_PATH)
    once = "--once" in sys.argv
    while True:
        try:
            run_once(seen)
        except Exception:
            log.exception("ошибка в цикле")
            if once:
                sys.exit(1)
        if once:
            return
        time.sleep(POLL_MINUTES * 60)


if __name__ == "__main__":
    main()
