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
            if self.keep_dir is not None and req.resource_type in ("xhr", "fetch") and self._n < 80:
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


def collect_pair(page, cap, cfg, dest_key, dep_name, dep_id, country_id, hot):
    dcfg = cfg["destinations"][dest_key]
    url = search_url(cfg, country_id, dep_id, hot=hot)
    ctx = {"dest": dest_key, "dep": dep_name, "country": dcfg["country"], "kind": "hot" if hot else "tour",
           "url": url, "cur": {0: "RUB", 1: "USD", 2: "EUR"}.get(cfg.get("currency_param", 0), "RUB")}
    cap.reset()
    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    n_cards = wait_for_results(page, cfg["search_timeout_s"])
    offers = []
    for _, obj in cap.bodies:
        offers += extract_offers_from_json(obj, ctx)
    from_json = len(offers)
    if not offers:
        for card in page.evaluate(JS_CARDS):
            raw = parse_card_text(card["text"], [dcfg["country"]])
            if raw:
                raw.update({"dest": dest_key, "dep": dep_name, "kind": ctx["kind"], "cur": raw.get("cur") or ctx["cur"],
                            "url": card.get("href") or url})
                offers.append(raw)
    return offers, {"cards": n_cards, "json": from_json, "total": len(offers)}


def run_scrape(cfg):
    from playwright.sync_api import sync_playwright
    prev = read_json(ROOT / "data.json")
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
            for dest_key, dcfg in cfg["destinations"].items():
                for dep in cfg["departures"]:
                    for hot in (False, True):
                        label = "%s|%s|%s" % (dest_key, dep, "hot" if hot else "tour")
                        try:
                            offers, info = collect_pair(page, cap, cfg, dest_key, dep, dep_ids[dep],
                                                        country_ids[dcfg["country"]], hot)
                            status["pairs"][label] = info
                            raw += offers
                            if info["total"] == 0:
                                (DEBUG / ("empty_%s.html" % re.sub(r"\W+", "_", label))).write_text(
                                    page.content()[:400_000], encoding="utf-8")
                        except Exception as e:  # noqa: BLE001
                            status["errors"].append("%s: %s" % (label, str(e)[:200]))
                            failed.append((dest_key, dep))
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
        write_json(ROOT / "status.json", status)
        print(json.dumps(status, ensure_ascii=False, indent=1))
        return 1

    data = agg.build_data(raw, cfg, prev=prev, failed_pairs=failed, today=date.today())
    status["ok"] = not status["errors"]
    status["stats"] = data["stats"]
    write_json(ROOT / "data.json", data)
    write_json(ROOT / "status.json", status)
    print(json.dumps({"stats": data["stats"], "errors": status["errors"]}, ensure_ascii=False))
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

            cid = cfg.get("country_ids", {}).get("ОАЭ", 9)
            guess_dep = next((report["ids_guess"][n] for n in cfg["departures"] if n in report["ids_guess"]), 1)
            page.goto(search_url(cfg, cid, guess_dep), wait_until="domcontentloaded", timeout=60_000)
            n = wait_for_results(page, 90)
            snap("03_search_uae", "карточек: %d" % n)
            report["cards_sample"] = [c["text"][:300] for c in page.evaluate(JS_CARDS)[:5]]
            ctx = {"dest": "dubai", "dep": "recon", "country": "ОАЭ", "kind": "tour", "cur": "RUB"}
            report["json_offers_sample"] = [o for _, b in cap.bodies for o in extract_offers_from_json(b, ctx)][:5]

            page.goto(search_url(cfg, cid, guess_dep, hot=True), wait_until="domcontentloaded", timeout=60_000)
            n = wait_for_results(page, 60)
            snap("04_hot_uae", "карточек: %d" % n)
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
