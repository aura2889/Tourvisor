"""Тестовые данные, чтобы посмотреть приложение до первого настоящего сбора. НЕ реальные цены."""
import random
from datetime import timedelta

HOTELS = {
    "dubai": [
        ("Rove Downtown", 3, "Дубай"), ("Citymax Bur Dubai", 3, "Дубай"), ("Ibis Al Barsha", 3, "Дубай"),
        ("Gulf Court Hotel Business Bay", 4, "Дубай"), ("Hilton Garden Inn Mall of the Emirates", 4, "Дубай"),
        ("Novotel Al Barsha", 4, "Дубай"), ("Atlantis The Palm", 5, "Дубай"), ("JW Marriott Marquis", 5, "Дубай"),
        ("Hyatt Regency Creek Heights", 5, "Дубай"), ("Rixos Premium Saadiyat", 5, "Абу-Даби"),
        ("Ajman Saray", 5, "Аджман"),
    ],
    "china": [
        ("Sanya Bay Resort", 4, "Хайнань, Санья"), ("Yalong Bay Mangrove Tree", 5, "Хайнань, Санья"),
        ("Haitang Bay Hotel", 4, "Хайнань, Хайтан"), ("Beijing Jianguo Hotel", 4, "Пекин"),
        ("Shanghai Bund Inn", 3, "Шанхай"), ("Guilin Lijiang Waterfall", 5, "Гуйлинь"),
    ],
    "georgia": [
        ("Gudauri Inn", 3, "Кудаури"), ("Hotel Gudauri Loft", 4, "Кудаури"), ("Marco Polo Gudauri", 5, "Кудаури"),
        ("Rooms Hotel Kazbegi", 4, "Казбеги"), ("Tbilisi Inn", 3, "Тбилиси"), ("Mercure Tbilisi", 4, "Тбилиси"),
        ("Radisson Blu Batumi", 5, "Батуми"), ("Batumi Sea Hotel", 4, "Батуми"),
    ],
}
COUNTRY = {"dubai": "ОАЭ", "china": "Китай", "georgia": "Грузия"}
BASE = {"dubai": 190_000, "china": 260_000, "georgia": 150_000}
DEP_K = {"Астана": 1.0, "Алматы": 1.08, "Актау": 0.97}
OPERATORS = ["Anex Tour", "Coral Travel", "Fun&Sun", "Kompas", "Pegas Touristik"]
MEALS = ["RO", "BB", "HB", "AI"]


def make_offers(cfg, today):
    rnd = random.Random(42)
    out = []
    for dest, hotels in HOTELS.items():
        for dep in cfg["departures"]:
            if dep == "Актау" and dest in ("dubai", "china"):
                continue  # как в жизни: из Актау в Дубай на Турвизоре туров нет
            for hotel, stars, resort in hotels:
                base = BASE[dest] * DEP_K.get(dep, 1.0) * (0.75 + 0.12 * (stars - 3)) * rnd.uniform(0.9, 1.15)
                op, meal = rnd.choice(OPERATORS), rnd.choice(MEALS)
                nights = rnd.choice([6, 7, 8, 10])
                # на половину дат цена одинаковая (склеится в диапазон), остальное — вразнобой
                for d in range(3, cfg["days_ahead"], 1):
                    if d % 3 and rnd.random() < 0.45:
                        continue
                    price = base if d % 4 else base * rnd.uniform(1.0, 1.3)
                    for kind in ("tour",) + (("hot",) if rnd.random() < 0.06 and d < 20 else ()):
                        p = round(price * (0.8 if kind == "hot" else 1.0) / 100) * 100
                        out.append({"dest": dest, "kind": kind, "dep": dep, "hotel": hotel, "stars": stars,
                                    "resort": resort, "country": COUNTRY[dest], "price": int(p), "cur": "KZT",
                                    "nights": nights, "meal": meal, "operator": op,
                                    "flight": "charter" if rnd.random() < 0.6 else "regular",
                                    "date": (today + timedelta(days=d)).isoformat(),
                                    "url": "https://tourvisor.ru/"})
    return out
