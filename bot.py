import html
import io
import json
import logging
import os
import re
import sys
import time
from calendar import timegm
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from PIL import Image

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHANNEL_ID = os.environ["CHANNEL_ID"]
MAX_POSTS_PER_RUN = int(os.getenv("MAX_POSTS_PER_RUN", "4"))
MAX_AGE_HOURS = float(os.getenv("MAX_AGE_HOURS", "16"))
SLOT_OVERRIDE = os.getenv("SLOT", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "").strip() or "gpt-4o-mini"
FOOTER = os.getenv("FOOTER", "").strip()
POST_WITHOUT_IMAGE = os.getenv("POST_WITHOUT_IMAGE", "0") == "1"

STATE_PATH = Path(__file__).with_name("seen.json")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"
CAPTION_LIMIT = 1024

# (url, нужен ли фильтр по футбольным словам, категория по умолчанию)
# br — бразильские турниры, eur — Европа (топ-5 + ЛЧ), latam — Латинская Америка
FEEDS = [
    ("https://ge.globo.com/rss/ge/futebol/brasileirao-serie-a/", False, "br"),
    ("https://ge.globo.com/rss/ge/futebol/brasileirao-serie-b/", False, "br"),
    ("https://ge.globo.com/rss/ge/futebol/copa-do-brasil/", False, "br"),
    ("https://ge.globo.com/rss/ge/futebol/selecao-brasileira/", False, "br"),
    ("https://ge.globo.com/rss/ge/futebol/futebol-internacional/", False, "eur"),
    ("https://ge.globo.com/rss/ge/futebol/libertadores/", False, "latam"),
    ("https://ge.globo.com/rss/ge/futebol/copa-sul-americana/", False, "latam"),
    ("https://ge.globo.com/rss/ge/futebol/mercado-da-bola/", False, "mercado"),
    ("https://trivela.com.br/feed/", False, "eur"),
    ("https://www.metropoles.com/esportes/futebol/feed", False, "br"),
    ("https://www.metropoles.com/celebridades/feed", True, "fofoca"),
    ("https://www.gazetaesportiva.com/feed/", True, "br"),
    ("https://www.torcedores.com/feed", True, "br"),
]

FOOTBALL_RE = re.compile(
    r"futebol|brasileir[aã]o|libertadores|sul-americana|copa do brasil|sele[cç][aã]o|"
    r"flamengo|palmeiras|corinthians|s[aã]o paulo|santos|fluminense|vasco|botafogo|"
    r"gr[eê]mio|internacional|cruzeiro|atl[eé]tico|bahia|fortaleza|athletico|"
    r"champions|premier league|la liga|neymar|vin[ií]cius|gol\b|t[eé]cnico|rodada|"
    r"jogador|atleta|craque|boleiro|zagueiro|atacante|goleiro|meia\b|lateral",
    re.I,
)

# порядок проверки важен: сначала «острые» темы, потом лиги
CATEGORY_RES = [
    ("escandalo", re.compile(
        r"pol[eê]mic|esc[aâ]ndalo|\bbriga\b|brigam|confus[aã]o|pancadaria|agress|racis|inj[uú]ria|homofob|"
        r"den[uú]ncia|acusad|investiga|pol[ií]cia|preso\b|doping|manipula[cç][aã]o|"
        r"puni[cç][aã]o|stjd|expuls|revolta|protesto|tumulto|insatisf", re.I)),
    ("fofoca", re.compile(
        r"namor|casament|casad[oa]|separa[cç][aã]o|div[oó]rcio|trai[cç][aã]o|affair|romance|"
        r"beijo|balada|esposa|mulher d[eo]|marido|bastidores|vida pessoal|influenciador|"
        r"barraco|fofoca|viraliza|casal|\bex-(mulher|namorada)", re.I)),
    ("mercado", re.compile(
        r"transfer|contrata[cç][aã]o|contratar|refor[cç]o|negocia|proposta|mercado da bola|"
        r"empr[eé]stimo|renova|rescis|acerta com|fecha com|sondag|especula|assina com|"
        r"cl[aá]usula|janela", re.I)),
    ("eur", re.compile(
        r"premier league|campeonato ingl|la ?liga|campeonato espanhol|campeonato italiano|"
        r"s[eé]rie a da it[aá]lia|bundesliga|campeonato alem|ligue 1|campeonato franc|"
        r"liga dos campe|champions|real madrid|barcelona|manchester|liverpool|arsenal|chelsea|"
        r"tottenham|bayern|dortmund|\bpsg\b|juventus|\bmilan\b|napoli|mbapp|haaland|bellingham", re.I)),
    ("latam", re.compile(
        r"libertadores|sul-americana|conmebol|argentin|boca juniors|river plate|col[oô]mbia|"
        r"uruguai|chile\b|paraguai|equador|bol[ií]via|venezuela|m[eé]xico|liga mx|"
        r"eliminat[oó]rias|copa am[eé]rica|pe[nñ]arol|atl[eé]tico nacional", re.I)),
    ("br", re.compile(
        r"brasileir[aã]o|s[eé]rie [ab]\b|copa do brasil|paulist[aã]o|carioca|ga[uú]cho|mineiro|"
        r"sele[cç][aã]o brasileira|flamengo|palmeiras|corinthians|s[aã]o paulo|santos|"
        r"fluminense|vasco|botafogo|gr[eê]mio|cruzeiro|bahia|fortaleza|athletico", re.I)),
]

# что постим в каждое окно (время по Бразилии); если категории пусты — добираем любыми
SLOT_PLANS = {
    "morning": ["eur", "br", "mercado", "latam"],
    "lunch": ["br", "mercado", "escandalo", "latam"],
    "evening": ["br", "eur", "fofoca", "mercado"],
}

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
    im.thumbnail((2560, 2560))  # Telegram отклоняет слишком большие картинки
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


def classify(text: str, default: str) -> str:
    for name, rx in CATEGORY_RES:
        if rx.search(text):
            return name
    return default


def current_slot() -> str:
    if SLOT_OVERRIDE in SLOT_PLANS:
        return SLOT_OVERRIDE
    # Бразилия: UTC-3, летнее время отменено
    hour = (datetime.now(timezone.utc) + timedelta(hours=-3)).hour
    return "morning" if hour < 12 else "lunch" if hour < 17 else "evening"


def published_ts(entry) -> float:
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    return timegm(t) if t else time.time()


def fetch_candidates(seen: Seen) -> list[dict]:
    out = []
    seen_now: set[str] = set()
    cutoff = time.time() - MAX_AGE_HOURS * 3600
    for url, need_filter, default_cat in FEEDS:
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
            if uid in seen or uid in seen_now:
                continue
            ts = published_ts(e)
            title = clean(e.get("title", ""))
            summary = clean(e.get("summary", ""))
            cats = " ".join(t.get("term", "") for t in e.get("tags", []) or [])
            text = f"{title} {cats}"
            if need_filter and not FOOTBALL_RE.search(f"{text} {summary}"):
                continue
            seen_now.add(uid)
            out.append({"uid": uid, "link": link, "title": title, "summary": summary,
                        "ts": ts, "fresh": ts >= cutoff, "image": entry_image(e),
                        "cat": classify(text, default_cat)})
    return out


RUBRICS = {
    "br": ("🇧🇷", "FUTEBOL BRASILEIRO", "#Brasileirão #FutebolBrasileiro"),
    "eur": ("🌍", "EUROPA & CHAMPIONS", "#Champions #FutebolEuropeu"),
    "latam": ("🌎", "AMÉRICA LATINA", "#Libertadores #SulAmericana"),
    "mercado": ("🔁", "MERCADO DA BOLA", "#MercadoDaBola #Transferências"),
    "escandalo": ("🚨", "POLÊMICA", "#Polêmica #Futebol"),
    "fofoca": ("🔥", "BASTIDORES", "#Bastidores #Futebol"),
}

SYSTEM_PROMPT = (
    "Você é redator de um canal de Telegram de futebol para o público brasileiro. "
    "Reescreva a notícia recebida em português do Brasil, com texto próprio e envolvente, "
    "tom de torcedor bem informado, sem copiar frases do original. "
    "Regras: use SOMENTE fatos presentes no material, não invente nada (placares, nomes, valores); "
    "em temas de polêmica ou vida pessoal, deixe claro quando algo é alegação ou 'segundo a imprensa' "
    "e nunca afirme como fato o que não está confirmado; "
    "não cite o veículo de origem, não inclua links nem hashtags; "
    "no máximo 1 ou 2 emojis relevantes no corpo. "
    'Responda em JSON: {"title": "manchete curta e chamativa, até 90 caracteres", '
    '"body": "2 a 4 frases curtas, até 450 caracteres"}'
)


def ai_rewrite(item: dict) -> bool:
    """Переписывает заголовок и текст через OpenAI. False — если не вышло."""
    material = f"Categoria: {item['cat']}\nTítulo: {item['title']}\nResumo: {item['summary']}"
    try:
        r = session.post(
            "https://api.openai.com/v1/chat/completions", timeout=60,
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            json={"model": OPENAI_MODEL, "response_format": {"type": "json_object"},
                  "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                               {"role": "user", "content": material}]})
        r.raise_for_status()
        data = json.loads(r.json()["choices"][0]["message"]["content"])
        title, body = clean(data["title"]), clean(data["body"])
    except (requests.RequestException, KeyError, IndexError, ValueError) as e:
        log.error("OpenAI не ответил нормально: %s", e)
        return False
    if not title or not body:
        return False
    item["title"], item["summary"] = title[:120], body[:600]
    return True


def build_caption(item: dict) -> str:
    emoji, name, tags = RUBRICS.get(item["cat"], RUBRICS["br"])
    head = f"{emoji} <b>{name}</b>\n\n<b>{html.escape(item['title'])}</b>"
    tail = f"\n\n{tags}" + (f"\n\n{FOOTER}" if FOOTER else "")
    room = CAPTION_LIMIT - len(head) - len(tail) - 4
    body = ""
    summary = item["summary"]
    if summary and summary != item["title"] and room > 40:
        s = summary if len(summary) <= room else summary[: room - 1].rsplit(" ", 1)[0] + "…"
        body = "\n\n" + html.escape(s)
    return head + body + tail


def send(item: dict) -> bool:
    caption = build_caption(item)
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
    if item["image"] or POST_WITHOUT_IMAGE:  # фото не приняли — лучше текст, чем потерять пост
        r = session.post(f"{api}/sendMessage", timeout=30, data={
            "chat_id": CHANNEL_ID, "text": caption, "parse_mode": "HTML"})
        if r.ok:
            return True
        log.error("sendMessage: %s", r.text[:300])
    return False


def pick(fresh: list[dict], plan: list[str]) -> list[dict]:
    """По одной свежей новости на категорию из плана, остаток добираем любыми."""
    pool = sorted(fresh, key=lambda c: c["ts"], reverse=True)
    chosen: list[dict] = []
    for cat in plan:
        for c in pool:
            if c["cat"] == cat and c not in chosen:
                chosen.append(c)
                break
    for c in pool:
        if len(chosen) >= len(plan):
            break
        if c not in chosen:
            chosen.append(c)
    return chosen


def run_once(seen: Seen) -> None:
    slot = current_slot()
    plan = SLOT_PLANS[slot][:MAX_POSTS_PER_RUN]
    cands = fetch_candidates(seen)
    for c in cands:
        if not c["fresh"]:
            seen.add(c["uid"])
    fresh = [c for c in cands if c["fresh"]]
    log.info("окно %s, свежих кандидатов: %d", slot, len(fresh))

    posted = 0
    failed = 0
    while posted < len(plan) and fresh and failed < 3:
        for c in pick(fresh, plan[posted:]):
            fresh.remove(c)
            if not c["image"] or not c["summary"]:
                img, desc = page_meta(c["link"])
                c["image"] = c["image"] or img
                c["summary"] = c["summary"] or clean(desc or "")
            if not c["image"] and not POST_WITHOUT_IMAGE:
                log.info("нет картинки, пропускаю: %s", c["title"])
                seen.add(c["uid"])
                continue
            if OPENAI_API_KEY and not ai_rewrite(c):
                failed += 1
                continue
            if send(c):
                log.info("опубликовано [%s]: %s", c["cat"], c["title"])
                posted += 1
                seen.add(c["uid"])
                time.sleep(3)
            else:
                failed += 1
                log.error("не удалось отправить, повторю в следующий раз: %s", c["title"])

    seen.prune()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        run_once(Seen(STATE_PATH))
    except Exception:
        log.exception("ошибка")
        sys.exit(1)


if __name__ == "__main__":
    main()
