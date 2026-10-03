"""Оффлайн-режим (без HH-токена): готовые отклики сохраняются в data/outbox, отправка — вручную."""
from __future__ import annotations

import logging
from pathlib import Path

import click

from .apply import build_message
from .models import BatchItem
from .storage import Storage

log = logging.getLogger(__name__)
OUTBOX = Path("data/outbox")


def save(items: list[BatchItem]) -> None:
    OUTBOX.mkdir(parents=True, exist_ok=True)
    for it in items:
        path = OUTBOX / f"{it.vacancy.id}.txt"
        path.write_text(f"{it.vacancy.title} — {it.vacancy.employer_name}\n{it.vacancy.url}\n"
                        f"{'-' * 60}\n{build_message(it)}\n", encoding="utf-8")
        click.echo(f"\n{'═' * 78}\n{it.vacancy.title} — {it.vacancy.employer_name}\n"
                   f"Откликнуться: {it.vacancy.url}\n{'─' * 78}\n{build_message(it)}")
        log.info("Отклик сохранён в outbox: %s", path)
    click.echo(f"\nФайлы с письмами: {OUTBOX}/  (скопируйте письмо, откликнитесь на hh.kz, затем "
               f"`python main.py mark-sent`)")


def show_pending(storage: Storage) -> list[BatchItem]:
    items = []
    for app in storage.approved_for_outbox():
        v = storage.get_vacancy(app.vacancy_id)
        if v:
            items.append(BatchItem(v, app))
    return items


def mark_sent(storage: Storage, ids: tuple[str, ...], all_: bool) -> int:
    items = show_pending(storage)
    if not items:
        click.echo("Нет одобренных, но не отмеченных откликов."); return 0
    chosen = []
    for it in items:
        if all_ or it.vacancy.id in ids:
            chosen.append(it)
        elif not ids and click.confirm(f"Отправлен? {it.vacancy.title} — {it.vacancy.employer_name}", default=False):
            chosen.append(it)
    for it in chosen:
        storage.update_application(it.application.id, status="sent")
        (OUTBOX / f"{it.vacancy.id}.txt").unlink(missing_ok=True)
    return len(chosen)
