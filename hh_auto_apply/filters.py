"""Простые конфигурируемые фильтры (раздел 5 ТЗ). Без LLM."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from .models import Vacancy

log = logging.getLogger(__name__)


@dataclass
class FilterResult:
    passed: bool
    reason: str = ""
    notes: str = ""          # пометки для батча, напр. «зарплата не указана»


def _norm(s: str) -> str:
    return (s or "").lower().replace("ё", "е")


def check_vacancy(v: Vacancy, f: dict) -> FilterResult:
    title = _norm(v.title)
    text = _norm(v.title + " " + v.raw_description)

    # работодатели
    for e in f.get("excluded_employers") or []:
        if str(e) == v.employer_id or _norm(str(e)) == _norm(v.employer_name):
            return FilterResult(False, f"работодатель исключён: {v.employer_name}")

    # стоп-слова (с исключением для разрешённых ролей в названии)
    included = any(_norm(r) in title for r in f.get("include_roles") or [])
    if not included:
        for w in f.get("stop_words") or []:
            if _norm(w) in text:
                return FilterResult(False, f"стоп-слово «{w}»")

    # гео
    areas = [str(a) for a in f.get("areas") or []]
    if areas:
        ok = v.area_id in areas or _norm(v.area) in {_norm(a) for a in areas}
        if not ok and not (f.get("allow_remote", True) and v.remote):
            return FilterResult(False, f"регион «{v.area}» не в списке")

    # зарплата
    notes = ""
    min_salary = int(f.get("min_salary") or 0)
    if min_salary:
        if v.salary_from is None and v.salary_to is None:
            notes = "зарплата не указана"
        elif v.salary_currency and v.salary_currency != f.get("salary_currency", "KZT"):
            notes = f"зарплата в {v.salary_currency} — не сравнивалась"
        else:
            top = v.salary_to or v.salary_from or 0
            if top < min_salary:
                return FilterResult(False, f"зарплата {v.salary} ниже {min_salary}")
    elif v.salary_from is None and v.salary_to is None:
        notes = "зарплата не указана"
    return FilterResult(True, notes=notes)
