import html
import io
import json
import logging
import os
import random
import re
import sys
import time
import unicodedata
from calendar import timegm
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHANNEL_ID = os.environ["CHANNEL_ID"]
DAILY_LIMIT = int(os.getenv("DAILY_LIMIT", "5"))  # максимум постов в сутки (по Бразилии), прогревы входят
MAX_PREVIEWS_PER_RUN = int(os.getenv("MAX_PREVIEWS_PER_RUN", "1"))
MAX_AGE_HOURS = float(os.getenv("MAX_AGE_HOURS", "16"))
POST_DELAY_MIN = float(os.getenv("POST_DELAY_MIN", "15"))  # пауза между постами, минуты
POST_DELAY_MAX = float(os.getenv("POST_DELAY_MAX", "30"))
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "").strip() or "gpt-4o-mini"
FOOTER = os.getenv("FOOTER", "").strip()
POST_WITHOUT_IMAGE = os.getenv("POST_WITHOUT_IMAGE", "0") == "1"

STATE_PATH = Path(__file__).with_name("seen.json")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"
CAPTION_LIMIT = 1024
BRT = timezone(timedelta(hours=-3))  # Бразилия, летнее время отменено

# (url, фильтр по футбольным словам, категория по умолчанию, категория фиксирована)
# Иноязычные ленты (en/es) — категория фиксированная, текст всё равно переписывается на pt-BR.
FEEDS = [
    ("https://ge.globo.com/rss/ge/futebol/brasileirao-serie-a/", False, "br", False),
    ("https://ge.globo.com/rss/ge/futebol/brasileirao-serie-b/", False, "br", False),
    ("https://ge.globo.com/rss/ge/futebol/copa-do-brasil/", False, "br", False),
    ("https://ge.globo.com/rss/ge/futebol/selecao-brasileira/", False, "br", False),
    ("https://ge.globo.com/rss/ge/futebol/futebol-internacional/", False, "eur", False),
    ("https://ge.globo.com/rss/ge/futebol/libertadores/", False, "latam", False),
    ("https://ge.globo.com/rss/ge/futebol/copa-sul-americana/", False, "latam", False),
    ("https://ge.globo.com/rss/ge/futebol/mercado-da-bola/", False, "mercado", False),
    ("https://trivela.com.br/feed/", False, "eur", False),
    ("https://www.metropoles.com/esportes/futebol/feed", False, "br", False),
    ("https://www.metropoles.com/celebridades/feed", True, "fofoca", False),
    ("https://www.gazetaesportiva.com/feed/", True, "br", False),
    ("https://www.torcedores.com/feed", True, "br", False),
    ("https://www.theguardian.com/football/premierleague/rss", False, "epl", True),
    ("https://www.theguardian.com/football/laligafootball/rss", False, "laliga", True),
    ("https://www.theguardian.com/football/serieafootball/rss", False, "seriea", True),
    ("https://www.theguardian.com/football/bundesligafootball/rss", False, "bundesliga", True),
    ("https://www.theguardian.com/football/ligue1football/rss", False, "ligue1", True),
    ("https://www.theguardian.com/football/championsleague/rss", False, "ucl", True),
    ("https://e00-marca.uecdn.es/rss/futbol/primera-division.xml", False, "laliga", True),
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
    ("ucl", re.compile(r"liga dos campe|champions", re.I)),
    ("epl", re.compile(
        r"premier league|campeonato ingl|manchester city|manchester united|liverpool|arsenal|"
        r"chelsea|tottenham|newcastle|aston villa|haaland", re.I)),
    ("laliga", re.compile(
        r"la ?liga|campeonato espanhol|real madrid|barcelona|atl[eé]tico de madri|girona|"
        r"sevilla|lamine yamal|endrick", re.I)),
    ("seriea", re.compile(
        r"s[eé]rie a da it[aá]lia|campeonato italiano|juventus|inter de mil|\bmilan\b|napoli|"
        r"\blazio\b", re.I)),
    ("bundesliga", re.compile(r"bundesliga|campeonato alem|bayern|dortmund|leverkusen", re.I)),
    ("ligue1", re.compile(r"ligue 1|campeonato franc|\bpsg\b|paris saint|marseille", re.I)),
    ("eur", re.compile(r"mbapp|bellingham|premier|europa league|liga europa", re.I)),
    ("latam", re.compile(
        r"libertadores|sul-americana|conmebol|argentin|boca juniors|river plate|col[oô]mbia|"
        r"uruguai|chile\b|paraguai|equador|bol[ií]via|venezuela|m[eé]xico|liga mx|"
        r"eliminat[oó]rias|copa am[eé]rica|pe[nñ]arol|atl[eé]tico nacional", re.I)),
    ("br", re.compile(
        r"brasileir[aã]o|s[eé]rie [ab]\b|copa do brasil|paulist[aã]o|carioca|ga[uú]cho|mineiro|"
        r"sele[cç][aã]o brasileira|flamengo|palmeiras|corinthians|s[aã]o paulo|santos|"
        r"fluminense|vasco|botafogo|gr[eê]mio|cruzeiro|bahia|fortaleza|athletico", re.I)),
]

# «europa» в плане = любая из этих категорий (топ-5 лиг, ЛЧ и общая Европа)
GROUPS = {"europa": {"epl", "laliga", "seriea", "bundesliga", "ligue1", "ucl", "eur"}}

# Когда выходят посты (время по Бразилии): к каждому моменту должно быть выложено столько постов,
# сколько моментов уже наступило. Если запуск GitHub опоздал или пропущен — следующий догонит,
# но не больше одного поста за запуск, так что пачкой не сыпется.
DUE_TIMES = [(9, 0), (11, 0), (13, 0), (16, 0), (20, 0)]

# Очередь рубрик; каждый день сдвигается, так что рубрики чередуются
DAY_PLAN = ["europa", "br", "mercado", "europa", "latam", "br", "escandalo", "europa", "fofoca", "br"]

RUBRICS = {
    "br": ("🇧🇷", "FUTEBOL BRASILEIRO", "#Brasileirão #FutebolBrasileiro"),
    "eur": ("🌍", "FUTEBOL EUROPEU", "#FutebolEuropeu"),
    "epl": ("🦁", "PREMIER LEAGUE", "#PremierLeague #FutebolInglês"),
    "laliga": ("🇪🇸", "LA LIGA", "#LaLiga #FutebolEspanhol"),
    "seriea": ("🇮🇹", "SÉRIE A ITALIANA", "#SerieA #FutebolItaliano"),
    "bundesliga": ("🇩🇪", "BUNDESLIGA", "#Bundesliga #FutebolAlemão"),
    "ligue1": ("🇫🇷", "LIGUE 1", "#Ligue1 #FutebolFrancês"),
    "ucl": ("⭐", "CHAMPIONS LEAGUE", "#Champions #LigaDosCampeões"),
    "latam": ("🌎", "AMÉRICA LATINA", "#Libertadores #SulAmericana"),
    "mercado": ("🔁", "MERCADO DA BOLA", "#MercadoDaBola #Transferências"),
    "escandalo": ("🚨", "POLÊMICA", "#Polêmica #Futebol"),
    "fofoca": ("🔥", "BASTIDORES", "#Bastidores #Futebol"),
    "preview": ("⏰", "PRÉ-JOGO", "#PréJogo #Futebol"),
}

SYSTEM_PROMPT = (
    "Você é redator de um canal de Telegram de futebol para o público brasileiro. "
    "Reescreva a notícia recebida em português do Brasil, com texto próprio e envolvente, "
    "tom de torcedor bem informado, sem copiar frases do original. "
    "O material pode estar em inglês ou espanhol: traduza e reescreva. "
    "Regras: use SOMENTE fatos presentes no material, não invente nada (placares, nomes, valores); "
    "em temas de polêmica ou vida pessoal, deixe claro quando algo é alegação ou 'segundo a imprensa' "
    "e nunca afirme como fato o que não está confirmado; "
    "não cite o veículo de origem, não inclua links nem hashtags; "
    "no máximo 1 ou 2 emojis relevantes no corpo. "
    'Responda em JSON: {"title": "manchete curta e chamativa, até 90 caracteres", '
    '"body": "2 a 4 frases curtas, até 450 caracteres"}'
)

PREVIEW_PROMPT = (
    "Você é redator de um canal de Telegram de futebol para o público brasileiro e vai escrever o "
    "'aquecimento' (pré-jogo) de uma partida importante que começa em poucas horas. "
    "Tom animado, de quem está ansioso pela bola rolar, convidando a torcida a comentar. "
    "Regras: use SOMENTE os fatos fornecidos (competição, times, horário de Brasília, estádio, contexto); "
    "não invente estatísticas, retrospecto, escalações, lesões ou classificação; "
    "o horário de Brasília deve aparecer no texto; não cite veículos nem inclua links ou hashtags; "
    "no máximo 2 emojis. "
    'Responda em JSON: {"title": "chamada curta e empolgante, até 90 caracteres", '
    '"body": "2 a 3 frases curtas, até 350 caracteres"}'
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

    @staticmethod
    def _day_key() -> str:
        return "__posts__:" + datetime.now(BRT).strftime("%Y-%m-%d")

    def posts_today(self) -> int:
        """Сколько постов уже вышло сегодня (по Бразилии)."""
        return self.data.get(self._day_key(), 0)

    def count_post(self) -> None:
        self.data[self._day_key()] = self.posts_today() + 1
        self.save()

    def prune(self, days: int = 14) -> None:
        cutoff = time.time() - days * 86400
        self.data = {k: v for k, v in self.data.items() if k.startswith("__") or v >= cutoff}
        self.data = {k: v for k, v in self.data.items()
                     if not k.startswith("__posts__:") or k >= "__posts__:" + (datetime.now(BRT) - timedelta(days=3)).strftime("%Y-%m-%d")}
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
    best, best_w = None, -1
    for key in ("media_content", "media_thumbnail"):
        for m in entry.get(key, []) or []:
            if not m.get("url"):
                continue
            w = int(m.get("width") or 0)  # Guardian отдаёт несколько размеров — берём крупнейший
            if w > best_w:
                best, best_w = m["url"], w
    if best:
        return best
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


def published_ts(entry) -> float:
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    return timegm(t) if t else time.time()


def fetch_candidates(seen: Seen) -> list[dict]:
    out = []
    seen_now: set[str] = set()
    cutoff = time.time() - MAX_AGE_HOURS * 3600
    for url, need_filter, default_cat, fixed in FEEDS:
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
                        "cat": default_cat if fixed else classify(text, default_cat)})
    return out


# ---------- OpenAI ----------

def ai_chat(system: str, material: str) -> tuple[str, str] | None:
    try:
        r = session.post(
            "https://api.openai.com/v1/chat/completions", timeout=60,
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            json={"model": OPENAI_MODEL, "response_format": {"type": "json_object"},
                  "messages": [{"role": "system", "content": system},
                               {"role": "user", "content": material}]})
        r.raise_for_status()
        data = json.loads(r.json()["choices"][0]["message"]["content"])
        title, body = clean(data["title"]), clean(data["body"])
    except (requests.RequestException, KeyError, IndexError, ValueError) as e:
        log.error("OpenAI не ответил нормально: %s", e)
        return None
    return (title[:120], body[:600]) if title and body else None


def ai_rewrite(item: dict) -> bool:
    """Переписывает заголовок и текст новости. False — если не вышло."""
    res = ai_chat(SYSTEM_PROMPT, f"Categoria: {item['cat']}\nTítulo: {item['title']}\nResumo: {item['summary']}")
    if not res:
        return False
    item["title"], item["summary"] = res
    return True


# ---------- оформление и отправка ----------

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


_sent = 0


def pace() -> None:
    """Пауза перед очередным постом (кроме первого за запуск), чтобы не сыпать пачкой."""
    if _sent:
        pause = random.uniform(POST_DELAY_MIN, POST_DELAY_MAX) * 60
        log.info("пауза %.0f мин до следующего поста", pause / 60)
        time.sleep(pause)


def send(item: dict) -> bool:
    global _sent
    pace()
    ok = _send(item)
    _sent += ok
    return ok


def _send(item: dict) -> bool:
    caption = build_caption(item)
    api = f"https://api.telegram.org/bot{BOT_TOKEN}"
    form = {"chat_id": CHANNEL_ID, "caption": caption, "parse_mode": "HTML"}
    if item.get("image_bytes"):
        r = session.post(f"{api}/sendPhoto", timeout=60, data=form,
                         files={"photo": ("card.jpg", item["image_bytes"])})
        if r.ok:
            return True
        log.warning("карточка не отправилась: %s", r.text[:200])
    elif item["image"]:
        r = session.post(f"{api}/sendPhoto", timeout=30, data={**form, "photo": item["image"]})
        if r.ok:
            return True
        log.warning("sendPhoto по ссылке не прошёл (%s), качаю файл", r.text[:200])
        try:
            img = session.get(item["image"], timeout=20)
            img.raise_for_status()
            r = session.post(f"{api}/sendPhoto", timeout=60, data=form,
                             files={"photo": ("image.jpg", to_jpeg(img.content))})
            if r.ok:
                return True
            log.warning("загрузка файлом не прошла: %s", r.text[:200])
        except (requests.RequestException, OSError) as e:
            log.warning("не скачалась/не сконвертировалась картинка: %s", e)
    if item.get("image_bytes") or item["image"] or POST_WITHOUT_IMAGE:
        # фото не приняли — лучше текст, чем потерять пост
        r = session.post(f"{api}/sendMessage", timeout=30, data={
            "chat_id": CHANNEL_ID, "text": caption, "parse_mode": "HTML"})
        if r.ok:
            return True
        log.error("sendMessage: %s", r.text[:300])
    return False


# ---------- прогревы перед топ-матчами ----------

# (код лиги ESPN, название, хватит ли одного «большого» клуба)
PREVIEW_LEAGUES = [
    ("eng.1", "Premier League", False),
    ("esp.1", "La Liga", False),
    ("ita.1", "Série A (Itália)", False),
    ("ger.1", "Bundesliga", False),
    ("fra.1", "Ligue 1", False),
    ("uefa.champions", "Champions League", True),
    ("uefa.europa", "Europa League", True),
    ("bra.1", "Brasileirão", False),
    ("bra.copa_do_brazil", "Copa do Brasil", False),
    ("conmebol.libertadores", "Libertadores", True),
    ("conmebol.sudamericana", "Sul-Americana", True),
]

BIG_TEAMS = [
    "real madrid", "barcelona", "atletico madrid", "manchester city", "manchester united",
    "liverpool", "arsenal", "chelsea", "tottenham", "bayern", "dortmund", "leverkusen",
    "juventus", "inter milan", "ac milan", "napoli", "paris saint", "marseille",
    "flamengo", "palmeiras", "corinthians", "sao paulo", "santos", "fluminense", "vasco",
    "botafogo", "gremio", "internacional", "cruzeiro", "atletico-mg", "atletico mineiro",
    "boca juniors", "river plate",
]


def norm(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()


def big_count(*names: str) -> int:
    return sum(any(b in norm(n) for b in BIG_TEAMS) for n in names)


def fetch_top_matches() -> list[dict]:
    """Матчи топ-уровня, которые начнутся через 20 минут – 5 часов."""
    now = datetime.now(timezone.utc)
    dates = {now.strftime("%Y%m%d"), (now + timedelta(days=1)).strftime("%Y%m%d")}
    found: dict[str, dict] = {}
    for code, league, one_is_enough in PREVIEW_LEAGUES:
        for ds in sorted(dates):
            try:
                r = session.get(f"https://site.api.espn.com/apis/site/v2/sports/soccer/{code}/scoreboard",
                                params={"dates": ds}, timeout=20)
                r.raise_for_status()
                events = r.json().get("events", [])
            except (requests.RequestException, ValueError) as e:
                log.warning("расписание %s недоступно: %s", code, e)
                continue
            for ev in events:
                try:
                    kickoff = datetime.fromisoformat(ev["date"].replace("Z", "+00:00"))
                    comp = ev["competitions"][0]["competitors"]
                    home = next(c for c in comp if c["homeAway"] == "home")["team"]
                    away = next(c for c in comp if c["homeAway"] == "away")["team"]
                    state = ev["status"]["type"]["state"]
                    venue = (ev["competitions"][0].get("venue") or {}).get("fullName", "")
                except (KeyError, StopIteration, ValueError):
                    continue
                wait = (kickoff - now).total_seconds() / 3600
                big = big_count(home["displayName"], away["displayName"])
                if state != "pre" or not (0.33 <= wait <= 5) or big < (1 if one_is_enough else 2):
                    continue
                found[ev["id"]] = {"id": ev["id"], "league": league, "kickoff": kickoff, "big": big,
                                   "home": home, "away": away, "venue": venue}
    # сильнее всего — матчи двух грандов, потом по времени
    return sorted(found.values(), key=lambda m: (-m["big"], m["kickoff"]))


def load_font(size: int):
    for path in ("C:/Windows/Fonts/arialbd.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def fetch_logo(team: dict) -> Image.Image | None:
    url = team.get("logo")
    if not url:
        return None
    try:
        r = session.get(url, timeout=15)
        r.raise_for_status()
        return Image.open(io.BytesIO(r.content)).convert("RGBA")
    except (requests.RequestException, OSError):
        return None


def make_card(m: dict, when: str) -> bytes:
    w, h = 1280, 720
    img = Image.new("RGB", (w, h))
    px = ImageDraw.Draw(img)
    for y in range(h):  # вертикальный градиент: тёмно-синий → тёмно-зелёный
        t = y / h
        px.line([(0, y), (w, y)], fill=(int(8 + 6 * t), int(18 + 70 * t), int(46 - 10 * t)))
    px.rectangle([0, 0, w, 14], fill=(250, 204, 21))
    px.text((w // 2, 80), f"PRÉ-JOGO  •  {m['league'].upper()}", font=load_font(46), fill="white", anchor="mm")
    for team, cx in ((m["home"], 280), (m["away"], w - 280)):
        logo = fetch_logo(team)
        if logo:
            logo.thumbnail((340, 340))
            img.paste(logo, (cx - logo.width // 2, 330 - logo.height // 2), logo)
        name = team.get("shortDisplayName") or team["displayName"]
        px.text((cx, 560), name.upper(), font=load_font(40 if len(name) < 14 else 32), fill="white", anchor="mm")
    px.text((w // 2, 330), "X", font=load_font(120), fill=(250, 204, 21), anchor="mm")
    px.text((w // 2, 650), when, font=load_font(48), fill=(250, 204, 21), anchor="mm")
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=92)
    return buf.getvalue()


def preview_item(m: dict, news: list[dict]) -> dict:
    local = m["kickoff"].astimezone(BRT)
    day = "HOJE" if local.date() == datetime.now(BRT).date() else "AMANHÃ"
    hhmm = local.strftime("%H:%M")
    home, away = m["home"]["displayName"], m["away"]["displayName"]
    when = f"{day} • {hhmm} (BRASÍLIA)"
    item = {"cat": "preview", "image": None, "image_bytes": make_card(m, when),
            "title": f"{home} x {away}: a bola rola {day.lower()} às {hhmm}",
            "summary": f"Jogo grande pela {m['league']}! Horário de Brasília: {hhmm}. Já escolheu o seu lado?"}
    if OPENAI_API_KEY:
        names = [norm(m["home"].get("shortDisplayName", "")), norm(m["away"].get("shortDisplayName", ""))]
        context = [n["title"] for n in news if any(x and x in norm(n["title"]) for x in names)][:3]
        facts = (f"Competição: {m['league']}\nMandante: {home}\nVisitante: {away}\n"
                 f"Horário (Brasília): {day.lower()} às {hhmm}\nEstádio: {m['venue'] or 'não informado'}\n"
                 f"Notícias recentes (contexto): {' | '.join(context) or 'nenhuma'}")
        res = ai_chat(PREVIEW_PROMPT, facts)
        if not res:
            return {}
        item["title"], item["summary"] = res
    return item


def post_previews(seen: Seen, news: list[dict], limit: int) -> int:
    posted = 0
    for m in fetch_top_matches():
        if posted >= limit:
            break
        uid = f"preview:{m['id']}"
        if uid in seen:
            continue
        item = preview_item(m, news)
        if item and send(item):
            log.info("прогрев: %s x %s (%s)", m["home"]["displayName"], m["away"]["displayName"], m["league"])
            seen.add(uid)
            seen.count_post()
            posted += 1
        else:
            log.error("прогрев не отправлен: %s x %s", m["home"]["displayName"], m["away"]["displayName"])
    return posted


# ---------- основной цикл ----------

def pick(fresh: list[dict], plan: list[str]) -> list[dict]:
    """По одной свежей новости на пункт плана (разные рубрики), остаток добираем любыми."""
    pool = sorted(fresh, key=lambda c: c["ts"], reverse=True)
    chosen: list[dict] = []
    for entry in plan:
        allowed = GROUPS.get(entry, {entry})
        options = [c for c in pool if c["cat"] in allowed and c not in chosen]
        new_cat = [c for c in options if c["cat"] not in {x["cat"] for x in chosen}]
        if new_cat or options:
            chosen.append((new_cat or options)[0])
    for c in pool:
        if len(chosen) >= len(plan):
            break
        if c not in chosen:
            chosen.append(c)
    return chosen


def posts_due() -> int:
    now = datetime.now(BRT)
    return sum((now.hour, now.minute) >= t for t in DUE_TIMES)


def run_once(seen: Seen) -> None:
    cands = fetch_candidates(seen)
    for c in cands:
        if not c["fresh"]:
            seen.add(c["uid"])
    fresh = [c for c in cands if c["fresh"]]
    log.info("свежих кандидатов: %d; сегодня постов %d из %d, по графику должно быть %d",
             len(fresh), seen.posts_today(), DAILY_LIMIT, posts_due())

    left = DAILY_LIMIT - seen.posts_today()
    if left <= 0:
        seen.prune()
        return

    # прогрев матча привязан ко времени игры, поэтому выходит независимо от графика (но входит в лимит)
    # и только в бодрствующие часы по Бразилии, чтобы не будить канал ночью
    if 8 <= datetime.now(BRT).hour < 23:
        try:
            post_previews(seen, cands, min(MAX_PREVIEWS_PER_RUN, left))
        except Exception:
            log.exception("ошибка в прогревах")

    # новость — не больше одной за запуск и только если по графику пора
    if seen.posts_today() < min(posts_due(), DAILY_LIMIT):
        day = datetime.now(BRT).timetuple().tm_yday
        entry = DAY_PLAN[(day * DAILY_LIMIT + seen.posts_today()) % len(DAY_PLAN)]
        failed = 0
        while fresh and failed < 3:
            c = pick(fresh, [entry])[0]
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
                seen.add(c["uid"])
                seen.count_post()
                break
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
