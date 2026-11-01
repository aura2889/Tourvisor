#!/usr/bin/env python3
"""Проверка разбора ответов сайта на выдуманных, но похожих примерах: python scraper/test_extract.py
Настоящие образцы появятся после разведки (--mode recon); тогда сюда добавим их."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tourvisor_scraper as ts  # noqa: E402

CTX = {"dest": "dubai", "dep": "Астана", "country": "ОАЭ", "kind": "tour", "cur": "KZT", "url": "u"}


def test_hotels_with_tours():
    obj = {"data": {"hotels": [
        {"hotelname": "Rove Downtown", "stars": 3, "resortname": "Дубай", "countryname": "ОАЭ",
         "tours": [
             {"price": 199000, "flydate": "2026-12-01", "nights": 7, "mealrussian": "Завтрак",
              "operatorname": "Anex Tour", "charter": 1},
             {"price": 210500, "flydate": "2026-12-08", "nights": 7, "mealrussian": "Все включено",
              "operatorname": "Coral Travel", "charter": 0}]},
        {"hotelname": "Ajman Saray", "stars": 5, "resortname": "Аджман",
         "tours": [{"price": 300000, "flydate": "2026-12-02", "nights": 7}]},
    ]}}
    offers = ts.extract_offers_from_json(obj, CTX)
    assert len(offers) == 3, offers
    o = offers[0]
    assert (o["hotel"], o["stars"], o["resort"], o["price"], o["date"], o["nights"]) == \
        ("Rove Downtown", 3, "Дубай", 199000, "2026-12-01", 7)
    assert o["meal"] == "BB" and o["operator"] == "Anex Tour" and o["flight"] == "charter"
    assert offers[1]["meal"] == "AI" and offers[1]["flight"] == "regular"


def test_flat_and_nested_values():
    obj = [{"title": "Hilton Garden Inn", "star": "4", "region": {"name": "Дубай"},
            "price": {"value": "250 000"}, "date": "02.12.2026", "nights": 6}]
    offers = ts.extract_offers_from_json(obj, CTX)
    assert len(offers) == 1
    assert offers[0]["price"] == 250000 and offers[0]["resort"] == "Дубай" and offers[0]["stars"] == 4


def test_no_offers_in_unrelated_json():
    assert ts.extract_offers_from_json({"countries": [{"id": 9, "name": "ОАЭ"}]}, CTX) == []


CARD = """4.6
Хит продаж
−14%
27 783 руб
23 893 руб
5*
Marine Garden Sochi
Россия, Сочи
4 ноя, 7 нч
Завтрак
Сегодня, 08:10"""


def test_card_text():
    raw = ts.parse_card_text(CARD, ["Россия"])
    assert raw["hotel"] == "Marine Garden Sochi", raw
    assert (raw["stars"], raw["price"], raw["country"], raw["resort"], raw["nights"], raw["meal"]) == \
        (5, 23893, "Россия", "Сочи", 7, "BB")
    assert raw["date"] == "4 ноя"
    assert ts.parse_card_text("просто текст без цены", ["ОАЭ"]) is None
    inline = "5* Atlantis The Palm\nОАЭ, Дубай\n12 дек, 7 нч\nВсе включено\n1 250 000 ₸"
    raw = ts.parse_card_text(inline, ["ОАЭ"])
    assert raw["hotel"] == "Atlantis The Palm" and raw["price"] == 1250000 and raw["cur"] == "KZT"


def test_find_ids():
    html = ('<select><option value="2">Астана</option><option value="3">Алматы</option></select>'
            '<script>var c=[{"id":9,"name":"\\u041e\\u0410\\u042d"},{"name":"Грузия","id":"22"}];</script>')
    blobs = [{"deps": [{"id": 5, "name": "Актау"}]}]
    found = ts.find_ids(blobs, [html], ["Актау", "Астана", "Алматы", "ОАЭ", "Грузия", "Китай"])
    assert found == {"Актау": 5, "Астана": 2, "Алматы": 3, "ОАЭ": 9, "Грузия": 22}, found


def _real():
    import json
    from datetime import date
    cfg = json.loads((Path(__file__).resolve().parent.parent / "config.json").read_text(encoding="utf-8"))
    fx = json.loads((Path(__file__).resolve().parent / "fixtures" / "modresult.json").read_text(encoding="utf-8"))
    return cfg, fx, date(2026, 11, 1)


def test_real_modresult_aktau_dubai():
    """Настоящие ответы Турвизора (разведка 2026-11-01): Актау -> ОАЭ."""
    import aggregate as agg
    cfg, fx, today = _real()
    ctx = {"dest": "dubai", "dep": "Актау", "dep_id": 74, "country_id": 9, "country": "ОАЭ", "cur": "RUB"}
    raw = ts.offers_from_modresult(fx["aktau_uae"], ctx)
    assert len(raw) == 66
    data = agg.build_data(raw, cfg, today=today)
    assert data["stats"]["accepted"] == 35          # Шарджа и Фуджейра отсеяны, остался только Дубай
    assert {o["resort"] for o in data["offers"]} == {"Дубай"}
    assert all(o["stars"] >= 3 for o in data["offers"])
    best = data["offers"][0]
    assert best["price"] == 115700 and best["cur"] == "RUB" and best["dep"] == "Актау"
    assert best["flight"] == "regular" and "FlyArystan" in best["note"] and best["operator"] == "Kompas (KZ)"
    assert best["hotel"] == "Concorde Deira Hotel (Ex. Golden Tulip Deira)", best["hotel"]
    assert "x_hotel_codes=" in best["url"] and "s_flyfrom=74" in best["url"]


def test_real_modresult_china_and_blocks_merge():
    import aggregate as agg
    cfg, fx, today = _real()
    ctx = {"dest": "china", "dep": "Алматы", "dep_id": 60, "country_id": 13, "country": "Китай", "cur": "RUB"}
    # два блока ответа склеиваются в один набор туров, повторы по id не дублируются
    once = ts.offers_from_modresult(fx["almaty_china"], ctx)
    twice = ts.offers_from_modresult(fx["almaty_china"] + fx["almaty_china"], ctx)
    assert len(once) == len(twice) > 0
    data = agg.build_data(once, cfg, today=today)
    assert data["mins"]["china|Алматы|tour"]["min"] == 139724


def test_hotel_cards_room_photo_and_map():
    import aggregate as agg
    cfg, fx, today = _real()
    ctx = {"dest": "dubai", "dep": "Актау", "dep_id": 74, "country_id": 9, "country": "ОАЭ", "cur": "RUB"}
    raw = ts.offers_from_modresult(fx["aktau_uae"], ctx)
    hotels = ts.hotels_from_modresult(fx["aktau_uae"])
    data = agg.build_data(raw, cfg, today=today, hotels=hotels)
    best = data["offers"][0]
    h = data["hotels"][str(best["hid"])]
    assert 24 < h["lat"] < 26 and 54 < h["lng"] < 56            # Дубай
    assert h["page"].endswith("#!/hotel=%s" % best["hid"])
    assert h["pic"].startswith("https://static.tourvisor.ru/") and h["photos"][0].endswith(".jpg")
    assert best["room"] and best["rphoto"].startswith("https://static.tourvisor.ru/hotel_pics/rooms/")
    assert len(data["hotels"]) <= len({o["hid"] for o in data["offers"]})   # лишние отели (Шарджа и др.) не тянем


def test_windows_cover_period():
    from datetime import date
    ws = list(ts.windows({"days_ahead": 60, "window_days": 20}, date(2026, 11, 1)))
    assert ws[0][0] == date(2026, 11, 2) and ws[-1][1] == date(2026, 12, 31)
    assert all((b - a).days <= 20 for a, b in ws)
    assert all(ws[i + 1][0] > ws[i][1] for i in range(len(ws) - 1))


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("все проверки прошли")
