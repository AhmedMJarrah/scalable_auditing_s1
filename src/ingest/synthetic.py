"""
Synthetic legislation generator.

Produces fake laws/bylaws in EXACTLY the JSON shape the colleague's email
specified (Leg_Name, Leg_Number, Year, Base_Articles, Mod_Legs with their own
Base_Articles/Reflected_Articles). This lets every downstream stage —
ingestion, sampling, item building, DB writes — run and be tested today.
Swapping this for real data later is a one-line change: point ingestion at
the real file instead of calling this generator. No other code changes.

Seeded: same seed -> same synthetic population, every time.
"""

from __future__ import annotations

import random
from typing import Any

ARABIC_LAW_TOPICS = [
    "التجارة", "الضريبة", "العمل", "الصحة", "التعليم", "البيئة",
    "الاستثمار", "الجمارك", "النقل", "الإسكان", "الزراعة", "الاتصالات",
]

BOILERPLATE_TITLE = "يسمى هذا {kind} {name} ويعمل به من تاريخ نشره في الجريدة الرسمية."
BOILERPLATE_DEFS = "يكون للكلمات والعبارات التالية المعاني المخصصة لها أدناه ما لم تدل القرينة على خلاف ذلك."
BOILERPLATE_ENACT = "على رئيس الوزراء والوزراء تنفيذ أحكام هذا القانون."
SUBSTANTIVE_TEMPLATE = "تحدد اللائحة التنفيذية الأحكام المتعلقة بالمادة {n} من هذا القانون فيما يخص {topic}."


def _make_articles(count: int, name: str, kind: str, topic: str) -> list[dict[str, Any]]:
    articles = []
    for i in range(1, count + 1):
        if i == 1:
            text = BOILERPLATE_TITLE.format(kind=kind, name=name)
        elif i == 2:
            text = BOILERPLATE_DEFS
        elif i == count:
            text = BOILERPLATE_ENACT
        else:
            text = SUBSTANTIVE_TEMPLATE.format(n=i, topic=topic)
        articles.append({
            "text": text, "title": f"- المادة {i}", "article_number": str(i),
            "enforcement_date": "01-01-2000",
        })
    return articles


def _make_amendment(
    base_articles: list[dict], mod_number: str, mod_year: str, rng: random.Random,
) -> dict[str, Any]:
    touched_count = rng.randint(1, max(1, len(base_articles) // 3))
    touched = rng.sample(base_articles, k=min(touched_count, len(base_articles)))

    mod_base = [dict(a) for a in touched]
    mod_reflected = []
    for a in touched:
        new = dict(a)
        new["text"] = a["text"] + " (بصيغته المعدلة)"
        mod_reflected.append(new)

    return {
        "Leg_Name": f"قانون معدل رقم {mod_number} لسنة {mod_year}",
        "Publication": "المنشور في الجريدة الرسمية",
        "Leg_Number": mod_number,
        "Year": mod_year,
        "Article_Count": str(len(mod_reflected)),
        "Base_Articles": mod_base,
        "Reflected_Articles": mod_reflected,
    }


def generate_legislation(
    n: int,
    leg_type: str,
    seed: int,
    amendment_rate: float = 0.4,
    min_articles: int = 3,
    max_articles: int = 30,
    start_number: int = 1,
) -> list[dict[str, Any]]:
    """
    Generate n synthetic legislation records of the given leg_type
    ('law' or 'bylaw'), matching the source JSON shape exactly.
    """
    rng = random.Random(seed)
    kind = "القانون" if leg_type == "law" else "النظام"
    records: list[dict[str, Any]] = []

    for i in range(n):
        number = str(start_number + i)
        year = str(rng.randint(1970, 2024))
        topic = rng.choice(ARABIC_LAW_TOPICS)
        name = f"{kind} رقم {number} لسنة {year} ({topic})"
        article_count = rng.randint(min_articles, max_articles)
        base_articles = _make_articles(article_count, name, kind, topic)

        mod_legs = []
        if rng.random() < amendment_rate:
            n_amendments = rng.choices([1, 2, 3], weights=[0.6, 0.3, 0.1])[0]
            for a in range(n_amendments):
                mod_year = str(min(2025, int(year) + rng.randint(1, 8)))
                mod_legs.append(_make_amendment(
                    base_articles, f"{number}-{a + 1}", mod_year, rng,
                ))
            mod_legs.sort(key=lambda m: m["Year"])

        records.append({
            "Leg_Name": name, "Publication": "المنشور في الجريدة الرسمية",
            "Leg_Number": number, "Year": year,
            "Article_Count": str(article_count),
            "Replaced_For": "", "Canceled_By": "",
            "Magazine_Number": str(rng.randint(4000, 5000)),
            "Magazine_Page": str(rng.randint(1, 300)),
            "Magazine_Date": f"01-01-{year}", "Issue_Date": f"15-12-{int(year) - 1}",
            "Active_Date": f"01-02-{year}", "End_Date": "", "Replaced_By": "",
            "Base_Articles": base_articles, "Mod_Legs": mod_legs,
        })

    return records
