"""Чистая логика без сети: нормализация предложений, фильтры, склейка дат, история цен.

Парсер (tourvisor_scraper.py) приносит «сырые» предложения, а здесь они превращаются
в data.json. Всё тут проверяется тестами: python scraper/test_aggregate.py
"""
import hashlib
import re
from datetime import date, datetime, timedelta

MONTHS = {
    "янв": 1, "фев": 2, "мар": 3, "апр": 4, "мая": 5, "май": 5, "июн": 6,
    "июл": 7, "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12,
}

CURRENCY = {"₽": "RUB", "руб": "RUB", "р.": "RUB", "₸": "KZT", "тг": "KZT",
            "тенге": "KZT", "$": "USD", "€": "EUR"}

PRICE_RE = re.compile(
    r"(\d{1,3}(?:[\s  ]\d{3})+|\d{3,})\s*(₽|руб\.?|р\.|₸|тг\.?|тенге|\$|€)", re.I)
STARS_RE = re.compile(r"(\d)\s*(?:\*|★|звезд|зв\b)")
DATE_TEXT_RE = re.compile(r"(\d{1,2})\s+(янв|фев|мар|апр|мая|май|июн|июл|авг|сен|окт|ноя|дек)", re.I)
NIGHTS_RE = re.compile(r"(\d{1,2})\s*(?:нч|ноч|н\.)", re.I)

MEALS = [
    ("ultra", re.compile(r"ультра", re.I)),
    ("AI", re.compile(r"в[сc][её]\s*включено|all\s*incl", re.I)),
    ("FB", re.compile(r"полный\s*пансион|завтрак,\s*обед,\s*ужин|full\s*board", re.I)),
    ("HB", re.compile(r"полупансион|завтрак,\s*ужин|half\s*board", re.I)),
    ("BB", re.compile(r"завтрак|bed\s*&?\s*breakfast", re.I)),
    ("RO", re.compile(r"без\s*питания|room\s*only", re.I)),
]


def parse_price(text):
    """Самая низкая цена в тексте (в карточке со скидкой старая цена выше). -> (int, 'KZT') или None."""
    found = []
    for m in PRICE_RE.finditer(text or ""):
        num = int(re.sub(r"\D", "", m.group(1)))
        cur = CURRENCY.get(m.group(2).lower().rstrip("."), None) or CURRENCY.get(m.group(2), "RUB")
        found.append((num, cur))
    return min(found) if found else None


def parse_stars(text):
    m = STARS_RE.search(text or "")
    return int(m.group(1)) if m else None


def parse_meal(text):
    for code, rx in MEALS:
        if rx.search(text or ""):
            return code
    return None


def parse_nights(text):
    m = NIGHTS_RE.search(text or "")
    return int(m.group(1)) if m else None


def parse_date(value, today=None):
    """'2026-12-04' / '04.12.2026' / '4 дек' -> 'YYYY-MM-DD' или None."""
    today = today or date.today()
    if not value:
        return None
    s = str(value).strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return "%s-%s-%s" % m.groups()
    m = re.match(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})$", s)
    if m:
        return "%s-%02d-%02d" % (m.group(3), int(m.group(2)), int(m.group(1)))
    m = DATE_TEXT_RE.search(s)
    if m:
        day, mon = int(m.group(1)), MONTHS[m.group(2).lower()]
        year = today.year
        try:
            d = date(year, mon, day)
        except ValueError:
            return None
        if d < today - timedelta(days=30):
            d = date(year + 1, mon, day)
        return d.isoformat()
    return None


def classify(country, resort, cfg):
    """Какой из наших направлений (dubai/china/georgia) подходит предложению. None — не наше."""
    country_l = (country or "").strip().lower()
    resort_l = (resort or "").strip().lower()
    for key, d in cfg["destinations"].items():
        if d["country"].lower() != country_l:
            continue
        inc, exc = d.get("resort_include"), d.get("resort_exclude")
        if inc and not re.search(inc, resort_l, re.I):
            continue
        if exc and re.search(exc, resort_l, re.I):
            continue
        return key
    return None


def norm_offer(raw, cfg, today=None):
    """Сырое предложение -> нормализованное. None, если не подходит под фильтры."""
    today = today or date.today()
    hotel = (raw.get("hotel") or "").strip()
    price = raw.get("price")
    if isinstance(price, str):
        pp = parse_price(price)
        price = pp[0] if pp else None
    if not hotel or not price or price <= 0:
        return None
    dest = raw.get("dest") or classify(raw.get("country"), raw.get("resort"), cfg)
    if dest not in cfg["destinations"]:
        return None
    # направление задано поиском, но курорт известен — всё равно отсекаем чужие курорты (Абу-Даби и т.п.)
    resort_l = (raw.get("resort") or "").strip().lower()
    inc = cfg["destinations"][dest].get("resort_include")
    exc = cfg["destinations"][dest].get("resort_exclude")
    # если для направления важен курорт (Дубай), а у отеля он не указан — не берём, чтобы не пустить чужие эмираты
    if (inc and not re.search(inc, resort_l, re.I)) or (exc and resort_l and re.search(exc, resort_l, re.I)):
        return None
    stars = raw.get("stars")
    if not isinstance(stars, int) or stars < cfg.get("min_stars", 3):
        return None
    dt = parse_date(raw.get("date"), today)
    if not dt:
        return None
    return {
        "dest": dest,
        "kind": "hot" if raw.get("kind") == "hot" else "tour",
        "dep": raw.get("dep") or "",
        "hotel": hotel,
        "stars": stars,
        "resort": (raw.get("resort") or "").strip(),
        "price": int(price),
        "cur": raw.get("cur") or "RUB",
        "nights": raw.get("nights"),
        "meal": raw.get("meal"),
        "operator": (raw.get("operator") or "").strip() or None,
        "flight": raw.get("flight"),
        "date": dt,
        "url": raw.get("url"),
        "sub": raw.get("sub") or None,
        "note": raw.get("note") or None,
    }


def _key(o):
    return (o["dest"], o["kind"], o["dep"], o["hotel"].lower(), o["stars"], o["nights"],
            o["meal"], o["price"], o["operator"], o["flight"])


def group_offers(offers):
    """Одинаковые предложения на разные даты склеиваем в одну строку со списком дат."""
    groups = {}
    for o in offers:
        k = _key(o)
        g = groups.get(k)
        if g is None:
            g = dict(o)
            g.pop("date")
            g["dates"] = set()
            groups[k] = g
        g["dates"].add(o["date"])
        if not g.get("url") and o.get("url"):
            g["url"] = o["url"]
    out = []
    for k, g in groups.items():
        g["dates"] = sorted(g["dates"])
        g["id"] = hashlib.sha1("|".join(map(str, k)).encode("utf-8")).hexdigest()[:10]
        out.append(g)
    out.sort(key=lambda g: (g["price"], g["hotel"], g["dep"], g["dates"][0]))
    return out


def minimums(groups):
    """{'dubai|Астана|tour': {'min': 123456, 'cur': 'KZT'}, ...} — для истории и уведомлений."""
    res = {}
    for g in groups:
        k = "%s|%s|%s" % (g["dest"], g["dep"], g["kind"])
        if k not in res or g["price"] < res[k]["min"]:
            res[k] = {"min": g["price"], "cur": g["cur"], "hotel": g["hotel"], "dates": g["dates"][:3]}
    return res


def update_history(prev_history, mins, today_iso, keep_days=90):
    hist = {k: list(v) for k, v in (prev_history or {}).items()}
    for k, v in mins.items():
        rows = [r for r in hist.get(k, []) if r[0] != today_iso]
        rows.append([today_iso, v["min"]])
        hist[k] = rows[-keep_days:]
    return hist


def build_data(raw_offers, cfg, prev=None, failed_pairs=(), meta=None, today=None, max_per_pair=400):
    """Главная функция: сырые предложения -> содержимое data.json."""
    today = today or date.today()
    prev = prev or {}
    normed = [o for o in (norm_offer(r, cfg, today) for r in raw_offers) if o]
    normed.sort(key=lambda o: o["date"])   # у склеенной строки ссылка и пометка — от самой ранней даты
    groups = group_offers(normed)

    # не больше max_per_pair самых дешёвых строк на пару (направление, город вылета, вид)
    seen, kept = {}, []
    for g in groups:
        pk = (g["dest"], g["dep"], g["kind"])
        seen[pk] = seen.get(pk, 0) + 1
        if seen[pk] <= max_per_pair:
            kept.append(g)

    # если по паре не удалось собрать данные — оставляем вчерашние, помечая как устаревшие
    carried = 0
    failed = {tuple(p) for p in failed_pairs}
    if failed:
        have = {(g["dest"], g["dep"]) for g in kept}
        for g in prev.get("offers", []):
            if (g["dest"], g["dep"]) in failed and (g["dest"], g["dep"]) not in have:
                g = dict(g)
                g["stale"] = True
                kept.append(g)
                carried += 1
        kept.sort(key=lambda g: (g["price"], g["hotel"], g["dep"]))

    mins = minimums([g for g in kept if not g.get("stale")])
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    data = {
        "generated": now,
        "today": today.isoformat(),
        "adults": cfg.get("adults", 2),
        "min_stars": cfg.get("min_stars", 3),
        "destinations": {k: {"title": d["title"], "focus_regex": d.get("focus_regex")}
                         for k, d in cfg["destinations"].items()},
        "departures": cfg.get("departures", []),
        "offers": kept,
        "mins": mins,
        "history": update_history(prev.get("history"), mins, today.isoformat(), cfg.get("history_days", 90)),
    }
    if meta:
        data.update(meta)
    data["stats"] = {"raw": len(raw_offers), "accepted": len(normed), "rows": len(kept), "carried": carried}
    return data
