#!/usr/bin/env python3
"""Проверка логики без сети: python scraper/test_aggregate.py"""
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import aggregate as agg  # noqa: E402

CFG = json.loads((Path(__file__).resolve().parent.parent / "config.json").read_text(encoding="utf-8"))
TODAY = date(2026, 11, 1)


def offer(**kw):
    base = {"dest": "dubai", "kind": "tour", "dep": "Астана", "hotel": "Rove Downtown", "stars": 3,
            "resort": "Дубай", "price": 200000, "cur": "KZT", "nights": 7, "meal": "BB",
            "operator": "Anex Tour", "flight": "charter", "date": "2026-12-01"}
    base.update(kw)
    return base


def test_parsers():
    assert agg.parse_price("−14% 27 783 руб 23 893 руб") == (23893, "RUB")
    assert agg.parse_price("от 1 234 567 ₸") == (1234567, "KZT")
    assert agg.parse_price("нет цены") is None
    assert agg.parse_stars("5* Marine Garden") == 5
    assert agg.parse_nights("4 ноя, 7 нч") == 7
    assert agg.parse_meal("Завтрак") == "BB"
    assert agg.parse_meal("Ультра все включено") == "ultra"
    assert agg.parse_meal("Все включено") == "AI"
    assert agg.parse_meal("Завтрак, ужин") == "HB"
    assert agg.parse_date("4 ноя", TODAY) == "2026-11-04"
    assert agg.parse_date("4 янв", date(2026, 12, 20)) == "2027-01-04"
    assert agg.parse_date("04.12.2026", TODAY) == "2026-12-04"
    assert agg.parse_date("2026-12-04T00:00:00", TODAY) == "2026-12-04"


def test_classify_and_filters():
    assert agg.classify("ОАЭ", "Дубай", CFG) == "dubai"
    assert agg.classify("ОАЭ", "Абу-Даби", CFG) is None
    assert agg.classify("ОАЭ", "Аджман", CFG) is None
    assert agg.classify("Китай", "Хайнань, Санья", CFG) == "china"
    assert agg.classify("Грузия", "Кудаури", CFG) == "georgia"
    assert agg.classify("Турция", "Анталья", CFG) is None
    # 2 звезды и «без звёзд» не берём
    assert agg.norm_offer(offer(stars=2), CFG, TODAY) is None
    assert agg.norm_offer(offer(stars=None), CFG, TODAY) is None
    assert agg.norm_offer(offer(stars=3), CFG, TODAY)["stars"] == 3
    # поиск по Дубаю принёс отель в Аджмане — убираем, хотя направление задано поиском
    assert agg.norm_offer(offer(resort="Аджман"), CFG, TODAY) is None
    assert agg.norm_offer(offer(resort="Абу-Даби"), CFG, TODAY) is None
    # без цены / без отеля / без даты
    assert agg.norm_offer(offer(price=0), CFG, TODAY) is None
    assert agg.norm_offer(offer(hotel=""), CFG, TODAY) is None
    assert agg.norm_offer(offer(date=None), CFG, TODAY) is None


def test_group_dates():
    raws = [offer(date="2026-12-01"), offer(date="2026-12-02"), offer(date="2026-12-03"),
            offer(date="2026-12-10", price=250000),
            offer(date="2026-12-05", dep="Алматы", price=190000)]
    data = agg.build_data(raws, CFG, today=TODAY)
    rows = data["offers"]
    assert len(rows) == 3, rows
    assert [r["price"] for r in rows] == [190000, 200000, 250000]          # дешёвые первыми
    same = [r for r in rows if r["price"] == 200000][0]
    assert same["dates"] == ["2026-12-01", "2026-12-02", "2026-12-03"]     # одна цена — одна строка
    assert data["mins"]["dubai|Астана|tour"]["min"] == 200000
    assert data["mins"]["dubai|Алматы|tour"]["min"] == 190000
    assert len({r["id"] for r in rows}) == 3


def test_service_hotels_and_demo_carry():
    assert agg.norm_offer(offer(hotel="Test 2 Hotel"), CFG, TODAY) is None
    assert agg.norm_offer(offer(hotel="Latest Hotel"), CFG, TODAY)
    prev = agg.build_data([offer(dep="Алматы")], CFG, today=TODAY, meta={"demo": True})
    now = agg.build_data([offer()], CFG, prev=prev, failed_pairs=[("dubai", "Алматы")], today=TODAY)
    assert now["stats"]["carried"] == 0        # демо-данные не переносятся


def test_hot_separate_and_cap():
    raws = [offer(kind="hot", price=150000), offer(price=200000)]
    data = agg.build_data(raws, CFG, today=TODAY)
    assert {r["kind"] for r in data["offers"]} == {"hot", "tour"}
    assert "dubai|Астана|hot" in data["mins"] and "dubai|Астана|tour" in data["mins"]
    many = [offer(hotel="H%d" % i, price=100000 + i) for i in range(10)]
    data = agg.build_data(many, CFG, today=TODAY, max_per_pair=4, per_date_min=0)
    assert len(data["offers"]) == 4 and data["offers"][0]["price"] == 100000


def test_expensive_date_not_cut():
    raws = [offer(hotel="H%d" % i, price=100000 + i, date="2026-12-01") for i in range(10)]
    raws += [offer(hotel="Jan Hotel", price=900000, date="2027-01-05")]
    data = agg.build_data(raws, CFG, today=TODAY, max_per_pair=3, per_date_min=2)
    assert any("2027-01-05" in r["dates"] for r in data["offers"])     # дорогая январская дата осталась
    assert len(data["offers"]) == 3 + 0 or len(data["offers"]) <= 4


def test_carry_failed_pair_and_history():
    prev = agg.build_data([offer(dep="Алматы", price=210000), offer(price=205000)], CFG, today=date(2026, 10, 31))
    # сегодня по Алматы сбор упал, по Астане прошёл
    now = agg.build_data([offer(price=199000)], CFG, prev=prev, failed_pairs=[("dubai", "Алматы")], today=TODAY)
    deps = {(r["dep"], r.get("stale", False)) for r in now["offers"]}
    assert ("Алматы", True) in deps and ("Астана", False) in deps
    assert now["stats"]["carried"] == 1
    assert "dubai|Алматы|tour" not in now["mins"]                        # устаревшее в минимумы не идёт
    hist = now["history"]["dubai|Астана|tour"]
    assert hist == [["2026-10-31", 205000], ["2026-11-01", 199000]], hist
    # повторный запуск в тот же день не плодит записи
    again = agg.build_data([offer(price=198000)], CFG, prev=now, today=TODAY)
    assert again["history"]["dubai|Астана|tour"][-1] == ["2026-11-01", 198000]
    assert len(again["history"]["dubai|Астана|tour"]) == 2


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("все проверки прошли")
