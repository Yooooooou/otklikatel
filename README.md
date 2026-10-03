# hh.kz Auto-Apply Bot

Локальный CLI: ищет вакансии на hh.kz через официальный HH API, фильтрует по стоп-словам/гео/зарплате,
генерирует письма через Claude API (на основе вашего резюме с hh.kz), показывает батч на проверку и
отправляет только одобренные отклики через `POST /negotiations`. На капче — останавливается и ждёт вас.

## Установка
```bash
python -m venv .venv && source .venv/bin/activate   # Python 3.11+
pip install -r requirements.txt
cp .env.example .env     # впишите HH_CLIENT_ID, HH_CLIENT_SECRET, ANTHROPIC_API_KEY
```
1. Зарегистрируйте приложение на dev.hh.ru, получите client_id/secret.
2. `python main.py auth` — OAuth (токены сохраняются в `.env` и обновляются автоматически).
3. `python main.py resumes` — найдите `resume_id` и впишите в `config.yaml`.

## Команды
```bash
python main.py run                  # поиск + письма + батч на проверку + отправка
python main.py run --batch-size 15
python main.py run --dry-run        # без отправки
python main.py run --prepare-only   # для cron: только поиск и генерация; просмотр — обычным `run`
python main.py history --days 7 [--full]
python main.py stats
python main.py retry-captcha
```
В батче: `<№> a|e|s|r|v` (одобрить / править / пропустить / новое письмо / вакансия), `all`, `list`, `done`, `q`.
Отправка — только после итогового подтверждения `y`.

## Заметки по реализации
- Незавершённые заявки (`generated`/`approved`/`captcha_pending`) подхватываются следующим запуском,
  повторно письма не генерируются, дублей отправки нет (проверка по SQLite).
- Анкетные вопросы: HH API не отдаёт тексты вопросов теста — вакансия помечается блоком «АНКЕТА»,
  ответ вы пишете сами (`e`); он добавляется в конец сообщения отклика. Без ответа → `skip`.
  Вопросы/тесты, которые API принять не может, фиксируются как `error` (раздел 9 ТЗ, открытый вопрос).
- Пропущенные вами (`skipped`) вакансии больше не показываются; отключить: `batch.hide_skipped: false`.
- Секреты не попадают в логи (фильтр) и в git (`.env` в `.gitignore`).
- Тесты: `python -m pytest`.

> Эндпоинты и формат ошибки капчи (`capcha_required`) реализованы по документации HH API; проверьте на
> реальном аккаунте (этап 9 ТЗ) — особенно `similar_vacancies` и вопросы анкеты.
