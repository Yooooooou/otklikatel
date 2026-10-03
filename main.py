#!/usr/bin/env python3
"""hh.kz Auto-Apply Bot — точка входа CLI."""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from datetime import datetime

import click

from hh_auto_apply import batch_ui, outbox
from hh_auto_apply.apply import send_batch
from hh_auto_apply.claude_client import ClaudeClient, LetterError
from hh_auto_apply.config import load_config, setup_logging
from hh_auto_apply.hh_client import AuthError, HHClient, HHError, resume_to_text
from hh_auto_apply.models import BatchItem
from hh_auto_apply.pipeline import Counters, leftover_items, prepare_batch
from hh_auto_apply.storage import Storage

log = logging.getLogger("main")


def _setup(config_path: str):
    cfg = load_config(config_path)
    setup_logging(cfg)
    storage = Storage(cfg.get("database_path", "data/hh_apply.db"))
    if cfg.get("batch", {}).get("hide_skipped", True):
        storage.blocking = ("sent", "approved", "skipped")
    return cfg, storage


def _resume_text(hh: HHClient, cfg: dict) -> str:
    """Онлайн — резюме из HH API; без токена — из локального файла (resume_file)."""
    if hh.has_token:
        return resume_to_text(hh.get_resume(_need_resume(cfg)))
    path = Path(cfg.get("resume_file", "data/resume.md"))
    if not path.exists() or not path.read_text(encoding="utf-8").strip():
        raise click.ClickException(
            f"HH-токена нет (оффлайн-режим). Вставьте текст вашего резюме с hh.kz в файл {path}")
    return path.read_text(encoding="utf-8")


def _need_resume(cfg: dict) -> str:
    if not cfg.get("resume_id"):
        raise click.ClickException("Укажите resume_id в config.yaml (список резюме: python main.py resumes)")
    return str(cfg["resume_id"])


@click.group(invoke_without_command=True)
@click.option("--config", "config_path", default="config.yaml", show_default=True)
@click.pass_context
def cli(ctx: click.Context, config_path: str) -> None:
    """Автоотклики на hh.kz с письмами от Claude и ручным подтверждением батча."""
    ctx.obj = config_path
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@cli.command()
@click.pass_obj
def auth(config_path: str) -> None:
    """Первичная OAuth-авторизация в hh.kz."""
    cfg = load_config(config_path)
    setup_logging(cfg)
    try:
        HHClient().authorize_interactive()
    except AuthError as e:
        raise click.ClickException(str(e))
    click.echo("Готово: токены сохранены в .env")


@cli.command()
@click.pass_obj
def resumes(config_path: str) -> None:
    """Показать ваши резюме (чтобы выбрать resume_id)."""
    cfg = load_config(config_path)
    setup_logging(cfg)
    try:
        for r in HHClient().my_resumes():
            click.echo(f"{r['id']}  {r.get('title', '')}")
    except (AuthError, HHError) as e:
        raise click.ClickException(str(e))


@cli.command()
@click.option("--batch-size", type=int, help="Размер батча (по умолчанию из config.yaml)")
@click.option("--dry-run", is_flag=True, help="Найти и сгенерировать письма, ничего не отправляя")
@click.option("--prepare-only", is_flag=True,
              help="Только поиск и генерация без интерактива (для cron); просмотр — следующим `run`")
@click.pass_obj
def run(config_path: str, batch_size: int | None, dry_run: bool, prepare_only: bool) -> None:
    """Поиск → фильтры → письма → батч на проверку → отправка."""
    cfg, storage = _setup(config_path)
    hh = HHClient()
    offline = not hh.has_token
    resume_id = str(cfg.get("resume_id") or "") if offline else _need_resume(cfg)
    if offline:
        click.echo("ℹ Оффлайн-режим: нет HH-токена. Письма сохранятся в data/outbox, отправка вручную.")
    size = batch_size or int(cfg.get("batch", {}).get("size", 10))
    interactive = not (dry_run or prepare_only)
    if interactive and not sys.stdin.isatty():
        raise click.ClickException("Интерактивный батч требует терминала. Для cron используйте --prepare-only.")

    cl = cfg.get("claude", {})
    counters = Counters()
    batch_id = storage.start_batch()
    sent = errors = 0
    items: list[BatchItem] = []
    try:
        claude = ClaudeClient(cl.get("model", "claude-sonnet-4-5"), int(cl.get("max_letter_chars", 10000)),
                              int(cl.get("letter_target_chars", 1500)))
        log.info("Загружаю резюме %s", resume_id)
        resume_text = _resume_text(hh, cfg)
        items = prepare_batch(hh, claude, storage, cfg, resume_text, size, counters, offline)

        def regenerate(it: BatchItem) -> str:
            return claude.generate_letter(resume_text, it.vacancy, variant=True)

        if not items:
            click.echo("Подходящих новых вакансий не найдено.")
        elif not interactive:
            batch_ui.render_batch(items)
            click.echo("\nDry-run: ничего не отправлено." if dry_run else
                       "\nПодготовлено. Для просмотра и отправки запустите: python main.py run")
        else:
            approved = batch_ui.review(
                items, storage, regenerate,
                "Сохранить одобренные письма в outbox?" if offline else "Отправить одобренные отклики?")
            if approved and offline:
                outbox.save(approved)
            elif approved:
                res = send_batch(hh, storage, approved, resume_id,
                                 delay=float(cfg.get("batch", {}).get("delay_seconds", 3)))
                sent, errors = res.sent, res.errors
                if res.captchas:
                    click.echo(f"Капч за запуск: {res.captchas}")
            elif approved is None:
                click.echo("Выход без отправки. Заявки сохранены и будут предложены при следующем запуске.")
    except (AuthError, HHError) as e:
        log.error("Аварийная ошибка: %s", e)
        raise click.ClickException(str(e))
    except KeyboardInterrupt:
        click.echo("\nПрервано. Незавершённый батч сохранён в базе.")
    finally:
        storage.finish_batch(batch_id, counters.found, counters.filtered, sent, errors)
    click.echo(f"\nСводка: найдено {counters.found}, отфильтровано {counters.filtered}, "
               f"в батче {len(items)}, отправлено {sent}, ошибок/капч {errors}")


@cli.command()
@click.option("--days", default=7, show_default=True)
@click.option("--full", is_flag=True, help="Показывать полный текст писем")
@click.pass_obj
def history(config_path: str, days: int, full: bool) -> None:
    """История откликов за N дней."""
    _, storage = _setup(config_path)
    rows = storage.history(days)
    if not rows:
        click.echo("Пусто."); return
    for r in rows:
        click.echo(f"{r['created_at']}  [{r['status']:<15}] {r['title']} — {r['employer_name']}  {r['url']}")
        if r["error_message"]:
            click.echo(f"    ошибка: {r['error_message']}")
        if full and r["cover_letter_text"]:
            click.echo("    " + r["cover_letter_text"].replace("\n", "\n    "))
    c: dict[str, int] = {}
    for r in rows:
        c[r["status"]] = c.get(r["status"], 0) + 1
    click.echo(f"\nИтого за {days} дн.: " + ", ".join(f"{k}={v}" for k, v in sorted(c.items())))


@cli.command()
@click.pass_obj
def stats(config_path: str) -> None:
    """Статистика за всё время."""
    _, storage = _setup(config_path)
    s = storage.stats()
    click.echo(f"Запусков: {s['runs']}\nНайдено: {s['found']}\nОтфильтровано: {s['filtered']}")
    for k in ("generated", "approved", "sent", "skipped", "error", "captcha_pending"):
        click.echo(f"{k}: {s.get(k, 0)}")


@cli.command("mark-sent")
@click.argument("vacancy_ids", nargs=-1)
@click.option("--all", "all_", is_flag=True, help="Отметить все одобренные как отправленные")
@click.pass_obj
def mark_sent(config_path: str, vacancy_ids: tuple[str, ...], all_: bool) -> None:
    """Оффлайн: отметить вручную отправленные отклики (чтобы они не предлагались снова)."""
    _, storage = _setup(config_path)
    n = outbox.mark_sent(storage, vacancy_ids, all_)
    click.echo(f"Отмечено отправленными: {n}")


@cli.command("outbox")
@click.pass_obj
def show_outbox(config_path: str) -> None:
    """Оффлайн: показать одобренные письма, ожидающие ручной отправки."""
    _, storage = _setup(config_path)
    items = outbox.show_pending(storage)
    if not items:
        click.echo("Outbox пуст."); return
    outbox.save(items)


@cli.command("retry-captcha")
@click.pass_obj
def retry_captcha(config_path: str) -> None:
    """Показать вакансии captcha_pending и доотправить их."""
    cfg, storage = _setup(config_path)
    resume_id = _need_resume(cfg)
    if not HHClient().has_token:
        raise click.ClickException("Нужен HH-токен (python main.py auth).")
    items = [it for it in leftover_items(storage, 1000) if it.application.status == "captcha_pending"]
    if not items:
        click.echo("Нет вакансий в статусе captcha_pending."); return
    for it in items:
        click.echo(f"• {it.vacancy.title} — {it.vacancy.employer_name}  {it.vacancy.url}")
    click.echo("\nСначала решите капчу в браузере (откройте любую вакансию на hh.kz).")
    if not click.confirm("Доотправить эти отклики сейчас?", default=False):
        return
    try:
        res = send_batch(HHClient(), storage, items, resume_id,
                         delay=float(cfg.get("batch", {}).get("delay_seconds", 3)))
    except (AuthError, HHError) as e:
        raise click.ClickException(str(e))
    click.echo(f"Отправлено {res.sent}, ошибок {res.errors}, капч {res.captchas}")


if __name__ == "__main__":
    cli()
