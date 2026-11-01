#!/usr/bin/env python3
"""Турвизор для Актау: ежедневный сбор самых дешёвых туров в Дубай, Китай, Грузию.

Что делает:
  1. для каждого направления и каждого города вылета (Актау, Астана, Алматы) открывает
     обычный поиск Турвизора и страницу горящих туров в headless-браузере (Playwright);
  2. забирает предложения из JSON-ответов сайта, а если их нет — из текста карточек;
  3. aggregate.py фильтрует (3–5 звёзд, только Дубай и т.д.), склеивает даты, считает минимумы;
  4. пишет data.json и status.json в корень репозитория.

Режимы:
  python scraper/tourvisor_scraper.py                 # обычный сбор
  python scraper/tourvisor_scraper.py --mode recon    # разведка: что сайт отдаёт, какие id у городов
  python scraper/tourvisor_scraper.py --mode demo     # тестовые данные для проверки приложения
Тесты:  python scraper/test_aggregate.py && python scraper/test_extract.py
"""
import argparse
import html as htmllib
import json
import random
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent))
import aggregate as agg  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DEBUG = ROOT / "debug"
TZ = timezone(timedelta(hours=5))  # Актау, UTC+5
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/130.0.0.0 Safari/537.36")


class SetupNeeded(Exception):
    """Не удалось определить id городов вылета/стран — нужна разведка (--mode recon)."""


# --------------------------------------------------------------------------
#  Достаём предложения из JSON-ответов сайта (имена полей угадываем по смыслу)
# --------------------------------------------------------------------------

K_TOURS = ("tours", "offers", "items", "variants", "prices")
K_HOTEL = ("hotelname", "hotel_name", "hotel", "name", "title")
K_PRICE = ("price", "minprice", "min_price", "cost", "totalprice", "total_price")
K_STARS = ("stars", "star", "hotelstars", "hotel_stars", "category", "starscount")
K_RESORT = ("resortname", "resort_name", "resort", "regionname", "region_name", "region", "city")
K_COUNTRY = ("countryname", "country_name", "country")
K_DATE = ("flydate", "fly_date", "date", "datefrom", "date_from", "departuredate", "departure_date", "dt")
K_NIGHTS = ("nights", "nightsfrom", "nights_from", "night", "duration")
K_MEAL = ("mealrussian", "mealname", "meal_name", "meal", "food", "pansion")
K_OPER = ("operatorname", "operator_name", "operator", "tourop")
K_CHARTER = ("charter", "ischarter", "is_charter")


def _pick(d, keys):
    low = {str(k).lower(): v for k, v in d.items()}
    for k in keys:
        if k in low and low[k] not in (None, "", [], {}):
            return low[k]
    return None


def _text(v):
    """Строка из значения: строка как есть, словарь — его name/title."""
    if isinstance(v, dict):
        v = _pick(v, ("name", "title", "text", "value", "nameru", "name_ru"))
    if isinstance(v, (str, int, float)):
        return str(v).strip()
    return ""


def _num(v):
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str):
        digits = re.sub(r"[^\d]", "", v.split(",")[0].split(".")[0])
        return int(digits) if digits else None
    if isinstance(v, dict):
        return _num(_pick(v, ("value", "amount", "price", "total", "current")))
    return None


def _walk(o):
    if isinstance(o, dict):
        yield o
        for v in o.values():
            yield from _walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from _walk(v)


def _offer_from(hotel_d, tour_d, ctx):
    price = _num(_pick(tour_d, K_PRICE))
    if not price:
        return None
    stars = _pick(hotel_d, K_STARS) or _pick(tour_d, K_STARS)
    stars = _num(stars) if not isinstance(stars, int) else stars
    meal = _text(_pick(tour_d, K_MEAL))
    charter = _pick(tour_d, K_CHARTER)
    return {
        "dest": ctx.get("dest"), "kind": ctx.get("kind", "tour"), "dep": ctx.get("dep", ""),
        "hotel": _text(_pick(hotel_d, K_HOTEL)),
        "stars": stars,
        "resort": _text(_pick(hotel_d, K_RESORT)) or _text(_pick(tour_d, K_RESORT)),
        "country": _text(_pick(hotel_d, K_COUNTRY)) or ctx.get("country"),
        "price": price,
        "cur": ctx.get("cur"),
        "date": _text(_pick(tour_d, K_DATE)),
        "nights": _num(_pick(tour_d, K_NIGHTS)),
        "meal": agg.parse_meal(meal) or (meal or None),
        "operator": _text(_pick(tour_d, K_OPER)),
        "flight": ("charter" if charter in (True, 1, "1") else "regular") if charter is not None else None,
        "url": ctx.get("url"),
    }


def extract_offers_from_json(obj, ctx):
    """Ищем отели с массивом туров внутри, а также «плоские» туры с названием отеля."""
    out, used = [], set()
    for d in _walk(obj):
        tours = None
        for k in list(d.keys()):
            v = d[k]
            if str(k).lower() in K_TOURS and isinstance(v, list) and v and all(isinstance(t, dict) for t in v):
                if any(_pick(t, K_PRICE) for t in v):
                    tours = v
                    break
        if tours is not None and _text(_pick(d, K_HOTEL)):
            for t in tours:
                used.add(id(t))
                o = _offer_from(d, t, ctx)
                if o:
                    out.append(o)
            used.add(id(d))
    for d in _walk(obj):
        if id(d) in used:
            continue
        if _text(_pick(d, K_HOTEL)) and _pick(d, K_PRICE) and _pick(d, K_DATE):
            o = _offer_from(d, d, ctx)
            if o:
                out.append(o)
    return out


# --------------------------------------------------------------------------
#  Разбор текста карточки (горящие туры и запасной вариант для обычного поиска)
# --------------------------------------------------------------------------

def parse_card_text(text, countries):
    """Текст карточки -> сырое предложение или None. countries — названия стран, которые знаем."""
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    if not lines:
        return None
    price = agg.parse_price(text)
    nights = agg.parse_nights(text)
    dt = agg.DATE_TEXT_RE.search(text)
    if not price or not dt:
        return None
    stars = agg.parse_stars(text)
    country = resort = None
    idx = None
    cl = {c.lower() for c in countries}
    for i, l in enumerate(lines):
        if "," in l:
            first, rest = [x.strip() for x in l.split(",", 1)]
            if first.lower() in cl:
                country, resort, idx = first, rest, i
                break
    hotel = None
    if idx is not None:
        for j in range(idx - 1, -1, -1):
            cand = lines[j]
            if re.fullmatch(r"[\d.,\s*★−\-%]+", cand) or agg.PRICE_RE.search(cand):
                continue
            if re.fullmatch(r"\d\s*(?:\*|★)", cand) or cand.lower() in ("хит продаж", "популярный", "горящий"):
                continue
            hotel = re.sub(r"^\d\s*(?:\*|★)\s*", "", cand)
            break
    if not hotel:
        return None
    return {"hotel": hotel, "stars": stars, "country": country, "resort": resort,
            "price": price[0], "cur": price[1], "date": dt.group(0), "nights": nights,
            "meal": agg.parse_meal(text)}


JS_CARDS = r"""
() => {
  const priceRe = /\d[\d\s  ]{2,}\s*(₽|руб|₸|тг|тенге|\$|€)/i;
  const nightsRe = /\d+\s*(нч|ноч)/i;
  const ok = el => { const t = el.innerText || ''; return t.length < 700 && priceRe.test(t) && nightsRe.test(t); };
  const all = Array.from(document.querySelectorAll('a, li, div, article')).filter(ok);
  const inner = all.filter(el => !Array.from(el.querySelectorAll('*')).some(c => c !== el && ok(c)));
  return inner.map(el => {
    const a = el.tagName === 'A' ? el : el.querySelector('a[href]');
    return {text: el.innerText, href: a ? a.href : null};
  });
}
"""


# --------------------------------------------------------------------------
#  Поиск id городов вылета и стран в том, что отдал сайт
# --------------------------------------------------------------------------

def _norm(s):
    return re.sub(r"\s+", " ", (s or "").strip().lower().replace("ё", "е"))


def _unescape_js(text):
    return re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), text)


def find_ids(blobs, html_texts, names):
    """Ищем пары (id, название) для names. blobs — JSON-объекты, html_texts — исходный текст страниц."""
    want = {_norm(n): n for n in names}
    found = {}
    id_keys = ("id", "value", "code", "key", "cityid", "countryid")

    for obj in blobs:
        for d in _walk(obj):
            nm = None
            for k, v in d.items():
                if isinstance(v, str) and _norm(v) in want:
                    nm = v
                    break
            if nm is None:
                continue
            for k in id_keys:
                for kk, vv in d.items():
                    if str(kk).lower() == k and str(vv).isdigit():
                        found.setdefault(want[_norm(nm)], int(vv))
                        break
                if want[_norm(nm)] in found:
                    break

    for text in html_texts:
        t = htmllib.unescape(_unescape_js(text))
        for m in re.finditer(r'<option[^>]*value="?(\d+)"?[^>]*>\s*([^<]+?)\s*<', t):
            if _norm(m.group(2)) in want:
                found.setdefault(want[_norm(m.group(2))], int(m.group(1)))
        for m in re.finditer(r'"(?:id|value|code)"\s*:\s*"?(\d+)"?\s*,\s*"(?:name|title|text|label)"\s*:\s*"([^"]+)"', t):
            if _norm(m.group(2)) in want:
                found.setdefault(want[_norm(m.group(2))], int(m.group(1)))
        for m in re.finditer(r'"(?:name|title|text|label)"\s*:\s*"([^"]+)"\s*,\s*"(?:id|value|code)"\s*:\s*"?(\d+)"?', t):
            if _norm(m.group(1)) in want:
                found.setdefault(want[_norm(m.group(1))], int(m.group(2)))
    return found


# --------------------------------------------------------------------------
#  Браузер
# --------------------------------------------------------------------------

class Capture:
    """Запоминает JSON-ответы, которые страница получает по сети."""

    def __init__(self, page, keep_dir=None):
        self.bodies = []      # (url, объект)
        self.log = []         # (метод, url, статус, content-type, размер)
        self.keep_dir = keep_dir
        self._n = 0
        page.on("response", self._on)

    def reset(self):
        self.bodies = []

    def _on(self, resp):
        try:
            ctype = (resp.headers.get("content-type") or "").lower()
            req = resp.request
            if req.resource_type not in ("xhr", "fetch", "document", "script"):
                return
            size = None
            body = None
            if "json" in ctype or "xml" in ctype or "javascript" in ctype or "text" in ctype:
                body = resp.text()
                size = len(body)
            self.log.append((req.method, resp.url, resp.status, ctype, size))
            if body is None:
                return
            if "json" in ctype:
                try:
                    self.bodies.append((resp.url, json.loads(body)))
                except ValueError:
                    pass
            if self.keep_dir is not None and re.search(r"modsearch|modresult|modmin|modhot|hotoffer", resp.url):
                self._k = getattr(self, "_k", 0) + 1
                path = re.sub(r"(session|referrer)=[^&]*&?", "", resp.url.split("//", 1)[-1])
                name = re.sub(r"[^\w.=-]+", "_", path)[:110]
                (self.keep_dir / ("key_%03d_%s.json" % (self._k, name))).write_text(body[:400_000], encoding="utf-8")
            elif self.keep_dir is not None and req.resource_type in ("xhr", "fetch") and self._n < 80:
                self._n += 1
                name = re.sub(r"[^\w.-]+", "_", resp.url.split("//", 1)[-1])[:90]
                (self.keep_dir / ("%03d_%s.txt" % (self._n, name))).write_text(body[:200_000], encoding="utf-8")
        except Exception:  # noqa: BLE001 — сбор не должен падать из-за одного ответа
            pass


def new_browser(p):
    browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
    ctx = browser.new_context(user_agent=UA, locale="ru-RU", timezone_id="Asia/Aqtobe",
                              viewport={"width": 1366, "height": 900})
    return browser, ctx


def try_click_text(page, text, timeout=2500):
    try:
        page.get_by_text(text, exact=True).first.click(timeout=timeout)
        return True
    except Exception:  # noqa: BLE001
        return False


def wait_for_results(page, timeout_s):
    """Ждём, пока на странице появятся карточки и их число перестанет расти."""
    end = time.time() + timeout_s
    last, stable = -1, 0
    while time.time() < end:
        page.wait_for_timeout(2500)
        try:
            n = len(page.evaluate(JS_CARDS))
        except Exception:  # noqa: BLE001
            n = 0
        if n > 0 and n == last:
            stable += 1
            if stable >= 3:
                return n
        else:
            stable = 0
        last = n
        if n > 0:
            try:
                page.mouse.wheel(0, 4000)
            except Exception:  # noqa: BLE001
                pass
    return max(last, 0)


def search_url(cfg, country_id, dep_id, hot=False):
    site = cfg["site"].rstrip("/")
    if hot:
        return "%s/goryashchie-tury?%s" % (site, urlencode(
            {"departure": dep_id, "country": country_id, "sort": 0, "nights": 0}))
    today = date.today()
    q = {
        "ts_dosearch": 1, "s_form_mode": 0,
        "s_nights_from": cfg["nights_from"], "s_nights_to": cfg["nights_to"],
        "s_directflight": 0, "s_regular": 1, "s_adults": cfg["adults"],
        "s_flyfrom": dep_id, "s_country": country_id, "s_currency": cfg.get("currency_param", 0),
        "s_j_date_from": (today + timedelta(days=1)).strftime("%d.%m.%Y"),
        "s_j_date_to": (today + timedelta(days=cfg["days_ahead"])).strftime("%d.%m.%Y"),
    }
    return "%s/search.php?%s" % (site, urlencode(q))


# --------------------------------------------------------------------------
#  Режимы
# --------------------------------------------------------------------------

def load_cfg():
    return json.loads((ROOT / "config.json").read_text(encoding="utf-8"))


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_json(path, obj, indent=None):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=indent, separators=(",", ":") if indent is None else None),
                    encoding="utf-8")


def resolve_ids(page, cap, cfg):
    """id городов вылета и стран: из config.json, иначе ищем в ответах сайта."""
    dep_ids = dict(cfg.get("departure_ids") or {})
    country_ids = dict(cfg.get("country_ids") or {})
    need_dep = [d for d in cfg["departures"] if d not in dep_ids]
    need_cty = sorted({d["country"] for d in cfg["destinations"].values() if d["country"] not in country_ids})
    if not need_dep and not need_cty:
        return dep_ids, country_ids

    page.goto(cfg["site"].rstrip("/") + "/search.php", wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(6000)
    htmls = [page.content()]
    try_click_text(page, "Казахстан")           # переключатель региона сайта
    page.wait_for_timeout(3000)
    htmls.append(page.content())
    found = find_ids([b for _, b in cap.bodies], htmls, need_dep + need_cty)
    for n in need_dep:
        if n in found:
            dep_ids[n] = found[n]
    for n in need_cty:
        if n in found:
            country_ids[n] = found[n]
    missing = [n for n in need_dep if n not in dep_ids] + [n for n in need_cty if n not in country_ids]
    if missing:
        raise SetupNeeded("не нашёл id: " + ", ".join(missing))
    return dep_ids, country_ids


MODSEARCH = "/xml/modsearch.php"
MODRESULT = "https://search3.tourvisor.ru/modresult.php"


def merge_modresult(datas):
    """Ответ modresult приходит кусками (блоками). Склеиваем справочники и собираем все туры."""
    dec, ops, tours = {}, {}, []
    for d in datas:
        for k, v in (d.get("decode") or {}).items():
            if isinstance(v, dict):
                dec.setdefault(k, {}).update(v)
        for o in d.get("operators") or []:
            ops[o["id"]] = o
        for blk in d.get("block") or []:
            for h in blk.get("hotel") or []:
                for t in h.get("tour") or []:
                    tours.append((h["id"], t))
    return dec, ops, tours


def _hotel_name(name):
    """Турвизор часто отдаёт названия ЗАГЛАВНЫМИ — приводим к виду «Red Castle Hotel»."""
    name = (name or "").strip()
    if name.isupper():
        return re.sub(r"[A-Za-zА-Яа-яЁё]+", lambda m: m.group(0).capitalize(), name.lower())
    return name


def offers_from_modresult(datas, ctx):
    """Сырые предложения из ответов поиска Турвизора (формат подтверждён разведкой 2026-11-01)."""
    dec, ops, tours = merge_modresult(datas)
    out, seen = [], set()
    for hid, t in tours:
        tid = t.get("id")
        if tid in seen:
            continue
        seen.add(tid)
        h = (dec.get("hotels") or {}).get(str(hid)) or {}
        try:
            stars = int(h.get("stars") or 0)
        except (TypeError, ValueError):
            stars = 0
        meal = ((dec.get("meal") or {}).get(str(t.get("ml"))) or {}).get("name") or ""
        note = ((dec.get("tourname") or {}).get(str(t.get("nm"))) or {}).get("name")
        reg = t.get("reg")
        out.append({
            "dest": ctx["dest"], "kind": "tour", "dep": ctx["dep"],
            "hotel": _hotel_name(h.get("name")), "stars": stars,
            "resort": h.get("region") or "", "sub": h.get("subregion"), "country": h.get("country") or ctx.get("country"),
            "price": t.get("pr"), "cur": ctx["cur"], "date": t.get("dt"), "nights": t.get("nt"),
            "meal": agg.parse_meal(meal) or None,
            "operator": (ops.get(t.get("op")) or {}).get("name"),
            "flight": "regular" if reg == 1 else "charter" if reg == 2 else None,
            "note": note,
            "url": hotel_link(ctx, hid, t.get("dt"), t.get("nt")),
        })
    return out


def tv_date(iso):
    y, m, d = iso.split("-")
    return "%s.%s.%s" % (d, m, y)


def hotel_link(ctx, hotel_id, iso_date, nights):
    """Ссылка на поиск именно этого отеля на эту дату на Турвизоре."""
    if not iso_date:
        return None
    q = {"ts_dosearch": 1, "s_flyfrom": ctx["dep_id"], "s_country": ctx["country_id"],
         "s_j_date_from": tv_date(iso_date), "s_j_date_to": tv_date(iso_date),
         "s_nights_from": nights or "", "s_nights_to": nights or "", "s_adults": ctx.get("adults", 2),
         "s_child": 0, "s_regular": 1, "s_currency": ctx.get("currency_param", 0), "x_hotel_codes": hotel_id}
    return "https://tourvisor.ru/search.php?" + urlencode(q)


def find_session(cap):
    for _, url, *_ in cap.log:
        m = re.search(r"[?&]session=([0-9a-f]{20,})", url)
        if m:
            return m.group(1)
    return None


def run_search(req, cfg, session, country_id, dep_id, d_from, d_to, regular):
    """Один поиск на сайте: modsearch -> requestid -> опрос modresult, пока поиск не завершится."""
    site = cfg["site"].rstrip("/")
    ref = site + "/search.php"
    q = {"datefrom": d_from.strftime("%d.%m.%Y"), "dateto": d_to.strftime("%d.%m.%Y"), "directflight": 0,
         "regular": regular, "nightsfrom": cfg["nights_from"], "nightsto": cfg["nights_to"],
         "adults": cfg["adults"], "child": 0, "meal": 0, "rating": 0, "country": country_id, "departure": dep_id,
         "pricefrom": 0, "priceto": 0, "currency": cfg.get("currency_param", 0), "formmode": 0, "pricetype": 0,
         "referrer": ref}
    if session:
        q["session"] = session
    headers = {"Referer": ref, "Accept": "application/json, text/javascript, */*"}
    r = req.get(site + MODSEARCH + "?" + urlencode(q), headers=headers, timeout=60_000)
    rid = r.json()["result"]["requestid"]
    datas, last = [], 0
    deadline = time.time() + cfg.get("max_search_seconds", 150)
    while time.time() < deadline:
        qq = {"requestid": rid, "referrer": ref}
        if last:
            qq["lastblock"] = last
        if session:
            qq["session"] = session
        d = req.get(MODRESULT + "?" + urlencode(qq), headers=headers, timeout=60_000).json().get("data") or {}
        datas.append(d)
        ids = [b["id"] for b in d.get("block") or [] if "id" in b]
        if ids:
            last = max(ids + [last])
        st = d.get("status") or {}
        if st.get("finished") == 1:
            return datas, True
        time.sleep(1.5)
    return datas, False


def windows(cfg, today):
    """Сайт принимает окно дат около 20 дней, поэтому весь период режем на окна."""
    start = today + timedelta(days=1)
    end = today + timedelta(days=cfg["days_ahead"])
    step = timedelta(days=cfg.get("window_days", 20))
    cur = start
    while cur <= end:
        yield cur, min(cur + step, end)
        cur = cur + step + timedelta(days=1)


def fetch_rub_kzt():
    """Курс рубля к тенге с сайта Нацбанка РК (для пометки «≈ в тенге»). None, если не удалось."""
    import urllib.request
    try:
        url = "https://nationalbank.kz/rss/get_rates.cfm?fdate=" + date.today().strftime("%d.%m.%Y")
        xml = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=20).read().decode("utf-8", "ignore")
        for item in re.findall(r"<item>(.*?)</item>", xml, re.S):
            if re.search(r"<title>\s*RUB\s*</title>", item):
                rate = float(re.search(r"<description>\s*([\d.,]+)", item).group(1).replace(",", "."))
                m = re.search(r"<quant>\s*([\d.]+)", item)
                return round(rate / (float(m.group(1)) if m else 1.0), 4)
    except Exception:  # noqa: BLE001
        return None
    return None


def collect_hot(page, cap, cfg, dest_key, dep_name, dep_id, country_id):
    """Горящие туры: страница сайта отдаёт карточки в самом HTML, разбираем их текст."""
    dcfg = cfg["destinations"][dest_key]
    url = search_url(cfg, country_id, dep_id, hot=True)
    cap.reset()
    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(8000)
    cards = page.evaluate(JS_CARDS)
    out = []
    for card in cards:
        raw = parse_card_text(card["text"], [dcfg["country"]])
        if raw:
            raw.update({"dest": dest_key, "dep": dep_name, "kind": "hot", "cur": raw.get("cur") or "RUB",
                        "url": card.get("href") or url})
            out.append(raw)
    return out, {"cards": len(cards), "parsed": len(out), "sample": [c["text"][:260] for c in cards[:2]]}


def run_scrape(cfg):
    from playwright.sync_api import sync_playwright
    prev = read_json(ROOT / "data.json")
    today = date.today()
    status = {"ok": False, "time": datetime.now(TZ).strftime("%Y-%m-%dT%H:%M:%S%z"), "mode": "scrape",
              "pairs": {}, "errors": []}
    raw, failed = [], []
    DEBUG.mkdir(exist_ok=True)
    with sync_playwright() as p:
        browser, bctx = new_browser(p)
        page = bctx.new_page()
        cap = Capture(page)
        try:
            dep_ids, country_ids = resolve_ids(page, cap, cfg)
            status["ids"] = {"departures": dep_ids, "countries": country_ids}
            if not cap.log:
                page.goto(cfg["site"].rstrip("/") + "/search.php", wait_until="domcontentloaded", timeout=60_000)
                page.wait_for_timeout(6000)
            session = find_session(cap)
            status["session_found"] = bool(session)
            for dest_key, dcfg in cfg["destinations"].items():
                for dep in cfg["departures"]:
                    ctx = {"dest": dest_key, "dep": dep, "dep_id": dep_ids[dep], "country_id": country_ids[dcfg["country"]],
                           "country": dcfg["country"], "cur": "RUB", "adults": cfg["adults"],
                           "currency_param": cfg.get("currency_param", 0)}
                    label = "%s|%s|tour" % (dest_key, dep)
                    got, errs, unfinished = [], 0, 0
                    for d_from, d_to in windows(cfg, today):
                        for regular in cfg.get("regular_modes", [1, 2]):
                            try:
                                datas, done = run_search(bctx.request, cfg, session, ctx["country_id"], ctx["dep_id"],
                                                         d_from, d_to, regular)
                                got += offers_from_modresult(datas, ctx)
                                unfinished += 0 if done else 1
                            except Exception as e:  # noqa: BLE001
                                errs += 1
                                if errs <= 2:
                                    status["errors"].append("%s %s..%s: %s" % (label, d_from, d_to, str(e)[:160]))
                            time.sleep(random.uniform(1.0, 2.5))
                    status["pairs"][label] = {"offers": len(got), "errors": errs, "unfinished": unfinished}
                    raw += got
                    if errs and not got:
                        failed.append((dest_key, dep))
                    # горящие
                    hl = "%s|%s|hot" % (dest_key, dep)
                    try:
                        hot, info = collect_hot(page, cap, cfg, dest_key, dep, ctx["dep_id"], ctx["country_id"])
                        status["pairs"][hl] = info
                        raw += hot
                    except Exception as e:  # noqa: BLE001
                        status["errors"].append("%s: %s" % (hl, str(e)[:160]))
                    time.sleep(random.uniform(cfg["delay_min"], cfg["delay_max"]))
        except SetupNeeded as e:
            status["errors"].append(str(e))
            status["setup_needed"] = True
        finally:
            try:
                page.screenshot(path=str(DEBUG / "last.png"))
            except Exception:  # noqa: BLE001
                pass
            browser.close()

    if status.get("setup_needed") or not raw:
        if not raw and not status["errors"]:
            status["errors"].append("ни одного предложения: сайт не отдал результаты (блокировка или смена вёрстки)")
        status["ok"] = False
        write_json(ROOT / "status.json", status)
        print(json.dumps(status, ensure_ascii=False, indent=1))
        return 1

    meta = {}
    rate = fetch_rub_kzt() if cfg.get("rub_to_kzt") == "auto" else cfg.get("rub_to_kzt")
    if isinstance(rate, (int, float)) and rate > 0:
        meta["rub_kzt"] = rate
    data = agg.build_data(raw, cfg, prev=prev, failed_pairs=failed, today=today, meta=meta)
    status["ok"] = not any(v.get("errors") for v in status["pairs"].values()) and not status["errors"]
    status["stats"] = data["stats"]
    write_json(ROOT / "data.json", data)
    write_json(ROOT / "status.json", status)
    print(json.dumps({"stats": data["stats"], "errors": status["errors"], "rate": meta.get("rub_kzt")}, ensure_ascii=False))
    return 0


def run_recon(cfg):
    """Разведка: сохраняем то, что видит браузер, чтобы подогнать парсер под реальный сайт."""
    from playwright.sync_api import sync_playwright
    out = DEBUG / "recon"
    out.mkdir(parents=True, exist_ok=True)
    for f in out.glob("*"):
        f.unlink()
    report = {"time": datetime.now(TZ).isoformat(), "steps": []}
    site = cfg["site"].rstrip("/")
    with sync_playwright() as p:
        browser, bctx = new_browser(p)
        page = bctx.new_page()
        cap = Capture(page, keep_dir=out)

        def snap(name, note=""):
            try:
                (out / (name + ".html")).write_text(page.content()[:600_000], encoding="utf-8")
                page.screenshot(path=str(out / (name + ".png")))
            except Exception as e:  # noqa: BLE001
                note += " snap-error: %s" % e
            report["steps"].append({"name": name, "url": page.url, "note": note, "requests": len(cap.log)})

        try:
            page.goto(site + "/search.php", wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(8000)
            snap("01_search_default")
            clicked = try_click_text(page, "Казахстан")
            page.wait_for_timeout(4000)
            snap("02_after_kz_click", "клик по «Казахстан»: %s" % clicked)
            htmls = [page.content()]
            names = cfg["departures"] + sorted({d["country"] for d in cfg["destinations"].values()})
            report["ids_guess"] = find_ids([b for _, b in cap.bodies], htmls, names)

            ids = dict(cfg.get("country_ids") or {})
            ids.update({k: v for k, v in report["ids_guess"].items() if k in ("ОАЭ", "Китай", "Грузия")})
            deps = dict(cfg.get("departure_ids") or {})
            deps.update({k: v for k, v in report["ids_guess"].items() if k in cfg["departures"]})
            report["ids_used"] = {"countries": ids, "departures": deps}
            plan = [("ОАЭ", "Актау"), ("ОАЭ", "Астана"), ("Грузия", "Астана"), ("Китай", "Алматы")]
            for i, (cty, dep) in enumerate(plan, 1):
                if cty in ids and dep in deps:
                    page.goto(search_url(cfg, ids[cty], deps[dep]), wait_until="domcontentloaded", timeout=60_000)
                    page.wait_for_timeout(70_000)
                    snap("1%d_search_%s_%s" % (i, cty, dep), "прошло 70 с")
            for i, (cty, dep) in enumerate([("ОАЭ", "Актау"), ("Грузия", "Астана")], 1):
                if cty in ids and dep in deps:
                    page.goto(search_url(cfg, ids[cty], deps[dep], hot=True), wait_until="domcontentloaded", timeout=60_000)
                    page.wait_for_timeout(25_000)
                    snap("2%d_hot_%s_%s" % (i, cty, dep))
        except Exception as e:  # noqa: BLE001
            report["error"] = str(e)[:400]
            snap("99_error")
        finally:
            report["requests"] = cap.log[-300:]
            browser.close()
    write_json(out / "recon.json", report, indent=1)
    print("recon: шагов %d, ошибка: %s" % (len(report["steps"]), report.get("error")))
    return 0


def run_demo(cfg):
    import demo_data
    raw = demo_data.make_offers(cfg, date.today())
    data = agg.build_data(raw, cfg, today=date.today(), meta={"demo": True})
    write_json(ROOT / "data.json", data)
    write_json(ROOT / "status.json", {"ok": True, "demo": True, "mode": "demo",
                                      "time": datetime.now(TZ).strftime("%Y-%m-%dT%H:%M:%S%z"), "stats": data["stats"]})
    print(json.dumps(data["stats"]))
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["scrape", "recon", "demo"], default="scrape")
    args = ap.parse_args()
    cfg = load_cfg()
    return {"scrape": run_scrape, "recon": run_recon, "demo": run_demo}[args.mode](cfg)


if __name__ == "__main__":
    sys.exit(main())
