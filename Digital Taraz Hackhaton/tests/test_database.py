"""Тесты слоя данных. Запуск: python -m unittest discover -s tests -v"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_TMP = tempfile.TemporaryDirectory()
os.environ["QADAM_DB"] = str(Path(_TMP.name) / "test.db")

import database as db  # noqa: E402


def _reset_db() -> None:
    for suffix in ("", "-wal", "-shm"):
        Path(str(db.DB_PATH) + suffix).unlink(missing_ok=True)
    db.init_db()
    db.create_account("t1", {"name": "Test", "type": "Кафе / ресторан", "password_hash": "h"})


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        _reset_db()

    def test_account_roundtrip(self):
        acc = db.get_accounts()["t1"]
        self.assertEqual(acc["name"], "Test")
        self.assertFalse(acc["demo"])
        with self.assertRaises(Exception):  # логин уникален
            db.create_account("t1", {"name": "X", "type": "Другое", "password_hash": "h"})

    def test_orders_and_snapshot(self):
        db.add_order("t1", "ул. Абая 12", 7200, "Kaspi", [{"name": "Плов", "qty": 2, "price": 2500}])
        db.add_order("t1", "Самовывоз", 1000, "Наличные", [{"name": "Чай", "qty": 1, "price": 1000}])
        snap = db.business_snapshot("t1")
        self.assertEqual(snap["orders"]["count"], 2)
        self.assertEqual(snap["orders"]["revenue"], 8200)
        self.assertEqual(snap["orders"]["avg_check"], 4100)
        self.assertEqual(snap["orders"]["top_items"][0]["name"], "Плов")

    def test_audit_and_feedback_persist(self):
        db.add_audit("t1", {"qa_score": 70, "missed_revenue_kzt": 500, "cashier_hint": "x"})
        db.add_feedback("t1", "ок", {"sentiment": "ПОЗИТИВ", "category": "сервис", "summary": "s", "reply": "r"})
        self.assertEqual(db.last_audit("t1")["qa_score"], 70)
        self.assertEqual(db.last_feedback("t1")["category"], "сервис")
        self.assertEqual(db.business_snapshot("t1")["audits"]["total_missed"], 500)

    def test_sessions(self):
        token = db.create_session("t1")
        self.assertEqual(db.resolve_session(token), "t1")
        db.delete_session(token)
        self.assertIsNone(db.resolve_session(token))
        self.assertIsNone(db.resolve_session("нет-такого-токена"))

    def test_expired_session(self):
        token = db.create_session("t1")
        past = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        with db._db() as conn:
            conn.execute("UPDATE sessions SET expires_at = ? WHERE token = ?", (past, token))
        self.assertIsNone(db.resolve_session(token))

    def test_chats_persist_and_clear(self):
        db.add_chat_message("t1", "user", "привет")
        db.add_chat_message("t1", "assistant", "здравствуйте", raw="raw", receipt={"total_amount": 100})
        chat = db.load_chat("t1")
        self.assertEqual([m["role"] for m in chat], ["user", "assistant"])
        self.assertEqual(chat[1]["receipt"]["total_amount"], 100)
        db.add_assistant_message("t1", "user", "вопрос")
        self.assertEqual(len(db.load_assistant("t1")), 1)
        db.clear_chat("t1")
        db.clear_assistant("t1")
        self.assertEqual(db.load_chat("t1"), [])
        self.assertEqual(db.load_assistant("t1"), [])

    def test_reports(self):
        rid = db.add_report("t1", "overview", "Обзор", "# текст")
        self.assertEqual(db.list_reports("t1")[0]["title"], "Обзор")
        db.delete_report("t1", rid)
        self.assertEqual(db.list_reports("t1"), [])

    def test_demo_replace_keeps_real_data(self):
        db.add_order("t1", "реальный", 500, "Kaspi")
        demo = [{"detail": "d", "total": 100, "payment": "K", "items": [], "created_at": "2026-01-01 10:00:00"}]
        db.replace_demo("t1", demo, [{"result": {"qa_score": 50, "missed_revenue_kzt": 1}}], [])
        self.assertEqual(len(db.list_orders("t1")), 2)
        db.replace_demo("t1", demo, [], [])  # повторная загрузка не плодит дубликаты
        self.assertEqual(len(db.list_orders("t1")), 2)
        db.clear_demo("t1")
        self.assertEqual(len(db.list_orders("t1")), 1)
        self.assertEqual(db.list_audits("t1"), [])

    def test_export(self):
        db.add_order("t1", "x", 1, "K")
        data = db.export_account("t1")
        self.assertNotIn("password_hash", data["account"])
        json.dumps(data, ensure_ascii=False, default=str)

    def test_legacy_import(self):
        _reset_db()
        with db._db() as conn:
            conn.execute("DELETE FROM accounts")
        tmp = Path(_TMP.name)
        (tmp / "data").mkdir(exist_ok=True)
        (tmp / "accounts.json").write_text(
            json.dumps({"old": {"name": "Old", "type": "Автомойка", "password_hash": "h", "demo": False}}),
            encoding="utf-8",
        )
        (tmp / "data" / "old.json").write_text(
            json.dumps({"orders": [{"time": "16.10 08:50", "address": "a", "total": 100, "payment": "Kaspi"}]}),
            encoding="utf-8",
        )
        self.assertTrue(db.import_legacy_json(tmp / "accounts.json", tmp / "data"))
        self.assertEqual(db.list_orders("old")[0]["total"], 100)
        self.assertFalse(db.import_legacy_json(tmp / "accounts.json", tmp / "data"))  # второй раз — ничего


if __name__ == "__main__":
    unittest.main()
