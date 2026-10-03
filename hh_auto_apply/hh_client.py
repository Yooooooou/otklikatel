"""Клиент HH API: OAuth2, поиск вакансий, резюме, отклики (negotiations)."""
from __future__ import annotations

import logging
import os
import time
import webbrowser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
from dotenv import set_key

from .models import strip_html

log = logging.getLogger(__name__)

API = "https://api.hh.ru"
AUTH_URL = "https://hh.ru/oauth/authorize"
TOKEN_URL = f"{API}/token"
MAX_RETRIES = 3


class HHError(Exception):
    def __init__(self, status: int, message: str, errors: list[dict] | None = None):
        super().__init__(message)
        self.status, self.errors = status, errors or []


class CaptchaRequired(HHError):
    def __init__(self, status: int, message: str, url: str = ""):
        super().__init__(status, message)
        self.url = url


class AuthError(Exception):
    """Аварийная ошибка авторизации (например, истёк refresh_token)."""


def _is_captcha(errors: list[dict]) -> bool:
    return any("capcha" in str(e.get("value", "")).lower() or "captcha" in str(e.get("value", "")).lower()
               or "capcha" in str(e.get("type", "")).lower() or "captcha" in str(e.get("type", "")).lower()
               for e in errors)


class HHClient:
    def __init__(self, env_path: str | Path = ".env"):
        self.env_path = Path(env_path)
        self.client_id = os.environ.get("HH_CLIENT_ID", "")
        self.client_secret = os.environ.get("HH_CLIENT_SECRET", "")
        self.redirect_uri = os.environ.get("HH_REDIRECT_URI", "")
        self.access_token = os.environ.get("HH_ACCESS_TOKEN", "")
        self.refresh_token = os.environ.get("HH_REFRESH_TOKEN", "")
        ua = os.environ.get("HH_USER_AGENT", "hh-auto-apply/1.0")
        self.http = httpx.Client(timeout=30, headers={"User-Agent": ua, "HH-User-Agent": ua})

    # --- OAuth ------------------------------------------------------------
    def authorize_interactive(self) -> None:
        if not (self.client_id and self.client_secret):
            raise AuthError("Заполните HH_CLIENT_ID и HH_CLIENT_SECRET в .env")
        params = {"response_type": "code", "client_id": self.client_id}
        if self.redirect_uri:
            params["redirect_uri"] = self.redirect_uri
        url = str(httpx.URL(AUTH_URL, params=params))
        print(f"Откройте ссылку и разрешите доступ:\n{url}")
        try:
            webbrowser.open(url)
        except Exception:
            pass
        raw = input("Вставьте code или полный URL, на который вас перенаправило: ").strip()
        code = parse_qs(urlparse(raw).query).get("code", [raw])[0]
        data = {"grant_type": "authorization_code", "client_id": self.client_id,
                "client_secret": self.client_secret, "code": code}
        if self.redirect_uri:
            data["redirect_uri"] = self.redirect_uri
        self._token_request(data)
        log.info("Авторизация HH выполнена, токены сохранены")

    def _token_request(self, data: dict) -> None:
        r = self.http.post(TOKEN_URL, data=data)
        if r.status_code != 200:
            raise AuthError(f"HH не выдал токен ({r.status_code}): {r.text[:200]}")
        j = r.json()
        self.access_token, self.refresh_token = j["access_token"], j["refresh_token"]
        os.environ["HH_ACCESS_TOKEN"], os.environ["HH_REFRESH_TOKEN"] = self.access_token, self.refresh_token
        self.env_path.touch(exist_ok=True)
        set_key(str(self.env_path), "HH_ACCESS_TOKEN", self.access_token)
        set_key(str(self.env_path), "HH_REFRESH_TOKEN", self.refresh_token)

    def refresh(self) -> None:
        if not self.refresh_token:
            raise AuthError("Нет refresh_token. Выполните: python main.py auth")
        log.info("Обновляю access_token")
        try:
            self._token_request({"grant_type": "refresh_token", "refresh_token": self.refresh_token,
                                 "client_id": self.client_id, "client_secret": self.client_secret})
        except AuthError as e:
            raise AuthError(f"Не удалось обновить токен: {e}. Выполните: python main.py auth") from e

    # --- запросы ----------------------------------------------------------
    def request(self, method: str, path: str, **kw: Any) -> Any:
        refreshed = False
        attempt = 0
        while True:
            headers = {"Authorization": f"Bearer {self.access_token}"} if self.access_token else {}
            try:
                r = self.http.request(method, f"{API}{path}", headers=headers, **kw)
            except httpx.TransportError as e:
                if attempt >= MAX_RETRIES:
                    raise HHError(0, f"Сеть недоступна: {e}") from e
                self._sleep(attempt, None); attempt += 1
                continue
            log.debug("HH %s %s -> %s", method, path, r.status_code)
            if r.status_code == 401 and not refreshed:
                self.refresh(); refreshed = True
                continue
            if r.status_code == 429 or r.status_code >= 500:
                if attempt >= MAX_RETRIES:
                    raise HHError(r.status_code, f"HH API: {r.status_code} после {MAX_RETRIES} повторов")
                self._sleep(attempt, r.headers.get("Retry-After")); attempt += 1
                continue
            if r.status_code >= 400:
                try:
                    body = r.json()
                except ValueError:
                    body = {}
                errors = body.get("errors") or []
                if _is_captcha(errors):
                    url = next((e.get("captcha_url") or e.get("capcha_url") or "" for e in errors), "")
                    raise CaptchaRequired(r.status_code, "Требуется капча", url)
                msg = body.get("description") or "; ".join(
                    f"{e.get('type')}:{e.get('value')}" for e in errors) or r.text[:200]
                raise HHError(r.status_code, f"HH API {r.status_code}: {msg}", errors)
            return r.json() if r.content else {}

    @staticmethod
    def _sleep(attempt: int, retry_after: str | None) -> None:
        delay = float(retry_after) if retry_after and retry_after.isdigit() else 2 ** (attempt + 1)
        log.warning("Повтор через %.0f c", delay)
        time.sleep(delay)

    # --- API --------------------------------------------------------------
    def my_resumes(self) -> list[dict]:
        return self.request("GET", "/resumes/mine").get("items", [])

    def get_resume(self, resume_id: str) -> dict:
        return self.request("GET", f"/resumes/{resume_id}")

    def similar_vacancies(self, resume_id: str, page: int = 0, per_page: int = 20) -> dict:
        return self.request("GET", f"/resumes/{resume_id}/similar_vacancies",
                            params={"page": page, "per_page": per_page})

    def search_vacancies(self, text: str, area: str | None = None, salary: int | None = None,
                         page: int = 0, per_page: int = 20) -> dict:
        params: dict[str, Any] = {"text": text, "page": page, "per_page": per_page}
        if area:
            params["area"] = area
        if salary:
            params.update(salary=salary, only_with_salary=False)
        return self.request("GET", "/vacancies", params=params)

    def get_vacancy(self, vacancy_id: str) -> dict:
        return self.request("GET", f"/vacancies/{vacancy_id}")

    def negotiate(self, vacancy_id: str, resume_id: str, message: str) -> dict:
        return self.request("POST", "/negotiations",
                            data={"vacancy_id": vacancy_id, "resume_id": resume_id, "message": message})


def resume_to_text(r: dict) -> str:
    """Плоский текст резюме для промпта."""
    out = [f"Должность: {r.get('title', '')}"]
    if r.get("skills"):
        out.append(f"О себе: {strip_html(r['skills'])}")
    if r.get("skill_set"):
        out.append("Ключевые навыки: " + ", ".join(r["skill_set"]))
    if r.get("total_experience"):
        out.append(f"Общий стаж: {r['total_experience'].get('months', 0) // 12} лет")
    for e in r.get("experience") or []:
        out.append(f"\nОпыт: {e.get('company', '')} — {e.get('position', '')} "
                   f"({e.get('start', '')} — {e.get('end') or 'по настоящее время'})\n"
                   f"{strip_html(e.get('description'))}")
    for ed in (r.get("education") or {}).get("primary", []):
        out.append(f"Образование: {ed.get('name', '')}, {ed.get('organization', '')} {ed.get('year', '')}")
    for lang in r.get("language") or []:
        out.append(f"Язык: {lang.get('name')} ({(lang.get('level') or {}).get('name', '')})")
    return "\n".join(out)
