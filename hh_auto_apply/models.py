from __future__ import annotations

import re
from dataclasses import dataclass, field
from html import unescape
from typing import Any

_TAG_RE = re.compile(r"<[^>]+>")
_BR_RE = re.compile(r"<\s*(br|/p|/li|/ul)\s*/?>", re.I)


def strip_html(text: str | None) -> str:
    if not text:
        return ""
    text = _BR_RE.sub("\n", text)
    text = re.sub(r"<li>", "• ", text, flags=re.I)
    text = unescape(_TAG_RE.sub("", text))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


@dataclass
class Vacancy:
    id: str
    title: str
    employer_name: str = ""
    employer_id: str = ""
    salary: str = ""            # человекочитаемая строка
    salary_from: int | None = None
    salary_to: int | None = None
    salary_currency: str = ""
    area: str = ""
    area_id: str = ""
    url: str = ""
    raw_description: str = ""
    key_skills: list[str] = field(default_factory=list)
    remote: bool = False
    already_applied: bool = False   # relations содержит got_response
    has_questionnaire: bool = False
    questionnaire_text: str = ""
    questionnaire_answer: str = ""

    @classmethod
    def from_api(cls, d: dict[str, Any]) -> "Vacancy":
        sal = d.get("salary") or {}
        s_from, s_to, cur = sal.get("from"), sal.get("to"), sal.get("currency") or ""
        parts = []
        if s_from:
            parts.append(f"от {s_from}")
        if s_to:
            parts.append(f"до {s_to}")
        salary = (" ".join(parts) + f" {cur}").strip() if parts else ""
        emp = d.get("employer") or {}
        area = d.get("area") or {}
        schedule = (d.get("schedule") or {}).get("id")
        work_format = [w.get("id") for w in d.get("work_format") or []]
        test = d.get("test") or {}
        has_q = bool(d.get("has_test") or test)
        q_text = ""
        if has_q:
            q_text = (
                "Работодатель требует пройти тест/ответить на вопросы"
                + (" (обязательно)" if test.get("required") else "")
                + ". Вопросы не отдаются через API — откройте вакансию по ссылке, "
                "скопируйте их и впишите ответ ниже ([e]dit)."
            )
        elif d.get("response_letter_required"):
            q_text = "Работодатель требует сопроводительное письмо (письмо будет приложено)."
        return cls(
            id=str(d["id"]),
            title=d.get("name", ""),
            employer_name=emp.get("name", ""),
            employer_id=str(emp.get("id") or ""),
            salary=salary,
            salary_from=s_from,
            salary_to=s_to,
            salary_currency=cur,
            area=area.get("name", ""),
            area_id=str(area.get("id") or ""),
            url=d.get("alternate_url", f"https://hh.kz/vacancy/{d['id']}"),
            raw_description=strip_html(d.get("description")),
            key_skills=[k.get("name", "") for k in d.get("key_skills") or []],
            remote=schedule == "remote" or "REMOTE" in work_format,
            already_applied="got_response" in (d.get("relations") or []),
            has_questionnaire=has_q,
            questionnaire_text=q_text if has_q else "",
        )


@dataclass
class Application:
    id: int | None
    vacancy_id: str
    resume_id: str
    cover_letter_text: str = ""
    status: str = "generated"   # generated|approved|sent|error|captcha_pending|skipped
    error_message: str | None = None
    created_at: str = ""
    sent_at: str | None = None


@dataclass
class BatchItem:
    """Элемент батча: вакансия + заявка + решение пользователя."""
    vacancy: Vacancy
    application: Application
    decision: str = "pending"   # pending|approve|skip
    note: str = ""              # напр. «зарплата не указана», ошибка генерации
