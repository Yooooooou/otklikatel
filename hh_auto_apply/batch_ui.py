"""Рендер батча в терминале и интерактивная проверка (разделы 7–8 ТЗ)."""
from __future__ import annotations

from typing import Callable

import click

from .models import BatchItem
from .storage import Storage

HELP = ("Команды: <№> a — одобрить, <№> e — править, <№> s — пропустить, "
        "<№> r — новое письмо, <№> v — полный текст вакансии.\n"
        "          all — одобрить все (кроме анкетных без ответа), list — показать батч, "
        "done — завершить просмотр, q — выйти без отправки.")

STATUS_MARK = {"pending": "·", "approve": "✔", "skip": "✘"}


def render_item(n: int, it: BatchItem, total: int) -> str:
    v = it.vacancy
    lines = [
        "─" * 78,
        f"[{n}/{total}] {STATUS_MARK[it.decision]} {v.title} — {v.employer_name}",
        f"    Зарплата: {v.salary or 'не указана'} | Регион: {v.area}",
        f"    {v.url}",
    ]
    if it.note:
        lines.append(f"    ⚠ {it.note}")
    letter = it.application.cover_letter_text
    lines.append("    Письмо:" if letter else "    Письмо: <ПУСТО — генерация не удалась, используйте r или e>")
    if letter:
        lines += ["      " + ln for ln in letter.splitlines()]
    if v.has_questionnaire:
        lines += ["    ┌ АНКЕТА РАБОТОДАТЕЛЯ ─────────────",
                  "    │ " + (v.questionnaire_text or "Есть вопросы работодателя"),
                  "    │ Ваш ответ: " + (v.questionnaire_answer or "<не заполнен — [e]dit>"),
                  "    └──────────────────────────────────"]
    return "\n".join(lines)


def render_batch(items: list[BatchItem]) -> None:
    click.echo(f"\n===== БАТЧ: {len(items)} вакансий =====")
    for i, it in enumerate(items, 1):
        click.echo(render_item(i, it, len(items)))
    click.echo("─" * 78)


def _ready(it: BatchItem) -> str | None:
    """Причина, по которой вакансию нельзя одобрить, либо None."""
    if not it.application.cover_letter_text.strip():
        return "пустое письмо"
    if it.vacancy.has_questionnaire and not it.vacancy.questionnaire_answer.strip():
        return "не заполнен ответ на анкетные вопросы"
    return None


def _edit(it: BatchItem, storage: Storage) -> None:
    target = "letter"
    if it.vacancy.has_questionnaire:
        target = click.prompt("Что править? [l]etter / [a]nswer", type=click.Choice(["l", "a"]), default="l")
        target = "letter" if target == "l" else "answer"
    if target == "letter":
        new = click.edit(it.application.cover_letter_text)
        if new is not None:
            it.application.cover_letter_text = new.strip()
            storage.update_application(it.application.id, letter=it.application.cover_letter_text)
    else:
        new = click.edit(it.vacancy.questionnaire_answer or "")
        if new is not None:
            it.vacancy.questionnaire_answer = new.strip()
            storage.set_questionnaire_answer(it.vacancy.id, it.vacancy.questionnaire_answer)


def review(items: list[BatchItem], storage: Storage,
           regenerate: Callable[[BatchItem], str],
           confirm_text: str = "Отправить одобренные отклики?",
           auto_approve: bool = False) -> list[BatchItem] | None:
    """Интерактивный просмотр. Возвращает одобренные элементы или None, если пользователь вышел/не подтвердил."""
    render_batch(items)
    if auto_approve:
        for it in items:
            if _ready(it) is None:
                it.decision = "approve"
        raw_first = "done"
    else:
        click.echo(HELP)
        raw_first = None
    while True:
        if raw_first:
            raw, raw_first = raw_first, None
        else:
            raw = click.prompt("batch>", default="", show_default=False).strip().lower()
        if not raw:
            continue
        if raw == "q":
            return None
        if raw == "list":
            render_batch(items); continue
        if raw == "all":
            for it in items:
                if _ready(it) is None:
                    it.decision = "approve"
            continue
        if raw == "done":
            approved = _finalize(items, storage)
            click.echo(f"\nОдобрено: {len(approved)}, пропущено: {len(items) - len(approved)}")
            if approved and click.confirm(confirm_text, default=False):
                return approved
            if not approved:
                return []
            click.echo("Отправка отменена; можно продолжить редактирование или q для выхода.")
            for it in items:   # откатываем временное одобрение
                if it.decision == "approve":
                    storage.update_application(it.application.id, status="generated")
            continue
        parts = raw.split()
        if len(parts) != 2 or not parts[0].isdigit() or parts[1] not in {"a", "e", "s", "r", "v"}:
            click.echo(HELP); continue
        n, cmd = int(parts[0]), parts[1]
        if not 1 <= n <= len(items):
            click.echo("Нет такого номера"); continue
        it = items[n - 1]
        if cmd == "a":
            why = _ready(it)
            if why:
                click.echo(f"⚠ Нельзя одобрить: {why}")
            else:
                it.decision = "approve"
        elif cmd == "s":
            it.decision = "skip"
        elif cmd == "e":
            _edit(it, storage)
            it.decision = "pending"
        elif cmd == "r":
            try:
                new = regenerate(it)
                it.application.cover_letter_text = new
                storage.update_application(it.application.id, letter=new)
                it.decision, it.note = "pending", ""
            except Exception as e:  # noqa: BLE001 — показываем пользователю
                click.echo(f"⚠ Не удалось сгенерировать: {e}")
        elif cmd == "v":
            click.echo(f"\n{it.vacancy.title}\n{it.vacancy.url}\n\n{it.vacancy.raw_description}\n")
            continue
        click.echo(render_item(n, it, len(items)))


def _finalize(items: list[BatchItem], storage: Storage) -> list[BatchItem]:
    """Применяет итоговые решения. Одобренные без обязательного ответа → skip с предупреждением."""
    import logging
    log = logging.getLogger(__name__)
    approved = []
    for it in items:
        reason = _ready(it) if it.decision == "approve" else None
        if it.decision == "approve" and reason:
            log.warning("Вакансия %s пропущена: %s", it.vacancy.id, reason)
            click.echo(f"⚠ {it.vacancy.title}: {reason} → skip")
            it.decision = "skip"
        if it.decision == "approve":
            storage.update_application(it.application.id, status="approved")
            approved.append(it)
        else:
            storage.update_application(it.application.id, status="skipped")
            it.decision = "skip"
    return approved
