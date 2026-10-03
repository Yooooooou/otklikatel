"""Генерация сопроводительных писем через Claude API (раздел 6 ТЗ)."""
from __future__ import annotations

import logging
import time

import anthropic

from .models import Vacancy

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """Ты помогаешь соискателю писать сопроводительные письма к вакансиям на hh.kz.
Правила:
- Пиши на языке вакансии (обычно русский), кратко и по-деловому, 3–5 абзацев, без шаблонных фраз вроде «с большим интересом».
- Свяжи конкретный опыт соискателя из резюме с конкретными требованиями этой вакансии.
- Используй ТОЛЬКО факты из резюме. Не выдумывай опыт, навыки, цифры, компании и образование, которых там нет.
  Если требования вакансии резюме не покрывает — просто не упоминай их.
- Не добавляй тему письма, плейсхолдеры в квадратных скобках и комментарии от себя.
- Выведи только текст письма."""


class LetterError(Exception):
    pass


def build_user_prompt(resume_text: str, v: Vacancy, target_chars: int) -> str:
    skills = ", ".join(v.key_skills)
    return (f"<resume>\n{resume_text}\n</resume>\n\n<vacancy>\n"
            f"Название: {v.title}\nРаботодатель: {v.employer_name}\n"
            f"Ключевые навыки: {skills}\n\nОписание:\n{v.raw_description}\n</vacancy>\n\n"
            f"Напиши сопроводительное письмо (ориентир — около {target_chars} символов).")


class ClaudeClient:
    def __init__(self, model: str, max_chars: int = 10000, target_chars: int = 1500,
                 client: anthropic.Anthropic | None = None, retries: int = 2, retry_delay: float = 3.0):
        self.client = client or anthropic.Anthropic(max_retries=3, timeout=90)
        self.model, self.max_chars, self.target_chars = model, max_chars, target_chars
        self.retries, self.retry_delay = retries, retry_delay

    def _call(self, messages: list[dict]) -> str:
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                resp = self.client.messages.create(
                    model=self.model, max_tokens=2000, system=SYSTEM_PROMPT, messages=messages)
                return "".join(b.text for b in resp.content if b.type == "text").strip()
            except anthropic.APIError as e:
                last = e
                log.warning("Claude API ошибка (попытка %d): %s", attempt + 1, type(e).__name__)
                if attempt < self.retries:
                    time.sleep(self.retry_delay * (attempt + 1))
        raise LetterError(f"Claude API недоступен: {type(last).__name__}")

    def generate_letter(self, resume_text: str, v: Vacancy, variant: bool = False) -> str:
        prompt = build_user_prompt(resume_text, v, self.target_chars)
        if variant:
            prompt += "\n\nСделай другой вариант: иная структура и другие акценты, чем в типичном письме."
        messages = [{"role": "user", "content": prompt}]
        text = self._call(messages)
        if len(text) > self.max_chars:
            log.info("Письмо %d симв. > лимита %d, прошу сократить", len(text), self.max_chars)
            messages += [{"role": "assistant", "content": text},
                         {"role": "user", "content":
                          f"Слишком длинно. Сократи письмо до {int(self.max_chars * 0.8)} символов, сохранив суть."}]
            text = self._call(messages)
            if len(text) > self.max_chars:
                text = text[: self.max_chars].rsplit("\n", 1)[0].rstrip()
        if not text:
            raise LetterError("Claude вернул пустой ответ")
        return text
