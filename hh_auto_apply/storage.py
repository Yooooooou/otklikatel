from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from .models import Application, Vacancy

SCHEMA = """
CREATE TABLE IF NOT EXISTS vacancies (
    id TEXT PRIMARY KEY,
    title TEXT, employer_name TEXT, employer_id TEXT, salary TEXT, area TEXT, url TEXT,
    raw_description TEXT,
    has_questionnaire INTEGER DEFAULT 0, questionnaire_text TEXT, questionnaire_answer TEXT,
    found_at TEXT
);
CREATE TABLE IF NOT EXISTS applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vacancy_id TEXT NOT NULL REFERENCES vacancies(id),
    resume_id TEXT,
    cover_letter_text TEXT,
    status TEXT NOT NULL CHECK (status IN
        ('generated','approved','sent','error','captcha_pending','skipped')),
    error_message TEXT,
    created_at TEXT, sent_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_app_vacancy ON applications(vacancy_id);
CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT, finished_at TEXT,
    total_found INTEGER DEFAULT 0, total_filtered INTEGER DEFAULT 0,
    total_sent INTEGER DEFAULT 0, total_errors INTEGER DEFAULT 0
);
"""

# Вакансии с такими заявками повторно в батч не попадают (разделы 5 и 11 ТЗ)
BLOCKING_STATUSES = ("sent", "approved")


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Storage:
    def __init__(self, path: str = "data/hh_apply.db"):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.blocking: tuple[str, ...] = BLOCKING_STATUSES
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # --- вакансии -------------------------------------------------------
    def save_vacancy(self, v: Vacancy) -> None:
        self.conn.execute(
            """INSERT INTO vacancies (id,title,employer_name,employer_id,salary,area,url,
                   raw_description,has_questionnaire,questionnaire_text,questionnaire_answer,found_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET title=excluded.title, salary=excluded.salary,
                   raw_description=excluded.raw_description,
                   has_questionnaire=excluded.has_questionnaire,
                   questionnaire_text=excluded.questionnaire_text""",
            (v.id, v.title, v.employer_name, v.employer_id, v.salary, v.area, v.url,
             v.raw_description, int(v.has_questionnaire), v.questionnaire_text,
             v.questionnaire_answer, now()),
        )
        self.conn.commit()

    def set_questionnaire_answer(self, vacancy_id: str, answer: str) -> None:
        self.conn.execute("UPDATE vacancies SET questionnaire_answer=? WHERE id=?", (answer, vacancy_id))
        self.conn.commit()

    def get_vacancy_answer(self, vacancy_id: str) -> str:
        r = self.conn.execute("SELECT questionnaire_answer FROM vacancies WHERE id=?", (vacancy_id,)).fetchone()
        return (r["questionnaire_answer"] or "") if r else ""

    # --- дедупликация ---------------------------------------------------
    def is_applied(self, vacancy_id: str) -> bool:
        q = ",".join("?" * len(self.blocking))
        return self.conn.execute(
            f"SELECT 1 FROM applications WHERE vacancy_id=? AND status IN ({q}) LIMIT 1",
            (vacancy_id, *self.blocking),
        ).fetchone() is not None

    def is_applied_sent(self, vacancy_id: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM applications WHERE vacancy_id=? AND status='sent' LIMIT 1", (vacancy_id,)
        ).fetchone() is not None

    # --- заявки ---------------------------------------------------------
    @staticmethod
    def _app(r: sqlite3.Row) -> Application:
        return Application(
            id=r["id"], vacancy_id=r["vacancy_id"], resume_id=r["resume_id"],
            cover_letter_text=r["cover_letter_text"] or "", status=r["status"],
            error_message=r["error_message"], created_at=r["created_at"], sent_at=r["sent_at"],
        )

    def create_application(self, vacancy_id: str, resume_id: str, letter: str,
                           status: str = "generated", error: str | None = None) -> Application:
        cur = self.conn.execute(
            "INSERT INTO applications (vacancy_id,resume_id,cover_letter_text,status,error_message,created_at)"
            " VALUES (?,?,?,?,?,?)",
            (vacancy_id, resume_id, letter, status, error, now()),
        )
        self.conn.commit()
        return self.get_application(cur.lastrowid)

    def get_application(self, app_id: int) -> Application:
        return self._app(self.conn.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone())

    def latest_application(self, vacancy_id: str, statuses: tuple[str, ...]) -> Application | None:
        q = ",".join("?" * len(statuses))
        r = self.conn.execute(
            f"SELECT * FROM applications WHERE vacancy_id=? AND status IN ({q}) ORDER BY id DESC LIMIT 1",
            (vacancy_id, *statuses),
        ).fetchone()
        return self._app(r) if r else None

    def update_application(self, app_id: int, *, status: str | None = None,
                           letter: str | None = None, error: str | None = None) -> None:
        sets, args = [], []
        if status is not None:
            sets.append("status=?"); args.append(status)
            if status == "sent":
                sets.append("sent_at=?"); args.append(now())
        if letter is not None:
            sets.append("cover_letter_text=?"); args.append(letter)
        if status is not None or error is not None:
            sets.append("error_message=?"); args.append(error)
        if not sets:
            return
        self.conn.execute(f"UPDATE applications SET {','.join(sets)} WHERE id=?", (*args, app_id))
        self.conn.commit()

    def pending_applications(self, statuses: tuple[str, ...], limit: int | None = None) -> list[Application]:
        q = ",".join("?" * len(statuses))
        sql = f"SELECT * FROM applications WHERE status IN ({q}) ORDER BY id"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [self._app(r) for r in self.conn.execute(sql, statuses)]

    def approved_for_outbox(self) -> list[Application]:
        return self.pending_applications(("approved",))

    def get_vacancy(self, vacancy_id: str) -> Vacancy | None:
        r = self.conn.execute("SELECT * FROM vacancies WHERE id=?", (vacancy_id,)).fetchone()
        if not r:
            return None
        return Vacancy(
            id=r["id"], title=r["title"], employer_name=r["employer_name"] or "",
            employer_id=r["employer_id"] or "", salary=r["salary"] or "", area=r["area"] or "",
            url=r["url"], raw_description=r["raw_description"] or "",
            has_questionnaire=bool(r["has_questionnaire"]),
            questionnaire_text=r["questionnaire_text"] or "",
            questionnaire_answer=r["questionnaire_answer"] or "",
        )

    # --- батчи и статистика ----------------------------------------------
    def start_batch(self) -> int:
        cur = self.conn.execute("INSERT INTO batches (started_at) VALUES (?)", (now(),))
        self.conn.commit()
        return cur.lastrowid

    def finish_batch(self, batch_id: int, found: int, filtered: int, sent: int, errors: int) -> None:
        self.conn.execute(
            "UPDATE batches SET finished_at=?, total_found=?, total_filtered=?, total_sent=?, total_errors=? WHERE id=?",
            (now(), found, filtered, sent, errors, batch_id),
        )
        self.conn.commit()

    def history(self, days: int) -> list[sqlite3.Row]:
        since = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        return list(self.conn.execute(
            """SELECT a.*, v.title, v.employer_name, v.url FROM applications a
               JOIN vacancies v ON v.id = a.vacancy_id
               WHERE a.created_at >= ? ORDER BY a.id DESC""", (since,)))

    def stats(self) -> dict[str, int]:
        b = self.conn.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(total_found),0) f, COALESCE(SUM(total_filtered),0) fl FROM batches"
        ).fetchone()
        out = {"runs": b["n"], "found": b["f"], "filtered": b["fl"]}
        for r in self.conn.execute("SELECT status, COUNT(*) n FROM applications GROUP BY status"):
            out[r["status"]] = r["n"]
        return out
