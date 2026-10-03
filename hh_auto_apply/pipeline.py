"""Оркестрация цикла: поиск → дедупликация → фильтры → письма (шаги 2–5 ТЗ)."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterator

from .claude_client import ClaudeClient, LetterError
from .filters import check_vacancy
from .hh_client import HHClient, HHError
from .models import Application, BatchItem, Vacancy
from .storage import Storage

log = logging.getLogger(__name__)

RESUME_STATUSES = ("generated", "approved", "captcha_pending")


@dataclass
class Counters:
    found: int = 0
    filtered: int = 0


def iter_candidates(hh: HHClient, cfg: dict) -> Iterator[dict]:
    """Сырые вакансии: рекомендации под резюме, затем доп. запросы из config.yaml."""
    s, f = cfg.get("search", {}), cfg.get("filters", {})
    per_page, max_pages = int(s.get("per_page", 20)), int(s.get("max_pages", 5))
    seen: set[str] = set()

    def pages(fetch) -> Iterator[dict]:
        for page in range(max_pages):
            try:
                data = fetch(page)
            except HHError as e:
                log.error("Ошибка поиска (стр. %d): %s", page, e)
                return
            for item in data.get("items", []):
                if item["id"] not in seen:
                    seen.add(item["id"])
                    yield item
            if page + 1 >= data.get("pages", 0):
                return

    if hh.has_token and cfg.get("resume_id"):
        yield from pages(lambda p: hh.similar_vacancies(cfg["resume_id"], p, per_page))
    elif not s.get("queries"):
        log.error("Без токена рекомендации недоступны: задайте search.queries в config.yaml")
    areas = [a for a in f.get("areas") or [] if str(a).isdigit()]
    for q in s.get("queries") or []:
        yield from pages(lambda p, q=q: hh.search_vacancies(
            q, area=areas[0] if areas else None, salary=f.get("min_salary") or None, page=p, per_page=per_page))


def leftover_items(storage: Storage, limit: int,
                   statuses: tuple[str, ...] = RESUME_STATUSES) -> list[BatchItem]:
    """Незавершённые заявки прошлых запусков (идемпотентность, раздел 13)."""
    items = []
    for app in storage.pending_applications(statuses, limit):
        v = storage.get_vacancy(app.vacancy_id)
        if v and not storage.is_applied_sent(v.id):
            note = "возобновлено с прошлого запуска" + (" (ждала капчи)" if app.status == "captcha_pending" else "")
            items.append(BatchItem(v, app, note=note))
    return items


def prepare_batch(hh: HHClient, claude: ClaudeClient, storage: Storage, cfg: dict,
                  resume_text: str, size: int, counters: Counters,
                  offline: bool = False) -> list[BatchItem]:
    # в оффлайне одобренные уже лежат в outbox и в батч не возвращаются
    items = leftover_items(storage, size, ("generated",) if offline else RESUME_STATUSES)
    if items:
        log.info("Возобновляю %d незавершённых заявок", len(items))
    have = {it.vacancy.id for it in items}
    resume_id = str(cfg.get("resume_id") or "offline")

    for raw in iter_candidates(hh, cfg):
        if len(items) >= size:
            break
        vid = str(raw["id"])
        if vid in have:
            continue
        counters.found += 1
        if storage.is_applied(vid) or "got_response" in (raw.get("relations") or []):
            log.info("Дубликат, пропуск: %s", vid)
            counters.filtered += 1
            continue
        try:
            v = Vacancy.from_api(hh.get_vacancy(vid))
        except HHError as e:
            log.error("Не удалось загрузить вакансию %s: %s", vid, e)
            continue
        res = check_vacancy(v, cfg.get("filters", {}))
        if not res.passed:
            log.info("Отфильтровано %s «%s»: %s", vid, v.title, res.reason)
            counters.filtered += 1
            continue
        storage.save_vacancy(v)
        note = res.notes
        try:
            letter = claude.generate_letter(resume_text, v)
        except LetterError as e:
            letter, note = "", f"{note}; ошибка генерации письма: {e}".strip("; ")
            log.error("Генерация письма для %s не удалась: %s", vid, e)
        app: Application = storage.create_application(vid, resume_id, letter)
        items.append(BatchItem(v, app, note=note))
        have.add(vid)
        log.info("Подготовлено: %s «%s»", vid, v.title)
    return items
