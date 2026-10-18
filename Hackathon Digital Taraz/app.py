"""
Qadam AI — Операционная ИИ-платформа для ШОБ
MVP для хакатона Zhambyl Hub (г. Тараз) · трек "AI for SME & Local Business"

Настройка:
    скопируйте .env.example в .env и впишите QWEN_API_KEY (ключ вводить в интерфейсе не нужно)

Все данные сохраняются в базе qadam.db (SQLite) рядом с app.py.

Демо-аккаунты (пароль demo123): cafe, salon, wash. Новые аккаунты создаются на экране входа.

Запуск:
    pip install -r requirements.txt            # основное
    pip install -r requirements-audio.txt      # необязательно: расшифровка аудио (Whisper)
    streamlit run app.py

Подробности — в README.md.
"""

import hashlib
import hmac
import html
import io
import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import streamlit.components.v1 as components
from audio_recorder_streamlit import audio_recorder
from openai import BadRequestError, OpenAI

try:  # переменные из файла .env рядом с app.py (системные переменные имеют приоритет)
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent / ".env", override=False)
except ImportError:
    pass  # python-dotenv не установлен — читаем только системные переменные

import database as db  # noqa: E402  (после load_dotenv: читает QADAM_DB)

# ───────────────────────────── Конфигурация ─────────────────────────────

# API-ключ берётся ТОЛЬКО из переменной окружения / файла .env (см. .env.example)
API_KEY = (
    os.getenv("QWEN_API_KEY") or os.getenv("XKIRO_API_KEY") or os.getenv("OPENAI_API_KEY") or ""
).strip()
NO_KEY_MSG = "API-ключ не задан. Добавьте QWEN_API_KEY в файл .env и перезапустите приложение."

MODEL = os.getenv("QWEN_MODEL", "qwen/qwen3.8-max:free")
BASE_URL = os.getenv("QWEN_BASE_URL", "https://api.xkiro.com/v1")
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "small")  # локальная расшифровка: tiny/base/small/medium/large-v3

APP_DIR = Path(__file__).resolve().parent
ACCOUNTS_FILE = APP_DIR / "accounts.json"  # только для переноса данных из старой версии (JSON → SQLite)
DATA_DIR = APP_DIR / "data"
DEMO_PASSWORD = "demo123"

# Параметры, зависящие от типа бизнеса: от них строятся промпты и подписи в интерфейсе
BUSINESS_TYPES = {
    "Кафе / ресторан": {
        "icon": "restaurant",
        "place_gen": "кафе",
        "staff": "кассир", "staff_gen": "кассира", "staff_dat": "кассиру",
        "order_noun": "заказ", "order_plural": "заказы", "orders_title": "Заказов принято",
        "catalog_label": "Меню",
        "upsell": "напитки, закуски, десерты",
        "detail_label": "адрес доставки (для самовывоза пиши «Самовывоз»)",
        "detail_short": "Адрес",
    },
    "Салон красоты": {
        "icon": "spa",
        "place_gen": "салона красоты",
        "staff": "администратор", "staff_gen": "администратора", "staff_dat": "администратору",
        "order_noun": "запись", "order_plural": "записи", "orders_title": "Записей принято",
        "catalog_label": "Прайс-лист",
        "upsell": "уходовые процедуры, массаж, маски, дополнительные услуги к основной",
        "detail_label": "дату и время записи",
        "detail_short": "Запись на",
    },
    "Автомойка": {
        "icon": "local_car_wash",
        "place_gen": "автомойки",
        "staff": "приёмщик", "staff_gen": "приёмщика", "staff_dat": "приёмщику",
        "order_noun": "запись", "order_plural": "записи", "orders_title": "Записей принято",
        "catalog_label": "Прайс-лист",
        "upsell": "химчистка салона, воск, полировка, чернение резины, мойка двигателя",
        "detail_label": "марку автомобиля и удобное время",
        "detail_short": "Авто и время",
    },
    "Магазин": {
        "icon": "storefront",
        "place_gen": "магазина",
        "staff": "продавец", "staff_gen": "продавца", "staff_dat": "продавцу",
        "order_noun": "заказ", "order_plural": "заказы", "orders_title": "Заказов принято",
        "catalog_label": "Каталог",
        "upsell": "сопутствующие товары и наборы",
        "detail_label": "адрес доставки (для самовывоза пиши «Самовывоз»)",
        "detail_short": "Адрес",
    },
    "Другое": {
        "icon": "business",
        "place_gen": "компании",
        "staff": "сотрудник", "staff_gen": "сотрудника", "staff_dat": "сотруднику",
        "order_noun": "заказ", "order_plural": "заказы", "orders_title": "Заказов принято",
        "catalog_label": "Каталог услуг",
        "upsell": "сопутствующие услуги",
        "detail_label": "контактные данные и удобное время",
        "detail_short": "Детали",
    },
}


# Демо-заказы по типам бизнеса: (детали, оплата, [(позиция, количество, цена)])
DEMO_ORDERS = {
    "Кафе / ресторан": [
        ("ул. Абая 12", "Kaspi", [("Плов", 2, 2500), ("Самса", 2, 800), ("Чай чёрный (чайник)", 1, 600)]),
        ("Самовывоз", "Наличные", [("Бешбармак", 2, 4500), ("Шурпа", 1, 1900)]),
        ("мкр. Самал 5", "Kaspi", [("Лагман", 2, 2200), ("Компот домашний", 2, 500)]),
    ],
    "Салон красоты": [
        ("Сегодня, 16:00", "Kaspi", [("Женская стрижка", 1, 6000), ("Маска для волос", 1, 3500)]),
        ("Завтра, 11:30", "Наличные", [("Маникюр с гель-лаком", 1, 7000), ("Педикюр", 1, 8000)]),
        ("Пятница, 14:00", "Kaspi", [("Окрашивание (один цвет)", 1, 18000), ("Брови: коррекция и окрашивание", 1, 4500)]),
    ],
    "Автомойка": [
        ("Toyota Camry · 12:00", "Kaspi", [("Комплексная мойка (кузов и салон)", 1, 5500), ("Воск-защита", 1, 2500)]),
        ("Toyota Prado · 15:30", "Kaspi", [("Бесконтактная мойка кузова", 1, 3000), ("Мойка двигателя", 1, 4000)]),
        ("Kia Rio · 17:00", "Наличные", [("Химчистка салона", 1, 25000)]),
    ],
    "Магазин": [
        ("ул. Абая 12", "Kaspi", [("Товар А", 2, 2000), ("Товар Б", 1, 1500)]),
        ("Самовывоз", "Наличные", [("Товар В", 3, 1200)]),
        ("мкр. Самал 5", "Kaspi", [("Товар А", 1, 2000), ("Товар Г", 2, 2500)]),
    ],
    "Другое": [
        ("Заявка 1", "Kaspi", [("Услуга 1", 1, 5000)]),
        ("Заявка 2", "Наличные", [("Услуга 2", 2, 3500)]),
        ("Заявка 3", "Kaspi", [("Услуга 1", 1, 5000), ("Услуга 3", 1, 2000)]),
    ],
}


def type_info(account: dict) -> dict:
    info = dict(BUSINESS_TYPES.get(account.get("type"), BUSINESS_TYPES["Другое"]))
    info["staff_cap"] = info["staff"].capitalize()
    return info


def hash_password(login: str, password: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), f"qadam:{login}".encode(), 100_000).hex()


def default_accounts() -> dict:
    """Три демо-бизнеса (примеры данных вымышленные). Пароль для всех: demo123."""
    return {
        "cafe": {
            "name": "Тараз Lezzeti",
            "type": "Кафе / ресторан",
            "address": "г. Тараз, ул. Толе би 45",
            "catalog": (
                "• Плов — 2 500 ₸\n• Лагман — 2 200 ₸\n• Бешбармак — 4 500 ₸\n• Манты (5 шт.) — 2 000 ₸\n"
                "• Самса — 800 ₸\n• Шашлык из баранины — 1 800 ₸\n• Шурпа — 1 900 ₸\n"
                "• Баурсаки (порция) — 1 200 ₸\n• Чай чёрный (чайник) — 600 ₸\n• Компот домашний — 500 ₸"
            ),
            "rules": (
                "Скидка 10% действует при самовывозе: применяй её к итоговой сумме.\n"
                "Баурсаки в подарок при заказе от 10 000 ₸ — обязательно сообщи об этом клиенту."
            ),
            "demo": True,
        },
        "salon": {
            "name": "Aruzhan Beauty",
            "type": "Салон красоты",
            "address": "г. Тараз, ул. Желтоксан 20",
            "catalog": (
                "• Женская стрижка — 6 000 ₸\n• Окрашивание (один цвет) — 18 000 ₸\n"
                "• Маникюр с гель-лаком — 7 000 ₸\n• Педикюр — 8 000 ₸\n• Чистка лица — 12 000 ₸\n"
                "• Брови: коррекция и окрашивание — 4 500 ₸\n• Массаж спины (30 мин) — 8 000 ₸\n"
                "• Маска для волос — 3 500 ₸"
            ),
            "rules": (
                "Скидка 10% при записи на две и более услуги.\n"
                "Маска для волос в подарок при записи на сумму от 20 000 ₸.\n"
                "Работаем ежедневно с 10:00 до 20:00."
            ),
            "demo": True,
        },
        "wash": {
            "name": "Taraz Wash",
            "type": "Автомойка",
            "address": "г. Тараз, ул. Казыбек би 100",
            "catalog": (
                "• Бесконтактная мойка кузова — 3 000 ₸\n• Комплексная мойка (кузов и салон) — 5 500 ₸\n"
                "• Химчистка салона — 25 000 ₸\n• Полировка кузова — 30 000 ₸\n• Мойка двигателя — 4 000 ₸\n"
                "• Чернение резины — 1 000 ₸\n• Воск-защита — 2 500 ₸"
            ),
            "rules": (
                "Скидка 15% по будням с 9:00 до 12:00.\n"
                "Чернение резины в подарок при заказе комплексной мойки.\n"
                "Работаем круглосуточно."
            ),
            "demo": True,
        },
    }


def load_accounts() -> dict:
    return db.get_accounts()


@st.cache_resource(show_spinner=False)
def setup_database() -> bool:
    """Создаёт таблицы; при первом запуске переносит старые JSON-данные или создаёт демо-аккаунты."""
    db.init_db()
    if db.count_accounts() == 0 and not db.import_legacy_json(ACCOUNTS_FILE, DATA_DIR):
        for login, acc in default_accounts().items():
            acc["password_hash"] = hash_password(login, DEMO_PASSWORD)
            db.create_account(login, acc)
    return True


# ───────────────────────────── Промпты (строятся из профиля бизнеса) ─────────────────────────────


def greeting(b: dict) -> str:
    t = type_info(b)
    return (
        f"Сәлем! Добро пожаловать в «{b['name']}»! Я помогу оформить {t['order_noun']}. "
        "Что бы вы хотели выбрать?"
    )


def sales_prompt(b: dict) -> str:
    t = type_info(b)
    rules = (b.get("rules") or "").strip() or "Особых акций нет."
    return f"""
Ты — вежливый ИИ-администратор {t['place_gen']} «{b['name']}» ({b['address']}).
Общаешься с клиентами на русском или казахском — на том языке, на котором пишет клиент.
Тон: тёплый, гостеприимный, лаконичный. Не используй эмодзи и смайлики.

{t['catalog_label'].upper()}:
{b['catalog']}

ПРАВИЛА:
1. Помогай выбрать, предлагай уместные дополнения (допродажи): {t['upsell']}.
2. Акции и особые условия бизнеса — сообщай клиенту, когда они применимы, и учитывай при расчёте:
{rules}
3. Уточни у клиента: {t['detail_label']}, а также способ оплаты (Kaspi или Наличные).
4. Не выдумывай позиции и цены, которых нет в списке выше.
5. Когда клиент подвёл итог и сообщил нужные данные — оформи чек: кратко перечисли позиции, итоговую сумму
   и ОБЯЗАТЕЛЬНО в самом конце ответа выведи JSON строго в таком формате
   (в поле customer_address запиши: {t['detail_label']}):
```json
{{
  "customer_address": "значение",
  "items": [{{"name": "название", "qty": 1, "price": 1000}}],
  "total_amount": 1000,
  "payment_method": "Kaspi/Наличные"
}}
```
Пока {t['order_noun']} не оформлен, JSON не выводи.
"""


def audit_prompt(b: dict) -> str:
    t = type_info(b)
    return f"""
Ты — QA-аудитор качества обслуживания в {t['place_gen']} «{b['name']}» (Тараз).
Тебе дан текст диалога {t['staff_gen']} с клиентом (расшифровка аудиозаписи). Проанализируй его
и верни ТОЛЬКО валидный JSON без пояснений:
{{
  "transcript": "аккуратно оформленный диалог (на языке оригинала, с метками {t['staff_cap']}:/Клиент:)",
  "cashier_hint": "1–3 конкретные подсказки {t['staff_dat']}, что сказать/предложить в следующий раз",
  "qa_score": целое число 0–100,
  "criteria": {{
    "Приветствие": число 0–10,
    "Выявление потребностей": число 0–10,
    "Допродажи": число 0–10,
    "Вежливость": число 0–10,
    "Завершение сделки": число 0–10
  }},
  "qa_comment": "краткий вывод по качеству обслуживания",
  "missed_revenue_kzt": целое число — оценка упущенной выручки в тенге из-за непредложенных допродаж (0 если нет),
  "missed_revenue_reason": "что именно не было предложено и почему это упущенная выручка"
}}
{t['catalog_label']} для оценки допродаж:
{b['catalog']}
Все поля, кроме transcript, пиши на русском языке, без эмодзи.
"""


def feedback_prompt(b: dict) -> str:
    t = type_info(b)
    return f"""
Ты — менеджер по работе с клиентами {t['place_gen']} «{b['name']}» (Тараз).
Проанализируй отзыв клиента и верни ТОЛЬКО валидный JSON без пояснений:
{{
  "sentiment": "ПОЗИТИВ" | "НЕГАТИВ" | "НЕЙТРАЛЬНО",
  "category": "тема отзыва (например: качество, сервис, цены, ожидание, чистота)",
  "summary": "суть отзыва в одном предложении на русском",
  "reply": "вежливый персонализированный ответ клиенту от имени бизнеса"
}}
Правила для reply: отвечай на том языке, на котором написан отзыв (русский или казахский);
благодари за обратную связь; при негативе извинись, предложи конкретное решение
(например, компенсацию или повторный визит); подпись — «Команда {b['name']}». Не используй эмодзи.
"""


REPORT_KINDS = {
    "overview": (
        "Общий обзор бизнеса",
        "Сделай сводный разбор: выручка, средний чек, качество обслуживания, отзывы. "
        "Выдели 3 главных вывода и 3 первоочередных действия.",
    ),
    "sales": (
        "Продажи и допродажи",
        "Разбери выручку, средний чек и самые доходные позиции. Найди, где теряются допродажи, "
        "и предложи конкретные допродажи, комбо-наборы и формулировки для сотрудников.",
    ),
    "service": (
        "Качество обслуживания",
        "Разбери результаты аудитов: оценки, повторяющиеся ошибки, упущенную выручку. "
        "Дай чек-лист для обучения сотрудников и оцени потенциал роста в тенге.",
    ),
    "reviews": (
        "Отзывы клиентов",
        "Разбери тональность и темы отзывов: что хвалят, на что жалуются, что исправить в первую очередь. "
        "Предложи шаблон ответа на самую частую претензию.",
    ),
    "plan": (
        "План на неделю",
        "Составь план действий на ближайшие 7 дней по дням: приоритеты, ответственные роли и ожидаемый эффект в тенге.",
    ),
}


def report_messages(b: dict, kind: str, snapshot: dict) -> list:
    t = type_info(b)
    system = f"""
Ты — бизнес-аналитик для малого бизнеса в Таразе. Анализируешь данные {t['place_gen']} «{b['name']}».
Правила:
- Опирайся ТОЛЬКО на переданные данные. Если данных мало или их нет — прямо скажи об этом и объясни, что собрать. Цифры не выдумывай.
- Суммы указывай в тенге (₸). Пиши по-русски, просто, без канцелярита и без эмодзи.
- Формат — Markdown: заголовки ##, короткие списки. Структура: «## Главное», «## Что показывают данные», «## Что делать».
- Рекомендации — конкретные и выполнимые для малого бизнеса, с ожидаемым эффектом там, где его можно оценить.
"""
    user = (
        f"Задача: {REPORT_KINDS[kind][1]}\n\n"
        f"{t['catalog_label']} бизнеса:\n{b['catalog']}\n\nАкции и условия:\n{b.get('rules') or 'нет'}\n\n"
        f"Данные бизнеса (JSON):\n{json.dumps(snapshot, ensure_ascii=False)}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def assistant_prompt(b: dict, snapshot: dict) -> str:
    t = type_info(b)
    return f"""
Ты — персональный ИИ-ассистент владельца {t['place_gen']} «{b['name']}» ({b['address']}).
Помогаешь с ежедневными задачами: разбор цифр, идеи акций, тексты для Instagram, Telegram и WhatsApp,
ответы клиентам, обучение сотрудников, приоритеты на день и неделю.
Стиль: по делу, дружелюбно, без эмодзи. Отвечай на языке пользователя (русский или казахский).
Если просят совет — давай 2–4 конкретных шага. Цифры не выдумывай: опирайся на данные ниже,
а если данных не хватает — скажи об этом и предложи, что собрать.

{t['catalog_label'].upper()} БИЗНЕСА:
{b['catalog']}

АКЦИИ И УСЛОВИЯ:
{b.get('rules') or 'нет'}

АКТУАЛЬНЫЕ ДАННЫЕ БИЗНЕСА (JSON):
{json.dumps(snapshot, ensure_ascii=False)}
"""


# ───────────────────────────── Страница и стили ─────────────────────────────

EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D]+", flags=re.UNICODE
)


def strip_emoji(text) -> str:
    """Подстраховка: убирает эмодзи из ответов модели."""
    return EMOJI_RE.sub("", str(text or "")).replace("  ", " ").strip()


def to_int(value, default: int = 0) -> int:
    """Безопасно приводит ответ модели к int ('85', 85.0, '85.5' -> 85)."""
    try:
        return int(float(str(value).replace(" ", "").replace(",", ".")))
    except (TypeError, ValueError):
        return default


def fmt_money(value) -> str:
    return f"{to_int(value):,}".replace(",", " ")


def ico(name: str) -> str:
    """Иконка Material Symbols для HTML-блоков."""
    return f'<span class="mi">{name}</span>'


st.set_page_config(
    page_title="Qadam AI",
    page_icon=":material/rocket_launch:",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown(
    """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');
@import url('https://fonts.googleapis.com/css2?family=Material+Symbols+Rounded:opsz,wght,FILL,GRAD@24,400,0,0');

:root {
    --bg: #FFFFFF;
    --surface: #F8FAFC;
    --card: #FFFFFF;
    --border: #E5E7EB;
    --text: #111827;
    --muted: #6B7280;
    --blue: #8DB6E8;
    --blue-deep: #5B8FCF;
    --blue-soft: #EEF4FC;
    --black: #111827;
    color-scheme: light;
}

html, body, [class*="css"], .stApp, button, input, textarea {
    font-family: 'Inter', 'Roboto', -apple-system, 'Segoe UI', sans-serif !important;
}
.stApp { background: var(--bg); color: var(--text); }
.stApp p, .stApp label, .stApp li, .stApp span { color: inherit; }
.block-container { padding: 0 3rem 3rem 3rem; max-width: 1400px; }
header[data-testid="stHeader"] { display: none; }

/* Иконки */
.mi, [data-testid="stIconMaterial"] {
    font-family: 'Material Symbols Rounded' !important; font-weight: normal; font-style: normal;
    letter-spacing: normal; text-transform: none; white-space: nowrap; direction: ltr;
    -webkit-font-feature-settings: 'liga'; font-feature-settings: 'liga';
}
.mi { display: inline-block; font-size: 1.3em; line-height: 1; vertical-align: -0.22em; margin-right: 6px; }
.card-title .mi { color: var(--blue-deep); }
.qadam-title .mi { font-size: 0.95em; color: var(--blue-deep); margin-right: 10px; }
.receipt-title .mi, .receipt-meta .mi { color: var(--blue-deep) !important; font-size: 1.2em; }
.badge-ok .mi { font-size: 1.15em; margin-right: 4px; color: #2F5C94 !important; }
.sent-badge .mi { font-size: 1.2em; margin-right: 6px; }

/* Заголовки */
.qadam-title { font-size: 2.6rem; font-weight: 800; letter-spacing: -0.02em; margin: 0; line-height: 1.15; color: var(--black); }
.qadam-grad {
    background: linear-gradient(90deg, #111827 0%, #374151 40%, #6B9BD6 100%);
    -webkit-background-clip: text; background-clip: text;
    -webkit-text-fill-color: transparent; color: transparent;
}
.qadam-sub { color: var(--muted); font-size: 1.1rem; margin: 6px 0 22px 0; }

/* Верхняя панель (вместо боковой) */
[data-testid="stSidebar"], [data-testid="stSidebarCollapsedControl"], [data-testid="collapsedControl"],
[data-testid="stToolbar"], [data-testid="stDeployButton"], #MainMenu { display: none !important; }

div[class*="st-key-topbar"] {
    border-bottom: 1px solid var(--border); padding: 14px 0; margin-bottom: 28px;
}
.brand {
    display: flex; align-items: center; font-weight: 800; font-size: 1.2rem;
    letter-spacing: -0.01em; color: var(--black); white-space: nowrap;
}
.brand .mi { color: var(--blue-deep); font-size: 1.35em; margin-right: 6px; }
.brand .qadam-grad { background: linear-gradient(90deg, #111827 0%, #374151 55%, #5B8FCF 100%);
    -webkit-background-clip: text; background-clip: text; -webkit-text-fill-color: transparent; }

/* Навигация как на сайте */
div[class*="st-key-nav"] [role="radiogroup"] { gap: 4px; justify-content: center; flex-wrap: wrap; }
div[class*="st-key-nav"] label[data-baseweb="radio"] {
    margin: 0; padding: 10px 18px; border-radius: 8px; cursor: pointer; transition: background .15s ease;
}
div[class*="st-key-nav"] label[data-baseweb="radio"] > div:first-child { display: none; }
div[class*="st-key-nav"] label[data-baseweb="radio"] p { font-weight: 600; font-size: 1rem; color: var(--muted); }
div[class*="st-key-nav"] label[data-baseweb="radio"]:hover { background: var(--surface); }
div[class*="st-key-nav"] label[data-baseweb="radio"]:hover p { color: var(--black); }
div[class*="st-key-nav"] label[data-baseweb="radio"]:has(input:checked) {
    background: var(--blue-soft); box-shadow: inset 0 -3px 0 var(--blue);
}
div[class*="st-key-nav"] label[data-baseweb="radio"]:has(input:checked) p { color: var(--black); }

/* Кнопка аккаунта справа */
div[class*="st-key-topbar"] [data-testid="stColumn"]:last-child [data-testid="stVerticalBlock"],
div[class*="st-key-topbar"] [data-testid="column"]:last-child [data-testid="stVerticalBlock"] { align-items: flex-end; }
div[class*="st-key-topbar"] [data-testid="stPopover"] button {
    background: #FFFFFF; color: var(--black); border: 1px solid var(--border); border-radius: 10px;
    font-weight: 600; max-width: 260px; box-shadow: none;
}
div[class*="st-key-topbar"] [data-testid="stPopover"] button:hover { border-color: var(--blue); background: var(--blue-soft); }
div[class*="st-key-topbar"] [data-testid="stPopover"] button p { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

/* Заголовок раздела */
.page-title { font-size: 2rem; font-weight: 800; letter-spacing: -0.02em; color: var(--black); line-height: 1.2; }
.page-sub { color: var(--muted); font-size: 1.05rem; margin: 6px 0 22px 0; }

/* Метрики */
[data-testid="stMetric"] {
    background: var(--card); border: 1px solid var(--border); border-radius: 12px;
    padding: 18px 22px; box-shadow: 0 1px 3px rgba(17, 24, 39, 0.06);
}
[data-testid="stMetricLabel"] p { color: var(--muted); font-size: 0.95rem; font-weight: 500; }
[data-testid="stMetricValue"] { color: var(--black); font-weight: 700; font-size: 2rem; }

/* Карточки */
div[class*="st-key-card_"] {
    background: var(--card); border: 1px solid var(--border); border-radius: 12px;
    padding: 20px 22px; box-shadow: 0 1px 3px rgba(17, 24, 39, 0.06); margin-bottom: 8px;
}
div[class*="st-key-receipt_"] {
    background: #FFFFFF; color: var(--black); border: 1px dashed #9CA3AF; border-radius: 12px;
    padding: 20px 24px; box-shadow: 0 2px 10px rgba(17, 24, 39, 0.07); max-width: 560px;
}
div[class*="st-key-receipt_"] * { color: var(--black); }

.card-title { font-size: 1.1rem; font-weight: 700; color: var(--black); margin: 0 0 12px 0; }
.card-scroll { max-height: 260px; overflow-y: auto; white-space: pre-wrap; color: var(--text); line-height: 1.55; }
.gray-box { background: #F3F4F6; border-radius: 10px; padding: 14px 16px; color: #374151; }

/* Чек */
.badge-ok {
    display: inline-block; background: var(--blue-soft); color: #2F5C94 !important;
    border: 1px solid var(--blue); border-radius: 999px; padding: 3px 12px; font-weight: 700; font-size: .85rem;
}
.receipt-head { display:flex; justify-content:space-between; align-items:center; margin-bottom: 6px; }
.receipt-title { font-weight: 800; font-size: 1.15rem; letter-spacing: .04em; }
.receipt-meta { font-size: .85rem; color: var(--muted) !important; }
.receipt-total { display:flex; justify-content:space-between; font-size: 1.3rem; font-weight: 800; margin-top: 8px; }

/* Badges тональности (нейтральная палитра) */
.sent-badge { display:inline-block; padding: 8px 22px; border-radius: 999px; font-weight: 800; font-size: 1.05rem; letter-spacing: .05em; }
.sent-pos { background: var(--blue-soft); color: #2F5C94; border: 1px solid var(--blue); }
.sent-neg { background: var(--black); color: #FFFFFF; border: 1px solid var(--black); }
.sent-neu { background: #F3F4F6; color: #374151; border: 1px solid #D1D5DB; }

/* Упущенная выручка */
.loss-card { border-radius: 12px; padding: 18px 20px; font-weight: 600; }
.loss-red { background: var(--black); border: 1px solid var(--black); color: #E5E7EB; }
.loss-green { background: var(--blue-soft); border: 1px solid var(--blue); color: #2F5C94; }
.loss-amount { font-size: 2.2rem; font-weight: 800; margin: 4px 0 8px 0; }
.loss-red .loss-amount { color: #FFFFFF; }
.loss-green .loss-amount { color: var(--black); }
.loss-reason { font-weight: 400; font-size: .95rem; opacity: .95; }

/* Вкладки */
.stTabs [data-baseweb="tab-list"] { gap: 10px; border-bottom: 1px solid var(--border); }
.stTabs [data-baseweb="tab"] {
    height: 58px; padding: 0 26px; font-size: 1.1rem; font-weight: 600; color: var(--muted);
    background: transparent; border-radius: 12px 12px 0 0; transition: all .2s ease;
}
.stTabs [data-baseweb="tab"]:hover { color: var(--black); background: var(--surface); }
.stTabs [aria-selected="true"] { color: var(--black) !important; background: var(--blue-soft); }
.stTabs [data-baseweb="tab-highlight"] { background-color: var(--blue) !important; height: 3px; }
.stTabs [data-baseweb="tab-panel"] { padding-top: 1.6rem; }

/* Кнопки */
.stButton > button, .stDownloadButton > button {
    background: var(--black); color: #FFFFFF; border: 1px solid var(--black);
    border-radius: 10px; padding: 0.6rem 1.4rem; font-weight: 600; font-size: 1rem;
    box-shadow: 0 1px 3px rgba(17,24,39,.15); transition: all .2s ease;
}
.stButton > button:hover { background: var(--blue); color: var(--black); border-color: var(--blue); transform: translateY(-2px); box-shadow: 0 6px 16px rgba(91,143,207,.3); }
.stButton > button:active { transform: translateY(0); }
.stButton > button:focus:not(:active) { color: #FFFFFF; border-color: var(--black); }
.stButton > button:disabled { background: #E5E7EB; color: #9CA3AF; border-color: #E5E7EB; box-shadow: none; transform: none; }
.stButton > button p { color: inherit; }

/* Поля ввода */
.stTextInput input, .stTextArea textarea {
    background: #FFFFFF !important; border: 1px solid var(--border) !important;
    border-radius: 10px !important; color: var(--text) !important;
}
.stTextInput input:focus, .stTextArea textarea:focus { border-color: var(--blue) !important; box-shadow: 0 0 0 2px rgba(141,182,232,.35) !important; }
[data-testid="stChatInput"] { border-radius: 12px; }
[data-testid="stChatMessage"] { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 14px 18px; }
[data-testid="stFileUploader"] section { background: var(--surface); border: 1px dashed #D1D5DB; border-radius: 12px; }
.stProgress > div > div > div > div { background: var(--blue); }
[data-testid="stAlert"] { border-radius: 12px; }

/* Адаптивность */
@media (max-width: 900px) {
    .block-container { padding: 0 1rem 2rem 1rem; }
    .qadam-title { font-size: 1.8rem; }
    .stTabs [data-baseweb="tab"] { padding: 0 14px; font-size: 1rem; }
}
@media (min-width: 1800px) {
    .block-container { max-width: 1700px; }
    .qadam-title { font-size: 3rem; }
}
</style>
""",
    unsafe_allow_html=True,
)

# ───────────────────────────── Аккаунты и состояние ─────────────────────────────


def current_login() -> str:
    return st.session_state.get("account", "")


def current_business():
    return load_accounts().get(current_login())


def welcome_message(b) -> dict:
    return {"role": "assistant", "content": greeting(b) if b else "", "receipt": None}


def reset_chat() -> None:
    """Очищает историю чата продаж (в том числе в базе данных)."""
    login, b = current_login(), current_business()
    if login:
        db.clear_chat(login)
    st.session_state.messages = [welcome_message(b)]


def clear_assistant_chat() -> None:
    login = current_login()
    if login:
        db.clear_assistant(login)
    st.session_state.assistant_messages = []


def _open_workspace(login: str) -> None:
    """Загружает из базы всё, что нужно для работы под этим аккаунтом."""
    st.session_state.account = login
    st.session_state.last_audit = db.last_audit(login)
    st.session_state.last_feedback = db.last_feedback(login)
    st.session_state.messages = [welcome_message(current_business())] + db.load_chat(login)
    st.session_state.assistant_messages = db.load_assistant(login)
    st.session_state.page = "sales"


def login_user(login: str) -> None:
    _open_workspace(login)
    token = db.create_session(login)  # токен в ссылке: после обновления страницы входить заново не придётся
    st.session_state.auth_token = token
    st.query_params["s"] = token


def restore_session(accounts: dict) -> None:
    token = st.query_params.get("s", "")
    login = db.resolve_session(token) if token else None
    if login and login in accounts:
        _open_workspace(login)
        st.session_state.auth_token = token


def logout() -> None:
    db.delete_session(st.session_state.get("auth_token", ""))
    st.session_state.clear()
    st.query_params.clear()


def load_demo_data() -> None:
    """Заменяет демо-записи аккаунта (реальные данные не трогает)."""
    login, b = current_login(), current_business()
    if not login or not b:
        return
    t = type_info(b)

    def when(days_ago: int, hour: int = 12) -> str:
        return (datetime.now() - timedelta(days=days_ago)).replace(hour=hour, minute=15, second=0).strftime("%Y-%m-%d %H:%M:%S")

    orders = []
    for i, (detail, payment, items) in enumerate(DEMO_ORDERS.get(b["type"], DEMO_ORDERS["Другое"])):
        rows = [{"name": n, "qty": q, "price": p} for n, q, p in items]
        orders.append(
            {"detail": detail, "payment": payment, "items": rows,
             "total": sum(r["qty"] * r["price"] for r in rows), "created_at": when(2 - i, 11 + i * 2)}
        )
    audit_specs = [
        (72, 2400, f"Не предложены {t['upsell']}.", "Предлагайте одно дополнение к каждому обращению."),
        (85, 600, "Допродажа предложена не всем клиентам.", "Закрепите приветствие и уточняющий вопрос."),
        (64, 3100, "Дополнительные позиции не предлагались совсем.", "Называйте акцию при каждом обращении."),
    ]
    audits = [
        {"created_at": when(2 - i, 15),
         "result": {"qa_score": s, "missed_revenue_kzt": m, "missed_revenue_reason": r, "cashier_hint": h,
                    "criteria": {"Приветствие": 8, "Допродажи": max(2, s // 12), "Вежливость": 9}}}
        for i, (s, m, r, h) in enumerate(audit_specs)
    ]
    feedback_specs = [
        ("ПОЗИТИВ", "качество", "Клиенту понравилось качество.", "Очень понравилось, спасибо!"),
        ("ПОЗИТИВ", "сервис", "Похвала за вежливый персонал.", "Приятный персонал, вернёмся."),
        ("НЕГАТИВ", "ожидание", "Жалоба на долгое ожидание.", "Пришлось долго ждать."),
        ("НЕЙТРАЛЬНО", "цены", "Клиент нейтрально отметил цены.", "Нормально, но цены чуть высоковаты."),
    ]
    feedbacks = [
        {"created_at": when(3 - i, 18), "sentiment": s, "category": c, "summary": sm, "review": rv}
        for i, (s, c, sm, rv) in enumerate(feedback_specs)
    ]
    db.replace_demo(login, orders, audits, feedbacks)


def clear_demo_data() -> None:
    login = current_login()
    if login:
        db.clear_demo(login)


# ───────────────────────────── Хелперы Qwen ─────────────────────────────

THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


def get_api_key() -> str:
    """Ключ задаётся переменной QWEN_API_KEY (.env), в интерфейсе его вводить не нужно."""
    if API_KEY:
        return API_KEY
    try:
        return str(st.secrets.get("QWEN_API_KEY", "")).strip()
    except Exception:
        return ""


def get_client():
    key = get_api_key()
    return OpenAI(base_url=BASE_URL, api_key=key, timeout=90) if key else None


def call_qwen(messages: list, json_mode: bool = False, temperature: float = 0.7) -> str:
    """Запрос к Qwen через OpenAI-совместимый API. messages — список {"role", "content"}."""
    client = get_client()
    if client is None:
        raise RuntimeError(NO_KEY_MSG)
    kwargs = dict(model=MODEL, messages=messages, temperature=temperature)
    try:
        if json_mode:
            response = client.chat.completions.create(response_format={"type": "json_object"}, **kwargs)
        else:
            response = client.chat.completions.create(**kwargs)
    except BadRequestError:
        if not json_mode:
            raise
        # некоторые провайдеры не поддерживают response_format — пробуем без него
        response = client.chat.completions.create(**kwargs)
    text = response.choices[0].message.content or ""
    return THINK_RE.sub("", text).strip()  # убираем блок рассуждений <think>…</think>, если он есть


@st.cache_resource(show_spinner="Загружаем модель распознавания речи (при первом запуске — несколько минут)…")
def load_whisper(size: str):
    from faster_whisper import WhisperModel

    return WhisperModel(size, device="cpu", compute_type="int8")


def _decode_wav(audio_bytes: bytes):
    """Читает 16-битный WAV без PyAV (запись с микрофона): моно, 16 кГц, float32. Иначе None."""
    import wave

    import numpy as np

    try:
        with wave.open(io.BytesIO(audio_bytes), "rb") as w:
            channels, width, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
            raw = w.readframes(w.getnframes())
    except (wave.Error, EOFError):
        return None
    if width != 2 or not raw:
        return None
    data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        data = data[: len(data) // channels * channels].reshape(-1, channels).mean(axis=1)
    if rate != 16000:
        new_len = int(len(data) * 16000 / rate)
        data = np.interp(np.linspace(0, len(data) - 1, new_len), np.arange(len(data)), data)
    return data.astype(np.float32)


def transcribe_audio(audio_bytes: bytes, filename: str = "") -> str:
    """Локальная расшифровка аудио через faster-whisper (API xkiro не поддерживает /audio/transcriptions)."""
    import sys

    try:
        model = load_whisper(WHISPER_MODEL)
    except ImportError as exc:
        raise RuntimeError(
            "Для расшифровки аудио установите faster-whisper "
            f'(команда: "{sys.executable}" -m pip install faster-whisper) '
            "или вставьте текст диалога вручную в поле ниже."
        ) from exc
    try:
        audio = _decode_wav(audio_bytes)  # WAV читаем сами, остальные форматы декодирует PyAV
        source = audio if audio is not None else io.BytesIO(audio_bytes)
        segments, _ = model.transcribe(source, beam_size=5, vad_filter=True)
        text = " ".join(seg.text.strip() for seg in segments).strip()
    except Exception as exc:
        if "metadata_errors" in str(exc):
            try:
                from importlib.metadata import version

                av_version = version("av")
            except Exception:
                av_version = "не найдена"
            raise RuntimeError(
                f"Устарела библиотека PyAV (сейчас: {av_version}). Streamlit запущен из: {sys.executable}. "
                f'Выполните ИМЕННО эту команду и перезапустите приложение: "{sys.executable}" -m pip install -U av faster-whisper. '
                "Запись с микрофона (WAV) работает и без неё; для MP3/M4A обновление нужно. "
                "Пока можно вставить текст диалога вручную."
            ) from exc
        raise RuntimeError(f"Не удалось расшифровать аудио ({exc}). Вставьте текст диалога вручную.") from exc
    if not text:
        raise RuntimeError("В записи не найдена речь. Вставьте текст диалога вручную.")
    return text


def parse_json_loose(text: str):
    """Достаёт JSON-объект из ответа модели (с ```json``` или без)."""
    text = (text or "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return None


def extract_receipt(text: str):
    """Возвращает (текст_без_json, словарь_чека | None)."""
    fenced = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        data = parse_json_loose(fenced.group(1))
        clean = (text[: fenced.start()] + text[fenced.end():]).strip()
    else:
        data = None
        clean = text
        for candidate in re.finditer(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", text, re.DOTALL):
            parsed = parse_json_loose(candidate.group(0))
            if parsed and "total_amount" in parsed:
                data, clean = parsed, (text[: candidate.start()] + text[candidate.end():]).strip()
                break
    if data and "total_amount" in data and "items" in data:
        return clean, data
    return text, None


def card(key: str):
    return st.container(key=f"card_{key}")


# ───────────────────────────── Вход и верхняя панель ─────────────────────────────

NAV_LABELS = {
    "sales": ":material/chat: Онлайн-продажи",
    "coach": ":material/mic: Live-Coach",
    "feedback": ":material/star: Отзывы",
    "analytics": ":material/insights: ИИ-анализ",
    "assistant": ":material/smart_toy: Ассистент",
}
LOGIN_RE = re.compile(r"^[a-z0-9_-]{3,20}$")


def render_topbar(b=None) -> None:
    """Шапка как на обычном сайте: логотип слева, разделы по центру, аккаунт справа."""
    with st.container(key="topbar"):
        c_brand, c_nav, c_user = st.columns([1.6, 7.6, 2.8], vertical_alignment="center")
        with c_brand:
            st.markdown(
                f'<div class="brand">{ico("rocket_launch")}<span class="qadam-grad">Qadam AI</span></div>',
                unsafe_allow_html=True,
            )
        if b is None:
            return
        with c_nav:
            with st.container(key="nav"):
                st.radio(
                    "Раздел", list(NAV_LABELS), format_func=NAV_LABELS.get,
                    horizontal=True, key="page", label_visibility="collapsed",
                )
        with c_user:
            with st.popover(f":material/{type_info(b)['icon']}: {b['name']}"):
                st.markdown(f"**{b['name']}**")
                st.caption(f":material/category: {b['type']}")
                st.caption(f":material/location_on: {b['address']}")
                st.button(":material/dataset: Загрузить демо-данные", on_click=load_demo_data, key="menu_demo", use_container_width=True)
                st.button(":material/delete: Сбросить историю чата", on_click=reset_chat, key="menu_reset", use_container_width=True)
                st.button(":material/delete_sweep: Удалить демо-данные", on_click=clear_demo_data, key="menu_clear_demo", use_container_width=True)
                st.download_button(
                    ":material/download: Скачать мои данные",
                    data=json.dumps(db.export_account(current_login()), ensure_ascii=False, indent=2, default=str),
                    file_name=f"qadam_{current_login()}.json", mime="application/json",
                    key="menu_export", use_container_width=True,
                )
                st.button(":material/logout: Выйти", on_click=logout, key="menu_logout", use_container_width=True)


def page_header(title: str, subtitle: str) -> None:
    st.markdown(
        f'<div class="page-title">{html.escape(title)}</div><div class="page-sub">{html.escape(subtitle)}</div>',
        unsafe_allow_html=True,
    )


def render_auth(accounts: dict) -> None:
    _, mid, _ = st.columns([1, 2, 1])
    with mid:
        st.markdown(
            '<div class="page-title">Рабочее пространство вашего бизнеса</div>'
            '<div class="page-sub">Войдите в аккаунт или создайте новый — у каждого бизнеса свои данные, меню и ИИ-настройки.</div>',
            unsafe_allow_html=True,
        )
        tab_in, tab_new = st.tabs([":material/login: Вход", ":material/add_business: Создать аккаунт"])

        with tab_in:
            demo = [(k, v) for k, v in accounts.items() if v.get("demo")]
            if demo:
                st.markdown("**Демо-аккаунты** — вход в один клик")
                for login, acc in demo:
                    st.button(
                        f":material/{type_info(acc)['icon']}: {acc['name']} · {acc['type']}",
                        key=f"demo_{login}", on_click=login_user, args=(login,), use_container_width=True,
                    )
                st.caption(f"Для ручного входа: логин {', '.join(k for k, _ in demo)}; пароль {DEMO_PASSWORD}.")
                st.divider()
            login = st.text_input("Логин", key="auth_login").strip().lower()
            password = st.text_input("Пароль", type="password", key="auth_password")
            if st.button("Войти", key="auth_submit"):
                acc = accounts.get(login)
                ok = acc is not None and hmac.compare_digest(
                    acc.get("password_hash", ""), hash_password(login, password)
                )
                if ok:
                    login_user(login)
                    st.rerun()
                else:
                    st.error("Неверный логин или пароль.")

        with tab_new:
            with st.form("register_form"):
                name = st.text_input("Название бизнеса", placeholder="Например: Кофейня «Арай»")
                btype = st.selectbox("Тип бизнеса", list(BUSINESS_TYPES))
                address = st.text_input("Город и адрес", value="г. Тараз, ")
                catalog = st.text_area(
                    "Меню или прайс-лист (одна позиция в строке)", height=150,
                    placeholder="• Капучино — 1 500 ₸\n• Чизкейк — 1 800 ₸",
                )
                rules = st.text_area(
                    "Акции и особые условия (необязательно)", height=90,
                    placeholder="Скидка 10% по будням до 12:00",
                )
                new_login = st.text_input("Логин (латиница, цифры, _ или -)").strip().lower()
                new_password = st.text_input("Пароль (минимум 6 символов)", type="password")
                submitted = st.form_submit_button("Создать аккаунт")

            if submitted:
                problems = []
                if not name.strip():
                    problems.append("Укажите название бизнеса.")
                if not catalog.strip():
                    problems.append("Добавьте меню или прайс-лист — по нему ИИ будет отвечать клиентам.")
                if not LOGIN_RE.match(new_login):
                    problems.append("Логин: 3–20 символов, только латиница, цифры, _ и -.")
                elif new_login in accounts:
                    problems.append("Такой логин уже занят.")
                if len(new_password) < 6:
                    problems.append("Пароль должен быть не короче 6 символов.")
                if problems:
                    for p in problems:
                        st.error(p)
                else:
                    db.create_account(new_login, {
                        "name": name.strip(),
                        "type": btype,
                        "address": address.strip(),
                        "catalog": catalog.strip(),
                        "rules": rules.strip(),
                        "demo": False,
                        "password_hash": hash_password(new_login, new_password),
                    })
                    login_user(new_login)
                    st.rerun()


setup_database()
ACCOUNTS = load_accounts()
if "account" not in st.session_state:
    restore_session(ACCOUNTS)  # вход по сохранённой ссылке после обновления страницы
B = ACCOUNTS.get(st.session_state.get("account", ""))
render_topbar(B)
if B is None:
    render_auth(ACCOUNTS)
    st.stop()

LOGIN = current_login()
T = type_info(B)
page = st.session_state.get("page", "sales")

# ───────────────────────────── Вкладка 1: Sales ─────────────────────────────


def render_receipt(data: dict, key: str) -> None:
    items = data.get("items") or []
    df = pd.DataFrame(items) if items else pd.DataFrame(columns=["name", "qty", "price"])
    if not df.empty:
        df = df.reindex(columns=["name", "qty", "price"])
        df["qty"] = pd.to_numeric(df["qty"], errors="coerce").fillna(1).astype(int)
        df["price"] = pd.to_numeric(df["price"], errors="coerce").fillna(0)
        df["sum"] = df["qty"] * df["price"]
    total = to_int(data.get("total_amount", 0))

    with st.container(key=f"receipt_{key}"):
        st.markdown(
            f"""
            <div class="receipt-head">
                <span class="receipt-title">{ico('receipt_long')}ЧЕК · {html.escape(B['name'].upper())}</span>
                <span class="badge-ok">{ico('check_circle')}Успешно</span>
            </div>
            <div class="receipt-meta">{datetime.now().strftime('%d.%m.%Y %H:%M')} · {html.escape(B['address'])}</div>
            <hr style="border:none;border-top:1px dashed #9CA3AF;margin:12px 0;">
            """,
            unsafe_allow_html=True,
        )
        if not df.empty:
            view = df.rename(columns={"name": "Позиция", "qty": "Кол-во", "price": "Цена, ₸", "sum": "Сумма, ₸"})
            st.dataframe(view, hide_index=True, use_container_width=True)
        st.markdown(
            f"""
            <hr style="border:none;border-top:1px dashed #9CA3AF;margin:12px 0;">
            <div class="receipt-meta">{ico('location_on')}{html.escape(T['detail_short'])}: <b>{html.escape(str(data.get('customer_address', '—')))}</b></div>
            <div class="receipt-meta">{ico('credit_card')}Оплата: <b>{html.escape(str(data.get('payment_method', '—')))}</b></div>
            <div class="receipt-total"><span>ИТОГО</span><span>{fmt_money(total)} ₸</span></div>
            """,
            unsafe_allow_html=True,
        )


if page == "sales":
    page_header("Онлайн-продажи", f"ИИ-администратор принимает {T['order_plural']} в чате круглосуточно и предлагает допродажи.")
    orders = db.list_orders(LOGIN)
    n_orders = len(orders)
    avg_check = int(sum(o["total"] for o in orders) / n_orders) if n_orders else 0

    m1, m2, m3 = st.columns(3)
    m1.metric(f":material/shopping_bag: {T['orders_title']}", n_orders)
    m2.metric(":material/payments: Средний чек (₸)", fmt_money(avg_check))
    m3.metric(":material/smart_toy: Конверсия ИИ", "100%", help="Доля диалогов, доведённых ботом до заказа без участия оператора")

    st.write("")
    st.markdown("#### :material/chat: Чат с ИИ-администратором")

    for i, msg in enumerate(st.session_state.messages):
        with st.chat_message(msg["role"], avatar=":material/support_agent:" if msg["role"] == "assistant" else ":material/person:"):
            if msg["content"]:
                st.markdown(msg["content"])
            if msg.get("receipt"):
                render_receipt(msg["receipt"], key=f"hist_{i}")

    prompt = st.chat_input("Напишите сообщение… (Сәлем!)")
    if prompt:
        st.session_state.messages.append({"role": "user", "content": prompt, "receipt": None})
        db.add_chat_message(LOGIN, "user", prompt)
        with st.chat_message("user", avatar=":material/person:"):
            st.markdown(prompt)

        with st.chat_message("assistant", avatar=":material/support_agent:"):
            if get_client() is None:
                st.warning(NO_KEY_MSG)
            else:
                try:
                    chat = [{"role": "system", "content": sales_prompt(B)}] + [
                        {"role": m["role"], "content": m.get("raw") or m["content"] or "Заказ оформлен."}
                        for m in st.session_state.messages[-40:]
                    ]
                    with st.spinner("Qadam AI печатает…"):
                        raw = call_qwen(chat, temperature=0.6)
                    clean, receipt = extract_receipt(raw)
                    clean = strip_emoji(clean)
                    st.markdown(clean)
                    if receipt:
                        render_receipt(receipt, key="new")
                        db.add_order(
                            LOGIN,
                            receipt.get("customer_address", "—"),
                            to_int(receipt.get("total_amount", 0)),
                            receipt.get("payment_method", "—"),
                            receipt.get("items") or [],
                        )
                    st.session_state.messages.append(
                        {"role": "assistant", "content": clean, "receipt": receipt, "raw": raw}
                    )
                    db.add_chat_message(LOGIN, "assistant", clean, raw=raw, receipt=receipt)
                    if receipt:
                        st.rerun()  # обновить метрики сверху
                except Exception as exc:
                    st.error(f"Ошибка Qwen: {exc}")

    if orders:
        with st.expander(":material/receipt_long: Журнал заказов"):
            st.dataframe(
                pd.DataFrame(orders)[["time", "address", "total", "payment"]].iloc[::-1].rename(
                    columns={"time": "Время", "address": T["detail_short"], "total": "Сумма, ₸", "payment": "Оплата"}
                ),
                hide_index=True,
                use_container_width=True,
            )

# ───────────────────────────── Вкладка 2: Live-Coach ─────────────────────────────


def criteria_chart(criteria: dict) -> go.Figure:
    labels = list(criteria.keys())
    values = [float(to_int(v)) for v in criteria.values()]
    fig = go.Figure(
        go.Bar(
            x=values, y=labels, orientation="h",
            marker=dict(color="#8DB6E8"),
            text=[f"{v:.0f}/10" for v in values], textposition="outside",
            textfont=dict(color="#111827"),
        )
    )
    fig.update_layout(
        height=240, margin=dict(l=0, r=30, t=10, b=0),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        xaxis=dict(range=[0, 11], showgrid=False, visible=False),
        yaxis=dict(autorange="reversed", color="#111827"),
        font=dict(family="Inter, Roboto, sans-serif"),
    )
    return fig


def run_audit(dialog_text: str) -> None:
    messages = [
        {"role": "system", "content": audit_prompt(B)},
        {
            "role": "user",
            "content": f"Текст диалога (если в нём нет меток {T['staff_cap']}:/Клиент:, это автоматическая расшифровка "
            f"без разделения по ролям — определи роли по смыслу):\n{dialog_text}",
        },
    ]
    raw = call_qwen(messages, json_mode=True, temperature=0.3)
    data = parse_json_loose(raw)
    if not data:
        raise ValueError("Модель вернула ответ в неожиданном формате.")
    data["qa_score"] = max(0, min(100, to_int(data.get("qa_score", 0))))
    data["missed_revenue_kzt"] = max(0, to_int(data.get("missed_revenue_kzt", 0)))
    for k in ("transcript", "cashier_hint", "qa_comment", "missed_revenue_reason"):
        data[k] = strip_emoji(data.get(k, ""))
    db.add_audit(LOGIN, data)
    st.session_state.last_audit = data


if page == "coach":
    page_header("Live-Coach", f"Аудит диалогов {T['staff_gen']} с клиентами: оценка качества и упущенная выручка.")
    audits = db.list_audits(LOGIN)
    avg_qa = round(sum(a["qa_score"] for a in audits) / len(audits)) if audits else 0
    total_missed = sum(a["missed"] for a in audits)

    c1, c2, c3 = st.columns(3)
    c1.metric(f":material/grading: QA Score {T['staff_gen']}", f"{avg_qa}/100" if audits else "—")
    c2.metric(":material/money_off: Упущенная выручка (₸)", fmt_money(total_missed))
    c3.metric(":material/fact_check: Всего аудитов", len(audits))

    st.write("")
    with card("audio_input"):
        st.markdown(f'<div class="card-title">{ico("mic")}Запись диалога {T["staff_gen"]} с клиентом</div>', unsafe_allow_html=True)
        left, right = st.columns([1, 2])
        with left:
            st.caption("Нажмите на микрофон, чтобы начать/остановить запись")
            recorded = audio_recorder(
                text="", recording_color="#111827", neutral_color="#8DB6E8",
                icon_size="3x", pause_threshold=15.0, key="recorder",
            )
        with right:
            uploaded = st.file_uploader(
                "…или загрузите файл", type=["wav", "mp3", "m4a", "ogg", "webm"], key="audio_upload"
            )

        audio_bytes, mime, filename = None, "audio/wav", "recording.wav"
        if uploaded is not None:
            audio_bytes, mime, filename = uploaded.getvalue(), uploaded.type or "audio/mpeg", uploaded.name
        elif recorded:
            audio_bytes = recorded

        if audio_bytes:
            st.audio(audio_bytes, format=mime)
        dialog_text = st.text_area(
            "…или вставьте текст диалога (если расшифровка аудио недоступна)",
            height=120,
            placeholder=f"{T['staff_cap']}: Добрый день! Чем могу помочь?\nКлиент: Здравствуйте, подскажите по цене.",
            key="dialog_text",
        )
        has_input = audio_bytes is not None or bool(dialog_text.strip())
        run = st.button(":material/play_arrow: Запустить ИИ-аудит", disabled=not has_input, key="run_audit")

    if run and has_input:
        if get_client() is None:
            st.warning(NO_KEY_MSG)
        else:
            try:
                with st.spinner("Расшифровываем запись и готовим аудит…"):
                    text = dialog_text.strip() or transcribe_audio(audio_bytes, filename)
                    run_audit(text)
                st.rerun()
            except Exception as exc:
                st.error(f"Ошибка аудита: {exc}")

    result = st.session_state.last_audit
    if result:
        st.markdown("#### :material/search: Результаты ИИ-аудита")
        r1a, r1b = st.columns(2)
        r2a, r2b = st.columns(2)

        with r1a, card("transcript"):
            st.markdown(f'<div class="card-title">{ico("description")}Транскрипция</div>', unsafe_allow_html=True)
            st.markdown(
                f'<div class="gray-box card-scroll">{html.escape(str(result.get("transcript", "—")))}</div>',
                unsafe_allow_html=True,
            )

        with r1b, card("hint"):
            st.markdown(f'<div class="card-title">{ico("lightbulb")}Подсказка {T["staff_dat"]}</div>', unsafe_allow_html=True)
            st.info(str(result.get("cashier_hint", "—")))

        with r2a, card("qa"):
            st.markdown(f'<div class="card-title">{ico("bar_chart")}QA Оценка</div>', unsafe_allow_html=True)
            score = result["qa_score"]
            st.progress(score / 100, text=f"Общая оценка: {score}/100")
            criteria = result.get("criteria") or {}
            if criteria:
                st.plotly_chart(criteria_chart(criteria), use_container_width=True, config={"displayModeBar": False})
            if result.get("qa_comment"):
                st.caption(str(result["qa_comment"]))

        with r2b, card("missed"):
            st.markdown(f'<div class="card-title">{ico("money_off")}Упущенная выручка</div>', unsafe_allow_html=True)
            missed = result["missed_revenue_kzt"]
            reason = html.escape(str(result.get("missed_revenue_reason", "")))
            if missed > 0:
                st.markdown(
                    f'<div class="loss-card loss-red">{ico("trending_down")}Допродажи не предложены'
                    f'<div class="loss-amount">−{fmt_money(missed)} ₸</div>'
                    f'<div class="loss-reason">{reason}</div></div>',
                    unsafe_allow_html=True,
                )
            else:
                st.markdown(
                    f'<div class="loss-card loss-green">{ico("check_circle")}Отличная работа'
                    '<div class="loss-amount">0 ₸</div>'
                    f'<div class="loss-reason">Упущенной выручки нет. {reason}</div></div>',
                    unsafe_allow_html=True,
                )

    if len(audits) >= 2:
        with card("trend"):
            st.markdown(f'<div class="card-title">{ico("show_chart")}Динамика QA Score</div>', unsafe_allow_html=True)
            fig = go.Figure(
                go.Scatter(
                    x=list(range(1, len(audits) + 1)), y=[a["qa_score"] for a in audits],
                    mode="lines+markers", line=dict(color="#5B8FCF", width=3), marker=dict(size=9),
                    fill="tozeroy", fillcolor="rgba(141,182,232,0.22)",
                )
            )
            fig.update_layout(
                height=260, margin=dict(l=0, r=0, t=10, b=0),
                paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                xaxis=dict(title="№ аудита", dtick=1, gridcolor="#E5E7EB", color="#6B7280"),
                yaxis=dict(range=[0, 100], gridcolor="#E5E7EB", color="#6B7280"),
            )
            st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})

# ───────────────────────────── Вкладка 3: Feedback ─────────────────────────────

SENTIMENT_STYLE = {
    "ПОЗИТИВ": ("sent-pos", "thumb_up"),
    "НЕГАТИВ": ("sent-neg", "thumb_down"),
    "НЕЙТРАЛЬНО": ("sent-neu", "remove"),
}


def normalize_sentiment(value: str) -> str:
    v = (value or "").upper()
    if "НЕГАТ" in v:
        return "НЕГАТИВ"
    if "ПОЗИТ" in v:
        return "ПОЗИТИВ"
    return "НЕЙТРАЛЬНО"


def copy_button(text: str) -> None:
    payload = json.dumps(text)
    svg_copy = (
        '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
        'stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-3px;margin-right:8px">'
        '<rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>'
    )
    svg_ok = (
        '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" '
        'stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-3px;margin-right:8px">'
        '<polyline points="20 6 9 17 4 12"/></svg>'
    )
    label_copy = json.dumps(svg_copy + "Скопировать ответ")
    label_ok = json.dumps(svg_ok + "Скопировано")
    components.html(
        f"""
        <button id="cp" style="width:100%;padding:12px 18px;border:1px solid #111827;border-radius:10px;cursor:pointer;
            background:#111827;color:#fff;font-size:16px;font-weight:600;
            font-family:Inter,Roboto,sans-serif;box-shadow:0 1px 3px rgba(17,24,39,.15);transition:all .2s;"></button>
        <script>
        const text = {payload};
        const labelCopy = {label_copy};
        const labelOk = {label_ok};
        const btn = document.getElementById('cp');
        btn.innerHTML = labelCopy;
        btn.onmouseenter = () => {{ btn.style.background = '#8DB6E8'; btn.style.color = '#111827'; btn.style.borderColor = '#8DB6E8'; }};
        btn.onmouseleave = () => {{ btn.style.background = '#111827'; btn.style.color = '#fff'; btn.style.borderColor = '#111827'; }};
        btn.onclick = async () => {{
            try {{ await navigator.clipboard.writeText(text); }}
            catch (e) {{
                const ta = document.createElement('textarea'); ta.value = text;
                document.body.appendChild(ta); ta.select(); document.execCommand('copy'); ta.remove();
            }}
            btn.innerHTML = labelOk;
            setTimeout(() => btn.innerHTML = labelCopy, 2000);
        }};
        </script>
        """,
        height=56,
    )


if page == "feedback":
    page_header("Отзывы", "Анализ тональности отзывов и готовые ответы клиентам.")
    fbs = db.list_feedbacks(LOGIN)
    counts = {k: sum(1 for f in fbs if f["sentiment"] == k) for k in SENTIMENT_STYLE}

    f1, f2, f3 = st.columns(3)
    f1.metric(":material/thumb_up: Позитивных", counts["ПОЗИТИВ"])
    f2.metric(":material/remove: Нейтральных", counts["НЕЙТРАЛЬНО"])
    f3.metric(":material/thumb_down: Негативных", counts["НЕГАТИВ"])

    st.write("")
    left, right = st.columns([3, 2], gap="large")

    with left:
        with card("feedback_input"):
            st.markdown(f'<div class="card-title">{ico("rate_review")}Отзыв клиента</div>', unsafe_allow_html=True)
            review = st.text_area(
                "Вставьте отзыв (2ГИС, Instagram, WhatsApp…)",
                height=160,
                placeholder="Например: Всё понравилось, но пришлось долго ждать…",
                key="review_text",
            )
            analyze = st.button(":material/auto_awesome: Проанализировать и ответить", key="run_feedback")

        if analyze:
            if not review.strip():
                st.warning("Вставьте текст отзыва.")
            elif get_client() is None:
                st.warning(NO_KEY_MSG)
            else:
                try:
                    with st.spinner("Анализируем отзыв…"):
                        raw = call_qwen(
                            [
                                {"role": "system", "content": feedback_prompt(B)},
                                {"role": "user", "content": f"Отзыв клиента:\n{review.strip()}"},
                            ],
                            json_mode=True, temperature=0.5,
                        )
                    data = parse_json_loose(raw)
                    if not data:
                        raise ValueError("Модель вернула ответ в неожиданном формате.")
                    data["sentiment"] = normalize_sentiment(data.get("sentiment", ""))
                    for k in ("category", "summary", "reply"):
                        data[k] = strip_emoji(data.get(k, ""))
                    db.add_feedback(LOGIN, review.strip(), data)
                    st.session_state.last_feedback = data
                    st.rerun()
                except Exception as exc:
                    st.error(f"Ошибка анализа: {exc}")

        fb = st.session_state.last_feedback
        if fb:
            css_class, emoji = SENTIMENT_STYLE[fb["sentiment"]]
            with card("feedback_result"):
                st.markdown(
                    f'<span class="sent-badge {css_class}">{ico(emoji)}{fb["sentiment"]}</span> '
                    f'<span style="color:#6B7280;margin-left:12px;">Тема: <b>{html.escape(str(fb.get("category", "—")))}</b></span>',
                    unsafe_allow_html=True,
                )
                if fb.get("summary"):
                    st.caption(f"Суть: {fb['summary']}")
                st.markdown(f'<div class="card-title" style="margin-top:14px;">{ico("chat")}Ответ клиенту</div>', unsafe_allow_html=True)
                reply = str(fb.get("reply", ""))
                st.markdown(
                    f'<div style="border:1px solid #8DB6E8;border-radius:12px;padding:16px 18px;'
                    f'background:#F3F8FE;line-height:1.6;white-space:pre-wrap;">{html.escape(reply)}</div>',
                    unsafe_allow_html=True,
                )
                st.write("")
                copy_button(reply)

    with right:
        with card("feedback_chart"):
            st.markdown(f'<div class="card-title">{ico("pie_chart")}Тональность отзывов</div>', unsafe_allow_html=True)
            if fbs:
                fig = go.Figure(
                    go.Pie(
                        labels=list(counts.keys()), values=list(counts.values()), hole=0.6,
                        marker=dict(colors=["#8DB6E8", "#111827", "#D1D5DB"]),
                        textinfo="none",
                    )
                )
                fig.update_layout(
                    height=320, margin=dict(l=0, r=0, t=10, b=0),
                    paper_bgcolor="rgba(0,0,0,0)", font=dict(color="#111827"),
                    legend=dict(orientation="h", y=-0.1),
                )
                st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
            else:
                st.caption("Пока нет данных. Проанализируйте первый отзыв или загрузите демо-данные.")


# ───────────────────────────── Вкладка 4: ИИ-анализ ─────────────────────────────

EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF\U00002B00-\U00002BFF\uFE0F\u200D]+"
)


def clean_md(text: str) -> str:
    """Убирает эмодзи, не ломая отступы Markdown."""
    return EMOJI_RE.sub("", text or "").strip()


if page == "analytics":
    page_header("ИИ-анализ", "Цифры вашего бизнеса и ИИ-отчёты с конкретными рекомендациями. Все отчёты сохраняются.")
    snap = db.business_snapshot(LOGIN)
    so, sa, sf = snap["orders"], snap["audits"], snap["feedbacks"]
    has_data = bool(so["count"] or sa["count"] or sf["count"])

    k1, k2, k3, k4 = st.columns(4)
    k1.metric(":material/payments: Выручка (₸)", fmt_money(so["revenue"]))
    k2.metric(f":material/shopping_bag: {T['orders_title']}", so["count"])
    k3.metric(":material/receipt_long: Средний чек (₸)", fmt_money(so["avg_check"]))
    k4.metric(":material/money_off: Упущено на допродажах (₸)", fmt_money(sa["total_missed"]))

    st.write("")
    if has_data:
        ch1, ch2 = st.columns(2)
        with ch1, card("an_revenue"):
            st.markdown(f'<div class="card-title">{ico("show_chart")}Выручка по дням</div>', unsafe_allow_html=True)
            days = so["revenue_by_day"]
            if days:
                fig = go.Figure(go.Bar(x=list(days.keys()), y=list(days.values()), marker_color="#8DB6E8"))
                fig.update_layout(
                    height=280, margin=dict(l=10, r=10, t=10, b=10), plot_bgcolor="rgba(0,0,0,0)",
                    paper_bgcolor="rgba(0,0,0,0)", font=dict(color="#111827"), yaxis_title="₸",
                )
                st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
            else:
                st.caption("Пока нет заказов.")
        with ch2, card("an_top"):
            st.markdown(f'<div class="card-title">{ico("leaderboard")}Самые доходные позиции</div>', unsafe_allow_html=True)
            top = so["top_items"][:6][::-1]
            if top:
                fig = go.Figure(go.Bar(x=[t_["revenue"] for t_ in top], y=[t_["name"] for t_ in top],
                                       orientation="h", marker_color="#5B8FCF"))
                fig.update_layout(
                    height=280, margin=dict(l=10, r=10, t=10, b=10), plot_bgcolor="rgba(0,0,0,0)",
                    paper_bgcolor="rgba(0,0,0,0)", font=dict(color="#111827"), xaxis_title="₸",
                )
                st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
            else:
                st.caption("В заказах пока нет позиций.")
    else:
        st.info(
            "Данных пока нет. Загрузите демо-данные в меню аккаунта (справа вверху) "
            f"или оформите первый {T['order_noun']} в разделе «Онлайн-продажи»."
        )

    st.markdown("#### :material/auto_awesome: Сгенерировать ИИ-отчёт")
    with card("an_generate"):
        kind = st.selectbox(
            "Тип отчёта", list(REPORT_KINDS), format_func=lambda k: REPORT_KINDS[k][0], key="report_kind"
        )
        gen = st.button(":material/psychology: Сгенерировать отчёт", disabled=not has_data, key="gen_report")

    if gen:
        if get_client() is None:
            st.warning(NO_KEY_MSG)
        else:
            try:
                with st.spinner("ИИ анализирует данные бизнеса…"):
                    text = clean_md(call_qwen(report_messages(B, kind, snap), temperature=0.4))
                if not text:
                    raise ValueError("Модель вернула пустой ответ.")
                db.add_report(LOGIN, kind, REPORT_KINDS[kind][0], text)
                st.rerun()
            except Exception as exc:
                st.error(f"Ошибка отчёта: {exc}")

    reports = db.list_reports(LOGIN)
    st.markdown("#### :material/history: Сохранённые отчёты")
    if not reports:
        st.caption("Отчётов пока нет. Сгенерируйте первый — он сохранится здесь.")
    for i, rep in enumerate(reports):
        with st.expander(f"{rep['title']} · {rep['time']}", expanded=(i == 0)):
            st.markdown(rep["content"])
            d1, d2, _ = st.columns([1, 1, 3])
            d1.download_button(
                ":material/download: Скачать", data=rep["content"], file_name=f"qadam_report_{rep['id']}.md",
                mime="text/markdown", key=f"dl_report_{rep['id']}", use_container_width=True,
            )
            d2.button(
                ":material/delete: Удалить", key=f"del_report_{rep['id']}", on_click=db.delete_report,
                args=(LOGIN, rep["id"]), use_container_width=True,
            )


# ───────────────────────────── Вкладка 5: Персональный ассистент ─────────────────────────────

QUICK_PROMPTS = [
    ("Сводка по бизнесу", "Сделай краткую сводку по моим данным: что идёт хорошо, что тревожит и на что обратить внимание сегодня."),
    ("Идеи акций", "Предложи 3 идеи акций, которые поднимут средний чек. Учти моё меню или прайс и данные."),
    ("Пост в Instagram", "Напиши пост для Instagram про мой бизнес с одной из актуальных акций: основной текст на русском и короткая версия на казахском."),
    ("Поднять средний чек", "Как мне поднять средний чек за месяц? Дай конкретные шаги."),
    ("Инструкция сотрудникам", "Составь короткую инструкцию из 5 пунктов для сотрудников по допродажам."),
]

if page == "assistant":
    page_header(
        "Персональный ассистент",
        "Спросите про цифры, акции, тексты и планы. Ассистент знает ваш бизнес и помнит диалог.",
    )
    cols = st.columns(len(QUICK_PROMPTS))
    for i, (label, text) in enumerate(QUICK_PROMPTS):
        if cols[i].button(label, key=f"quick_{i}", use_container_width=True):
            st.session_state.pending_prompt = text
    st.button(":material/delete: Очистить диалог", key="assistant_clear", on_click=clear_assistant_chat)

    with st.chat_message("assistant", avatar=":material/smart_toy:"):
        st.markdown(
            f"Здравствуйте! Я ваш персональный ассистент по бизнесу «{B['name']}». "
            "Помогу с цифрами, акциями, текстами для соцсетей и планом на день. "
            "Выберите быстрый вопрос выше или напишите свой."
        )
    for msg in st.session_state.assistant_messages:
        with st.chat_message(msg["role"], avatar=":material/smart_toy:" if msg["role"] == "assistant" else ":material/person:"):
            st.markdown(msg["content"])

    typed = st.chat_input("Спросите ассистента…", key="assistant_input")
    prompt = typed or st.session_state.pop("pending_prompt", None)
    if prompt:
        st.session_state.assistant_messages.append({"role": "user", "content": prompt})
        db.add_assistant_message(LOGIN, "user", prompt)
        with st.chat_message("user", avatar=":material/person:"):
            st.markdown(prompt)
        with st.chat_message("assistant", avatar=":material/smart_toy:"):
            if get_client() is None:
                st.warning(NO_KEY_MSG)
            else:
                try:
                    history = [
                        {"role": m["role"], "content": m["content"]}
                        for m in st.session_state.assistant_messages[-30:]
                    ]
                    chat = [{"role": "system", "content": assistant_prompt(B, db.business_snapshot(LOGIN))}] + history
                    with st.spinner("Ассистент думает…"):
                        answer = clean_md(call_qwen(chat, temperature=0.6))
                    st.markdown(answer)
                    st.session_state.assistant_messages.append({"role": "assistant", "content": answer})
                    db.add_assistant_message(LOGIN, "assistant", answer)
                except Exception as exc:
                    st.error(f"Ошибка ассистента: {exc}")
