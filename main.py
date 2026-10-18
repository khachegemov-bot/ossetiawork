import os
import sys
import sqlite3
import threading
import time
import hashlib


class _HideBotToken:
    """Прячет токен бота во всём, что приложение пишет в журнал.

    Telegram принимает токен прямо в адресе запроса (api.telegram.org/bot<токен>/...),
    и при сбоях связи библиотека бота печатает этот адрес целиком. Без замены
    токен открытым текстом копится в системном журнале.

    Стоит до подключения telebot и Flask: библиотека бота запоминает,
    куда писать, в момент подключения.
    """

    def __init__(self, stream, secret):
        self._stream = stream
        self._secret = secret

    def write(self, text):
        if isinstance(text, str):
            text = text.replace(self._secret, "<BOT_TOKEN>")
        return self._stream.write(text)

    def __getattr__(self, name):
        return getattr(self._stream, name)


_TOKEN_FOR_LOGS = os.getenv("BOT_TOKEN", "").strip()
if _TOKEN_FOR_LOGS:
    sys.stdout = _HideBotToken(sys.stdout, _TOKEN_FOR_LOGS)
    sys.stderr = _HideBotToken(sys.stderr, _TOKEN_FOR_LOGS)

from flask import Flask, render_template, request, jsonify, redirect, url_for, g
from werkzeug.exceptions import HTTPException
from datetime import datetime, timedelta
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo


import telebot
from telebot import types
from telebot.util import parse_web_app_data

# ======================
# SETTINGS
# ======================
print("MAIN FILE LOADED")
TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://ossetiawork.ru").strip().rstrip("/")

ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

# Что видит человек, если сервер не принял пропуск Telegram.
# Внутри Telegram это почти всегда означает устаревший пропуск:
# приложение долго провисело свёрнутым. Помогает переоткрыть его.
AUTH_ERROR_TEXT = "Сессия устарела. Закройте приложение и откройте его заново."

# initData max age
INITDATA_MAX_AGE_SEC = 7 * 24 * 60 * 60  # 7 дней # 10 минут

# anti-replay (НЕ включать в WebApp, обычно ломает)
ANTI_REPLAY = False

# ✅ ВАЖНО: по умолчанию DEV_MODE выключен
DEV_MODE = bool(int(os.getenv("DEV_MODE", "0")))
DEV_TELEGRAM_ID = int(os.getenv("DEV_TELEGRAM_ID", "0"))

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE_URL = os.getenv("DATABASE_URL", os.path.join(BASE_DIR, "database.db")).strip()

if not TOKEN:
    raise RuntimeError("BOT_TOKEN is not set. Set environment variable BOT_TOKEN.")

bot = telebot.TeleBot(TOKEN)
# Имя бота нужно, чтобы собирать ссылки на вакансии для пересылки.
# Спрашиваем у Telegram один раз при запуске; если он недоступен —
# берём запасное значение, иначе процесс не поднимется вообще.
try:
    BOT_USERNAME = bot.get_me().username
except Exception as _e:
    BOT_USERNAME = os.getenv("BOT_USERNAME", "rabota_alagir_bot")
    print(f"BOT USERNAME: не удалось спросить у Telegram ({_e}), беру {BOT_USERNAME}", flush=True)
app = Flask(__name__)




print("APP ID:", id(app))
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024  # 64KB



# ======================
# DB
# ======================

def db_connect():
    conn = sqlite3.connect(DATABASE_URL, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    return conn


def init_db():
    conn = db_connect()
    c = conn.cursor()

    c.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_id INTEGER UNIQUE,
        role TEXT NOT NULL DEFAULT 'applicant' CHECK(role IN ('applicant','employer')),
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """)

    c.execute("PRAGMA table_info(users)")
    cols = [row["name"] for row in c.fetchall()]
    if "role" not in cols:
        c.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'applicant'")
        conn.commit()
    if "active_mode" not in cols:
        c.execute("ALTER TABLE users ADD COLUMN active_mode TEXT DEFAULT 'applicant'")
        conn.commit()


    c.execute("""
    CREATE TABLE IF NOT EXISTS employer_profiles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_id INTEGER UNIQUE,
        company_name TEXT,
        contact_name TEXT,
        phone TEXT,
        status TEXT DEFAULT 'На проверке',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(telegram_id) REFERENCES users(telegram_id) ON DELETE CASCADE
    )
    """)

    c.execute("PRAGMA table_info(employer_profiles)")
    employer_cols = [row["name"] for row in c.fetchall()]

    if "status" not in employer_cols:
        c.execute("ALTER TABLE employer_profiles ADD COLUMN status TEXT DEFAULT 'На проверке'")
        conn.commit() 

    # Колонки профиля работодателя, добавленные позже
    for col, ddl in (
        ("verified", "INTEGER DEFAULT 0"),
        ("employer_type", "TEXT NOT NULL DEFAULT 'company'"),
        ("activity", "TEXT"),
        ("about", "TEXT"),
        ("link", "TEXT"),
    ):
        if col not in employer_cols:
            c.execute(f"ALTER TABLE employer_profiles ADD COLUMN {col} {ddl}")
    conn.commit()

    c.execute("""
    CREATE TABLE IF NOT EXISTS resumes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_id INTEGER UNIQUE,
        full_name TEXT,
        age INTEGER,
        city TEXT,
        position TEXT,
        experience TEXT,
        skills TEXT,
        about TEXT,
        phone TEXT,
        employment_type TEXT,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(telegram_id) REFERENCES users(telegram_id) ON DELETE CASCADE
    )
    """)
     
    c.execute("PRAGMA table_info(resumes)")
    resume_cols = [row["name"] for row in c.fetchall()]

    if "full_name" not in resume_cols:
        c.execute("ALTER TABLE resumes ADD COLUMN full_name TEXT")
        conn.commit()

    if "age" not in resume_cols:
        c.execute("ALTER TABLE resumes ADD COLUMN age INTEGER")
        conn.commit()

    if "city" not in resume_cols:
        c.execute("ALTER TABLE resumes ADD COLUMN city TEXT")
        conn.commit()

    if "position" not in resume_cols:
        c.execute("ALTER TABLE resumes ADD COLUMN position TEXT")
        conn.commit()

    if "experience" not in resume_cols:
        c.execute("ALTER TABLE resumes ADD COLUMN experience TEXT")
        conn.commit()

    if "skills" not in resume_cols:
        c.execute("ALTER TABLE resumes ADD COLUMN skills TEXT")
        conn.commit()

    if "about" not in resume_cols:
        c.execute("ALTER TABLE resumes ADD COLUMN about TEXT")
        conn.commit()

    if "phone" not in resume_cols:
        c.execute("ALTER TABLE resumes ADD COLUMN phone TEXT")
        conn.commit()

    if "employment_type" not in resume_cols:
        c.execute("ALTER TABLE resumes ADD COLUMN employment_type TEXT")
        conn.commit()

    c.execute("""
    CREATE TABLE IF NOT EXISTS jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        salary_text TEXT,
        description TEXT,
        city TEXT,
        status TEXT DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected')),
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(telegram_id) REFERENCES users(telegram_id) ON DELETE CASCADE
    )
    """)

    # migrations for jobs
    c.execute("PRAGMA table_info(jobs)")
    job_cols = [row["name"] for row in c.fetchall()]
    if "company_name_override" not in job_cols:
        c.execute("ALTER TABLE jobs ADD COLUMN company_name_override TEXT")
        conn.commit()
    if "phone_override" not in job_cols:
        c.execute("ALTER TABLE jobs ADD COLUMN phone_override TEXT")
        conn.commit()
    if "employment_type" not in job_cols:
        c.execute("ALTER TABLE jobs ADD COLUMN employment_type TEXT")
        conn.commit()

    if "reject_reason" not in job_cols:
        c.execute("ALTER TABLE jobs ADD COLUMN reject_reason TEXT")
        conn.commit()

    if "is_deleted" not in job_cols:
        c.execute("ALTER TABLE jobs ADD COLUMN is_deleted INTEGER NOT NULL DEFAULT 0")
        conn.commit()

    if "deleted_at" not in job_cols:
        c.execute("ALTER TABLE jobs ADD COLUMN deleted_at DATETIME")
        conn.commit()

    if "address" not in job_cols:
        c.execute("ALTER TABLE jobs ADD COLUMN address TEXT")
        conn.commit()


    c.execute("""
    CREATE TABLE IF NOT EXISTS applications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id INTEGER NOT NULL,
        applicant_id INTEGER NOT NULL,
        status TEXT DEFAULT 'new' CHECK(status IN ('new','interview','accepted','rejected')),
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(job_id, applicant_id),
        FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE,
        FOREIGN KEY(applicant_id) REFERENCES users(telegram_id) ON DELETE CASCADE
    )
    """)

    c.execute("PRAGMA table_info(applications)")
    app_cols = [row["name"] for row in c.fetchall()]

    if "reject_reason" not in app_cols:
        c.execute("ALTER TABLE applications ADD COLUMN reject_reason TEXT")
        conn.commit()

    c.execute("""
    CREATE TABLE IF NOT EXISTS chat_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        application_id INTEGER NOT NULL,
        sender_id INTEGER NOT NULL,
        message_text TEXT NOT NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(application_id) REFERENCES applications(id) ON DELETE CASCADE,
        FOREIGN KEY(sender_id) REFERENCES users(telegram_id) ON DELETE CASCADE
    )
    """)

    c.execute("CREATE INDEX IF NOT EXISTS idx_chat_application ON chat_messages(application_id);")
    c.execute("CREATE INDEX IF NOT EXISTS idx_chat_created ON chat_messages(created_at);")

    c.execute("""
    CREATE TABLE IF NOT EXISTS chat_reads (
        application_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        last_read_message_id INTEGER NOT NULL DEFAULT 0,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY(application_id, user_id),
        FOREIGN KEY(application_id) REFERENCES applications(id) ON DELETE CASCADE,
        FOREIGN KEY(user_id) REFERENCES users(telegram_id) ON DELETE CASCADE
    )
    """)

    c.execute("CREATE INDEX IF NOT EXISTS idx_chat_reads_user ON chat_reads(user_id);")


    c.execute("""
    CREATE TABLE IF NOT EXISTS favorites (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        applicant_id INTEGER NOT NULL,
        job_id INTEGER NOT NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(applicant_id, job_id),
        FOREIGN KEY(applicant_id) REFERENCES users(telegram_id) ON DELETE CASCADE,
        FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS job_views (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id INTEGER NOT NULL,
        viewer_id INTEGER NOT NULL,
        viewed_on TEXT NOT NULL DEFAULT (date('now')),
        UNIQUE(job_id, viewer_id, viewed_on),
        FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE,
        FOREIGN KEY(viewer_id) REFERENCES users(telegram_id) ON DELETE CASCADE
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS city_subscriptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        applicant_id INTEGER NOT NULL,
        city TEXT NOT NULL,
        is_enabled INTEGER NOT NULL DEFAULT 1,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(applicant_id),
        FOREIGN KEY(applicant_id) REFERENCES users(telegram_id) ON DELETE CASCADE
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS keyword_subscriptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        applicant_id INTEGER NOT NULL,
        city TEXT NOT NULL,
        keywords TEXT NOT NULL,
        is_enabled INTEGER NOT NULL DEFAULT 1,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(applicant_id),
        FOREIGN KEY(applicant_id) REFERENCES users(telegram_id) ON DELETE CASCADE
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS notification_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id INTEGER NOT NULL,
        applicant_id INTEGER NOT NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(job_id, applicant_id),
        FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE,
        FOREIGN KEY(applicant_id) REFERENCES users(telegram_id) ON DELETE CASCADE
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS security_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_type TEXT NOT NULL,
        telegram_id INTEGER,
        ip TEXT,
        user_agent TEXT,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_sec_type ON security_events(event_type);")
    c.execute("CREATE INDEX IF NOT EXISTS idx_sec_created ON security_events(created_at);")


    c.execute("""
    CREATE TABLE IF NOT EXISTS employer_bans (
        telegram_id INTEGER PRIMARY KEY,
        is_banned INTEGER NOT NULL DEFAULT 1,
        reason TEXT,
        banned_until DATETIME,
        banned_by INTEGER,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """)

    c.execute("PRAGMA table_info(employer_bans)")
    ban_cols = [row["name"] for row in c.fetchall()]

    if "banned_until" not in ban_cols:
        c.execute("ALTER TABLE employer_bans ADD COLUMN banned_until DATETIME")
        conn.commit()

    if "banned_by" not in ban_cols:
        c.execute("ALTER TABLE employer_bans ADD COLUMN banned_by INTEGER")
        conn.commit()



    # indexes
    c.execute("CREATE INDEX IF NOT EXISTS idx_fav_applicant ON favorites(applicant_id);")
    c.execute("CREATE INDEX IF NOT EXISTS idx_fav_job ON favorites(job_id);")
    c.execute("CREATE INDEX IF NOT EXISTS idx_views_job ON job_views(job_id);")
    c.execute("CREATE INDEX IF NOT EXISTS idx_views_viewer ON job_views(viewer_id);")
    c.execute("CREATE INDEX IF NOT EXISTS idx_views_day ON job_views(viewed_on);")
    c.execute("CREATE INDEX IF NOT EXISTS idx_sub_city ON city_subscriptions(city);")
    c.execute("CREATE INDEX IF NOT EXISTS idx_sub_enabled ON city_subscriptions(is_enabled);")
    c.execute("CREATE INDEX IF NOT EXISTS idx_kw_city ON keyword_subscriptions(city);")
    c.execute("CREATE INDEX IF NOT EXISTS idx_kw_enabled ON keyword_subscriptions(is_enabled);")
    c.execute("CREATE INDEX IF NOT EXISTS idx_notif_job ON notification_log(job_id);")
    c.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);")
    c.execute("CREATE INDEX IF NOT EXISTS idx_jobs_employer ON jobs(telegram_id);")
    c.execute("CREATE INDEX IF NOT EXISTS idx_jobs_city ON jobs(city);")
    c.execute("CREATE INDEX IF NOT EXISTS idx_apps_job ON applications(job_id);")
    c.execute("CREATE INDEX IF NOT EXISTS idx_apps_applicant ON applications(applicant_id);")

    # Напоминания работодателям и время первого ответа на отклик
    c.execute("PRAGMA table_info(applications)")
    app_cols = [row["name"] for row in c.fetchall()]
    if "reminded_at" not in app_cols:
        c.execute("ALTER TABLE applications ADD COLUMN reminded_at DATETIME")
        # Отклики, пришедшие до появления напоминаний, считаем уже напомненными:
        # иначе в первый же запуск работодатели получат сообщение о старых откликах
        c.execute("UPDATE applications SET reminded_at = CURRENT_TIMESTAMP")
    if "answered_at" not in app_cols:
        c.execute("ALTER TABLE applications ADD COLUMN answered_at DATETIME")

    c.execute("""
    CREATE TABLE IF NOT EXISTS employer_reminders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        employer_id INTEGER NOT NULL,
        applications_count INTEGER NOT NULL,
        sent_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_reminders_employer ON employer_reminders(employer_id, sent_at);")

    c.execute("""
        DELETE FROM jobs
        WHERE COALESCE(is_deleted, 0)=1
          AND deleted_at IS NOT NULL
          AND deleted_at <= datetime('now', '-10 days')
    """)
    conn.commit()
    conn.close()


init_db()

# ======================
# BOT COMMANDS
# ======================

def send_job_card(chat_id, job_id: int) -> bool:
    """Показывает вакансию тому, кто пришёл по пересланной ссылке.

    Возвращает False, если вакансии нет: её могли снять, отклонить
    или удалить уже после того, как ссылку переслали дальше.
    """
    conn = db_connect()
    c = conn.cursor()
    c.execute("""
        SELECT
            j.id, j.title, j.city, j.address, j.salary_text, j.description,
            COALESCE(j.company_name_override, ep.company_name, '') AS company_name
        FROM jobs j
        LEFT JOIN employer_profiles ep ON ep.telegram_id = j.telegram_id
        WHERE j.id=? AND j.status='approved' AND j.is_deleted=0
    """, (job_id,))
    job = c.fetchone()
    conn.close()

    if not job:
        return False

    place = " · ".join(x for x in [job["city"], job["address"]] if x)
    description = (job["description"] or "").strip()

    text = job["title"] or "Вакансия"

    if job["company_name"]:
        text += "\n" + job["company_name"]

    if job["salary_text"]:
        text += "\n\n" + job["salary_text"]

    if place:
        text += "\n" + place

    if description:
        text += "\n\n" + description[:400] + ("..." if len(description) > 400 else "")

    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton(
        "Откликнуться",
        web_app=types.WebAppInfo(f"{WEBAPP_URL}/job/{job_id}")
    ))
    markup.add(types.InlineKeyboardButton(
        "Все вакансии",
        web_app=types.WebAppInfo(WEBAPP_URL)
    ))

    bot.send_message(chat_id, text, reply_markup=markup)
    return True


@bot.message_handler(commands=["start"])
def start(message):
    # Ссылка вида t.me/<бот>?start=job_84 приходит сюда параметром.
    parts = (message.text or "").split(maxsplit=1)
    payload = parts[1].strip() if len(parts) > 1 else ""

    if payload.startswith("job_"):
        job_id = payload[4:]

        if job_id.isdigit() and send_job_card(message.chat.id, int(job_id)):
            return

        bot.send_message(
            message.chat.id,
            "Эту вакансию уже сняли. Посмотрите остальные — возможно, есть подходящая."
        )

    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Ossetia’s Work 🏔", web_app=types.WebAppInfo(WEBAPP_URL)))
    bot.send_message(
        message.chat.id,
        """Добро пожаловать в Ossetia’s Work

Поиск работы по Северной Осетии.

Если ищете работу — нажмите кнопку ниже. Там вакансии по городам и сёлам, отклик в два нажатия и переписка с работодателем прямо здесь, в Telegram.

Если ищете сотрудников — там же создаётся профиль компании и публикуются вакансии.

Есть вопрос? Напишите его прямо в этот чат — он придёт администратору, и ответ вернётся сюда же.""",
        reply_markup=markup
    )


@bot.message_handler(commands=["admin"])
def open_admin(message):
    if message.from_user.id == ADMIN_ID:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("Открыть админку", web_app=types.WebAppInfo(WEBAPP_URL + "/admin")))
        bot.send_message(message.chat.id, "Админка:", reply_markup=markup)


# ======================
# TELEGRAM AUTH
# ======================

_seen_init_hashes: dict[str, int] = {}
_seen_lock = threading.Lock()


def _cleanup_seen(now: int):
    dead = [k for k, exp in _seen_init_hashes.items() if exp <= now]
    for k in dead:
        _seen_init_hashes.pop(k, None)


PHONE_ALLOWED_EXTRA = " -()"


def normalize_phone(raw):
    """Приводит телефон к единому виду и проверяет его.

    Люди пишут номер как им удобно: «+7 900 000-00-00», «8(900)0000000».
    Разделители отбрасываем, проверяем только цифры. Подтвердить, что номер
    настоящий, этим нельзя — проверяется лишь написание.

    Возвращает (номер, ошибка). Ошибка None, если всё в порядке.
    """
    phone = str(raw or "").strip()

    if not phone:
        return None, "Укажите телефон"

    for ch in phone:
        if not (ch.isdigit() or ch == "+" or ch in PHONE_ALLOWED_EXTRA):
            return None, "Телефон должен состоять из цифр"

    digits = "".join(ch for ch in phone if ch.isdigit())

    if len(digits) < 10 or len(digits) > 15:
        return None, "Телефон должен содержать от 10 до 15 цифр"

    return ("+" if phone.lstrip().startswith("+") else "") + digits, None


def client_ip() -> str:
    """Настоящий адрес того, кто прислал запрос.

    Приложение стоит за nginx, поэтому само видит только 127.0.0.1.
    Настоящий адрес nginx передаёт в заголовке X-Real-IP. Заголовку верим,
    лишь когда запрос действительно пришёл от nginx с этого же сервера —
    иначе любой мог бы подставить в журнал чужой адрес.
    """
    addr = request.remote_addr or ""
    if addr in ("127.0.0.1", "::1"):
        return (request.headers.get("X-Real-IP") or addr).strip()[:64]
    return addr


def log_security_event(event_type: str, telegram_id: int | None):
    try:
        conn = db_connect()
        c = conn.cursor()
        c.execute("""
            INSERT INTO security_events (event_type, telegram_id, ip, user_agent)
            VALUES (?, ?, ?, ?)
        """, (event_type, telegram_id, client_ip(), request.headers.get("User-Agent")))
        conn.commit()
        conn.close()
    except Exception:
        pass


def _auth_from_initdata(init_data: str):
    try:
        data = parse_web_app_data(TOKEN, init_data)
    except Exception:
        log_security_event("invalid_initdata_signature", None)
        return None, None

    if not data or "user" not in data or not data["user"] or "id" not in data["user"]:
        return None, None

    try:
        auth_date = int(data.get("auth_date") or 0)
    except Exception:
        return None, None

    now = int(time.time())
    if INITDATA_MAX_AGE_SEC > 0 and (now - auth_date) > INITDATA_MAX_AGE_SEC:
        log_security_event("expired_initdata", int(data["user"]["id"]))
        return None, None

    if ANTI_REPLAY:
        h = hashlib.sha256(init_data.encode("utf-8")).hexdigest()
        with _seen_lock:
            _cleanup_seen(now)
            if h in _seen_init_hashes:
                log_security_event("replayed_initdata", int(data["user"]["id"]))
                return None, None
            _seen_init_hashes[h] = now + INITDATA_MAX_AGE_SEC

    return int(data["user"]["id"]), data["user"]


# Адреса, открытые без пропуска Telegram.
# Всё остальное под /api/ без действительного пропуска не пускаем.
PUBLIC_API_PATHS = {
    "/api/locations",   # список населённых пунктов
    "/api/jobs",        # витрина вакансий
}
PUBLIC_API_PREFIXES = (
    "/api/jobs/",       # одна вакансия из витрины
)


def _is_public_path(path: str) -> bool:
    """Страницы и статика открыты: данных в них нет, они только каркас.

    Данные отдаются исключительно через /api/, и там по умолчанию закрыто.
    Новый маршрут, добавленный без проверки, окажется закрыт, а не открыт.
    """
    if not path.startswith("/api/"):
        return True
    if path in PUBLIC_API_PATHS:
        return True
    return path.startswith(PUBLIC_API_PREFIXES)


@app.before_request
def check_auth():
    init_data = request.headers.get("X-Telegram-Init-Data", "").strip()

    if not init_data:
        if DEV_MODE:
            g.telegram_id = DEV_TELEGRAM_ID
            ensure_user(DEV_TELEGRAM_ID)
            return
        if _is_public_path(request.path):
            return
        log_security_event("missing_initdata", None)
        return jsonify({"error": AUTH_ERROR_TEXT}), 401

    tid, user = _auth_from_initdata(init_data)
    if tid:
        g.telegram_id = tid
        g.tg_user = user
        ensure_user(tid)
        return

    # пропуск есть, но подпись не сходится или он просрочен
    if _is_public_path(request.path):
        return
    return jsonify({"error": AUTH_ERROR_TEXT}), 401


# ======================
# LOCATIONS
# ======================

OSSETIA_LOCATIONS = [
    "Владикавказ", "Беслан", "Алагир", "Ардон", "Дигора", "Моздок", "Мизур", "Архонская",
    "Октябрьское", "Чикола", "Эльхотово", "Майское", "Ногир", "Михайловское", "Камбилеевское",
    "Дачное", "Чермен", "Гизель", "Сунжа", "Карджин", "Ольгинское", "Фиагдон", "Верхний Фиагдон",
    "Дзуарикау", "Хумалаг", "Зильги", "Коста", "Дарг-Кох", "Павлодольская",
]


# ======================
# ERROR / NO-CACHE
# ======================


@app.errorhandler(Exception)
def handle_error(e):
    import traceback
    traceback.print_exc()

    if isinstance(e, HTTPException):
        return jsonify({"error": e.description}), e.code

    return jsonify({"error": "Внутренняя ошибка сервера"}), 500






@app.after_request
def add_no_cache_headers(response):
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


# ======================
# HELPERS
# ======================

def require_auth():
    init_data = request.headers.get("X-Telegram-Init-Data")

    if not init_data:
        return None, (jsonify({"error": AUTH_ERROR_TEXT}), 401)

    try:
        data = parse_web_app_data(TOKEN, init_data)
        telegram_id = int(data["user"]["id"])
        return telegram_id, None
    except Exception:
        return None, (jsonify({"error": "Invalid signature"}), 403)


def ensure_user(tid: int, role: str = "applicant", active_mode: str = "applicant"):
    conn = db_connect()
    c = conn.cursor()

    c.execute("PRAGMA table_info(users)")
    cols = [row["name"] for row in c.fetchall()]

    if "role" in cols and "active_mode" in cols:
        c.execute("""
            INSERT OR IGNORE INTO users (telegram_id, role, active_mode)
            VALUES (?, ?, ?)
        """, (tid, role, active_mode))
    elif "role" in cols:
        c.execute("""
            INSERT OR IGNORE INTO users (telegram_id, role)
            VALUES (?, ?)
        """, (tid, role))
    elif "active_mode" in cols:
        c.execute("""
            INSERT OR IGNORE INTO users (telegram_id, active_mode)
            VALUES (?, ?)
        """, (tid, active_mode))
    else:
        c.execute("""
            INSERT OR IGNORE INTO users (telegram_id)
            VALUES (?)
        """, (tid,))

    conn.commit()
    conn.close()

def has_resume(tid: int) -> bool:
    conn = db_connect()
    c = conn.cursor()
    c.execute("SELECT 1 FROM resumes WHERE telegram_id=?", (tid,))
    ok = c.fetchone() is not None
    conn.close()
    return ok


def has_employer_profile(tid: int) -> bool:
    conn = db_connect()
    c = conn.cursor()
    c.execute("SELECT 1 FROM employer_profiles WHERE telegram_id=?", (tid,))
    ok = c.fetchone() is not None
    conn.close()
    return ok


def is_banned_employer(tid: int) -> tuple[bool, str]:
    conn = db_connect()
    c = conn.cursor()
    c.execute("""
        SELECT is_banned, reason, banned_until
        FROM employer_bans
        WHERE telegram_id=?
    """, (tid,))
    row = c.fetchone()

    if not row:
        conn.close()
        return False, ""

    is_banned = bool(row["is_banned"])
    reason = row["reason"] or ""
    banned_until = row["banned_until"]

    if not is_banned:
        conn.close()
        return False, ""

    # если бан временный и срок уже истёк — автоматически снимаем бан
    if banned_until:
        now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        if banned_until <= now_str:
            c.execute("""
                UPDATE employer_bans
                SET is_banned=0, banned_until=NULL
                WHERE telegram_id=?
            """, (tid,))
            c.execute("""
                UPDATE employer_profiles
                SET status='Проверен'
                WHERE telegram_id=?
            """, (tid,))
            conn.commit()
            conn.close()
            return False, ""

        conn.close()
        return True, f"{reason} До: {banned_until}".strip()

    conn.close()
    return True, reason
    # если бан временный и срок уже истёк — автоматически снимаем бан
    if banned_until:
        now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        if banned_until <= now_str:
            c.execute("""
                UPDATE employer_bans
                SET is_banned=0, banned_until=NULL
                WHERE telegram_id=?
            """, (tid,))
            c.execute("""
                UPDATE employer_profiles
                SET status='Проверен'
                WHERE telegram_id=?
            """, (tid,))
            conn.commit()
            conn.close()
            return False, ""

        conn.close()
        return True, f"{reason} До: {banned_until}".strip()

    conn.close()
    return True, reason



def get_employer_status(tid: int) -> str:
    banned, _ = is_banned_employer(tid)
    if banned:
        return "Заблокирован"

    conn = db_connect()
    c = conn.cursor()
    c.execute("SELECT status FROM employer_profiles WHERE telegram_id=?", (tid,))
    row = c.fetchone()
    conn.close()

    if not row:
        return "На проверке"

    status = row["status"] if "status" in row.keys() else None
    if not status:
        return "На проверке"

    return status



def require_admin():
    tid, err = require_auth()
    if err:
        return None, err
    if tid != ADMIN_ID:
        log_security_event("admin_access_denied", tid)
        return None, (jsonify({"error": "Нет доступа"}), 403)
    return tid, None


def require_mode(expected_mode: str):
    tid, err = require_auth()
    if err:
        return None, err

    conn = db_connect()
    c = conn.cursor()

    # гарантируем, что пользователь точно есть (в этой же транзакции/коннекции)
    c.execute("""
        INSERT OR IGNORE INTO users (telegram_id, role, active_mode)
        VALUES (?, ?, ?)
    """, (tid, expected_mode, expected_mode))
    # если режим не совпадает — автоматически переключаем
    c.execute("SELECT active_mode FROM users WHERE telegram_id=?", (tid,))
    row = c.fetchone()

    if row and row["active_mode"] != expected_mode:
        c.execute("UPDATE users SET active_mode=? WHERE telegram_id=?", (expected_mode, tid))

    conn.commit()
    conn.close()
    return tid, None
# ======================
# NOTIFY (CITY + KEYWORDS)
# ======================

def notify_city_subscribers(job_id: int):
    conn = db_connect()
    c = conn.cursor()

    c.execute("""
        SELECT
            j.id, j.title, j.salary_text, j.city,
            COALESCE(j.company_name_override, ep.company_name, '') AS company_name
        FROM jobs j
        LEFT JOIN employer_profiles ep ON ep.telegram_id = j.telegram_id
        WHERE j.id=? AND j.status='approved'
    """, (job_id,))
    job = c.fetchone()
    if not job or not job["city"]:
        conn.close()
        return

    city = job["city"]

    c.execute("""
        SELECT s.applicant_id
        FROM city_subscriptions s
        JOIN users u ON u.telegram_id = s.applicant_id
        WHERE s.city=? AND s.is_enabled=1
    """, (city,))
    subs_city = {r["applicant_id"] for r in c.fetchall()}

    c.execute("""
        SELECT applicant_id, keywords
        FROM keyword_subscriptions
        WHERE city=? AND is_enabled=1
    """, (city,))
    kw_rows = c.fetchall()

    job_text = f"{job['title']} {job['salary_text'] or ''} {job['company_name'] or ''}".lower()

    subs_kw = set()
    for r in kw_rows:
        keywords = (r["keywords"] or "").lower()
        kws = [k.strip() for k in keywords.split(",") if k.strip()]
        if not kws:
            continue
        if any(k in job_text for k in kws):
            subs_kw.add(r["applicant_id"])

    subs = list(subs_city.union(subs_kw))

    # Кому по этой вакансии уже отправляли — второй раз не шлём.
    # Вакансию могут отредактировать и одобрить повторно, а человеку
    # не должно прилетать одно и то же дважды.
    if subs:
        placeholders = ",".join("?" for _ in subs)
        c.execute(
            f"SELECT applicant_id FROM notification_log "
            f"WHERE job_id=? AND applicant_id IN ({placeholders})",
            [job_id, *subs]
        )
        already_sent = {r["applicant_id"] for r in c.fetchall()}
        subs = [s for s in subs if s not in already_sent]

    conn.close()

    if not subs:
        return

    title = job["title"] or "Вакансия"
    salary = job["salary_text"] or ""
    company = job["company_name"] or ""

    text = f"🔔 Новая вакансия в городе {city}\n\n{title}"
    if company:
        text += f"\n{company}"
    if salary:
        text += f"\n\n{salary}"

    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton(
        "Открыть вакансию",
        web_app=types.WebAppInfo(f"{WEBAPP_URL}/job/{job_id}")
    ))

    def _send_all():
        sent = 0
        failed = 0

        for applicant_id in subs:
            try:
                bot.send_message(applicant_id, text, reply_markup=markup)
                sent += 1
            except Exception as e:
                # человек мог заблокировать бота или удалить аккаунт —
                # это не повод прерывать рассылку остальным
                failed += 1
                print(f"NOTIFY: не доставлено {applicant_id}: {e}", flush=True)
                continue

            try:
                log_conn = db_connect()
                log_conn.execute(
                    "INSERT OR IGNORE INTO notification_log (job_id, applicant_id) VALUES (?, ?)",
                    (job_id, applicant_id)
                )
                log_conn.commit()
                log_conn.close()
            except Exception as e:
                print(f"NOTIFY: не записал в журнал {applicant_id}: {e}", flush=True)

            # Telegram не любит больше ~30 сообщений в секунду
            time.sleep(0.05)

        print(f"NOTIFY: вакансия {job_id}, отправлено {sent}, не доставлено {failed}", flush=True)

    # Отдельным потоком: иначе админ, нажавший «Одобрить», ждёт,
    # пока разойдётся вся рассылка.
    threading.Thread(target=_send_all, daemon=True).start()

    


# ======================# PAGES
# ======================

@app.context_processor
def inject_bot_username():
    """Отдаём имя бота во все шаблоны разом — из него страницы
    собирают ссылку «Поделиться»."""
    return {"bot_username": BOT_USERNAME}


@app.route("/")
def index():
    return render_template("index.html")

@app.route("/profile")
def profile_page():
    return render_template("profile.html")

@app.route("/job/<int:job_id>")
def job_detail_page(job_id):
    return render_template("job_detail.html", job_id=job_id)


@app.route("/resume")
def resume_page():
    return render_template("resume.html")


@app.route("/applicant")
def applicant_page():
    return render_template("applicant.html")


@app.route("/employer")
def employer_page():
    return render_template("employer.html")

@app.route("/my/applications")
def my_applications_page():
    return render_template("my_applications.html")


@app.route("/my/favorites")
def my_favorites_page():
    return render_template("my_favorites.html")


@app.route("/employer/profile")
def employer_profile_page():
    return render_template("employer_profile.html")


@app.route("/employer/jobs")
def employer_jobs_page():
    return render_template("employer_jobs.html")


@app.route("/employer/job/new")
def employer_job_new_page():
    return render_template("employer_job_form.html", job_id=0)


@app.route("/employer/job/<int:job_id>/edit")
def employer_job_edit_page(job_id):
    return render_template("employer_job_form.html", job_id=job_id)


@app.route("/employer/job/<int:job_id>/applications")
def employer_job_applications_page(job_id):
    return render_template("employer_job_applications.html", job_id=job_id)

@app.route("/chat/<int:application_id>")
def chat_page(application_id):
    return render_template("chat.html", application_id=application_id)


@app.route("/chats")
def chats_page():
    return render_template("chats.html")


@app.route("/admin")
def admin_page():
    # для WebApp не делаем  редирект
    return render_template("admin.html")


@app.route("/api/admin/stats")
def api_admin_stats():
    """Сводка для админки.

    Всё, что связано с аккаунтом админа (его тестовые вакансии, отклики,
    просмотры), не считается — иначе тесты навсегда раздувают цифры.
    """
    tid, err = require_admin()
    if err:
        return err

    A = ADMIN_ID
    conn = db_connect()
    c = conn.cursor()

    def one(sql, params=()):
        c.execute(sql, params)
        row = c.fetchone()
        return row[0] if row and row[0] is not None else 0

    def by_day(sql, params=()):
        c.execute(sql, params)
        return {row[0]: row[1] for row in c.fetchall()}

    # отклики живых людей: не на вакансии админа и не от самого админа
    real_apps = """
        FROM applications a
        JOIN jobs j ON j.id = a.job_id
        WHERE j.telegram_id != ? AND a.applicant_id != ?
    """
    real_views = """
        FROM job_views v
        JOIN jobs j ON j.id = v.job_id
        WHERE j.telegram_id != ? AND v.viewer_id != ?
    """

    users_total = one("SELECT COUNT(*) FROM users WHERE telegram_id != ?", (A,))
    users_7 = one("SELECT COUNT(*) FROM users WHERE telegram_id != ? AND created_at >= datetime('now', '-7 days')", (A,))
    users_30 = one("SELECT COUNT(*) FROM users WHERE telegram_id != ? AND created_at >= datetime('now', '-30 days')", (A,))
    last_user_at = one("SELECT MAX(created_at) FROM users WHERE telegram_id != ?", (A,)) or None

    resumes = one("SELECT COUNT(*) FROM resumes WHERE telegram_id != ?", (A,))
    employers = one("SELECT COUNT(*) FROM employer_profiles WHERE telegram_id != ?", (A,))
    subscriptions = (
        one("SELECT COUNT(*) FROM city_subscriptions WHERE is_enabled = 1 AND applicant_id != ?", (A,))
        + one("SELECT COUNT(*) FROM keyword_subscriptions WHERE is_enabled = 1 AND applicant_id != ?", (A,))
    )
    favorites = one("SELECT COUNT(*) FROM favorites WHERE applicant_id != ?", (A,))

    jobs_active = one("SELECT COUNT(*) FROM jobs WHERE status = 'approved' AND COALESCE(is_deleted, 0) = 0 AND telegram_id != ?", (A,))
    jobs_pending = one("SELECT COUNT(*) FROM jobs WHERE status = 'pending' AND COALESCE(is_deleted, 0) = 0 AND telegram_id != ?", (A,))

    views_7 = one("SELECT COUNT(*) " + real_views + " AND v.viewed_on >= date('now', '-6 days')", (A, A))
    viewers_7 = one("SELECT COUNT(DISTINCT v.viewer_id) " + real_views + " AND v.viewed_on >= date('now', '-6 days')", (A, A))

    apps_total = one("SELECT COUNT(*) " + real_apps, (A, A))
    apps_waiting = one("SELECT COUNT(*) " + real_apps + " AND a.status = 'new'", (A, A))
    avg_answer_hours = one(
        "SELECT AVG((julianday(a.answered_at) - julianday(a.created_at)) * 24) " + real_apps + " AND a.answered_at IS NOT NULL",
        (A, A)
    ) or None

    chat_messages = one("""
        SELECT COUNT(*)
        FROM chat_messages m
        JOIN applications a ON a.id = m.application_id
        JOIN jobs j ON j.id = a.job_id
        WHERE j.telegram_id != ? AND a.applicant_id != ?
    """, (A, A))
    reminders_30 = one("SELECT COUNT(*) FROM employer_reminders WHERE employer_id != ? AND sent_at >= datetime('now', '-30 days')", (A,))

    # по дням за последние 30 дней, по московскому времени
    users_days = by_day("""
        SELECT date(created_at, '+3 hours') AS d, COUNT(*)
        FROM users
        WHERE telegram_id != ? AND created_at >= datetime('now', '-31 days')
        GROUP BY d
    """, (A,))
    views_days = by_day("SELECT v.viewed_on AS d, COUNT(*) " + real_views + " AND v.viewed_on >= date('now', '-31 days') GROUP BY d", (A, A))
    apps_days = by_day("SELECT date(a.created_at, '+3 hours') AS d, COUNT(*) " + real_apps + " AND a.created_at >= datetime('now', '-31 days') GROUP BY d", (A, A))

    conn.close()

    today = (datetime.utcnow() + timedelta(hours=3)).date()
    days = [(today - timedelta(days=i)).isoformat() for i in range(29, -1, -1)]

    return jsonify({
        "users": {"total": users_total, "last_7": users_7, "last_30": users_30, "last_at": last_user_at},
        "resumes": resumes,
        "employers": employers,
        "subscriptions": subscriptions,
        "favorites": favorites,
        "jobs": {"active": jobs_active, "pending": jobs_pending},
        "views": {"last_7": views_7, "viewers_7": viewers_7},
        "applications": {
            "total": apps_total,
            "waiting": apps_waiting,
            "answered": apps_total - apps_waiting,
            "avg_answer_hours": avg_answer_hours,
        },
        "chat_messages": chat_messages,
        "reminders_30": reminders_30,
        "days": days,
        "series": {
            "users": [users_days.get(d, 0) for d in days],
            "views": [views_days.get(d, 0) for d in days],
            "applications": [apps_days.get(d, 0) for d in days],
        },
    })

# ======================
# API — ADMIN
# ======================

@app.route("/api/admin/employers", methods=["GET"])
def api_admin_employers():
    _, err = require_admin()
    if err:
        return err

    conn = db_connect()
    c = conn.cursor()
    c.execute("""
        SELECT
            ep.telegram_id,
            ep.company_name,
            ep.contact_name,
            ep.phone,
            ep.status,
            ep.created_at,
            eb.is_banned,
            eb.reason,
            eb.banned_until
        FROM employer_profiles ep
        LEFT JOIN employer_bans eb ON eb.telegram_id = ep.telegram_id
        ORDER BY ep.created_at DESC
    """)
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return jsonify(rows)


@app.route("/api/admin/employers/<int:telegram_id>/approve", methods=["POST"])
def api_admin_employer_approve(telegram_id: int):
    _, err = require_admin()
    if err:
        return err

    conn = db_connect()
    c = conn.cursor()

    c.execute("""
        UPDATE employer_profiles
        SET status='Проверен'
        WHERE telegram_id=?
    """, (telegram_id,))

    c.execute("""
        INSERT INTO employer_bans (telegram_id, is_banned, reason, banned_until, banned_by)
        VALUES (?, 0, NULL, NULL, ?)
        ON CONFLICT(telegram_id) DO UPDATE SET
            is_banned=0,
            reason=NULL,
            banned_until=NULL,
            banned_by=excluded.banned_by
    """, (telegram_id, ADMIN_ID))

    conn.commit()
    conn.close()

    try:
        bot.send_message(
            telegram_id,
            "✅ Вашей компании выдана галочка проверенной компании.\n\n"
            "Теперь пользователи видят, что ваша компания проверена и ей можно доверять.\n\n"
            "Спасибо, что пользуетесь Ossetia’s Work 🏔"
        )
    except Exception:
        pass

    return jsonify({"success": True, "status": "Проверен"})





@app.route("/api/admin/employers/<int:telegram_id>/unverify", methods=["POST"])
def api_admin_employer_unverify(telegram_id: int):
    _, err = require_admin()
    if err:
        return err

    conn = db_connect()
    c = conn.cursor()

    c.execute("""
        UPDATE employer_profiles
        SET status='На проверке'
        WHERE telegram_id=?
    """, (telegram_id,))

    conn.commit()
    conn.close()

    return jsonify({"success": True, "status": "На проверке"})







@app.route("/api/admin/jobs")
def api_admin_jobs():
    _, err = require_admin()
    if err:
        return err

    conn = db_connect()
    c = conn.cursor()
    c.execute("""
    SELECT *
    FROM jobs
    WHERE COALESCE(is_deleted, 0)=0
    ORDER BY created_at DESC
""")
    jobs = [dict(r) for r in c.fetchall()]
    conn.close()
    return jsonify(jobs)


@app.route("/api/admin/employers/<int:telegram_id>/ban-permanent", methods=["POST"])
def api_admin_employer_ban_permanent(telegram_id: int):
    _, err = require_admin()
    if err:
        return err

    data = request.get_json(silent=True) or {}
    reason = str(data.get("reason", "")).strip()

    conn = db_connect()
    c = conn.cursor()

    c.execute("""
        INSERT INTO employer_bans (telegram_id, is_banned, reason, banned_until, banned_by)
        VALUES (?, 1, ?, NULL, ?)
        ON CONFLICT(telegram_id) DO UPDATE SET
            is_banned=1,
            reason=excluded.reason,
            banned_until=NULL,
            banned_by=excluded.banned_by
    """, (telegram_id, reason, ADMIN_ID))

    c.execute("""
        UPDATE employer_profiles
        SET status='Заблокирован'
        WHERE telegram_id=?
    """, (telegram_id,))

    c.execute("DELETE FROM jobs WHERE telegram_id=?", (telegram_id,))

    conn.commit()
    conn.close()

    try:
        bot.send_message(
            telegram_id,
            "⛔ Ваш аккаунт работодателя заблокирован навсегда.\n\n"
            f"Причина: {reason or 'Серьёзное нарушение правил сервиса'}\n\n"
            "Вы больше не можете публиковать вакансии и пользоваться функциями работодателя."
        )
    except Exception:
        pass



    return jsonify({"success": True, "status": "Заблокирован"})


@app.route("/api/admin/employers/<int:telegram_id>/ban-temp", methods=["POST"])
def api_admin_employer_ban_temp(telegram_id: int):
    _, err = require_admin()
    if err:
        return err

    data = request.get_json(silent=True) or {}
    print("BAN TEMP CALLED:", telegram_id, data)
    reason = str(data.get("reason", "")).strip()
    days = int(data.get("days", 1))

    if days < 1:
        return jsonify({"error": "Количество дней должно быть не меньше 1"}), 400

    banned_until = (datetime.utcnow() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

    conn = db_connect()
    c = conn.cursor()

    c.execute("""
        INSERT INTO employer_bans (telegram_id, is_banned, reason, banned_until, banned_by)
        VALUES (?, 1, ?, ?, ?)
        ON CONFLICT(telegram_id) DO UPDATE SET
            is_banned=1,
            reason=excluded.reason,
            banned_until=excluded.banned_until,
            banned_by=excluded.banned_by
    """, (telegram_id, reason, banned_until, ADMIN_ID))

    c.execute("""
        UPDATE employer_profiles
        SET status='Заблокирован'
        WHERE telegram_id=?
    """, (telegram_id,))

    conn.commit()
    conn.close()

    try:
        bot.send_message(
            telegram_id,
            "⛔ Ваш аккаунт работодателя временно заблокирован.\n\n"
            f"Причина: {reason or 'Нарушение правил сервиса'}\n"
            f"Срок блокировки до: {banned_until}\n\n"
            "На время блокировки вы не сможете публиковать вакансии и пользоваться функциями работодателя."
        )
    except Exception:
        pass

    return jsonify({
        "success": True,
        "status": "Заблокирован",
        "banned_until": banned_until
    })





@app.route("/api/admin/jobs/<int:job_id>/approve", methods=["POST"])
def api_admin_approve(job_id: int):
    _, err = require_admin()
    if err:
        return err

    conn = db_connect()
    c = conn.cursor()
    c.execute("UPDATE jobs SET status='approved' WHERE id=?", (job_id,))
    conn.commit()
    conn.close()

    try:
        notify_city_subscribers(job_id)
    except Exception:
        pass

    return jsonify({"success": True})


@app.route("/api/admin/employers/<int:telegram_id>/unban", methods=["POST"])
def api_admin_employer_unban(telegram_id: int):
    _, err = require_admin()
    if err:
        return err

    conn = db_connect()
    c = conn.cursor()

    c.execute("""
        INSERT INTO employer_bans (telegram_id, is_banned, reason, banned_until, banned_by)
        VALUES (?, 0, NULL, NULL, ?)
        ON CONFLICT(telegram_id) DO UPDATE SET
            is_banned=0,
            reason=NULL,
            banned_until=NULL,
            banned_by=excluded.banned_by
    """, (telegram_id, ADMIN_ID))

    c.execute("""
        UPDATE employer_profiles
        SET status='Проверен'
        WHERE telegram_id=?
    """, (telegram_id,))

    conn.commit()
    conn.close()

    try:
        bot.send_message(
            telegram_id,
            "✅ Ваш аккаунт работодателя разблокирован.\n\n"
            "Теперь вы снова можете пользоваться всеми функциями работодателя и публиковать вакансии."
        )
    except Exception:
        pass


    return jsonify({"success": True, "status": "Проверен"})




@app.route("/api/admin/jobs/<int:job_id>/reject", methods=["POST"])
def api_admin_reject(job_id: int):
    _, err = require_admin()
    if err:
        return err

    data = request.get_json(silent=True) or {}
    reason = str(data.get("reason", "")).strip()

    if not reason:
        return jsonify({"error": "Укажите причину отклонения"}), 400

    conn = db_connect()
    c = conn.cursor()

    c.execute("""
        SELECT telegram_id, title
        FROM jobs
        WHERE id=? AND COALESCE(is_deleted, 0)=0
    """, (job_id,))
    job = c.fetchone()

    if not job:
        conn.close()
        return jsonify({"error": "Вакансия не найдена"}), 404

    c.execute("""
        UPDATE jobs
        SET status='rejected',
            reject_reason=?
        WHERE id=?
    """, (reason, job_id))

    conn.commit()
    conn.close()

    try:
        bot.send_message(
            int(job["telegram_id"]),
            f"❌ Ваша вакансия отклонена:\n{job['title']}\n\n"
            f"Причина: {reason}\n\n"
            "Исправьте вакансию и отправьте её заново на модерацию."
        )
    except Exception:
        pass

    return jsonify({"success": True})



@app.route("/api/admin/jobs/<int:job_id>", methods=["DELETE"])
def api_admin_delete(job_id: int):
    _, err = require_admin()
    if err:
        return err

    conn = db_connect()
    c = conn.cursor()

    c.execute("""
        UPDATE jobs
        SET is_deleted=1,
            deleted_at=CURRENT_TIMESTAMP
        WHERE id=? AND COALESCE(is_deleted, 0)=0
    """, (job_id,))

    deleted = c.rowcount

    conn.commit()
    conn.close()

    if deleted <= 0:
        return jsonify({"error": "Вакансия не найдена"}), 404

    return jsonify({"success": True})

# ======================
# API — ME / SWITCH MODE
# ======================


@app.route("/api/me")
def api_me():
    tid, err = require_auth()
    if err:
        return err

    ensure_user(tid)

    conn = db_connect()
    c = conn.cursor()

    c.execute("SELECT active_mode FROM users WHERE telegram_id=?", (tid,))
    user = c.fetchone()

    c.execute("SELECT 1 FROM resumes WHERE telegram_id=?", (tid,))
    has_r = bool(c.fetchone())

    c.execute("SELECT 1 FROM employer_profiles WHERE telegram_id=?", (tid,))
    has_e = bool(c.fetchone())

    conn.close()

    employer_status = None
    if has_e:
        try:
            employer_status = get_employer_status(tid)
        except Exception:
            employer_status = "На проверке"

    return jsonify({
        "exists": True,
        "telegram_id": tid,
        "is_admin": tid == ADMIN_ID,
        "active_mode": user["active_mode"] if user else "applicant",
        "has_resume": has_r,
        "has_employer_profile": has_e,
        "employer_status": employer_status
    })








@app.route("/api/switch-mode", methods=["POST"])
def api_switch_mode():
    tid, err = require_auth()
    if err:
        return err

    ensure_user(tid)

    data = request.get_json(silent=True) or {}
    mode = str(data.get("mode", "")).strip()

    if mode not in ["applicant", "employer"]:
        return jsonify({"error": "Неверный режим"}), 400

    conn = db_connect()
    c = conn.cursor()

    if mode == "employer":
        c.execute("SELECT 1 FROM employer_profiles WHERE telegram_id=?", (tid,))
        has_profile = c.fetchone() is not None

        if not has_profile:
            conn.close()
            return jsonify({
                "error": "Сначала нужно пройти регистрацию работодателя"
            }), 403

        banned, reason = is_banned_employer(tid)
        if banned:
            conn.close()
            return jsonify({
                "error": f"Аккаунт работодателя заблокирован. {reason}".strip()
            }), 403

    c.execute("""
        UPDATE users
        SET active_mode=?
        WHERE telegram_id=?
    """, (mode, tid))

    conn.commit()
    conn.close()

    return jsonify({"success": True, "active_mode": mode})





# ======================
# API — LOCATIONS
# ======================

@app.route("/api/locations")
def api_locations():
    return jsonify(OSSETIA_LOCATIONS)


# ======================
# API — SUBSCRIPTIONS
# ======================

@app.route("/api/subscription", methods=["GET"])
def api_subscription_get():
    tid, err = require_mode("applicant")
    if err:
        return err
    ensure_user(tid)

    conn = db_connect()
    c = conn.cursor()
    c.execute("SELECT city, is_enabled FROM city_subscriptions WHERE applicant_id=?", (tid,))
    row = c.fetchone()
    conn.close()

    if not row:
        return jsonify({"exists": False})
    return jsonify({"exists": True, "city": row["city"], "is_enabled": bool(row["is_enabled"])})


@app.route("/api/subscription", methods=["POST"])
def api_subscription_set():
    tid, err = require_mode("applicant")
    if err:
        return err
    ensure_user(tid)

    data = request.json or {}
    city = str(data.get("city", "")).strip()
    is_enabled = 1 if bool(data.get("is_enabled", True)) else 0

    if not city:
        return jsonify({"error": "Укажите город/село"}), 400

    conn = db_connect()
    c = conn.cursor()
    c.execute("""
        INSERT INTO city_subscriptions (applicant_id, city, is_enabled)
        VALUES (?, ?, ?)
        ON CONFLICT(applicant_id) DO UPDATE SET
          city=excluded.city,
          is_enabled=excluded.is_enabled
    """, (tid, city, is_enabled))
    conn.commit()
    conn.close()

    return jsonify({"success": True, "city": city, "is_enabled": bool(is_enabled)})


@app.route("/api/keyword-subscription", methods=["GET"])
def api_kw_sub_get():
    tid, err = require_mode("applicant")
    if err:
        return err
    ensure_user(tid)

    conn = db_connect()
    c = conn.cursor()
    c.execute("SELECT city, keywords, is_enabled FROM keyword_subscriptions WHERE applicant_id=?", (tid,))
    row = c.fetchone()
    conn.close()

    if not row:
        return jsonify({"exists": False})

    return jsonify({
        "exists": True,
        "city": row["city"],
        "keywords": row["keywords"],
        "is_enabled": bool(row["is_enabled"])
    })


@app.route("/api/keyword-subscription", methods=["POST"])
def api_kw_sub_set():
    tid, err = require_mode("applicant")
    if err:
        return err
    ensure_user(tid)

    data = request.json or {}
    city = str(data.get("city", "")).strip()
    keywords = str(data.get("keywords", "")).strip()
    is_enabled = 1 if bool(data.get("is_enabled", True)) else 0

    if not city:
        return jsonify({"error": "Укажите город/село"}), 400
    if not keywords:
        return jsonify({"error": "Укажите ключевые слова через запятую"}), 400

    keywords = ",".join([k.strip().lower() for k in keywords.split(",") if k.strip()])
    if not keywords:
        return jsonify({"error": "Ключевые слова пустые"}), 400

    conn = db_connect()
    c = conn.cursor()
    c.execute("""
        INSERT INTO keyword_subscriptions (applicant_id, city, keywords, is_enabled)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(applicant_id) DO UPDATE SET
          city=excluded.city,
          keywords=excluded.keywords,
          is_enabled=excluded.is_enabled
    """, (tid, city, keywords, is_enabled))
    conn.commit()
    conn.close()

    return jsonify({"success": True, "city": city, "keywords": keywords, "is_enabled": bool(is_enabled)})


# ======================
# API — RESUME
# ======================

@app.route("/api/resume", methods=["GET"])
def api_resume_get():
    tid, err = require_auth()
    if err:
        return err
    ensure_user(tid)

    conn = db_connect()
    c = conn.cursor()
    c.execute("SELECT * FROM resumes WHERE telegram_id=?", (tid,))
    row = c.fetchone()
    conn.close()

    if not row:
        return jsonify({"exists": False})
    return jsonify({"exists": True, "resume": dict(row)})



@app.route("/api/resume", methods=["POST"])
def api_resume_post():
    tid, err = require_auth()
    if err:
        return err

    data = request.get_json(silent=True) or {}

    required = ["full_name", "city", "position", "experience", "skills", "about", "phone", "employment_type"]
    for k in required:
        if not str(data.get(k, "")).strip():
            return jsonify({"error": f"Заполните поле: {k}"}), 400



    age_raw = str(data.get("age", "")).strip()

    if not age_raw:
        age_value = None
    else:
        if not age_raw.isdigit():
            return jsonify({"error": "Возраст должен быть числом"}), 400

        age_value = int(age_raw)

        if age_value < 14 or age_value > 100:
            return jsonify({"error": "Возраст должен быть от 14 до 100"}), 400

    phone, phone_error = normalize_phone(data.get("phone"))

    if phone_error:
        return jsonify({"error": phone_error}), 400

    conn = db_connect()

    c = conn.cursor()

    try:
        c.execute("PRAGMA table_info(users)")
        user_cols = [row["name"] for row in c.fetchall()]

        if "role" in user_cols and "active_mode" in user_cols:
            c.execute("""
                INSERT OR IGNORE INTO users (telegram_id, role, active_mode)
                VALUES (?, 'applicant', 'applicant')
            """, (tid,))
        elif "role" in user_cols:
            c.execute("""
                INSERT OR IGNORE INTO users (telegram_id, role)
                VALUES (?, 'applicant')
            """, (tid,))
        elif "active_mode" in user_cols:
            c.execute("""
                INSERT OR IGNORE INTO users (telegram_id, active_mode)
                VALUES (?, 'applicant')
            """, (tid,))
        else:
            c.execute("""
                INSERT OR IGNORE INTO users (telegram_id)
                VALUES (?)
            """, (tid,))

        if "active_mode" in user_cols:
            c.execute("""
                UPDATE users
                SET active_mode='applicant'
                WHERE telegram_id=?
            """, (tid,))

        c.execute("SELECT 1 FROM resumes WHERE telegram_id=?", (tid,))
        existing = c.fetchone()

        if existing:
            c.execute("""
                UPDATE resumes
                SET full_name=?,
                    age=?,
                    city=?,
                    position=?,
                    experience=?,
                    skills=?,
                    about=?,
                    phone=?,
                    employment_type=?
                WHERE telegram_id=?
            """, (
                str(data.get("full_name", "")).strip(),
                age_value,
                str(data.get("city", "")).strip(),
                str(data.get("position", "")).strip(),
                str(data.get("experience", "")).strip(),
                str(data.get("skills", "")).strip(),
                str(data.get("about", "")).strip(),
                str(data.get("phone", "")).strip(),
                str(data.get("employment_type", "")).strip(),
                tid
            ))
        else:
            c.execute("""
                INSERT INTO resumes
                (telegram_id, full_name, age, city, position, experience, skills, about, phone, employment_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                tid,
                str(data.get("full_name", "")).strip(),
                age_value,
                str(data.get("city", "")).strip(),
                str(data.get("position", "")).strip(),
                str(data.get("experience", "")).strip(),
                str(data.get("skills", "")).strip(),
                str(data.get("about", "")).strip(),
                str(data.get("phone", "")).strip(),
                str(data.get("employment_type", "")).strip(),
            ))

        conn.commit()
        return jsonify({"success": True})

    except Exception as e:
        conn.rollback()
        import traceback
        traceback.print_exc()
        print("ERROR IN /api/resume:", repr(e), "telegram_id=", tid)
        return jsonify({"error": "Внутренняя ошибка сервера"}), 500

    finally:
        conn.close()

# ======================
# API — EMPLOYER PROFILE
# ======================

@app.route("/api/employer/profile", methods=["GET"])
def api_employer_profile_get():
    tid, err = require_auth()
    if err:
        return err

    ensure_user(tid)

    conn = db_connect()
    c = conn.cursor()
    c.execute("SELECT * FROM employer_profiles WHERE telegram_id=?", (tid,))
    row = c.fetchone()
    conn.close()

    if not row:
        return jsonify({"exists": False})

    profile = dict(row)
    profile["status"] = get_employer_status(tid)

    return jsonify({"exists": True, "profile": profile})

@app.route("/api/employer/profile", methods=["POST"])
def api_employer_profile_post():
    try:
        tid, err = require_auth()
        if err:
            return err

        if not rate_limit(f"employer_register:{tid}", limit=3, per_seconds=300):
            return jsonify({
                "error": "Слишком много попыток. Подождите 5 минут."
            }), 429

        data = request.get_json(silent=True) or {}

        # company  — организация, ИП, магазин, кафе
        # person   — человек предлагает то, что умеет сам: фотограф, репетитор, мастер
        employer_type = str(data.get("employer_type", "company")).strip()
        if employer_type not in ("company", "person"):
            employer_type = "company"

        # company_name — отображаемое имя: название организации либо имя человека.
        # Столбец общий, потому что вакансии подписываются именно им.
        company_name = str(data.get("company_name", "")).strip()
        contact_name = str(data.get("contact_name", "")).strip()
        activity = str(data.get("activity", "")).strip()
        about = str(data.get("about", "")).strip()
        link = str(data.get("link", "")).strip()

        if len(company_name) < 2:
            return jsonify({
                "error": "Укажите название компании" if employer_type == "company" else "Укажите ваше имя"
            }), 400

        if not activity:
            return jsonify({
                "error": "Укажите сферу деятельности" if employer_type == "company" else "Укажите, чем вы занимаетесь"
            }), 400

        if employer_type == "company" and not contact_name:
            return jsonify({"error": "Укажите контактное лицо"}), 400

        if employer_type == "person":
            contact_name = ""

        phone, phone_error = normalize_phone(data.get("phone"))
        if phone_error:
            return jsonify({"error": phone_error}), 400

        if len(company_name) > 120 or len(activity) > 120 or len(contact_name) > 120:
            return jsonify({"error": "Слишком длинное значение в одном из полей"}), 400

        if len(about) > 1000:
            return jsonify({"error": "Описание слишком длинное"}), 400

        if len(link) > 200:
            return jsonify({"error": "Ссылка слишком длинная"}), 400

        conn = db_connect()
        cur = conn.cursor()


        cur.execute("""
            INSERT OR IGNORE INTO users (telegram_id, role, active_mode)
            VALUES (?, 'employer', 'employer')
        """, (tid,))

        cur.execute("""
            INSERT INTO employer_profiles
                (telegram_id, company_name, contact_name, phone, status,
                 employer_type, activity, about, link)
            VALUES (?, ?, ?, ?, 'На проверке', ?, ?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                company_name=excluded.company_name,
                contact_name=excluded.contact_name,
                phone=excluded.phone,
                employer_type=excluded.employer_type,
                activity=excluded.activity,
                about=excluded.about,
                link=excluded.link,
                status=CASE
                    WHEN employer_profiles.status='Проверен' THEN 'Проверен'
                    WHEN employer_profiles.status='Заблокирован' THEN 'Заблокирован'
                    ELSE 'На проверке'
                END
        """, (tid, company_name, contact_name, phone, employer_type, activity, about, link))

        cur.execute("""
            UPDATE users
            SET active_mode='employer'
            WHERE telegram_id=?
        """, (tid,))

        conn.commit()
        conn.close()

        return jsonify({"success": True, "status": "На проверке"})

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": "Internal error"}), 500


# ======================
# API — JOBS PUBLIC
# ======================

@app.route("/api/jobs")
def api_jobs_public():

    city = request.args.get("city", "").strip()
    q = request.args.get("q", "").strip()
    sort = request.args.get("sort", "new").strip()  # new | popular | hot


    viewer_id = getattr(g, "telegram_id", None)
    ensure_user(viewer_id) if viewer_id else None

    conn = db_connect()
    c = conn.cursor()



    sql = """
SELECT
j.id,
j.title,
j.salary_text,
j.description,
j.city,
j.address,
j.created_at,
COALESCE(j.company_name_override, ep.company_name, '') AS company_name,
CASE
    WHEN ep.status = 'Проверен' THEN 1
    ELSE 0
END AS verified
FROM jobs j
LEFT JOIN employer_profiles ep ON ep.telegram_id = j.telegram_id
LEFT JOIN employer_bans eb ON eb.telegram_id = j.telegram_id
WHERE j.status='approved'
  AND COALESCE(j.is_deleted, 0)=0
  AND (
        eb.telegram_id IS NULL
        OR eb.is_banned = 0
        OR (
            eb.banned_until IS NOT NULL
            AND eb.banned_until <= datetime('now')
        )
      )
    """


    params = []

    if city:
        sql += " AND LOWER(TRIM(j.city)) = LOWER(TRIM(?))"
        params.append(city.strip())
    if q:
        sql += " AND (LOWER(j.title) LIKE ? OR LOWER(j.description) LIKE ?)"
        like_q = f"%{q.lower()}%"
        params.extend([like_q, like_q])

    if sort == "popular":
        sql += """
            ORDER BY (
              SELECT COUNT(*) FROM job_views v
              WHERE v.job_id=j.id AND v.viewed_on >= date('now','-6 days')
            ) DESC, j.created_at DESC
        """
    elif sort == "hot":
        sql += """
            ORDER BY (
              SELECT COUNT(*) FROM applications a
              WHERE a.job_id=j.id AND date(a.created_at) >= date('now','-6 days')
            ) DESC, j.created_at DESC
        """
    else:
        sql += " ORDER BY j.created_at DESC"

    c.execute(sql, params)
    rows = [dict(r) for r in c.fetchall()]

    # views (cheap, once/day)
    if viewer_id:
        try:
            ensure_user(int(viewer_id))
            for r in rows:
                jid = r.get("id")
                if jid:
                    c.execute(
                        "INSERT OR IGNORE INTO job_views (job_id, viewer_id, viewed_on) VALUES (?, ?, date('now'))",
                        (int(jid), int(viewer_id))
                    )
            conn.commit()
        except Exception:
            pass

    # applied flags
    if viewer_id and rows:
        try:
            ids = [int(r["id"]) for r in rows if r.get("id")]
            if ids:
                placeholders = ",".join(["?"] * len(ids))
                c.execute(
                    f"SELECT job_id FROM applications WHERE applicant_id=? AND job_id IN ({placeholders})",
                    [int(viewer_id)] + ids
                )
                applied_ids = {int(x["job_id"]) for x in c.fetchall()}
                for r in rows:
                    r["applied"] = int(r["id"]) in applied_ids
        except Exception:
            for r in rows:
                r["applied"] = False
    else:
        for r in rows:
            r["applied"] = False

    conn.close()
    return jsonify(rows)





@app.route("/api/jobs/<int:job_id>")
def api_job_detail(job_id: int):
    conn = db_connect()
    c = conn.cursor()
    c.execute("""
        SELECT
            j.id, j.title, j.salary_text, j.description, j.city, j.address, j.created_at,
            COALESCE(j.company_name_override, ep.company_name, '') AS company_name,
            CASE WHEN ep.status = 'Проверен' THEN 1 ELSE 0 END AS verified,
            (SELECT COUNT(*) FROM job_views v WHERE v.job_id = j.id) AS views,
            (SELECT COUNT(*) FROM applications a WHERE a.job_id = j.id) AS applications
        FROM jobs j
        LEFT JOIN employer_profiles ep ON ep.telegram_id = j.telegram_id
        WHERE j.id=? AND j.status='approved' AND COALESCE(j.is_deleted, 0)=0
    """, (job_id,))
    row = c.fetchone()

    if not row:
        conn.close()
        return jsonify({"error": "Вакансия не найдена"}), 404

    result = dict(row)

    # Откликался ли уже этот человек: иначе страница вакансии предлагала
    # откликнуться повторно и получала отказ только после нажатия.
    result["applied"] = False

    viewer_id = getattr(g, "telegram_id", None)
    if viewer_id:
        try:
            ensure_user(int(viewer_id))
            c.execute(
                "INSERT OR IGNORE INTO job_views (job_id, viewer_id, viewed_on) VALUES (?, ?, date('now'))",
                (job_id, int(viewer_id))
            )
            c.execute(
                "SELECT 1 FROM applications WHERE job_id=? AND applicant_id=? LIMIT 1",
                (job_id, int(viewer_id))
            )
            result["applied"] = c.fetchone() is not None
            conn.commit()
        except Exception:
            pass

    conn.close()
    return jsonify(result)


# ======================
# API — FAVORITES
# ======================

@app.route("/api/favorites")
def api_favorites_list():
    tid, err = require_mode("applicant")
    if err:
        return err
    ensure_user(tid)

    conn = db_connect()
    c = conn.cursor()
    c.execute("""
        SELECT
          j.id, j.title, j.city, j.salary_text, j.description, j.created_at,
          COALESCE(j.company_name_override, ep.company_name, '') AS company_name
        FROM favorites f
        JOIN jobs j ON j.id=f.job_id
        LEFT JOIN employer_profiles ep ON ep.telegram_id = j.telegram_id
        WHERE f.applicant_id=? AND j.status='approved'
        ORDER BY f.created_at DESC
    """, (tid,))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return jsonify(rows)


@app.route("/api/favorites/<int:job_id>", methods=["POST"])
def api_favorites_add(job_id: int):
    tid, err = require_auth()
    if err:
        return err
    ensure_user(tid)

    conn = db_connect()
    c = conn.cursor()
    c.execute("SELECT 1 FROM jobs WHERE id=? AND status='approved'", (job_id,))
    if not c.fetchone():
        conn.close()
        return jsonify({"error": "Вакансия недоступна"}), 404

    c.execute("INSERT OR IGNORE INTO favorites (applicant_id, job_id) VALUES (?, ?)", (tid, job_id))
    conn.commit()
    conn.close()
    return jsonify({"success": True, "favorited": True})


@app.route("/api/favorites/<int:job_id>", methods=["DELETE"])
def api_favorites_remove(job_id: int):
    tid, err = require_auth()
    if err:
        return err
    ensure_user(tid)

    conn = db_connect()
    c = conn.cursor()
    c.execute("DELETE FROM favorites WHERE applicant_id=? AND job_id=?", (tid, job_id))
    conn.commit()
    conn.close()
    return jsonify({"success": True, "favorited": False})


# ======================
# API — APPLICATIONS (APPLICANT)
# ======================

@app.route("/api/applicant/applications")
def api_applicant_applications():
    tid, err = require_mode("applicant")
    if err:
        return err
    ensure_user(tid)

    conn = db_connect()
    c = conn.cursor()
    c.execute("""
        SELECT
    a.id AS application_id,
    a.status,
    a.created_at,

    j.id AS job_id,
    j.title,
    j.salary_text,
    j.city,

    COALESCE(j.company_name_override, ep.company_name, '') AS company_name,

    (SELECT COUNT(*) FROM job_views WHERE job_id = j.id) AS views,

    (SELECT COUNT(*) FROM applications WHERE job_id = j.id) AS applications

        FROM applications a
        JOIN jobs j ON j.id = a.job_id
        LEFT JOIN employer_profiles ep ON ep.telegram_id = j.telegram_id
        WHERE a.applicant_id=?
        ORDER BY a.created_at DESC
    """, (tid,))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return jsonify(rows)


# ======================
# API — EMPLOYER JOBS + RATE LIMIT
# ======================

_rate: dict[str, list[float]] = {}
_rate_lock = threading.Lock()
def rate_limit(key: str, limit: int, per_seconds: int) -> bool:
    now = time.time()
    with _rate_lock:
        arr = _rate.get(key, [])
        arr = [t for t in arr if now - t < per_seconds]
        if len(arr) >= limit:
            _rate[key] = arr
            return False
        arr.append(now)
        _rate[key] = arr
        return True

@app.route("/api/employer/jobs", methods=["GET", "POST"])
def api_employer_jobs():
    tid, err = require_mode("employer")
    if err:
        return err
    ensure_user(tid)

    banned, reason = is_banned_employer(tid)
    if banned:
        return jsonify({"error": f"Ваш аккаунт работодателя заблокирован. {reason}".strip()}), 403

    if not has_employer_profile(tid):
        return jsonify({"error": "Сначала заполните профиль компании (название и телефон)"}), 400

    employer_status = get_employer_status(tid)

    if request.method == "POST" and employer_status != "Проверен":
        return jsonify({
            "error": f"Ваш статус работодателя: {employer_status}. Дождитесь проверки администратором."
        }), 403

    if request.method == "GET":
        conn = db_connect()
        c = conn.cursor()
        c.execute("""
            SELECT
              j.*,
              (SELECT COUNT(*) FROM applications a WHERE a.job_id=j.id) AS applications,
              (SELECT COUNT(*) FROM job_views v WHERE v.job_id=j.id) AS views_total,
              (SELECT COUNT(*) FROM job_views v
               WHERE v.job_id=j.id AND v.viewed_on >= date('now','-6 days')) AS views_7d,
              (SELECT COUNT(*) FROM applications a
               WHERE a.job_id=j.id AND date(a.created_at) >= date('now','-6 days')) AS apps_7d
            FROM jobs j
            WHERE j.telegram_id=?
              AND COALESCE(j.is_deleted, 0)=0
            ORDER BY j.created_at DESC
        """, (tid,))
        rows = []
        for r in c.fetchall():
            d = dict(r)
            views_7d = int(d.get("views_7d") or 0)
            apps_7d = int(d.get("apps_7d") or 0)
            conv = int(round((apps_7d / views_7d) * 100)) if views_7d > 0 else 0
            d["conv_7d"] = conv
            rows.append(d)
        conn.close()
        return jsonify(rows)

    # rate limit: 5 per 10 minutes
    key = f"createjob:{tid}"
    now = time.time()
    with _rate_lock:
        arr = _rate.get(key, [])
        arr = [t for t in arr if now - t < 600]
        if len(arr) >= 5:
            return jsonify({"error": "Слишком много вакансий за короткое время. Подождите 10 минут."}), 429
        arr.append(now)
        _rate[key] = arr

    data = request.json or {}
    title = str(data.get("title", "")).strip()
    city = str(data.get("city", "")).strip().title()
    salary_text = str(data.get("salary_text", "")).strip()
    description = str(data.get("description", "")).strip()
    address = str(data.get("address", "")).strip()

    if not title:
        return jsonify({"error": "Введите название вакансии"}), 400
    if not city:
        return jsonify({"error": "Выберите город/село"}), 400
    if not description:
        return jsonify({"error": "Введите описание"}), 400

    conn = db_connect()
    c = conn.cursor()
    c.execute("""
        INSERT INTO jobs (telegram_id, title, city, address, salary_text, description, status)
        VALUES (?, ?, ?, ?, ?, ?, 'pending')
    """, (tid, title, city, address, salary_text, description))
    job_id = c.lastrowid
    conn.commit()
    conn.close()

    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton("✅ Одобрить", callback_data=f"approve_{job_id}"),
        types.InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_{job_id}")
    )

    try:
        bot.send_message(
            ADMIN_ID,
            f"🆕 Новая вакансия на модерации:\n\n"
            f"📌 {title}\n"
            f"📍 {city}\n"
            f"🏠 {address or 'Адрес не указан'}\n"
            f"💰 {salary_text or '—'}\n\n"
            f"{description[:700]}",
            reply_markup=markup
        )
    except Exception:
        pass

    return jsonify({"success": True, "job_id": job_id})



@app.route("/api/employer/jobs-archive", methods=["GET"])
def api_employer_jobs_archive():
    tid, err = require_mode("employer")
    if err:
        return err

    ensure_user(tid)

    conn = db_connect()
    c = conn.cursor()

    c.execute("""
        SELECT
          j.*,
          CAST(
            julianday(datetime(j.deleted_at, '+10 days')) - julianday('now')
            AS INTEGER
          ) AS days_left
        FROM jobs j
        WHERE j.telegram_id=?
          AND COALESCE(j.is_deleted, 0)=1
        ORDER BY j.deleted_at DESC
    """, (tid,))

    rows = [dict(r) for r in c.fetchall()]
    conn.close()

    return jsonify(rows)



@app.route("/api/employer/jobs/<int:job_id>", methods=["PUT"])
def api_employer_job_update(job_id: int):
    tid, err = require_mode("employer")
    if err:
        return err

    ensure_user(tid)

    banned, reason = is_banned_employer(tid)
    if banned:
        return jsonify({"error": f"Ваш аккаунт работодателя заблокирован. {reason}".strip()}), 403

    employer_status = get_employer_status(tid)
    if employer_status != "Проверен":
        return jsonify({
            "error": f"Ваш статус работодателя: {employer_status}. Дождитесь проверки администратором."
        }), 403

    data = request.get_json(silent=True) or {}

    title = str(data.get("title", "")).strip()
    city = str(data.get("city", "")).strip().title()
    salary_text = str(data.get("salary_text", "")).strip()
    description = str(data.get("description", "")).strip()
    address = str(data.get("address", "")).strip()

    if not title:
        return jsonify({"error": "Введите название вакансии"}), 400

    if not city:
        return jsonify({"error": "Выберите город/село"}), 400

    if not description:
        return jsonify({"error": "Введите описание"}), 400

    conn = db_connect()
    c = conn.cursor()

    c.execute("""
        SELECT id, status
        FROM jobs
        WHERE id=? AND telegram_id=?
    """, (job_id, tid))

    job = c.fetchone()

    if not job:
        conn.close()
        return jsonify({"error": "Вакансия не найдена или нет доступа"}), 404

    c.execute("""
        UPDATE jobs
        SET title=?,
            city=?,
            address=?,
            salary_text=?,
            description=?,
            status='pending'
        WHERE id=? AND telegram_id=?
    """, (title, city, address, salary_text, description, job_id, tid))
    conn.commit()
    conn.close()

    try:
        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton("✅ Одобрить", callback_data=f"approve_{job_id}"),
            types.InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_{job_id}")
        )

        bot.send_message(
            ADMIN_ID,
            f"✏️ Вакансия отредактирована и ждёт повторной модерации:\n\n"
            f"📌 {title}\n"
            f"📍 {city}\n"
            f"🏠 {address or 'Адрес не указан'}\n"
            f"💰 {salary_text or '—'}\n\n"
            f"{description[:700]}",
            reply_markup=markup
        )
    except Exception:
        pass

    return jsonify({
        "success": True,
        "status": "pending"
    })


@app.route("/api/employer/jobs/<int:job_id>", methods=["DELETE"])
def api_employer_job_delete(job_id: int):
    tid, err = require_mode("employer")
    if err:
        return err

    ensure_user(tid)

    conn = db_connect()
    c = conn.cursor()

    c.execute("""
        UPDATE jobs
        SET is_deleted=1,
            deleted_at=CURRENT_TIMESTAMP
        WHERE id=? AND telegram_id=? AND COALESCE(is_deleted, 0)=0
    """, (job_id, tid))

    deleted = c.rowcount

    conn.commit()
    conn.close()

    if deleted <= 0:
        return jsonify({"error": "Вакансия не найдена или нет доступа"}), 404

    return jsonify({"success": True})




# ======================
# API — EMPLOYER APPLICATIONS
# ======================

@app.route("/api/employer/jobs/<int:job_id>/applications", methods=["GET"])
def api_employer_job_applications(job_id: int):
    tid, err = require_mode("employer")
    if err:
        return err

    ensure_user(tid)

    banned, reason = is_banned_employer(tid)
    if banned:
        return jsonify({"error": f"Ваш аккаунт работодателя заблокирован. {reason}".strip()}), 403

    conn = db_connect()
    c = conn.cursor()

    c.execute("""
        SELECT
            j.id,
            j.title,
            j.city,
            j.address,
            j.salary_text,
            j.description,
            j.status
        FROM jobs j
        WHERE j.id=?
          AND j.telegram_id=?
          AND COALESCE(j.is_deleted, 0)=0
    """, (job_id, tid))

    job = c.fetchone()

    if not job:
        conn.close()
        return jsonify({"error": "Вакансия не найдена или нет доступа"}), 404

    c.execute("""
        SELECT
            a.id AS application_id,
            a.applicant_id,
            a.status,
            a.reject_reason,
            a.created_at,

            r.full_name,
            r.age,
            r.city AS applicant_city,
            r.position,
            r.experience,
            r.skills,
            r.about,
            r.phone,
            r.employment_type
        FROM applications a
        LEFT JOIN resumes r ON r.telegram_id = a.applicant_id
        WHERE a.job_id=?
        ORDER BY a.created_at DESC
    """, (job_id,))

    applications = [dict(r) for r in c.fetchall()]
    conn.close()

    return jsonify({
        "job": dict(job),
        "applications": applications
    })

@app.route("/api/employer/applications", methods=["GET"])
def api_employer_applications():
    tid, err = require_mode("employer")
    if err:
        return err
    ensure_user(tid)

    banned, reason = is_banned_employer(tid)
    if banned:
        return jsonify({"error": f"Ваш аккаунт работодателя заблокирован. {reason}".strip()}), 403
    employer_status = get_employer_status(tid)
    #if employer_status != "Проверен":
        #return jsonify({"error": f"Ваш статус работодателя: {employer_status}. Дождитесь проверки администратором."}), 403

    conn = db_connect()
    c = conn.cursor()
    c.execute("""
        SELECT
          a.id AS application_id,
          a.applicant_id,
          a.status,
          a.created_at,
          j.id AS job_id,
          j.title AS job_title,
          j.city AS job_city,
          COALESCE(j.company_name_override, ep.company_name, '') AS company_name,
          r.full_name,
          r.city AS applicant_city,
          r.position,
          r.experience,
          r.skills,
          r.phone,
          r.employment_type
        FROM applications a
        JOIN jobs j ON j.id=a.job_id
        LEFT JOIN employer_profiles ep ON ep.telegram_id=j.telegram_id
        LEFT JOIN resumes r ON r.telegram_id=a.applicant_id
        WHERE j.telegram_id=?
        ORDER BY a.created_at DESC
    """, (tid,))
    rows = [dict(x) for x in c.fetchall()]
    conn.close()
    return jsonify(rows)

@app.route("/api/employer/applications/<int:application_id>/status", methods=["POST"])
def api_employer_application_set_status(application_id: int):
    tid, err = require_mode("employer")
    if err:
        return err

    ensure_user(tid)

    banned, reason = is_banned_employer(tid)
    if banned:
        return jsonify({"error": f"Ваш аккаунт работодателя заблокирован. {reason}".strip()}), 403

    employer_status = get_employer_status(tid)
    if employer_status != "Проверен":
        return jsonify({
            "error": f"Ваш статус работодателя: {employer_status}. Дождитесь проверки администратором."
        }), 403

    data = request.get_json(silent=True) or {}
    status = str(data.get("status", "")).strip()
    reject_reason = str(data.get("reject_reason", "")).strip()

    if status not in ("new", "interview", "accepted", "rejected"):
        return jsonify({"error": "Неверный статус"}), 400

    if status == "rejected" and not reject_reason:
        return jsonify({"error": "Укажите причину отказа"}), 400

    conn = db_connect()
    c = conn.cursor()

    c.execute("""
	SELECT
            a.id,
            a.applicant_id,
            j.title,
            COALESCE(j.company_name_override, ep.company_name, '') AS company_name
        FROM applications a
        JOIN jobs j ON j.id = a.job_id
        LEFT JOIN employer_profiles ep ON ep.telegram_id = j.telegram_id
        WHERE a.id=? AND j.telegram_id=?
    """, (application_id, tid))

    row = c.fetchone()

    if not row:
        conn.close()
        return jsonify({"error": "Нет доступа"}), 403

    if status == "rejected":
        c.execute("""
            UPDATE applications
            SET status=?,
                reject_reason=?
            WHERE id=?
        """, (status, reject_reason, application_id))
    else:
        c.execute("""
            UPDATE applications
            SET status=?,
                reject_reason=NULL
            WHERE id=?
        """, (status, application_id))

    # Запоминаем только первый ответ: дальше статус может меняться сколько угодно
    if status != "new":
        c.execute("""
            UPDATE applications
            SET answered_at = CURRENT_TIMESTAMP
            WHERE id=? AND answered_at IS NULL
        """, (application_id,))

    conn.commit()
    conn.close()

    applicant_tid = int(row["applicant_id"])
    job_title = row["title"] or "Вакансия"
    company_name = row["company_name"] or "Компания"


    try:
        if status == "accepted":
            bot.send_message(
                applicant_tid,
                f"🎉 Ваш отклик принят!\n\n"
                f"Компания: {company_name}\n"
                f"Вакансия: {job_title}"
            )

        elif status == "interview":
            bot.send_message(
                applicant_tid,
                f"🗓 Вас пригласили на собеседование!\n\n"
                f"Компания: {company_name}\n"
                f"Вакансия: {job_title}"
            )

        elif status == "rejected":
            bot.send_message(
                applicant_tid,
                f"❌ По вашему отклику отказ.\n\n"
                f"Компания: {company_name}\n"
                f"Вакансия: {job_title}\n"
                f"Причина: {reject_reason or 'Не указана'}"
            )

    except Exception as e:
        print("send application status notify error:", e)

    return jsonify({
        "success": True,
        "status": status,
        "reject_reason": reject_reason if status == "rejected" else None
    })

# ======================
# API — CHAT
# ======================

@app.route("/api/chats/unread-count", methods=["GET"])
def api_chats_unread_count():
    tid, err = require_auth()
    if err:
        return err

    ensure_user(tid)

    conn = db_connect()
    c = conn.cursor()

    c.execute("""
        SELECT COUNT(*) AS cnt
        FROM chat_messages cm
        JOIN applications a ON a.id = cm.application_id
        JOIN jobs j ON j.id = a.job_id
        LEFT JOIN chat_reads cr
          ON cr.application_id = cm.application_id
         AND cr.user_id = ?
        WHERE cm.sender_id != ?
          AND cm.id > COALESCE(cr.last_read_message_id, 0)
          AND (
              a.applicant_id = ?
              OR j.telegram_id = ?
          )
    """, (tid, tid, tid, tid))

    row = c.fetchone()
    conn.close()

    return jsonify({
        "unread_count": int(row["cnt"] or 0)
    })

@app.route("/api/chats", methods=["GET"])
def api_chats_list():
    tid, err = require_auth()
    if err:
        return err

    ensure_user(tid)

    conn = db_connect()
    c = conn.cursor()

    c.execute("SELECT active_mode FROM users WHERE telegram_id=?", (tid,))
    user = c.fetchone()
    active_mode = user["active_mode"] if user else "applicant"

    if active_mode == "employer":
        where_sql = "j.telegram_id = ?"
    else:
        where_sql = "a.applicant_id = ?"

    c.execute(f"""
        SELECT
            a.id AS application_id,
            a.status,
            a.created_at,
            a.applicant_id,
            j.telegram_id AS employer_id,
            j.title AS job_title,
            j.city AS job_city,
            COALESCE(j.company_name_override, ep.company_name, '') AS company_name,
            r.full_name AS applicant_name,

            (
                SELECT cm.message_text
                FROM chat_messages cm
                WHERE cm.application_id = a.id
                ORDER BY cm.id DESC
                LIMIT 1
            ) AS last_message,

            (
                SELECT cm.created_at
                FROM chat_messages cm
                WHERE cm.application_id = a.id
                ORDER BY cm.id DESC
                LIMIT 1
            ) AS last_message_at,

            (
                SELECT COUNT(*)
                FROM chat_messages cm
                LEFT JOIN chat_reads cr
                  ON cr.application_id = cm.application_id
                 AND cr.user_id = ?
                WHERE cm.application_id = a.id
                  AND cm.sender_id != ?
                  AND cm.id > COALESCE(cr.last_read_message_id, 0)
            ) AS unread_count

        FROM applications a
        JOIN jobs j ON j.id = a.job_id
        LEFT JOIN employer_profiles ep ON ep.telegram_id = j.telegram_id
        LEFT JOIN resumes r ON r.telegram_id = a.applicant_id
        WHERE {where_sql}
        ORDER BY COALESCE(last_message_at, a.created_at) DESC
    """, (tid, tid, tid))

    rows = []
    for r in c.fetchall():
        d = dict(r)

        if active_mode == "employer":
            d["my_role"] = "employer"
            d["chat_title"] = d["applicant_name"] or "Соискатель"
        else:
            d["my_role"] = "applicant"
            d["chat_title"] = d["company_name"] or "Работодатель"

        d["chat_subtitle"] = d["job_title"] or ""
        d["unread_count"] = int(d.get("unread_count") or 0)

        rows.append(d)

    conn.close()
    return jsonify(rows)







def get_chat_access(application_id: int, tid: int):
    conn = db_connect()
    c = conn.cursor()
    c.execute("""
        SELECT
            a.id AS application_id,
            a.applicant_id,
            j.telegram_id AS employer_id,
            j.title AS job_title
        FROM applications a
        JOIN jobs j ON j.id = a.job_id
        WHERE a.id=?
    """, (application_id,))
    row = c.fetchone()
    conn.close()

    if not row:
        return None

    if tid != int(row["applicant_id"]) and tid != int(row["employer_id"]):
        return None

    return dict(row)



@app.route("/api/chat/<int:application_id>", methods=["GET"])
def api_chat_messages(application_id: int):
    tid, err = require_auth()
    if err:
        return err

    access = get_chat_access(application_id, tid)
    if not access:
        return jsonify({"error": "Нет доступа к чату"}), 403

    conn = db_connect()
    c = conn.cursor()

    c.execute("""
        SELECT id, application_id, sender_id, message_text, created_at
        FROM chat_messages
        WHERE application_id=?
        ORDER BY created_at ASC, id ASC
    """, (application_id,))

    messages = [dict(r) for r in c.fetchall()]

    last_message_id = 0
    if messages:
        last_message_id = int(messages[-1]["id"])

    c.execute("""
        INSERT INTO chat_reads (application_id, user_id, last_read_message_id, updated_at)
        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(application_id, user_id) DO UPDATE SET
            last_read_message_id=excluded.last_read_message_id,
            updated_at=CURRENT_TIMESTAMP
    """, (application_id, tid, last_message_id))

    conn.commit()
    conn.close()

    return jsonify({
        "application_id": application_id,
        "job_title": access["job_title"],
        "current_user_id": tid,
        "messages": messages
    })




@app.route("/api/chat/<int:application_id>", methods=["POST"])
def api_chat_send(application_id: int):
    tid, err = require_auth()
    if err:
        return err

    access = get_chat_access(application_id, tid)
    if not access:
        return jsonify({"error": "Нет доступа к чату"}), 403

    data = request.get_json(silent=True) or {}
    text = str(data.get("text", "")).strip()

    if not text:
        return jsonify({"error": "Введите сообщение"}), 400

    if len(text) > 1000:
        return jsonify({"error": "Сообщение слишком длинное"}), 400

    conn = db_connect()
    c = conn.cursor()
    c.execute("""
        INSERT INTO chat_messages (application_id, sender_id, message_text)
        VALUES (?, ?, ?)
    """, (application_id, tid, text))
    conn.commit()

    try:
        c.execute("""
            SELECT
                a.applicant_id,
                j.telegram_id AS employer_id,
                j.title
            FROM applications a
            JOIN jobs j ON j.id = a.job_id
            WHERE a.id = ?
        """, (application_id,))

        row = c.fetchone()

        if row:
            applicant_id = row["applicant_id"]
            employer_id = row["employer_id"]
            job_title = row["title"] or "Вакансия"

            receiver_id = employer_id if tid == applicant_id else applicant_id

            preview = text[:120]

            bot.send_message(
                receiver_id,
                f"💬 Новое сообщение\n\n"
                f"📌 {job_title}\n\n"
                f"{preview}",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "Открыть чат",
                            web_app=WebAppInfo(
                                url=f"{WEBAPP_URL}/chat/{application_id}"
                            )
                        )
                    ]
                ])
            )

    except Exception as e:
        print("chat notify error:", e)
    conn.close()

    try:
        if tid == int(access["applicant_id"]):
            receiver_id = int(access["employer_id"])
            prefix = "💬 Новое сообщение от соискателя"
        else:
            receiver_id = int(access["applicant_id"])
            prefix = "💬 Новое сообщение от работодателя"

        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton(
            "Открыть чат",
            web_app=types.WebAppInfo(f"{WEBAPP_URL}/chat/{application_id}")
        ))

        bot.send_message(
            receiver_id,
            f"{prefix}\n\n"
            f"Вакансия: {access['job_title']}\n\n"
            f"{text[:500]}",
            reply_markup=markup
        )
    except Exception:
        pass

    return jsonify({"success": True})
# ======================
# API — APPLY (APPLICANT ONLY)
# ======================

@app.route("/api/apply", methods=["POST"])
def api_apply():
    tid, err = require_mode("applicant")
    if err:
        return err
    ensure_user(tid)

    conn = db_connect()
    c = conn.cursor()

    if not has_resume(tid):
        conn.close()
        return jsonify({"error": "Сначала заполните резюме"}), 400

    job_id = (request.json or {}).get("job_id")
    if not job_id:
        conn.close()
        return jsonify({"error": "job_id обязателен"}), 400

    c.execute("SELECT id, telegram_id, title FROM jobs WHERE id=? AND status='approved'", (job_id,))
    job = c.fetchone()
    if not job:
        conn.close()
        return jsonify({"error": "Вакансия недоступна"}), 404

    try:
        c.execute("""
            INSERT INTO applications (job_id, applicant_id, status)
            VALUES (?, ?, 'new')
        """, (job_id, tid))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({"error": "Вы уже откликались на эту вакансию"}), 400

    c.execute("SELECT * FROM resumes WHERE telegram_id=?", (tid,))
    resume = c.fetchone()
    conn.close()

    try:
        bot.send_message(
            job["telegram_id"],
            f"📥 Новый отклик!\n\n"
            f"📌 Вакансия: {job['title']}\n\n"
            f"👤 {resume['full_name']}\n"
            f"📍 {resume['city']}\n"
            f"🎯 {resume['position']}\n"
            f"🕒 Опыт: {resume['experience']}\n"
            f"🛠 Навыки: {resume['skills']}\n"
            f"📞 {resume['phone']}\n"
            f"🗓 График: {resume['employment_type']}"
        )
    except Exception:
        pass

    return jsonify({"success": True})


# ======================
# BOT CALLBACK MODERATION
# ======================

@bot.callback_query_handler(func=lambda call: call.data.startswith(("approve_", "reject_")))
def moderation(call):
    if call.message.chat.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "Нет доступа")
        return

    action, job_id = call.data.split("_")
    job_id = int(job_id)

    conn = db_connect()
    c = conn.cursor()

    c.execute("SELECT telegram_id, title FROM jobs WHERE id=?", (job_id,))
    job = c.fetchone()
    if not job:
        conn.close()
        bot.answer_callback_query(call.id, "Вакансия не найдена")
        return

    employer_id = job["telegram_id"]
    title = job["title"]

    if action == "approve":
        c.execute("UPDATE jobs SET status='approved' WHERE id=?", (job_id,))
        conn.commit()
        conn.close()

        try:
            bot.edit_message_text("✅ Вакансия одобрена", call.message.chat.id, call.message.message_id)
        except Exception:
            pass

        try:
            bot.send_message(employer_id, f"✅ Ваша вакансия одобрена и опубликована:\n{title}")
        except Exception:
            pass

        try:
            notify_city_subscribers(job_id)
        except Exception:
            pass
    else:
        c.execute("UPDATE jobs SET status='rejected' WHERE id=?", (job_id,))
        conn.commit()
        conn.close()

        try:
            bot.edit_message_text("❌ Вакансия отклонена", call.message.chat.id, call.message.message_id)
        except Exception:
            pass

        try:
            bot.send_message(employer_id, f"❌ Ваша вакансия отклонена:\n{title}")
        except Exception:
            pass

    bot.answer_callback_query(call.id)


# ======================
# RUN
# ======================




# ======================
# BOT — МЕНЮ, ОПИСАНИЕ И ОТВЕТ НА ВСЁ ОСТАЛЬНОЕ
# ======================

BOT_SHORT_DESCRIPTION = (
    "Работа в Северной Осетии. Вакансии рядом с домом, "
    "отклик в два нажатия, переписка с работодателем прямо здесь."
)

BOT_DESCRIPTION = """Поиск работы по Северной Осетии.

Ищете работу — смотрите вакансии по городам и сёлам, откликайтесь в два нажатия, переписывайтесь с работодателем здесь же и получайте уведомления о новых вакансиях.

Нанимаете — создайте профиль компании и опубликуйте вакансию. Отклики с контактами кандидатов придут в этот чат.

Нажмите «Начать»."""


def _open_app_markup(path: str = "", label: str = "Открыть вакансии"):
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton(
        label,
        web_app=types.WebAppInfo(WEBAPP_URL + path)
    ))
    return markup


@bot.message_handler(commands=["help"])
def cmd_help(message):
    bot.send_message(
        message.chat.id,
        """Как это работает

Если ищете работу:
откройте вакансии, откликнитесь на подходящую — работодатель получит ваше резюме и сможет написать вам прямо здесь, в Telegram.

Если нанимаете:
создайте профиль компании и опубликуйте вакансию. После проверки она появится в списке, а отклики придут в этот чат.

Чтобы не заглядывать каждый день, включите уведомления о новых вакансиях: Профиль — Кабинет соискателя.""",
        reply_markup=_open_app_markup()
    )


@bot.message_handler(commands=["resume"])
def cmd_resume(message):
    bot.send_message(
        message.chat.id,
        "Резюме заполняется один раз. Дальше оно уходит работодателю при каждом вашем отклике.",
        reply_markup=_open_app_markup("/resume", "Открыть резюме")
    )


# ======================
# ВОПРОСЫ ОТ ЛЮДЕЙ
# ======================

# Время последних сообщений от каждого человека — чтобы один
# не завалил админский чат потоком.
_question_times = {}
_question_lock = threading.Lock()

# Не больше десяти сообщений за десять минут от одного человека.
# Именно окно, а не пауза между сообщениями: альбом из нескольких
# фотографий приходит отдельными сообщениями подряд и должен проходить.
QUESTION_WINDOW_SEC = 600
QUESTION_MAX_IN_WINDOW = 10

# Сколько символов вопроса пересылаем (у Telegram предел на длину сообщения)
QUESTION_MAX_LEN = 3000

# Что человек может прислать помимо текста
ATTACHMENT_TYPES = [
    "photo", "video", "document", "audio", "voice",
    "video_note", "animation", "sticker", "contact", "location"
]


def _question_allowed(user_id):
    """Пропускаем сообщение, если человек не вышел за предел окна."""
    now = time.time()

    with _question_lock:
        times = [t for t in _question_times.get(user_id, []) if now - t < QUESTION_WINDOW_SEC]

        if len(times) >= QUESTION_MAX_IN_WINDOW:
            _question_times[user_id] = times
            return False

        times.append(now)
        _question_times[user_id] = times
        return True


def _extract_user_id(text):
    """Достаёт номер пользователя из служебной строки «id: 123» в вопросе.

    На эту строку опирается ответ администратора: он отвечает
    на сообщение с вопросом, а бот по ней понимает, кому переслать ответ.
    """
    for line in (text or "").split("\n"):
        line = line.strip()
        if line.startswith("id: "):
            digits = line[4:].strip()
            if digits.isdigit():
                return int(digits)
    return None


def _describe_sender(user):
    name = " ".join(x for x in [user.first_name, user.last_name] if x).strip()
    username = "@" + user.username if user.username else "без имени пользователя"
    return (name or "Без имени") + " (" + username + ")"


# Ответ администратора: он нажимает «Ответить» на сообщении с вопросом.
# Отдельного состояния не нужно — кому отвечаем, понятно из самого сообщения.
@bot.message_handler(
    func=lambda m: (
        m.chat.type == "private"
        and m.from_user.id == ADMIN_ID
        and m.reply_to_message is not None
    ),
    content_types=["text"]
)
def admin_reply_to_question(message):
    target_id = _extract_user_id(message.reply_to_message.text)

    if not target_id:
        bot.send_message(
            message.chat.id,
            "Не вижу, кому отвечать. Нажмите «Ответить» именно на сообщение с вопросом."
        )
        return

    try:
        bot.send_message(
            target_id,
            "Ответ администратора:\n\n" + message.text,
            reply_markup=_open_app_markup()
        )
        bot.send_message(message.chat.id, "Отправлено.")
    except Exception as e:
        bot.send_message(message.chat.id, "Не удалось отправить: " + str(e))


# Фотографии, голосовые, файлы и прочее.
# Сначала пересылаем само вложение, следом карточку с номером человека:
# так карточка оказывается последней, и отвечать админу удобнее именно на неё.
@bot.message_handler(func=lambda m: m.chat.type == "private", content_types=ATTACHMENT_TYPES)
def incoming_attachment(message):
    user = message.from_user

    if user.id == ADMIN_ID:
        return

    if not _question_allowed(user.id):
        bot.send_message(
            message.chat.id,
            "Слишком много сообщений подряд. Подождите немного."
        )
        return

    caption = (message.caption or "").strip()[:QUESTION_MAX_LEN]

    card = (
        "📎 Вложение от пользователя\n\n"
        + _describe_sender(user) + "\n"
        + "id: " + str(user.id) + "\n"
    )

    if caption:
        card += "\n" + caption + "\n"

    card += "\nОтветьте на это сообщение — ответ уйдёт человеку."

    try:
        # copy_message, а не forward_message: пересылка показывает отправителя
        # только если он не закрыл это в настройках приватности, и тогда
        # ответить ему было бы невозможно. Копия приходит всегда.
        bot.copy_message(ADMIN_ID, message.chat.id, message.message_id)
        bot.send_message(ADMIN_ID, card)
    except Exception as e:
        print("ATTACHMENT: не доставлено админу:", e, flush=True)
        bot.send_message(
            message.chat.id,
            "Не получилось передать вложение. Попробуйте позже."
        )
        return

    bot.send_message(
        message.chat.id,
        "Отправлено администратору. Ответ придёт сюда же.",
        reply_markup=_open_app_markup()
    )


# Ловит всё, что не подошло другим обработчикам.
# Должен стоять ПОСЛЕ них: telebot берёт первый подходящий по порядку записи.
# Только личные сообщения — в группах бот молчит, иначе это спам.
@bot.message_handler(func=lambda m: m.chat.type == "private", content_types=["text"])
def incoming_question(message):
    user = message.from_user

    if user.id == ADMIN_ID:
        bot.send_message(
            message.chat.id,
            "Чтобы ответить человеку, нажмите «Ответить» на сообщении с его вопросом.",
            reply_markup=_open_app_markup()
        )
        return

    if not _question_allowed(user.id):
        bot.send_message(
            message.chat.id,
            "Слишком много сообщений подряд. Подождите немного — администратор уже видит ваши вопросы."
        )
        return

    question = (message.text or "").strip()[:QUESTION_MAX_LEN]

    card = (
        "❓ Вопрос от пользователя\n\n"
        + _describe_sender(user) + "\n"
        + "id: " + str(user.id) + "\n\n"
        + question + "\n\n"
        + "Ответьте на это сообщение — ответ уйдёт человеку."
    )

    try:
        bot.send_message(ADMIN_ID, card)
    except Exception as e:
        print("QUESTION: не доставлено админу:", e, flush=True)
        bot.send_message(
            message.chat.id,
            "Не получилось передать вопрос. Попробуйте позже."
        )
        return

    bot.send_message(
        message.chat.id,
        "Вопрос отправлен администратору. Ответ придёт сюда же.",
        reply_markup=_open_app_markup()
    )


def setup_bot_profile():
    """Меню команд и описание бота. Вызывается один раз при запуске.

    Описание видно тому, кто открыл бота впервые и ещё не нажал «Начать».
    Меню команд — это список, который Telegram показывает сам.

    Ошибки намеренно глушим: если Telegram в этот момент недоступен,
    бот всё равно должен подняться и работать.
    """
    try:
        bot.set_my_commands([
            types.BotCommand("start", "Открыть вакансии"),
            types.BotCommand("resume", "Моё резюме"),
            types.BotCommand("help", "Как это работает"),
        ])
        bot.set_my_short_description(BOT_SHORT_DESCRIPTION)
        bot.set_my_description(BOT_DESCRIPTION)
        print("BOT PROFILE: меню и описание обновлены", flush=True)
    except Exception as e:
        print(f"BOT PROFILE: не удалось обновить ({e})", flush=True)


# ======================
# НАПОМИНАНИЯ РАБОТОДАТЕЛЯМ
# ======================

REMIND_AFTER_HOURS = 24          # отклик без ответа дольше суток
REMIND_MIN_GAP_HOURS = 24        # одному работодателю — не чаще раза в сутки
REMIND_CHECK_EVERY_SEC = 30 * 60
REMIND_HOURS_MSK = (9, 21)       # пишем только днём по Москве
REMIND_MAX_TITLES = 5


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _reminder_text(apps) -> str:
    n = len(apps)
    if n == 1:
        head = "Один человек откликнулся больше суток назад и пока не получил ответа:"
    else:
        head = f"{n} {_plural(n, 'человек', 'человека', 'человек')} откликнулись больше суток назад и пока не получили ответа:"

    # сколько ждут по каждой вакансии, в порядке первого отклика
    per_job = {}
    for a in apps:
        title = (a["title"] or "Без названия").strip()
        if len(title) > 60:
            title = title[:57] + "…"
        per_job.setdefault(a["job_id"], [title, 0])[1] += 1

    lines = [f"• {title} — {count}" for title, count in list(per_job.values())[:REMIND_MAX_TITLES]]
    rest = len(per_job) - REMIND_MAX_TITLES
    if rest > 0:
        lines.append(f"• и ещё {rest} {_plural(rest, 'вакансия', 'вакансии', 'вакансий')}")

    return (
        "⏳ Кандидаты ждут ответа\n\n"
        + head + "\n\n"
        + "\n".join(lines)
        + "\n\nПригласить или отказать — пара нажатий. Даже короткий отказ лучше тишины."
    )


def remind_employers_once() -> int:
    """Одна проверка: кому из работодателей напомнить об откликах без ответа.

    Каждый отклик попадает в напоминание только один раз, а один работодатель
    получает не больше одного сообщения в сутки. Возвращает число отправленных.
    """
    hour_msk = (datetime.utcnow() + timedelta(hours=3)).hour
    if not (REMIND_HOURS_MSK[0] <= hour_msk < REMIND_HOURS_MSK[1]):
        return 0

    conn = db_connect()
    c = conn.cursor()
    c.execute("""
        SELECT a.id, a.job_id, j.telegram_id AS employer_id, j.title
        FROM applications a
        JOIN jobs j ON j.id = a.job_id
        WHERE a.status = 'new'
          AND a.reminded_at IS NULL
          AND a.created_at <= datetime('now', ?)
          AND COALESCE(j.is_deleted, 0) = 0
        ORDER BY a.created_at
    """, (f"-{REMIND_AFTER_HOURS} hours",))

    by_employer = {}
    for row in c.fetchall():
        by_employer.setdefault(row["employer_id"], []).append(row)

    sent = 0
    for employer_id, apps in by_employer.items():
        c.execute("""
            SELECT 1 FROM employer_reminders
            WHERE employer_id = ? AND sent_at > datetime('now', ?)
            LIMIT 1
        """, (employer_id, f"-{REMIND_MIN_GAP_HOURS} hours"))
        if c.fetchone():
            continue  # уже писали за последние сутки — эти отклики подождут до следующего раза

        banned, _ = is_banned_employer(employer_id)
        if banned:
            continue

        job_ids = {a["job_id"] for a in apps}
        if len(job_ids) == 1:
            markup = _open_app_markup(f"/employer/job/{apps[0]['job_id']}/applications", "Открыть отклики")
        else:
            markup = _open_app_markup("/employer/jobs", "Открыть мои вакансии")

        ids = [a["id"] for a in apps]
        marks = ",".join("?" * len(ids))

        try:
            bot.send_message(employer_id, _reminder_text(apps), reply_markup=markup)
        except telebot.apihelper.ApiTelegramException as e:
            if e.error_code in (400, 403):
                # заблокировал бота или удалил аккаунт — больше не пытаемся
                c.execute(f"UPDATE applications SET reminded_at = CURRENT_TIMESTAMP WHERE id IN ({marks})", ids)
                conn.commit()
                print(f"REMIND: {employer_id} недоступен ({e.error_code}), отклики отмечены", flush=True)
                continue
            print(f"REMIND: Telegram ответил ошибкой ({e}), повторю позже", flush=True)
            break
        except Exception as e:
            # Telegram недоступен: ничего не отмечаем, через полчаса попробуем снова
            print(f"REMIND: нет связи с Telegram ({e}), повторю позже", flush=True)
            break

        c.execute(f"UPDATE applications SET reminded_at = CURRENT_TIMESTAMP WHERE id IN ({marks})", ids)
        c.execute(
            "INSERT INTO employer_reminders (employer_id, applications_count) VALUES (?, ?)",
            (employer_id, len(ids))
        )
        conn.commit()
        sent += 1
        print(f"REMIND: работодателю {employer_id} — {len(ids)} откл.", flush=True)
        time.sleep(0.05)

    conn.close()
    return sent


def _reminder_loop():
    time.sleep(60)  # дать боту спокойно подняться после перезапуска
    while True:
        try:
            remind_employers_once()
        except Exception:
            import traceback
            print("REMIND: ошибка проверки", flush=True)
            traceback.print_exc()
        time.sleep(REMIND_CHECK_EVERY_SEC)


def _polling_guard():
    """Слушает Telegram. Если опрос по любой причине прекратился — гасит весь процесс.

    Без этого поток опроса может тихо умереть, а Flask продолжит работать:
    сайт отвечает, systemd считает службу живой, а бот при этом молчит,
    и снаружи это никак не видно. Выход из процесса заставляет systemd
    (Restart=always) поднять всё заново.
    """
    import traceback
    try:
        bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)
        print("POLLING STOPPED: опрос завершился без ошибки", flush=True)
    except BaseException:
        print("POLLING CRASHED:", flush=True)
        traceback.print_exc()
    # пауза, чтобы при недоступном Telegram не перезапускаться в цикле без передышки
    time.sleep(3)
    os._exit(1)


if __name__ == "__main__":
    setup_bot_profile()
    threading.Thread(target=_polling_guard, daemon=True).start()
    threading.Thread(target=_reminder_loop, daemon=True).start()

    # Только сам сервер: снаружи приложение доступно исключительно через nginx
    # с шифрованием. Раньше порт 5000 был открыт всему интернету напрямую.
    app.run(host="127.0.0.1", port=5000)
