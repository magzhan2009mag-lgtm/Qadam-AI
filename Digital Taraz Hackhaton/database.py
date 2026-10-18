"""
Qadam AI — слой данных на SQLite (встроен в Python, отдельный сервер не нужен).

Файл базы: qadam.db рядом с app.py (путь можно изменить переменной QADAM_DB).
Бэкап = копия этого файла. Режим WAL и внешние ключи включены.

Таблицы:
    accounts            аккаунты бизнесов (профиль, меню/прайс, акции, хеш пароля)
    sessions            токены «остаться в системе» после обновления страницы
    orders              заказы/записи (с позициями чека)
    audits              ИИ-аудиты диалогов (полный результат в result_json)
    feedbacks           отзывы клиентов и ответы ИИ
    chat_messages       история чата ИИ-администратора (продажи)
    assistant_messages  история диалога с персональным ассистентом
    ai_reports          сохранённые ИИ-отчёты (вкладка «ИИ-анализ»)

Все данные привязаны к аккаунту (login) и удаляются вместе с ним (ON DELETE CASCADE).
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = Path(os.getenv("QADAM_DB") or Path(__file__).resolve().parent / "qadam.db")
SESSION_DAYS = 30
SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    login         TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    type          TEXT NOT NULL,
    address       TEXT NOT NULL DEFAULT '',
    catalog       TEXT NOT NULL DEFAULT '',
    rules         TEXT NOT NULL DEFAULT '',
    demo          INTEGER NOT NULL DEFAULT 0,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    login      TEXT NOT NULL REFERENCES accounts(login) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    login      TEXT NOT NULL REFERENCES accounts(login) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    detail     TEXT NOT NULL DEFAULT '',
    total      INTEGER NOT NULL DEFAULT 0,
    payment    TEXT NOT NULL DEFAULT '',
    items_json TEXT NOT NULL DEFAULT '[]',
    is_demo    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_orders_login_date ON orders(login, created_at);

CREATE TABLE IF NOT EXISTS audits (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    login       TEXT NOT NULL REFERENCES accounts(login) ON DELETE CASCADE,
    created_at  TEXT NOT NULL,
    qa_score    INTEGER NOT NULL DEFAULT 0,
    missed      INTEGER NOT NULL DEFAULT 0,
    result_json TEXT NOT NULL DEFAULT '{}',
    is_demo     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_audits_login_date ON audits(login, created_at);

CREATE TABLE IF NOT EXISTS feedbacks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    login      TEXT NOT NULL REFERENCES accounts(login) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    review     TEXT NOT NULL DEFAULT '',
    sentiment  TEXT NOT NULL DEFAULT 'НЕЙТРАЛЬНО',
    category   TEXT NOT NULL DEFAULT '',
    summary    TEXT NOT NULL DEFAULT '',
    reply      TEXT NOT NULL DEFAULT '',
    is_demo    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_feedbacks_login_date ON feedbacks(login, created_at);

CREATE TABLE IF NOT EXISTS chat_messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    login        TEXT NOT NULL REFERENCES accounts(login) ON DELETE CASCADE,
    created_at   TEXT NOT NULL,
    role         TEXT NOT NULL,
    content      TEXT NOT NULL DEFAULT '',
    raw          TEXT,
    receipt_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_chat_login ON chat_messages(login, id);

CREATE TABLE IF NOT EXISTS assistant_messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    login      TEXT NOT NULL REFERENCES accounts(login) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    role       TEXT NOT NULL,
    content    TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_assistant_login ON assistant_messages(login, id);

CREATE TABLE IF NOT EXISTS ai_reports (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    login      TEXT NOT NULL REFERENCES accounts(login) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    kind       TEXT NOT NULL,
    title      TEXT NOT NULL,
    content    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reports_login_date ON ai_reports(login, created_at);
"""


# ───────────────────────────── Подключение ─────────────────────────────


@contextmanager
def _db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """Создаёт таблицы, если их ещё нет (безопасно вызывать при каждом запуске)."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _db() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _fmt(created_at: str) -> str:
    try:
        return datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S").strftime("%d.%m %H:%M")
    except (TypeError, ValueError):
        return str(created_at or "")


def _loads(text, default):
    try:
        value = json.loads(text or "")
        return value if value is not None else default
    except (TypeError, json.JSONDecodeError):
        return default


# ───────────────────────────── Аккаунты и сессии ─────────────────────────────


def count_accounts() -> int:
    with _db() as conn:
        return conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]


def get_accounts() -> dict:
    with _db() as conn:
        rows = conn.execute("SELECT * FROM accounts ORDER BY demo DESC, created_at").fetchall()
    return {
        r["login"]: {
            "name": r["name"], "type": r["type"], "address": r["address"], "catalog": r["catalog"],
            "rules": r["rules"], "demo": bool(r["demo"]), "password_hash": r["password_hash"],
        }
        for r in rows
    }


def create_account(login: str, acc: dict) -> None:
    with _db() as conn:
        conn.execute(
            "INSERT INTO accounts (login, name, type, address, catalog, rules, demo, password_hash, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                login, acc["name"], acc["type"], acc.get("address", ""), acc.get("catalog", ""),
                acc.get("rules", ""), int(bool(acc.get("demo"))), acc["password_hash"], _now(),
            ),
        )


def create_session(login: str) -> str:
    token = secrets.token_urlsafe(24)
    expires = (datetime.now() + timedelta(days=SESSION_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    with _db() as conn:
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (_now(),))
        conn.execute(
            "INSERT INTO sessions (token, login, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (token, login, _now(), expires),
        )
    return token


def resolve_session(token: str) -> str | None:
    if not token:
        return None
    with _db() as conn:
        row = conn.execute(
            "SELECT login FROM sessions WHERE token = ? AND expires_at >= ?", (token, _now())
        ).fetchone()
    return row["login"] if row else None


def delete_session(token: str) -> None:
    if token:
        with _db() as conn:
            conn.execute("DELETE FROM sessions WHERE token = ?", (token,))


# ───────────────────────────── Заказы ─────────────────────────────


def add_order(login: str, detail: str, total: int, payment: str, items: list | None = None,
              created_at: str | None = None, is_demo: bool = False) -> int:
    with _db() as conn:
        cur = conn.execute(
            "INSERT INTO orders (login, created_at, detail, total, payment, items_json, is_demo) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (login, created_at or _now(), str(detail or ""), int(total or 0), str(payment or ""),
             json.dumps(items or [], ensure_ascii=False), int(is_demo)),
        )
        return cur.lastrowid


def list_orders(login: str) -> list[dict]:
    with _db() as conn:
        rows = conn.execute("SELECT * FROM orders WHERE login = ? ORDER BY created_at, id", (login,)).fetchall()
    return [
        {
            "id": r["id"], "created_at": r["created_at"], "time": _fmt(r["created_at"]),
            "address": r["detail"], "total": r["total"], "payment": r["payment"],
            "items": _loads(r["items_json"], []),
        }
        for r in rows
    ]


# ───────────────────────────── Аудиты ─────────────────────────────


def add_audit(login: str, result: dict, created_at: str | None = None, is_demo: bool = False) -> int:
    with _db() as conn:
        cur = conn.execute(
            "INSERT INTO audits (login, created_at, qa_score, missed, result_json, is_demo) VALUES (?, ?, ?, ?, ?, ?)",
            (login, created_at or _now(), int(result.get("qa_score", 0)), int(result.get("missed_revenue_kzt", 0)),
             json.dumps(result, ensure_ascii=False), int(is_demo)),
        )
        return cur.lastrowid


def list_audits(login: str) -> list[dict]:
    with _db() as conn:
        rows = conn.execute(
            "SELECT id, created_at, qa_score, missed FROM audits WHERE login = ? ORDER BY created_at, id", (login,)
        ).fetchall()
    return [
        {"id": r["id"], "created_at": r["created_at"], "time": _fmt(r["created_at"]),
         "qa_score": r["qa_score"], "missed": r["missed"]}
        for r in rows
    ]


def last_audit(login: str) -> dict | None:
    """Последний аудит аккаунта с полным результатом."""
    with _db() as conn:
        row = conn.execute(
            "SELECT result_json FROM audits WHERE login = ? ORDER BY created_at DESC, id DESC LIMIT 1",
            (login,),
        ).fetchone()
    return _loads(row["result_json"], None) if row else None


# ───────────────────────────── Отзывы ─────────────────────────────


def add_feedback(login: str, review: str, data: dict, created_at: str | None = None, is_demo: bool = False) -> int:
    with _db() as conn:
        cur = conn.execute(
            "INSERT INTO feedbacks (login, created_at, review, sentiment, category, summary, reply, is_demo) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (login, created_at or _now(), review, data.get("sentiment", "НЕЙТРАЛЬНО"), data.get("category", ""),
             data.get("summary", ""), data.get("reply", ""), int(is_demo)),
        )
        return cur.lastrowid


def list_feedbacks(login: str) -> list[dict]:
    with _db() as conn:
        rows = conn.execute(
            "SELECT id, created_at, sentiment, category, summary FROM feedbacks WHERE login = ? ORDER BY created_at, id",
            (login,),
        ).fetchall()
    return [
        {"id": r["id"], "created_at": r["created_at"], "time": _fmt(r["created_at"]),
         "sentiment": r["sentiment"], "category": r["category"], "summary": r["summary"]}
        for r in rows
    ]


def last_feedback(login: str) -> dict | None:
    with _db() as conn:
        row = conn.execute(
            "SELECT sentiment, category, summary, reply FROM feedbacks WHERE login = ? "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (login,),
        ).fetchone()
    return dict(row) if row else None


# ───────────────────────────── Чат продаж ─────────────────────────────


def add_chat_message(login: str, role: str, content: str, raw: str | None = None, receipt: dict | None = None) -> None:
    with _db() as conn:
        conn.execute(
            "INSERT INTO chat_messages (login, created_at, role, content, raw, receipt_json) VALUES (?, ?, ?, ?, ?, ?)",
            (login, _now(), role, content or "", raw, json.dumps(receipt, ensure_ascii=False) if receipt else None),
        )


def load_chat(login: str, limit: int = 300) -> list[dict]:
    with _db() as conn:
        rows = conn.execute(
            "SELECT role, content, raw, receipt_json FROM (SELECT * FROM chat_messages WHERE login = ? "
            "ORDER BY id DESC LIMIT ?) ORDER BY id",
            (login, limit),
        ).fetchall()
    messages = []
    for r in rows:
        msg = {"role": r["role"], "content": r["content"], "receipt": _loads(r["receipt_json"], None)}
        if r["raw"]:
            msg["raw"] = r["raw"]
        messages.append(msg)
    return messages


def clear_chat(login: str) -> None:
    with _db() as conn:
        conn.execute("DELETE FROM chat_messages WHERE login = ?", (login,))


# ───────────────────────────── Персональный ассистент ─────────────────────────────


def add_assistant_message(login: str, role: str, content: str) -> None:
    with _db() as conn:
        conn.execute(
            "INSERT INTO assistant_messages (login, created_at, role, content) VALUES (?, ?, ?, ?)",
            (login, _now(), role, content),
        )


def load_assistant(login: str, limit: int = 300) -> list[dict]:
    with _db() as conn:
        rows = conn.execute(
            "SELECT role, content FROM (SELECT * FROM assistant_messages WHERE login = ? "
            "ORDER BY id DESC LIMIT ?) ORDER BY id",
            (login, limit),
        ).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in rows]


def clear_assistant(login: str) -> None:
    with _db() as conn:
        conn.execute("DELETE FROM assistant_messages WHERE login = ?", (login,))


# ───────────────────────────── ИИ-отчёты ─────────────────────────────


def add_report(login: str, kind: str, title: str, content: str) -> int:
    with _db() as conn:
        cur = conn.execute(
            "INSERT INTO ai_reports (login, created_at, kind, title, content) VALUES (?, ?, ?, ?, ?)",
            (login, _now(), kind, title, content),
        )
        return cur.lastrowid


def list_reports(login: str, limit: int = 50) -> list[dict]:
    with _db() as conn:
        rows = conn.execute(
            "SELECT id, created_at, kind, title, content FROM ai_reports WHERE login = ? "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            (login, limit),
        ).fetchall()
    return [{**dict(r), "time": _fmt(r["created_at"])} for r in rows]


def delete_report(login: str, report_id: int) -> None:
    with _db() as conn:
        conn.execute("DELETE FROM ai_reports WHERE login = ? AND id = ?", (login, report_id))


# ───────────────────────────── Демо-данные ─────────────────────────────


def clear_demo(login: str) -> None:
    with _db() as conn:
        for table in ("orders", "audits", "feedbacks"):
            conn.execute(f"DELETE FROM {table} WHERE login = ? AND is_demo = 1", (login,))


def replace_demo(login: str, orders: list, audits: list, feedbacks: list) -> None:
    """Заменяет демо-записи аккаунта на переданные. Реальные данные не затрагиваются."""
    clear_demo(login)
    for o in orders:
        add_order(login, o["detail"], o["total"], o["payment"], o.get("items"), o.get("created_at"), is_demo=True)
    for a in audits:
        add_audit(login, a["result"], a.get("created_at"), is_demo=True)
    for f in feedbacks:
        add_feedback(login, f.get("review", ""), f, f.get("created_at"), is_demo=True)


# ───────────────────────────── Аналитика ─────────────────────────────


def business_snapshot(login: str) -> dict:
    """Сводка по бизнесу — для дашборда, ИИ-отчётов и персонального ассистента."""
    orders = list_orders(login)
    revenue = sum(o["total"] for o in orders)

    items: dict[str, dict] = defaultdict(lambda: {"qty": 0, "revenue": 0})
    by_day: dict[str, int] = defaultdict(int)
    for o in orders:
        by_day[o["created_at"][:10]] += o["total"]
        for it in o["items"]:
            if not isinstance(it, dict):
                continue
            try:
                qty = int(float(it.get("qty", 1) or 1))
                price = float(it.get("price", 0) or 0)
            except (TypeError, ValueError):
                continue
            name = str(it.get("name", "—"))
            items[name]["qty"] += qty
            items[name]["revenue"] += int(qty * price)
    top_items = sorted(
        ({"name": n, **v} for n, v in items.items()), key=lambda x: x["revenue"], reverse=True
    )[:8]

    audits_meta = list_audits(login)
    with _db() as conn:
        audit_rows = conn.execute(
            "SELECT result_json FROM audits WHERE login = ? ORDER BY created_at DESC, id DESC LIMIT 5", (login,)
        ).fetchall()
    recent_audits = []
    for r in audit_rows:
        res = _loads(r["result_json"], {})
        recent_audits.append(
            {
                "qa_score": res.get("qa_score"),
                "missed_revenue_kzt": res.get("missed_revenue_kzt"),
                "reason": res.get("missed_revenue_reason", ""),
                "hint": res.get("cashier_hint", ""),
            }
        )

    feedbacks = list_feedbacks(login)
    sentiments = Counter(f["sentiment"] for f in feedbacks)
    categories = Counter(f["category"] for f in feedbacks if f["category"])

    return {
        "orders": {
            "count": len(orders),
            "revenue": revenue,
            "avg_check": int(revenue / len(orders)) if orders else 0,
            "payments": dict(Counter(o["payment"] for o in orders if o["payment"])),
            "top_items": top_items,
            "revenue_by_day": dict(sorted(by_day.items())),
            "last": [{"time": o["time"], "detail": o["address"], "total": o["total"]} for o in orders[-5:]],
        },
        "audits": {
            "count": len(audits_meta),
            "avg_qa": round(sum(a["qa_score"] for a in audits_meta) / len(audits_meta)) if audits_meta else 0,
            "total_missed": sum(a["missed"] for a in audits_meta),
            "recent": recent_audits,
        },
        "feedbacks": {
            "count": len(feedbacks),
            "sentiments": dict(sentiments),
            "top_categories": categories.most_common(5),
            "recent": [
                {"sentiment": f["sentiment"], "category": f["category"], "summary": f["summary"]}
                for f in feedbacks[-8:]
            ],
        },
    }


# ───────────────────────────── Экспорт и миграция ─────────────────────────────


def export_account(login: str) -> dict:
    """Все данные аккаунта одним словарём (для скачивания JSON-бэкапа)."""
    accounts = get_accounts()
    acc = dict(accounts.get(login, {}))
    acc.pop("password_hash", None)
    return {
        "account": acc,
        "orders": list_orders(login),
        "audits": list_audits(login),
        "last_audit": last_audit(login),
        "feedbacks": list_feedbacks(login),
        "sales_chat": load_chat(login),
        "assistant_chat": load_assistant(login),
        "ai_reports": list_reports(login, limit=1000),
    }


def import_legacy_json(accounts_file: Path, data_dir: Path) -> bool:
    """Однократный перенос аккаунтов и данных из старой версии (accounts.json + data/*.json)."""
    if count_accounts() > 0 or not accounts_file.exists():
        return False
    try:
        accounts = json.loads(accounts_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(accounts, dict) or not accounts:
        return False

    year = datetime.now().year

    def parse_time(text: str) -> str:
        try:
            return datetime.strptime(f"{year} {text}", "%Y %d.%m %H:%M").strftime("%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            return _now()

    for login, acc in accounts.items():
        if not isinstance(acc, dict) or "password_hash" not in acc or "name" not in acc:
            continue
        acc.setdefault("type", "Другое")
        create_account(login, acc)
        data_file = data_dir / f"{login}.json"
        if not data_file.exists():
            continue
        try:
            data = json.loads(data_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for o in data.get("orders", []):
            add_order(login, o.get("address", ""), o.get("total", 0), o.get("payment", ""),
                      created_at=parse_time(o.get("time", "")))
        for a in data.get("audits", []):
            add_audit(login, {"qa_score": a.get("qa_score", 0), "missed_revenue_kzt": a.get("missed", 0)},
                      created_at=parse_time(a.get("time", "")))
        for f in data.get("feedbacks", []):
            add_feedback(login, "", {"sentiment": f.get("sentiment", "НЕЙТРАЛЬНО")},
                         created_at=parse_time(f.get("time", "")))
    return count_accounts() > 0
