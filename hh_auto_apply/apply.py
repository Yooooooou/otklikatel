"""Последовательная отправка одобренных откликов и обработка капчи (разделы 9–10 ТЗ)."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable

import click

from .hh_client import CaptchaRequired, HHClient, HHError
from .models import BatchItem
from .storage import Storage

log = logging.getLogger(__name__)


@dataclass
class SendResult:
    sent: int = 0
    errors: int = 0
    captchas: int = 0
    remaining: list[str] = field(default_factory=list)   # vacancy_id, не отправленные из-за прерывания


def build_message(it: BatchItem) -> str:
    msg = it.application.cover_letter_text.strip()
    ans = it.vacancy.questionnaire_answer.strip()
    if it.vacancy.has_questionnaire and ans:
        msg += f"\n\nОтветы на вопросы работодателя:\n{ans}"
    return msg


def wait_captcha(it: BatchItem, url: str) -> bool:
    """Ждёт, пока пользователь решит капчу. True — продолжать, False — прервать цикл."""
    click.echo(f"\n🛑 КАПЧА на вакансии «{it.vacancy.title}» ({it.vacancy.employer_name}).")
    click.echo(f"   Откройте и решите проверку в браузере: {url or it.vacancy.url}")
    ans = click.prompt("Нажмите Enter (или введите continue), когда готовы продолжить; q — остановиться",
                       default="", show_default=False).strip().lower()
    return ans != "q"


def send_batch(client: HHClient, storage: Storage, items: list[BatchItem], resume_id: str,
               delay: float = 3.0, on_captcha: Callable[[BatchItem, str], bool] = wait_captcha) -> SendResult:
    res = SendResult()
    for idx, it in enumerate(items):
        app = it.application
        if storage.is_applied_sent(it.vacancy.id):   # защита от дублей при перезапуске
            log.info("Вакансия %s уже отправлена — пропуск", it.vacancy.id)
            continue
        while True:
            try:
                client.negotiate(it.vacancy.id, resume_id, build_message(it))
                storage.update_application(app.id, status="sent")
                res.sent += 1
                click.echo(f"✔ Отправлено: {it.vacancy.title} — {it.vacancy.employer_name}")
                log.info("Отклик отправлен: vacancy=%s", it.vacancy.id)
                break
            except CaptchaRequired as e:
                res.captchas += 1
                storage.update_application(app.id, status="captcha_pending", error=f"captcha {e.url}")
                log.warning("Капча на вакансии %s", it.vacancy.id)
                if not on_captcha(it, e.url):
                    res.remaining = [x.vacancy.id for x in items[idx:]]
                    click.echo("Остановлено. Вакансии останутся в статусе captcha_pending/approved "
                               "и будут предложены при следующем запуске.")
                    return res
                storage.update_application(app.id, status="approved")   # повтор той же вакансии
            except HHError as e:
                res.errors += 1
                storage.update_application(app.id, status="error", error=str(e)[:500])
                click.echo(f"✘ Ошибка: {it.vacancy.title}: {e}")
                log.error("Ошибка отправки vacancy=%s: %s", it.vacancy.id, e)
                break
        if idx < len(items) - 1:
            time.sleep(delay)
    return res
