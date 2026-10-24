#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Свой "GodGram" одним файлом. Только стандартная библиотека Python (pip не нужен).

Запуск в Termux:
    pkg install python
    python server.py
Открой в браузере то, что напишет сервер (обычно http://localhost:8080).

ВХОД КАК В НАСТОЯЩЕМ TELEGRAM: слайды -> номер телефона -> код -> (если новый) имя и фото.
SMS-шлюза нет, поэтому коды работают так:
  * код ВСЕГДА печатается в консоли Termux:  [КОД ВХОДА] +7999...: 12345
  * если аккаунт уже есть - код приходит в чат "Telegram" на устройстве, где ты уже вошёл
  * если номер новый - код ещё и показывается на экране (SHOW_CODE=0 отключает)

НОВОЕ В v5: боты (создание, сценарии, HTTP Bot API), каналы, заморозка ботов/каналов админом,
кошелёк ⭐, 3D-подарки, настройки. Удалять админ может только людей.

НОВОЕ В v6: профили в стиле Telegram (дата рождения, часы работы, адрес, канал, NFT-имена под основным),
метки (Telegram / Роскомнадзор / Мажор / Холд, СКАМ, ФЕЙК) для людей, ботов и каналов, жалобы, закрепы чатов,
каналы с настройками и бустами (Premium), маркет NFT-подарков (коллекции выпускает админ), удаление ботов/каналов админом.

Первый зарегистрированный пользователь = АДМИН.
Назначить админом:  python server.py --make-admin +79991234567   (или имя_пользователя)
Порт поменять:      PORT=9000 python server.py
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PORT = int(os.environ.get("PORT", "8080"))
SHOW_CODE = os.environ.get("SHOW_CODE", "1") == "1"
# Входить и регистрироваться можно только по ОСНОВНОМУ номеру. NFT-номера +888 - только для показа в профиле.
# NFT_LOGIN=1 вернёт вход по +888 номеру.
NFT_LOGIN = os.environ.get("NFT_LOGIN", "0") == "1"
MAIN_ONLY_MSG = "Войти и зарегистрироваться можно только по основному номеру телефона. Номера +888 - это NFT-номера, по ним вход закрыт"
NB_COLORS = 13  # сколько цветов баннера профиля
FOREVER = 32503680000.0  # "навсегда" для Premium

_here = os.path.dirname(os.path.abspath(__file__))
# SQLite плохо работает на общей памяти телефона (/storage, /sdcard) - база лежит в домашней папке Termux
if _here.startswith(("/storage", "/sdcard")):
    _here = os.path.expanduser("~")
DB_PATH = os.environ.get("DB_PATH", os.path.join(_here, "tgsite.db"))


def prepare_db():
    """Самая старая база (логин/пароль) несовместима - откладываем её в сторону."""
    if not os.path.exists(DB_PATH):
        return
    c = sqlite3.connect(DB_PATH)
    try:
        v = c.execute("PRAGMA user_version").fetchone()[0]
        has = c.execute("SELECT 1 FROM sqlite_master WHERE name='users'").fetchone()
    finally:
        c.close()
    if has and v < 2:
        bak = DB_PATH + ".old-" + str(int(time.time()))
        os.rename(DB_PATH, bak)
        print("Старая база несовместима с новым входом по номеру. Сохранена как:", bak)


prepare_db()

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  phone TEXT UNIQUE,
  username TEXT UNIQUE,
  nft_username TEXT,
  nft_number TEXT,
  first_name TEXT DEFAULT '',
  last_name TEXT DEFAULT '',
  name TEXT DEFAULT '',
  bio TEXT DEFAULT '',
  avatar TEXT DEFAULT '',
  avv INTEGER DEFAULT 0,
  is_admin INTEGER DEFAULT 0,
  is_service INTEGER DEFAULT 0,
  deleted INTEGER DEFAULT 0,
  created REAL,
  last_seen REAL DEFAULT 0,
  uorder TEXT DEFAULT '[]',
  premium_until REAL DEFAULT 0,
  banner_color INTEGER DEFAULT -1,
  banner_pattern TEXT DEFAULT '',
  emoji_status TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS nft_usernames(
  id INTEGER PRIMARY KEY AUTOINCREMENT, handle TEXT UNIQUE, owner_id INTEGER, created REAL,
  active INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS nft_numbers(
  id INTEGER PRIMARY KEY AUTOINCREMENT, number TEXT UNIQUE, owner_id INTEGER, created REAL
);
CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, user_id INTEGER);
CREATE TABLE IF NOT EXISTS chats(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT, title TEXT DEFAULT '', avatar TEXT DEFAULT '', avv INTEGER DEFAULT 0,
  owner INTEGER, created REAL,
  discussion_chat_id INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS members(
  chat_id INTEGER, user_id INTEGER, last_read INTEGER DEFAULT 0,
  PRIMARY KEY(chat_id, user_id)
);
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id INTEGER, user_id INTEGER, text TEXT, ts REAL
);
CREATE INDEX IF NOT EXISTS idx_msg_chat ON messages(chat_id, id);
PRAGMA user_version=3;
"""

lock = threading.RLock()
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
conn.row_factory = sqlite3.Row
conn.executescript(SCHEMA)


def _cols(t):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({t})")}


def _add_col(t, c, ddl):
    if c in _cols(t):
        return False
    conn.execute(f"ALTER TABLE {t} ADD COLUMN {c} {ddl}")
    return True


# миграция базы прошлой версии (аккаунты не теряются)
for _c, _d in [("uorder", "TEXT DEFAULT '[]'"), ("premium_until", "REAL DEFAULT 0"),
               ("banner_color", "INTEGER DEFAULT -1"), ("banner_pattern", "TEXT DEFAULT ''"),
               ("emoji_status", "TEXT DEFAULT ''"), ("umain", "TEXT DEFAULT '[]'")]:
    _add_col("users", _c, _d)
for _c, _d in [("cloud_hash", "TEXT DEFAULT ''"), ("cloud_salt", "TEXT DEFAULT ''"), ("cloud_hint", "TEXT DEFAULT ''")]:
    _add_col("users", _c, _d)  # облачный пароль (двухэтапная проверка)
if _add_col("nft_usernames", "active", "INTEGER DEFAULT 1"):
    conn.execute("""UPDATE nft_usernames SET active=0 WHERE owner_id IS NOT NULL AND
                    handle!=COALESCE((SELECT nft_username FROM users WHERE users.id=nft_usernames.owner_id),'')""")
_add_col("nft_usernames", "price", "INTEGER DEFAULT 0")
if _add_col("nft_usernames", "issued", "REAL DEFAULT 0"):  # старым выданным ставим дату создания
    conn.execute("UPDATE nft_usernames SET issued=COALESCE(created,0) WHERE owner_id IS NOT NULL")
_add_col("nft_usernames", "listed", "INTEGER DEFAULT 0")
_add_col("nft_usernames", "list_price", "INTEGER DEFAULT 0")
_add_col("nft_numbers", "listed", "INTEGER DEFAULT 0")
_add_col("nft_numbers", "list_price", "INTEGER DEFAULT 0")
_add_col("nft_numbers", "price", "INTEGER DEFAULT 0")
conn.commit()

# ---------- v5: кошелёк, подарки, боты, каналы, заморозка ----------
for _t, _c, _d in [("users", "is_bot", "INTEGER DEFAULT 0"), ("users", "bot_owner", "INTEGER DEFAULT 0"),
                   ("users", "bot_token", "TEXT"), ("users", "bot_script", "TEXT DEFAULT '{}'"),
                   ("users", "bot_desc", "TEXT DEFAULT ''"), ("users", "frozen", "INTEGER DEFAULT 0"),
                   ("users", "balance", "INTEGER DEFAULT 0"), ("users", "rating", "INTEGER DEFAULT 0"), ("chats", "frozen", "INTEGER DEFAULT 0"),
                   ("messages", "gift_id", "INTEGER DEFAULT 0")]:
    _add_col(_t, _c, _d)
conn.executescript("""
CREATE TABLE IF NOT EXISTS gifts(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, price INTEGER DEFAULT 0, model TEXT DEFAULT 'extrude',
  glyph TEXT DEFAULT '🎁', color TEXT DEFAULT '#2aabee', anim TEXT DEFAULT 'spin',
  supply INTEGER DEFAULT 0, sold INTEGER DEFAULT 0, active INTEGER DEFAULT 1, created REAL
);
CREATE TABLE IF NOT EXISTS user_gifts(
  id INTEGER PRIMARY KEY AUTOINCREMENT, gift_id INTEGER, owner_id INTEGER, from_id INTEGER,
  note TEXT DEFAULT '', number INTEGER DEFAULT 0, ts REAL
);
CREATE INDEX IF NOT EXISTS idx_ug_owner ON user_gifts(owner_id, id);
CREATE TABLE IF NOT EXISTS rating_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, delta INTEGER, kind TEXT, note TEXT DEFAULT '',
  from_id INTEGER DEFAULT 0, ref_id INTEGER DEFAULT 0, ts REAL
);
CREATE INDEX IF NOT EXISTS idx_rlog ON rating_log(user_id, id);
CREATE TABLE IF NOT EXISTS tx(
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, amount INTEGER, kind TEXT, note TEXT DEFAULT '', ts REAL
);
CREATE INDEX IF NOT EXISTS idx_tx_user ON tx(user_id, id);
CREATE TABLE IF NOT EXISTS star_checks(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT UNIQUE,
  creator_id INTEGER,
  amount INTEGER,
  max_uses INTEGER DEFAULT 1,
  used INTEGER DEFAULT 0,
  note TEXT DEFAULT '',
  active INTEGER DEFAULT 1,
  created REAL,
  expires REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_chk_code ON star_checks(code);
CREATE TABLE IF NOT EXISTS star_check_claims(
  check_id INTEGER, user_id INTEGER, ts REAL,
  PRIMARY KEY(check_id, user_id)
);
CREATE TABLE IF NOT EXISTS bot_updates(
  id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id INTEGER, chat_id INTEGER, from_id INTEGER, text TEXT, ts REAL
);
CREATE INDEX IF NOT EXISTS idx_botupd ON bot_updates(bot_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_bot_token ON users(bot_token) WHERE bot_token IS NOT NULL;
""")
GIFT_SEED = [("Сердце", 25, "extrude", "❤️", "#ff4d6d", "pulse"), ("Мишка", 50, "extrude", "🧸", "#ff8fb1", "float"),
             ("Торт", 75, "extrude", "🎂", "#ff9ccb", "bounce"), ("Подарок", 100, "box", "🎀", "#ff7a45", "spin"),
             ("Ракета", 150, "extrude", "🚀", "#5b8cff", "bounce"), ("Звезда", 200, "extrude", "⭐", "#ffd54a", "spin"),
             ("Алмаз", 500, "extrude", "💎", "#33c9ff", "spin"), ("Корона", 1000, "extrude", "👑", "#ffc83d", "float")]
if not conn.execute("SELECT 1 FROM gifts").fetchone():
    for _g in GIFT_SEED:
        conn.execute("INSERT INTO gifts(name,price,model,glyph,color,anim,created) VALUES(?,?,?,?,?,?,?)", _g + (time.time(),))
conn.commit()

# ---------- v6: метки, закрепы, жалобы, каналы+бусты, NFT-маркет, поля профиля ----------
for _t, _c, _d in [("users", "birthday", "TEXT DEFAULT ''"), ("users", "address", "TEXT DEFAULT ''"),
                   ("users", "work_from", "TEXT DEFAULT ''"), ("users", "work_to", "TEXT DEFAULT ''"),
                   ("users", "pchannel", "INTEGER DEFAULT 0"), ("users", "status_text", "TEXT DEFAULT ''"),
                   ("chats", "descr", "TEXT DEFAULT ''"), ("chats", "handle", "TEXT"),
                   ("chats", "sign", "INTEGER DEFAULT 0"), ("chats", "bcolor", "INTEGER DEFAULT -1"),
                   ("chats", "bpattern", "TEXT DEFAULT ''"), ("chats", "estatus", "TEXT DEFAULT ''"),
                   ("chats", "deleted", "INTEGER DEFAULT 0"), ("chats", "pub", "INTEGER DEFAULT 1"),
                   ("user_gifts", "listed", "INTEGER DEFAULT 0"), ("users", "status_nft", "INTEGER DEFAULT 0"),
                   ("users", "boost_inf", "INTEGER DEFAULT 0"), ("chats", "boost_extra", "INTEGER DEFAULT 0"), ("chats", "discussion_chat_id", "INTEGER DEFAULT 0")]:
    _add_col(_t, _c, _d)
conn.executescript("""
CREATE TABLE IF NOT EXISTS badge_defs(
  key TEXT PRIMARY KEY, title TEXT, descr TEXT, color TEXT, kind TEXT, icon TEXT, sort INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS entity_badges(
  kind TEXT, tid INTEGER, bkey TEXT, ts REAL, PRIMARY KEY(kind, tid, bkey)
);
CREATE TABLE IF NOT EXISTS pins(user_id INTEGER, chat_id INTEGER, ts REAL, PRIMARY KEY(user_id, chat_id));
CREATE TABLE IF NOT EXISTS reports(
  id INTEGER PRIMARY KEY AUTOINCREMENT, reporter_id INTEGER, tkind TEXT, tid INTEGER, reason TEXT,
  text TEXT DEFAULT '', status TEXT DEFAULT 'open', ts REAL, note TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_rep ON reports(status, id);
CREATE TABLE IF NOT EXISTS boosts(chat_id INTEGER, user_id INTEGER, ts REAL, PRIMARY KEY(chat_id, user_id));
CREATE UNIQUE INDEX IF NOT EXISTS idx_chat_handle ON chats(handle) WHERE handle IS NOT NULL;
CREATE TABLE IF NOT EXISTS nft_colls(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, glyph TEXT, supply INTEGER DEFAULT 0, minted INTEGER DEFAULT 0,
  price INTEGER DEFAULT 100, models TEXT DEFAULT '[]', active INTEGER DEFAULT 1, created REAL
);
CREATE TABLE IF NOT EXISTS nft_items(
  id INTEGER PRIMARY KEY AUTOINCREMENT, coll_id INTEGER, number INTEGER, owner_id INTEGER DEFAULT 0,
  model TEXT, backdrop TEXT, symbol TEXT, anim TEXT DEFAULT 'spin', price INTEGER DEFAULT 0,
  listed INTEGER DEFAULT 0, from_id INTEGER DEFAULT 0, ts REAL
);
CREATE INDEX IF NOT EXISTS idx_nfti_owner ON nft_items(owner_id, id);
CREATE INDEX IF NOT EXISTS idx_nfti_list ON nft_items(listed, price);
""")
MAX_PINS, MAX_PINS_PREM = 5, 10
BOOST_SLOTS = 3  # сколько бустов может раздать один Premium-пользователь
BOOST_LEVELS = [1, 3, 6, 10]  # бустов для уровней 1..4
OWNER_BADGE = {"key": "owner", "title": "Owner GodGram", "descr": "Owner GodGram", "color": "#000000",
               "kind": "verify", "icon": "</>"}
BADGE_SEED = [
    ("owner", "Owner GodGram", "Owner GodGram", "#000000", "verify", "</>", 0),
    ("telegram", "Подтверждено GodGram", "Этот аккаунт подтверждён как официальный самим GodGram.", "#2aabee", "verify", "✔", 1),
    ("rkn", "Роскомнадзор", "Этот аккаунт подтверждён как официальный Роскомнадзором.", "#e0443c", "verify", "✔", 2),
    ("major", "Мажор", "Особый статус: известная личность или крупный проект. Подтверждено администрацией.", "#f5b82e", "verify", "★", 3),
    ("hold", "Холд", "Аккаунт на холде: идёт проверка, часть действий может быть временно ограничена.", "#8a96a3", "verify", "⏳", 4),
    ("scam", "СКАМ", "Многие пользователи пожаловались, что этот аккаунт мошеннический. Будьте осторожны: не переводите деньги и не сообщайте личные данные.", "#e0443c", "label", "СКАМ", 5),
    ("fake", "ФЕЙК", "Многие пользователи пожаловались, что этот аккаунт выдаёт себя за другого человека или организацию.", "#f08a24", "label", "ФЕЙК", 6),
]
for _b in BADGE_SEED:
    conn.execute("INSERT OR IGNORE INTO badge_defs(key,title,descr,color,kind,icon,sort) VALUES(?,?,?,?,?,?,?)", _b)

NFT_BACKDROPS = [  # (название, цвет центра, цвет края, редкость в промилле)
    ("Рассвет", "#ffb58a", "#c2527a", 60), ("Небо", "#7cc7ff", "#2d6fd0", 90), ("Мята", "#8ff0c8", "#1f8f78", 80),
    ("Лаванда", "#c9a8ff", "#6a4fd0", 70), ("Уголь", "#5b6572", "#1a1f26", 120), ("Вишня", "#ff7a93", "#9c1f45", 50),
    ("Океан", "#4fd0e8", "#0b5a8a", 90), ("Лайм", "#d4f06a", "#4f9a1f", 70), ("Золото", "#ffe27a", "#b9741a", 20),
    ("Графит", "#9aa4b0", "#3a424d", 110), ("Роза", "#ffc0d9", "#d6618f", 80), ("Индиго", "#7f8cff", "#2a2f9c", 60)]
NFT_SYMBOLS = [("Искры", "✨", 90), ("Снежинки", "❄️", 80), ("Цветы", "🌸", 80), ("Огонь", "🔥", 60), ("Клевер", "🍀", 80),
               ("Луна", "🌙", 80), ("Кристаллы", "💎", 30), ("Молния", "⚡", 70), ("Шарики", "🎈", 90),
               ("Бабочки", "🦋", 60), ("Звёзды", "⭐", 90), ("Сердечки", "💗", 90)]
NFT_ANIMS = ("spin", "float", "bounce", "pulse")
NFT_COLL_SEED = [  # оригинальные коллекции: (название, значок, тираж, цена, [(модель, эмодзи, редкость)])
    ("Плюшевые друзья", "🧸", 3000, 150, [("Мишка", "🧸", 300), ("Панда", "🐼", 200), ("Коала", "🐨", 120), ("Бурый", "🐻", 380)]),
    ("Неон-котики", "🐱", 2500, 200, [("Мурзик", "🐱", 350), ("Улыбака", "😺", 250), ("Влюблённый", "😻", 100), ("Чёрный", "🐈‍⬛", 300)]),
    ("Звёздный флот", "🚀", 1500, 400, [("Ракета", "🚀", 350), ("Тарелка", "🛸", 200), ("Спутник", "🛰️", 250), ("Комета", "🌠", 200)]),
    ("Короны и шляпы", "👑", 800, 900, [("Корона", "👑", 150), ("Цилиндр", "🎩", 250), ("Кепка", "🧢", 350), ("Академик", "🎓", 250)]),
    ("Кристаллы", "💎", 1000, 700, [("Алмаз", "💎", 150), ("Шар", "🔮", 250), ("Ромб", "💠", 300), ("Диско", "🪩", 300)]),
    ("Сердца", "❤️", 5000, 100, [("Красное", "❤️", 350), ("Фиолетовое", "💜", 250), ("Синее", "💙", 250), ("Розовое", "🩷", 150)]),
    ("Пламя", "🔥", 2000, 300, [("Огонь", "🔥", 400), ("Комета", "☄️", 200), ("Вулкан", "🌋", 150), ("Взрыв", "💥", 250)]),
    ("Лесные духи", "🍄", 2200, 250, [("Гриб", "🍄", 300), ("Лист", "🌿", 300), ("Сова", "🦉", 150), ("Лис", "🦊", 250)]),
    ("Призраки", "👻", 1300, 350, [("Привидение", "👻", 400), ("Череп", "💀", 200), ("Тыква", "🎃", 250), ("Паук", "🕷️", 150)]),
    ("Космос", "🪐", 1800, 450, [("Планета", "🪐", 250), ("Луна", "🌙", 300), ("Звезда", "⭐", 300), ("Солнце", "🌞", 150)]),
    ("Кубки", "🏆", 700, 800, [("Кубок", "🏆", 300), ("Медаль", "🥇", 300), ("Мишень", "🎯", 200), ("Кость", "🎲", 200)]),
    ("Сладости", "🍭", 4000, 120, [("Леденец", "🍭", 300), ("Пончик", "🍩", 300), ("Кекс", "🧁", 250), ("Торт", "🎂", 150)])]
NFT_COLL_SEED2 = [  # (название, значок, тираж, цена, [(модель, "svg:рисунок:оттенок", редкость)])
    ("Леденцы", "svg:lollipop", 31400, 60, [("Малина", "svg:lollipop:0", 300), ("Лимон", "svg:lollipop:70", 250), ("Мята", "svg:lollipop:150", 250), ("Черника", "svg:lollipop:230", 200)]),
    ("Звёздочки", "svg:star", 31000, 80, [("Лазурная", "svg:star:0", 350), ("Лиловая", "svg:star:60", 250), ("Мятная", "svg:star:150", 250), ("Закатная", "svg:star:250", 150)]),
    ("Колпаки волшебников", "svg:wizard", 49900, 120, [("Индиго", "svg:wizard:0", 300), ("Изумруд", "svg:wizard:110", 250), ("Рубин", "svg:wizard:200", 250), ("Золото", "svg:wizard:290", 200)]),
    ("Колокольчики", "svg:bells", 53200, 90, [("Золотые", "svg:bells:0", 400), ("Серебряные", "svg:bells:180", 250), ("Розовые", "svg:bells:300", 200), ("Лесные", "svg:bells:80", 150)]),
    ("Волшебные палочки", "svg:wand", 107900, 70, [("Розовая", "svg:wand:0", 350), ("Лавандовая", "svg:wand:60", 250), ("Небесная", "svg:wand:150", 250), ("Лаймовая", "svg:wand:240", 150)]),
    ("Самоцветы", "svg:crystal", 5100, 600, [("Топаз", "svg:crystal:0", 300), ("Аметист", "svg:crystal:60", 250), ("Изумруд", "svg:crystal:150", 250), ("Рубин", "svg:crystal:260", 200)]),
    ("Шутовские колпаки", "svg:jester", 84700, 100, [("Алый", "svg:jester:0", 300), ("Лазурный", "svg:jester:200", 250), ("Лесной", "svg:jester:100", 250), ("Фиолетовый", "svg:jester:290", 200)]),
    ("Капкейки", "svg:cupcake", 22900, 110, [("Клубника", "svg:cupcake:0", 300), ("Ваниль", "svg:cupcake:40", 250), ("Мята", "svg:cupcake:140", 250), ("Черника", "svg:cupcake:230", 200)]),
    ("Шарики-сердца", "svg:balloon", 57900, 80, [("Красный", "svg:balloon:0", 350), ("Оранжевый", "svg:balloon:30", 250), ("Фиолетовый", "svg:balloon:290", 250), ("Синий", "svg:balloon:220", 150)]),
    ("Подарочные коробки", "svg:giftbox", 93900, 90, [("Бирюзовая", "svg:giftbox:0", 300), ("Розовая", "svg:giftbox:140", 250), ("Золотая", "svg:giftbox:220", 250), ("Фиолетовая", "svg:giftbox:100", 200)])]
conn.commit()


# ---------- v7: фото, голосовые, истории, коллекция «Обнимашки» ----------
for _t, _c, _d in [("messages", "media_key", "TEXT DEFAULT ''"), ("messages", "media_kind", "TEXT DEFAULT ''"),
                   ("messages", "media_dur", "INTEGER DEFAULT 0")]:
    _add_col(_t, _c, _d)
conn.executescript("""
CREATE TABLE IF NOT EXISTS media(key TEXT PRIMARY KEY, mime TEXT, data BLOB, owner INTEGER, ts REAL);
CREATE TABLE IF NOT EXISTS stories(
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, kind TEXT, media_key TEXT DEFAULT '',
  text TEXT DEFAULT '', bg INTEGER DEFAULT 0, ts REAL, expires REAL
);
CREATE INDEX IF NOT EXISTS idx_story_user ON stories(user_id, expires);
CREATE TABLE IF NOT EXISTS story_views(story_id INTEGER, user_id INTEGER, ts REAL, PRIMARY KEY(story_id, user_id));
""")
conn.commit()
HUG_COLLS = ("Обнимашки",)
NFT_COLL_SEED3 = [("Обнимашки", "svg:hugbear", 25000, 180,
                   [("Мятный", "svg:hugbear:165", 300), ("Малиновый", "svg:hugbear:340", 250),
                    ("Небесный", "svg:hugbear:205", 250), ("Лавандовый", "svg:hugbear:265", 200)])]
IMG_MIMES = ("image/jpeg", "image/png", "image/webp", "image/gif")
AUD_MIMES = ("audio/webm", "audio/ogg", "audio/mp4", "audio/mpeg", "audio/wav", "audio/aac", "audio/x-m4a")
VID_MIMES = ("video/webm", "video/mp4", "video/quicktime", "video/3gpp")
FILE_MIMES = IMG_MIMES + AUD_MIMES + VID_MIMES + (
    "application/pdf", "application/zip", "application/octet-stream",
    "text/plain", "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
)

cond = threading.Condition()
version = 0


def bump():
    global version
    with cond:
        version += 1
        cond.notify_all()


def q(sql, args=(), one=False):
    with lock:
        rows = conn.execute(sql, args).fetchall()
    if one:
        return rows[0] if rows else None
    return rows


def ex(sql, args=()):
    with lock:
        cur = conn.execute(sql, args)
        conn.commit()
        return cur.lastrowid


class ApiError(Exception):
    def __init__(self, code, msg):
        self.code, self.msg = code, msg


def ensure_service():
    r = q("SELECT id FROM users WHERE is_service=1", one=True)
    if r:
        return r["id"]
    return ex("INSERT INTO users(username,first_name,name,is_service,created) VALUES('godgram','GodGram','GodGram',1,?)",
              (time.time(),))


SERVICE_ID = ensure_service()


SEND_BOT_AVATAR = "data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAxMjggMTI4Ij48ZGVmcz48bGluZWFyR3JhZGllbnQgaWQ9ImciIHgxPSIwIiB5MT0iMCIgeDI9IjEiIHkyPSIxIj48c3RvcCBvZmZzZXQ9IjAiIHN0b3AtY29sb3I9IiNmNWM1MTgiLz48c3RvcCBvZmZzZXQ9IjEiIHN0b3AtY29sb3I9IiNlOGEzMTciLz48L2xpbmVhckdyYWRpZW50PjwvZGVmcz48Y2lyY2xlIGN4PSI2NCIgY3k9IjY0IiByPSI2NCIgZmlsbD0idXJsKCNnKSIvPjxwYXRoIGQ9Ik0zNiA2NmwxOCAxOCAzOC00MiIgZmlsbD0ibm9uZSIgc3Ryb2tlPSIjMWExMjAwIiBzdHJva2Utd2lkdGg9IjEyIiBzdHJva2UtbGluZWNhcD0icm91bmQiIHN0cm9rZS1saW5lam9pbj0icm91bmQiLz48L3N2Zz4="
OSINT_BOT_AVATAR = "data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAxMjggMTI4Ij48ZGVmcz48bGluZWFyR3JhZGllbnQgaWQ9ImciIHgxPSIwIiB5MT0iMCIgeDI9IjEiIHkyPSIxIj48c3RvcCBvZmZzZXQ9IjAiIHN0b3AtY29sb3I9IiMzZDhiZmQiLz48c3RvcCBvZmZzZXQ9IjEiIHN0b3AtY29sb3I9IiMxYTVmZDAiLz48L2xpbmVhckdyYWRpZW50PjwvZGVmcz48Y2lyY2xlIGN4PSI2NCIgY3k9IjY0IiByPSI2NCIgZmlsbD0idXJsKCNnKSIvPjxjaXJjbGUgY3g9IjU0IiBjeT0iNTQiIHI9IjI4IiBmaWxsPSJub25lIiBzdHJva2U9IiNmZmYiIHN0cm9rZS13aWR0aD0iMTAiLz48cGF0aCBkPSJNNzQgNzRsMjIgMjIiIGZpbGw9Im5vbmUiIHN0cm9rZT0iI2ZmZiIgc3Ryb2tlLXdpZHRoPSIxMCIgc3Ryb2tlLWxpbmVjYXA9InJvdW5kIi8+PC9zdmc+"


def ensure_send_bot():
    """Встроенный бот @send — чеки на звёзды ⭐ (как Send в Telegram)."""
    r = q("SELECT id FROM users WHERE username='send' AND is_bot=1 AND deleted=0", one=True)
    if r:
        return r["id"]
    script = json.dumps({
        "commands": {
            "/start": "⭐ Send — чеки на звёзды GodGram.\n\nКоманды:\n/new <сумма> [число_активаций] — создать чек\n/my — мои чеки\n/help — справка\n\nИли отправь код чека, чтобы получить звёзды.",
            "/help": "Как пользоваться:\n1) /new 100 — чек на 100⭐ (1 человек)\n2) /new 50 5 — чек на 50⭐, до 5 человек\n3) Перешли код друзьям\n4) Они пишут код мне или активируют в кошельке\n\nЧек списывает звёзды с твоего баланса сразу.",
            "/my": "Список чеков смотри в Кошелёк → Чеки ⭐",
        },
        "keywords": {},
        "default": "Отправь /new <сумма> чтобы создать чек, или код чека чтобы получить звёзды. /help — справка."
    }, ensure_ascii=False)
    bid = ex("""INSERT INTO users(username,first_name,name,is_bot,bot_owner,bot_desc,bot_script,avatar,avv,created,last_seen)
                VALUES('send','Send','Send',1,?,?,?,?,1,?,?)""",
             (SERVICE_ID, "Чеки на звёзды ⭐", script, SEND_BOT_AVATAR, time.time(), time.time()))
    ex("UPDATE users SET bot_token=? WHERE id=?", ("%s:%s" % (bid, secrets.token_urlsafe(24)), bid))
    return bid


def _ensure_bot_avatar(username, data_url):
    r = q("SELECT id, avatar FROM users WHERE username=? AND is_bot=1 AND deleted=0", (username,), one=True)
    if r and r["avatar"] != data_url:
        ex("UPDATE users SET avatar=?, avv=avv+1 WHERE id=?", (data_url, r["id"]))


SEND_BOT_ID = ensure_send_bot()
_ensure_bot_avatar("send", SEND_BOT_AVATAR)


def ensure_snos_bot():
    """Секретный бот-сносер @hshajsha — модерация аккаунтов (только для админов)."""
    r = q("SELECT id FROM users WHERE username='hshajsha' AND is_bot=1 AND deleted=0", one=True)
    if r:
        return r["id"]
    script = json.dumps({
        "commands": {
            "/start": "🔒 Секретный бот модерации.\nТолько для администраторов GodGram.\n/help — команды.",
            "/help": "Команды (только админ):\n/snos @user|id — заморозить\n/unsnos @user — разморозить\n/sessions @user — снести все сессии\n/del @user — удалить аккаунт\n/info @user — инфо\n/banchat id — заморозить чат\n/unbanchat id — разморозить чат",
        },
        "keywords": {},
        "default": "🔒 Нет доступа или неизвестная команда. /help"
    }, ensure_ascii=False)
    bid = ex("""INSERT INTO users(username,first_name,name,is_bot,bot_owner,bot_desc,bot_script,created,last_seen)
                VALUES('hshajsha','hshajsha','hshajsha',1,?,?,?,?,?)""",
             (SERVICE_ID, "", script, time.time(), time.time()))
    ex("UPDATE users SET bot_token=? WHERE id=?", ("%s:%s" % (bid, secrets.token_urlsafe(24)), bid))
    return bid


SNOS_BOT_ID = ensure_snos_bot()


def ensure_osint_bot():
    """OSINT-бот @osint — сбор открытых данных по пользователям GodGram."""
    r = q("SELECT id FROM users WHERE username='osint' AND is_bot=1 AND deleted=0", one=True)
    if r:
        return r["id"]
    script = json.dumps({
        "commands": {
            "/start": "🔍 OSINT-бот GodGram\n\nСобираю открытые данные по пользователям платформы.\n\n/user @name — карточка\n/id 123 — по id\n/phone 79… — поиск по номеру\n/help — справка",
            "/help": "Команды:\n/user @username — профиль, метки, рейтинг, NFT\n/id <число> — то же по id\n/phone <цифры> — кто с таким номером (если виден)\n/chat <id> — инфо о чате/канале",
        },
        "keywords": {},
        "default": "Напиши /user @username или /help"
    }, ensure_ascii=False)
    bid = ex("""INSERT INTO users(username,first_name,name,is_bot,bot_owner,bot_desc,bot_script,avatar,avv,created,last_seen)
                VALUES('osint','OSINT','OSINT',1,?,?,?,?,1,?,?)""",
             (SERVICE_ID, "OSINT по пользователям GodGram", script, OSINT_BOT_AVATAR, time.time(), time.time()))
    ex("UPDATE users SET bot_token=? WHERE id=?", ("%s:%s" % (bid, secrets.token_urlsafe(24)), bid))
    return bid


OSINT_BOT_ID = ensure_osint_bot()
_ensure_bot_avatar("osint", OSINT_BOT_AVATAR)
# миграция: старая база с «Telegram» -> GodGram
conn.execute("UPDATE users SET username='godgram', first_name='GodGram', name='GodGram' WHERE is_service=1 AND username='telegram'")
conn.execute("UPDATE badge_defs SET title='Подтверждено GodGram', descr='Этот аккаунт подтверждён как официальный самим GodGram.' WHERE key='telegram' AND title LIKE '%Telegram%'")
conn.commit()

# коды входа и временные токены регистрации (живут в памяти)
codes = {}
regtok = {}
pwtok = {}  # токены ожидания ввода облачного пароля: {токен: [user_id, истекает, попытки]}


def cloud_hash(pw, salt):
    return hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), bytes.fromhex(salt), 200_000).hex()


def cloud_check(u, pw):
    if not u["cloud_hash"]:
        return True
    return hmac.compare_digest(cloud_hash(pw, u["cloud_salt"]), u["cloud_hash"])


def check_cloud_pw(pw):
    if not isinstance(pw, str) or len(pw) < 4 or len(pw) > 64:
        raise ApiError(400, "Пароль: от 4 до 64 символов")
    return pw


def norm_phone(s):
    d = re.sub(r"\D", "", str(s or ""))
    # анонимные номера +888 бывают короткими (888 + 3 цифры), поэтому для них минимум 6 цифр
    if not (7 <= len(d) <= 15 or (d.startswith("888") and 6 <= len(d) <= 15)):
        raise ApiError(400, "Неверный номер телефона")
    return d


def find_user_by_phone(d):
    u = q("SELECT * FROM users WHERE phone=? AND deleted=0 AND is_service=0", (d,), one=True)
    if u:
        return u
    return q("""SELECT u.* FROM nft_numbers n JOIN users u ON u.id=n.owner_id
                WHERE n.number=? AND u.deleted=0""", (d,), one=True)


def premium_active(u):
    return (u["premium_until"] or 0) > time.time()


def user_handles(u):
    """Упорядоченный список ВКЛЮЧЁННЫХ юзернеймов человека: обычный + активные NFT. Первый - основной."""
    valid, meta = {}, {}
    if u["username"]:
        valid[u["username"]] = False
    for r in q("SELECT handle,price,issued FROM nft_usernames WHERE owner_id=? AND active=1 ORDER BY id", (u["id"],)):
        valid[r["handle"]] = True
        meta[r["handle"]] = {"price": r["price"] or 0, "issued": r["issued"] or 0}
    try:
        order = json.loads(u["uorder"] or "[]")
    except Exception:
        order = []
    res, seen = [], set()
    for x in order:
        if x in valid and x not in seen:
            res.append(x)
            seen.add(x)
    for x in valid:
        if x not in seen:
            res.append(x)
    try:
        umain = json.loads(u["umain"] or "[]")
    except Exception:
        umain = []
    mains = [x for x in res if x in umain] or (res[:1])
    res = [x for x in res if x in mains] + [x for x in res if x not in mains]  # основные сверху
    return [dict({"handle": x, "nft": valid[x], "main": x in mains}, **meta.get(x, {})) for x in res]


def pub(u, viewer=None, handles=True):
    """Как пользователь выглядит для других. Удалённый - всегда одинаковый."""
    if u is None or u["deleted"]:
        return {
            "id": u["id"] if u is not None else 0,
            "deleted": True, "service": False, "name": "Удалённый аккаунт", "first_name": "", "last_name": "",
            "username": None, "usernames": [], "nft_username": False, "nft_number": False, "own_username": None,
            "real_phone": "", "avatar_url": "", "bio": "", "phone": "", "online": False, "last_seen": 0,
            "is_admin": False, "is_bot": False, "frozen": False, "bot_desc": "", "balance": 0, "rating": 0, "premium": False, "premium_until": 0, "emoji_status": "", "banner": None,
            "badges": [], "birthday": "", "address": "", "work_from": "", "work_to": "", "status_text": "", "pchannel": None, "status_nft": None,
        }
    mine = viewer is not None and (viewer["id"] == u["id"] or viewer["is_admin"])
    if u["nft_number"]:
        phone = u["nft_number"]
    elif mine:
        phone = u["phone"] or ""
    else:
        phone = ""
    if handles:
        hs = user_handles(u)
    else:
        hs = [{"handle": u["username"], "nft": False}] if u["username"] else []
    prem = premium_active(u)
    bc = u["banner_color"] if u["banner_color"] is not None else -1
    return {
        "id": u["id"], "deleted": False, "service": bool(u["is_service"]),
        "name": u["name"], "first_name": u["first_name"], "last_name": u["last_name"],
        "username": hs[0]["handle"] if hs else None, "usernames": hs,
        "nft_username": bool(hs and hs[0]["nft"]), "nft_number": bool(u["nft_number"]), "phone": phone,
        "own_username": u["username"] if mine else None, "real_phone": (u["phone"] or "") if mine else "",
        "avatar_url": f"/av/u/{u['id']}?v={u['avv']}" if u["avatar"] else "",
        "bio": u["bio"], "online": time.time() - (u["last_seen"] or 0) < 45,
        "last_seen": u["last_seen"] or 0, "is_admin": bool(u["is_admin"]),
        "is_bot": bool(u["is_bot"]), "frozen": bool(u["frozen"]), "bot_desc": u["bot_desc"] or "",
        "balance": (u["balance"] or 0) if mine else 0,
        "rating": int(u["rating"] or 0) if "rating" in u.keys() else 0,
        "premium": prem, "premium_until": (u["premium_until"] if prem else 0), "boost_inf": bool(u["boost_inf"]) if mine else False,
        "emoji_status": (u["emoji_status"] or "") if prem else "",
        "banner": ({"color": bc, "pattern": u["banner_pattern"] or ""} if prem and (bc >= 0 or u["banner_pattern"]) else None),
        "badges": user_badges(u), "birthday": u["birthday"] or "", "address": u["address"] or "",
        "work_from": u["work_from"] or "", "work_to": u["work_to"] or "", "status_text": u["status_text"] or "",
        "pchannel": pchannel_view(u), "status_nft": status_nft_view(u),
        "has_cloud_pw": bool(u["cloud_hash"]) if viewer is not None and viewer["id"] == u["id"] else False,
    }


def auth(headers):
    h = headers.get("Authorization", "")
    token = h[7:] if h.startswith("Bearer ") else ""
    if not token:
        raise ApiError(401, "Нужен вход")
    u = q("SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=? AND u.deleted=0",
          (token,), one=True)
    if not u:
        raise ApiError(401, "Нужен вход")
    return u


def touch(uid):
    ex("UPDATE users SET last_seen=? WHERE id=?", (time.time(), uid))


def new_session(uid):
    t = secrets.token_urlsafe(32)
    ex("INSERT INTO sessions(token,user_id) VALUES(?,?)", (t, uid))
    return t


def check_avatar(s):
    if not isinstance(s, str) or not re.match(r"^data:image/(jpeg|jpg|png|webp|gif);base64,", s, re.I):
        raise ApiError(400, "Неверная картинка (нужен JPG/PNG/WebP/GIF)")
    if len(s) > 3_500_000:
        raise ApiError(400, "Картинка слишком большая (макс. ~2.5 МБ)")
    return s


def check_status_media(s):
    if not isinstance(s, str) or not re.match(r"^data:image/(jpeg|png|webp|gif);base64,", s):
        raise ApiError(400, "Неверный файл статуса")
    if len(s) > 700_000:
        raise ApiError(400, "Файл слишком большой (макс. ~500 КБ)")
    return s


def check_banner_media(s):
    """Фото для баннера профиля (чуть больше лимит)."""
    if not isinstance(s, str) or not re.match(r"^data:image/(jpeg|png|webp|gif);base64,", s):
        raise ApiError(400, "Неверный файл баннера")
    if len(s) > 1_200_000:
        raise ApiError(400, "Фото баннера слишком большое (макс. ~900 КБ)")
    return s


def check_username(un, uid=0):
    if not re.fullmatch(r"[a-z][a-z0-9_]{4,31}", un):
        raise ApiError(400, "5-32 символа: a-z, 0-9 и _, начинается с буквы")
    if un in ("telegram", "godgram"):
        raise ApiError(409, "Это имя занято")
    if q("SELECT 1 FROM nft_usernames WHERE handle=?", (un,), one=True):
        raise ApiError(409, "Это имя занято (NFT-юзернейм)")
    if q("SELECT 1 FROM users WHERE username=? AND id!=?", (un, uid), one=True):
        raise ApiError(409, "Это имя занято")


def is_member(chat_id, uid):
    return q("SELECT 1 FROM members WHERE chat_id=? AND user_id=?", (chat_id, uid), one=True) is not None


def peer_of(chat_id, uid):
    return q("SELECT u.* FROM members m JOIN users u ON u.id=m.user_id WHERE m.chat_id=? AND m.user_id!=?",
             (chat_id, uid), one=True)


def get_private_chat(a, b):
    row = q("""SELECT c.id FROM chats c
               JOIN members x ON x.chat_id=c.id AND x.user_id=?
               JOIN members y ON y.chat_id=c.id AND y.user_id=?
               WHERE c.kind='private'""", (a, b), one=True)
    return row["id"] if row else None


def make_private_chat(a, b):
    cid = ex("INSERT INTO chats(kind,owner,created) VALUES('private',?,?)", (a, time.time()))
    ex("INSERT INTO members(chat_id,user_id) VALUES(?,?)", (cid, a))
    if a != b:
        ex("INSERT INTO members(chat_id,user_id) VALUES(?,?)", (cid, b))
    return cid


def get_or_create_saved(uid):
    """Избранное (Saved Messages) — чат с самим собой, как в Telegram."""
    row = q("""SELECT c.id FROM chats c JOIN members m ON m.chat_id=c.id
               WHERE c.kind='saved' AND c.deleted=0 AND m.user_id=?""", (uid,), one=True)
    if row:
        return row["id"]
    cid = ex("INSERT INTO chats(kind,title,owner,created) VALUES('saved','Избранное',?,?)", (uid, time.time()))
    ex("INSERT INTO members(chat_id,user_id) VALUES(?,?)", (cid, uid))
    return cid


def deliver_code(user, code):
    """Код входа приходит в служебный чат "GodGram" - как в настоящем GodGramе."""
    cid = get_private_chat(SERVICE_ID, user["id"]) or make_private_chat(SERVICE_ID, user["id"])
    ex("INSERT INTO messages(chat_id,user_id,text,ts) VALUES(?,?,?,?)",
       (cid, SERVICE_ID, f"Код для входа: {code}\n\nНе давайте этот код никому, даже если его требуют от имени GodGram!",
        time.time()))
    bump()


def chats_for(me):
    rows = q("SELECT c.*, m.last_read FROM chats c JOIN members m ON m.chat_id=c.id WHERE m.user_id=? AND c.deleted=0",
             (me["id"],))
    pinned = {r["chat_id"]: r["ts"] for r in q("SELECT chat_id, ts FROM pins WHERE user_id=?", (me["id"],))}
    out = []
    for c in rows:
        last = q("SELECT id,user_id,text,ts FROM messages WHERE chat_id=? ORDER BY id DESC LIMIT 1",
                 (c["id"],), one=True)
        unread = q("SELECT COUNT(*) n FROM messages WHERE chat_id=? AND id>? AND user_id!=?",
                   (c["id"], c["last_read"], me["id"]), one=True)["n"]
        item = {"id": c["id"], "kind": c["kind"], "unread": unread, "last": None, "created": c["created"],
                "pinned": c["id"] in pinned, "badges": [], "handle": None}
        if c["kind"] == "saved":
            item.update(title="Избранное", avatar_url="", deleted=False, frozen=False, can_post=True,
                        peer={"id": me["id"], "name": "Избранное", "service": False, "deleted": False},
                        saved=True)
        elif c["kind"] == "private":
            p = peer_of(c["id"], me["id"])
            if p is None:
                continue
            p = pub(p, me, handles=False)
            item.update(peer=p, title=p["name"], avatar_url=p["avatar_url"], deleted=p["deleted"], frozen=p["frozen"],
                        can_post=not p["frozen"])
        else:
            cnt = q("SELECT COUNT(*) n FROM members WHERE chat_id=?", (c["id"],), one=True)["n"]
            item.update(title=c["title"], members=cnt, deleted=False, frozen=bool(c["frozen"]), owner=c["owner"],
                        can_post=(not c["frozen"]) and (c["kind"] != "channel" or c["owner"] == me["id"]),
                        avatar_url=f"/av/c/{c['id']}?v={c['avv']}" if c["avatar"] else "",
                        badges=badges_for("chat", c["id"]), handle=c["handle"], sign=bool(c["sign"]),
                        level=level_of(chat_boosts(c["id"])))
        if last:
            su = q("SELECT * FROM users WHERE id=?", (last["user_id"],), one=True)
            item["last"] = {"id": last["id"], "text": last["text"], "ts": last["ts"],
                            "mine": last["user_id"] == me["id"], "from": pub(su, handles=False)["name"]}
        out.append(item)
    out.sort(key=lambda x: x["last"]["ts"] if x["last"] else x["created"], reverse=True)
    out.sort(key=lambda x: (0 if x["pinned"] else 1, -(pinned.get(x["id"], 0))) if x["pinned"] else (1, 0))
    return out


# ---------- NFT ----------
NFT_KINDS = {
    "username": ("nft_usernames", "handle"),
    "number": ("nft_numbers", "number"),
}


def nft_assign(kind, nid, uid):
    table, col = NFT_KINDS[kind]
    row = q(f"SELECT * FROM {table} WHERE id=?", (nid,), one=True)
    if not row:
        raise ApiError(404, "NFT не найден")
    target = None
    if uid:
        target = q("SELECT * FROM users WHERE id=? AND deleted=0 AND is_service=0", (uid,), one=True)
        if not target:
            raise ApiError(404, "Пользователь не найден")
    if kind == "number":
        if row["owner_id"]:
            ex("UPDATE users SET nft_number=NULL WHERE id=? AND nft_number=?", (row["owner_id"], row[col]))
        if target:
            ex("UPDATE nft_numbers SET owner_id=? WHERE id=?", (target["id"], nid))
            if not target["nft_number"]:  # у человека ещё нет активного NFT-номера - включаем этот
                ex("UPDATE users SET nft_number=? WHERE id=?", (row[col], target["id"]))
        else:
            ex("UPDATE nft_numbers SET owner_id=NULL WHERE id=?", (nid,))
    else:
        # юзернейм: выданный сразу включён и встаёт рядом с основным
        if target and row["owner_id"] == target["id"]:
            ex("UPDATE nft_usernames SET active=1 WHERE id=?", (nid,))
        else:
            ex("UPDATE nft_usernames SET owner_id=?, active=1, issued=? WHERE id=?",
               (target["id"] if target else None, time.time() if target else 0, nid))


def nft_set_info(nid, body):
    """Админ задаёт цену (в ⭐) и дату выдачи NFT-юзернейма. Поле не передано - не меняем."""
    if "price" in body:
        try:
            price = int(float(body.get("price") or 0))
        except (TypeError, ValueError):
            raise ApiError(400, "Цена - число")
        if price < 0 or price > 10**12:
            raise ApiError(400, "Цена: от 0")
        ex("UPDATE nft_usernames SET price=? WHERE id=?", (price, nid))
    if "issued" in body:
        try:
            ts = float(body.get("issued") or 0)
        except (TypeError, ValueError):
            raise ApiError(400, "Плохая дата")
        if ts < 0 or ts > FOREVER:
            raise ApiError(400, "Плохая дата")
        ex("UPDATE nft_usernames SET issued=? WHERE id=?", (ts, nid))


def owner_pub(oid, viewer):
    if not oid:
        return None
    u = q("SELECT * FROM users WHERE id=?", (oid,), one=True)
    return pub(u, viewer, handles=False) if u and not u["deleted"] else None


# ---------- v6: метки / закрепы / каналы / NFT ----------
def badges_for(kind, tid):
    rows = q("""SELECT d.* FROM entity_badges e JOIN badge_defs d ON d.key=e.bkey
                WHERE e.kind=? AND e.tid=? ORDER BY d.sort""", (kind, tid))
    return [{"key": r["key"], "title": r["title"], "descr": r["descr"], "color": r["color"], "kind": r["kind"],
             "icon": r["icon"]} for r in rows]


def user_badges(u):
    """Метки человека. Админ автоматически получает верификацию Owner GodGram."""
    bs = badges_for("user", u["id"])
    if u["is_admin"] and not any(b["key"] == "owner" for b in bs):
        bs = [dict(OWNER_BADGE)] + bs
    return bs


INF_BOOSTS = 1_000_000


def chat_boosts(cid):
    n = q("SELECT COUNT(*) n FROM boosts WHERE chat_id=?", (cid,), one=True)["n"]
    ex_ = q("SELECT boost_extra FROM chats WHERE id=?", (cid,), one=True)
    return n + ((ex_["boost_extra"] or 0) if ex_ else 0)


def status_nft_view(u):
    iid = u["status_nft"] if "status_nft" in u.keys() else 0
    if not iid:
        return None
    r = q("SELECT * FROM nft_items WHERE id=? AND owner_id=?", (iid, u["id"]), one=True)
    if not r:
        return None
    m, bd, sy = r["model"].split("|"), r["backdrop"].split("|"), r["symbol"].split("|")
    return {"id": r["id"], "glyph": m[1], "c1": bd[1], "c2": bd[2], "symbol": sy[1], "anim": r["anim"],
            "name": "%s #%d" % ((q("SELECT name FROM nft_colls WHERE id=?", (r["coll_id"],), one=True) or {"name": "?"})["name"], r["number"])}


def level_of(n):
    lv = 0
    for i, need in enumerate(BOOST_LEVELS):
        if n >= need:
            lv = i + 1
    return lv


def chat_av(c):
    return f"/av/c/{c['id']}?v={c['avv']}" if c["avatar"] else ""


def pchannel_view(u):
    cid = u["pchannel"] if "pchannel" in u.keys() else 0
    if not cid:
        return None
    c = q("SELECT * FROM chats WHERE id=? AND kind='channel' AND deleted=0", (cid,), one=True)
    if not c:
        return None
    n = q("SELECT COUNT(*) n FROM members WHERE chat_id=?", (cid,), one=True)["n"]
    return {"id": cid, "title": c["title"], "members": n, "avatar_url": chat_av(c), "badges": badges_for("chat", cid)}


def channel_view(c, me, full=False):
    cnt = q("SELECT COUNT(*) n FROM members WHERE chat_id=?", (c["id"],), one=True)["n"]
    nb = chat_boosts(c["id"])
    lv = level_of(nb)
    d = {"id": c["id"], "kind": c["kind"], "title": c["title"], "members": cnt, "frozen": bool(c["frozen"]),
         "handle": c["handle"], "descr": c["descr"] or "", "avatar_url": chat_av(c), "owner": c["owner"],
         "joined": is_member(c["id"], me["id"]), "boosts": nb, "level": lv, "badges": badges_for("chat", c["id"]),
         "banner": ({"color": c["bcolor"], "pattern": c["bpattern"] or ""} if lv >= 1 else None),
         "estatus": (c["estatus"] or "") if lv >= 3 else "", "sign": bool(c["sign"]), "pub": bool(c["pub"]),
         "discussion_chat_id": int(c["discussion_chat_id"] or 0)}
    if full:
        mine = q("SELECT 1 FROM boosts WHERE chat_id=? AND user_id=?", (c["id"], me["id"]), one=True) is not None
        used = q("SELECT COUNT(*) n FROM boosts WHERE user_id=?", (me["id"],), one=True)["n"]
        d.update(boosted_by_me=mine, boosts_inf=nb >= INF_BOOSTS, can_boost=bool(premium_active(me) or me["boost_inf"]),
                 slots_left=(999 if me["boost_inf"] else (BOOST_SLOTS - used)) if (premium_active(me) or me["boost_inf"]) else 0,
                 next_need=next((x for x in BOOST_LEVELS if x > nb), None), levels=BOOST_LEVELS,
                 boosters=[pub(q("SELECT * FROM users WHERE id=?", (r["user_id"],), one=True), me, handles=False)
                           for r in q("SELECT user_id FROM boosts WHERE chat_id=? ORDER BY ts DESC LIMIT 20", (c["id"],))])
    return d


def check_chat_handle(hd, cid=0):
    if not re.fullmatch(r"[a-z][a-z0-9_]{4,31}", hd):
        raise ApiError(400, "Ссылка: 5-32 символа, a-z, 0-9 и _, начинается с буквы")
    if hd in ("telegram", "godgram") or q("SELECT 1 FROM users WHERE username=?", (hd,), one=True) \
            or q("SELECT 1 FROM nft_usernames WHERE handle=?", (hd,), one=True) \
            or q("SELECT 1 FROM chats WHERE handle=? AND id!=?", (hd, cid), one=True):
        raise ApiError(409, "Эта ссылка занята")


def wipe_chat(cid):
    ex("DELETE FROM media WHERE key IN (SELECT media_key FROM messages WHERE chat_id=? AND media_key!='')", (cid,))
    for t in ("members", "messages", "pins", "boosts"):
        ex(f"DELETE FROM {t} WHERE chat_id=?", (cid,))
    ex("DELETE FROM entity_badges WHERE kind='chat' AND tid=?", (cid,))
    ex("UPDATE users SET pchannel=0 WHERE pchannel=?", (cid,))
    ex("UPDATE chats SET deleted=1, handle=NULL, title='Удалённый канал', avatar='', avv=avv+1, descr='' WHERE id=?", (cid,))


def pick_weighted(items, idx):
    tot = sum(x[idx] for x in items)
    r = secrets.randbelow(max(1, tot))
    for x in items:
        r -= x[idx]
        if r < 0:
            return x
    return items[-1]


def mint_items(coll_id, count, price, owner=0, listed=1, from_id=0):
    """Выпускает NFT-подарки коллекции со случайными моделью, фоном и узором (редкость - в промилле)."""
    with lock:
        c = conn.execute("SELECT * FROM nft_colls WHERE id=?", (coll_id,)).fetchone()
        if not c:
            raise ApiError(404, "Коллекция не найдена")
        models = json.loads(c["models"] or "[]")
        left = (c["supply"] - c["minted"]) if c["supply"] else count
        count = min(count, left)
        if count <= 0:
            raise ApiError(409, "Тираж коллекции исчерпан")
        ids = []
        minted = c["minted"]
        for _ in range(count):
            m = pick_weighted([tuple(x) for x in models], 2)
            bd = pick_weighted(NFT_BACKDROPS, 3)
            sy = pick_weighted(NFT_SYMBOLS, 2)
            minted += 1
            n = minted
            conn.execute("UPDATE nft_colls SET minted=? WHERE id=?", (n, coll_id))
            cur = conn.execute(
                """INSERT INTO nft_items(coll_id,number,owner_id,model,backdrop,symbol,anim,price,listed,from_id,ts)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (coll_id, n, owner, "%s|%s|%s" % (m[0], m[1], m[2]), "%s|%s|%s|%s" % bd, "%s|%s|%s" % sy,
                 "hug" if c["name"] in HUG_COLLS else secrets.choice(NFT_ANIMS), price if listed else 0, 1 if listed else 0, from_id, time.time()))
            ids.append(cur.lastrowid)
        conn.commit()
        return ids


def nft_item_obj(r):
    c = q("SELECT * FROM nft_colls WHERE id=?", (r["coll_id"],), one=True)
    m, bd, sy = r["model"].split("|"), r["backdrop"].split("|"), r["symbol"].split("|")
    own = None
    if r["owner_id"]:
        ou = q("SELECT * FROM users WHERE id=?", (r["owner_id"],), one=True)
        own = pub(ou, handles=False) if ou else None
        if own:
            own = {"id": own["id"], "name": own["name"], "deleted": own["deleted"], "avatar_url": own["avatar_url"]}
    return {"id": r["id"], "coll_id": r["coll_id"], "coll": c["name"] if c else "?", "supply": c["supply"] if c else 0,
            "number": r["number"], "glyph": m[1], "model": {"name": m[0], "glyph": m[1], "rar": int(m[2])},
            "backdrop": {"name": bd[0], "c1": bd[1], "c2": bd[2], "rar": int(bd[3])},
            "symbol": {"name": sy[0], "glyph": sy[1], "rar": int(sy[2])}, "anim": r["anim"],
            "price": r["price"], "listed": bool(r["listed"]), "owner": own, "ts": r["ts"],
            "name": "%s #%d" % (c["name"] if c else "?", r["number"]), "nft": True}


def nft_items_of(uid):
    return [nft_item_obj(r) for r in q("SELECT * FROM nft_items WHERE owner_id=? ORDER BY id DESC LIMIT 100", (uid,))]


def seed_nft():
    if q("SELECT 1 FROM nft_colls", one=True):
        return
    for nm, gl, sup, pr, models in NFT_COLL_SEED:
        cid = ex("INSERT INTO nft_colls(name,glyph,supply,price,models,created) VALUES(?,?,?,?,?,?)",
                 (nm, gl, sup, pr, json.dumps(models, ensure_ascii=False), time.time()))
        mint_items(cid, 6, pr)


seed_nft()


def seed_nft2():
    if q("SELECT 1 FROM nft_colls WHERE name=?", (NFT_COLL_SEED2[0][0],), one=True):
        return
    for nm, gl, sup, pr, models in NFT_COLL_SEED2:
        cid = ex("INSERT INTO nft_colls(name,glyph,supply,price,models,created) VALUES(?,?,?,?,?,?)",
                 (nm, gl, sup, pr, json.dumps(models, ensure_ascii=False), time.time()))
        mint_items(cid, 6, pr)


seed_nft2()


def seed_nft3():
    if q("SELECT 1 FROM nft_colls WHERE name=?", (NFT_COLL_SEED3[0][0],), one=True):
        return
    for nm, gl, sup, pr, models in NFT_COLL_SEED3:
        cid = ex("INSERT INTO nft_colls(name,glyph,supply,price,models,created) VALUES(?,?,?,?,?,?)",
                 (nm, gl, sup, pr, json.dumps(models, ensure_ascii=False), time.time()))
        mint_items(cid, 6, pr)


seed_nft3()


# ---------- v5: помощники ----------
GIFT_MODELS = ("box", "extrude")
GIFT_ANIMS = ("spin", "float", "bounce", "pulse", "hug")


def post_message(cid, uid, text, gift_id=0):
    return ex("INSERT INTO messages(chat_id,user_id,text,ts,gift_id) VALUES(?,?,?,?,?)",
              (cid, uid, text, time.time(), gift_id))


def service_msg(uid, text, gift_id=0):
    cid = get_private_chat(SERVICE_ID, uid) or make_private_chat(SERVICE_ID, uid)
    post_message(cid, SERVICE_ID, text, gift_id)


def balance_of(uid):
    r = q("SELECT balance FROM users WHERE id=?", (uid,), one=True)
    return (r["balance"] or 0) if r else 0


def gen_check_code():
    # короткий код как у Telegram-чеков
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "EQ" + "".join(secrets.choice(alphabet) for _ in range(10))


def make_star_check(creator_id, amount, max_uses=1, note="", expires=0):
    amount = int(amount)
    max_uses = int(max_uses)
    if not 1 <= amount <= 1_000_000:
        raise ApiError(400, "Сумма чека: от 1 до 1 000 000 ⭐")
    if not 1 <= max_uses <= 1000:
        raise ApiError(400, "Активаций: от 1 до 1000")
    total = amount * max_uses
    with lock:
        b = conn.execute("SELECT balance FROM users WHERE id=?", (creator_id,)).fetchone()
        bal = (b["balance"] or 0) if b else 0
        if bal < total:
            raise ApiError(402, "Нужно %d ⭐ (сумма × активации), у тебя %d" % (total, bal))
        code = gen_check_code()
        for _ in range(8):
            if not conn.execute("SELECT 1 FROM star_checks WHERE code=?", (code,)).fetchone():
                break
            code = gen_check_code()
        now = time.time()
        conn.execute("UPDATE users SET balance=balance-? WHERE id=?", (total, creator_id))
        conn.execute("INSERT INTO tx(user_id,amount,kind,note,ts) VALUES(?,?,?,?,?)",
                     (creator_id, -total, "check_create", "Чек %s · %d×%d ⭐" % (code, amount, max_uses), now))
        cur = conn.execute(
            """INSERT INTO star_checks(code,creator_id,amount,max_uses,used,note,active,created,expires)
               VALUES(?,?,?,?,0,?,1,?,?)""",
            (code, creator_id, amount, max_uses, (note or "")[:120], now, float(expires or 0)))
        conn.commit()
        return {"id": cur.lastrowid, "code": code, "amount": amount, "max_uses": max_uses,
                "used": 0, "total": total, "note": note or "", "expires": expires or 0}


def claim_star_check(code, user_id):
    code = str(code or "").strip().upper().replace(" ", "")
    if not code:
        raise ApiError(400, "Введи код чека")
    with lock:
        r = conn.execute("SELECT * FROM star_checks WHERE code=?", (code,)).fetchone()
        if not r or not r["active"]:
            raise ApiError(404, "Чек не найден или уже недействителен")
        if r["expires"] and r["expires"] < time.time():
            raise ApiError(400, "Срок действия чека истёк")
        if r["used"] >= r["max_uses"]:
            raise ApiError(409, "Чек уже полностью активирован")
        if r["creator_id"] == user_id:
            raise ApiError(400, "Нельзя активировать свой чек")
        if conn.execute("SELECT 1 FROM star_check_claims WHERE check_id=? AND user_id=?",
                        (r["id"], user_id)).fetchone():
            raise ApiError(409, "Ты уже активировал этот чек")
        now = time.time()
        conn.execute("UPDATE star_checks SET used=used+1 WHERE id=?", (r["id"],))
        if r["used"] + 1 >= r["max_uses"]:
            conn.execute("UPDATE star_checks SET active=0 WHERE id=?", (r["id"],))
        conn.execute("INSERT INTO star_check_claims(check_id,user_id,ts) VALUES(?,?,?)",
                     (r["id"], user_id, now))
        conn.execute("UPDATE users SET balance=balance+? WHERE id=?", (r["amount"], user_id))
        conn.execute("INSERT INTO tx(user_id,amount,kind,note,ts) VALUES(?,?,?,?,?)",
                     (user_id, r["amount"], "check_claim", "Чек %s" % code, now))
        conn.commit()
        return {"amount": r["amount"], "code": code, "left": r["max_uses"] - r["used"] - 1}


def resolve_user_ref(ref):
    """Найти пользователя по @username, id, телефону или имени."""
    ref = str(ref or "").strip()
    if not ref:
        return None
    if ref.startswith("@"):
        ref = ref[1:]
    if ref.isdigit():
        u = q("SELECT * FROM users WHERE id=? AND deleted=0", (int(ref),), one=True)
        if u:
            return u
        # телефон
        phone = re.sub(r"\D", "", ref)
        if len(phone) >= 10:
            u = q("SELECT * FROM users WHERE phone=? AND deleted=0", (phone[-10:] if False else phone,), one=True)
            # try full match
            u = q("SELECT * FROM users WHERE phone=? AND deleted=0", (phone,), one=True)
            if u:
                return u
    un = ref.lower()
    u = q("SELECT * FROM users WHERE lower(username)=? AND deleted=0", (un,), one=True)
    if u:
        return u
    u = q("SELECT * FROM users WHERE lower(nft_username)=? AND deleted=0", (un,), one=True)
    if u:
        return u
    # по имени
    rows = q("SELECT * FROM users WHERE deleted=0 AND (lower(name)=? OR lower(first_name)=?) LIMIT 3",
             (ref.lower(), ref.lower()))
    return rows[0] if len(rows) == 1 else None


def snos_freeze_user(admin, target, freeze=True):
    if target["id"] == admin["id"]:
        raise ApiError(400, "Нельзя применить к себе")
    if target["is_admin"]:
        raise ApiError(403, "Нельзя трогать администратора")
    if target["is_service"]:
        raise ApiError(403, "Служебный аккаунт")
    if target["is_bot"] and target["username"] in ("send", "hshajsha", "godgram"):
        raise ApiError(403, "Системный бот")
    ex("UPDATE users SET frozen=? WHERE id=?", (1 if freeze else 0, target["id"]))
    action = "заморожен ❄️" if freeze else "разморожен"
    service_msg(target["id"], "Администратор: ваш аккаунт %s." % action)
    return action


def snos_delete_user(admin, target):
    if target["id"] == admin["id"]:
        raise ApiError(400, "Нельзя удалить себя")
    if target["is_admin"]:
        raise ApiError(403, "Нельзя удалить администратора")
    if target["is_service"]:
        raise ApiError(403, "Служебный аккаунт")
    # как /api/admin/delete
    ex("DELETE FROM sessions WHERE user_id=?", (target["id"],))
    ex("""UPDATE users SET deleted=1, phone='', username='', nft_username='', nft_number=NULL,
              first_name='', last_name='', name='', bio='', avatar='', avv=avv+1, is_admin=0, last_seen=0,
              frozen=0, balance=0, bot_token='', bot_script='', status_nft=0 WHERE id=?""", (target["id"],))
    service_msg(target["id"], "Ваш аккаунт удалён администратором.")
    return "удалён"


def snos_kill_sessions(admin, target):
    """Снести все сессии пользователя (вылогинить со всех устройств)."""
    if target["id"] == admin["id"]:
        raise ApiError(400, "Нельзя снести свои сессии через бота — выйди сам")
    if target["is_admin"]:
        raise ApiError(403, "Нельзя трогать сессии администратора")
    if target["is_service"]:
        raise ApiError(403, "Служебный аккаунт")
    n = q("SELECT COUNT(*) n FROM sessions WHERE user_id=?", (target["id"],), one=True)["n"]
    ex("DELETE FROM sessions WHERE user_id=?", (target["id"],))
    service_msg(target["id"], "Администратор завершил все ваши сессии. Войдите заново.")
    return n


def osint_report(target, viewer):
    """Открытые данные по пользователю для OSINT-бота."""
    nl = chr(10)
    p = pub(target, viewer)
    lines = []
    lines.append("🔍 OSINT · GodGram")
    lines.append("")
    lines.append("👤 " + (p["name"] or "—"))
    lines.append("id: %d" % target["id"])
    if p.get("usernames"):
        lines.append("usernames: " + ", ".join("@" + x["handle"] + (" 💎" if x.get("nft") else "") for x in p["usernames"]))
    elif target["username"]:
        lines.append("username: @" + target["username"])
    if target["nft_number"]:
        lines.append("номер: +" + target["nft_number"])
    # телефон только если viewer admin или сам
    if viewer and (viewer["is_admin"] or viewer["id"] == target["id"]):
        if target["phone"]:
            lines.append("телефон: +" + target["phone"])
    lines.append("бот: " + ("да" if target["is_bot"] else "нет"))
    lines.append("премиум: " + ("да" if premium_active(target) else "нет"))
    lines.append("заморожен: " + ("да ❄️" if target["frozen"] else "нет"))
    lines.append("рейтинг: %d" % rating_of(target["id"]))
    if target["is_admin"]:
        lines.append("роль: администратор")
    # метки
    bs = user_badges(target)
    if bs:
        lines.append("метки: " + ", ".join(b["title"] for b in bs))
    # даты
    if target["created"]:
        lines.append("создан: " + time.strftime("%d.%m.%Y %H:%M", time.localtime(target["created"])))
    if target["last_seen"]:
        lines.append("last_seen: " + time.strftime("%d.%m.%Y %H:%M", time.localtime(target["last_seen"])))
    if target["bio"]:
        lines.append("bio: " + (target["bio"] or "")[:200])
    # NFT count
    nu = q("SELECT COUNT(*) n FROM nft_usernames WHERE owner_id=?", (target["id"],), one=True)["n"]
    nn = q("SELECT COUNT(*) n FROM nft_numbers WHERE owner_id=?", (target["id"],), one=True)["n"]
    ni = q("SELECT COUNT(*) n FROM nft_items WHERE owner_id=?", (target["id"],), one=True)["n"]
    lines.append("NFT: юзы %d · номера %d · подарки %d" % (nu, nn, ni))
    # чаты count
    mc = q("SELECT COUNT(*) n FROM members WHERE user_id=?", (target["id"],), one=True)["n"]
    lines.append("чатов: %d" % mc)
    # баланс только админу
    if viewer and viewer["is_admin"]:
        lines.append("баланс: %d ⭐" % (target["balance"] or 0))
    return nl.join(lines)


def rating_of(uid):
    r = q("SELECT rating FROM users WHERE id=?", (uid,), one=True)
    return int(r["rating"] or 0) if r else 0


def change_rating(uid, delta, kind, note="", from_id=0, ref_id=0):
    """Меняет рейтинг пользователя (может уходить в минус). Возвращает новый рейтинг."""
    if not delta or not uid:
        return rating_of(uid)
    with lock:
        conn.execute("UPDATE users SET rating=COALESCE(rating,0)+? WHERE id=?", (int(delta), uid))
        conn.execute(
            "INSERT INTO rating_log(user_id,delta,kind,note,from_id,ref_id,ts) VALUES(?,?,?,?,?,?,?)",
            (uid, int(delta), kind, (note or "")[:120], from_id or 0, ref_id or 0, time.time()))
        conn.commit()
        row = conn.execute("SELECT rating FROM users WHERE id=?", (uid,)).fetchone()
        return int(row["rating"] or 0) if row else 0


def gift_rating_points(price):
    """Очки рейтинга за подарок: от цены в ⭐, минимум 1."""
    try:
        p = int(price or 0)
    except (TypeError, ValueError):
        p = 0
    return max(1, p // 5)  # 25⭐ → +5, 100⭐ → +20, 1000⭐ → +200


def gift_obj(ugid):
    r = q("""SELECT ug.id AS ugid, ug.note AS note, ug.ts AS ts, ug.from_id AS from_id, ug.number AS number, ug.owner_id AS owner_id,
                    g.id AS gid, g.name AS name, g.model AS model, g.glyph AS glyph, g.color AS color,
                    g.anim AS anim, g.price AS price, g.supply AS supply
             FROM user_gifts ug JOIN gifts g ON g.id=ug.gift_id WHERE ug.id=?""", (ugid,), one=True)
    if not r:
        return None
    fu = q("SELECT * FROM users WHERE id=?", (r["from_id"],), one=True)
    return {"id": r["ugid"], "gift_id": r["gid"], "name": r["name"], "model": r["model"], "glyph": r["glyph"],
            "color": r["color"], "anim": r["anim"], "price": r["price"], "supply": r["supply"], "number": r["number"],
            "note": r["note"] or "", "ts": r["ts"], "owner_id": r["owner_id"] if "owner_id" in r.keys() else 0,
            "from": {"id": r["from_id"], "name": pub(fu, handles=False)["name"] if fu else "Удалённый аккаунт"}}


def gifts_of(uid):
    return [gift_obj(r["id"]) for r in q("SELECT id FROM user_gifts WHERE owner_id=? ORDER BY id DESC LIMIT 100", (uid,))]


def give_gift(gift_id, owner_id, from_id, note="", pay_from=None):
    """Создаёт подарок. Если pay_from задан - списывает цену со счёта (всё под одним замком)."""
    with lock:
        g = conn.execute("SELECT * FROM gifts WHERE id=? AND active=1", (gift_id,)).fetchone()
        if not g:
            raise ApiError(404, "Подарок не найден")
        if g["supply"] and g["sold"] >= g["supply"]:
            raise ApiError(409, "Эти подарки закончились")
        if pay_from is not None:
            b = conn.execute("SELECT balance FROM users WHERE id=?", (pay_from,)).fetchone()["balance"] or 0
            if b < g["price"]:
                raise ApiError(402, "Недостаточно звёзд ⭐")
            conn.execute("UPDATE users SET balance=balance-? WHERE id=?", (g["price"], pay_from))
            conn.execute("INSERT INTO tx(user_id,amount,kind,note,ts) VALUES(?,?,?,?,?)",
                         (pay_from, -g["price"], "gift", "Подарок «%s»" % g["name"], time.time()))
        n = g["sold"] + 1
        conn.execute("UPDATE gifts SET sold=? WHERE id=?", (n, gift_id))
        cur = conn.execute("INSERT INTO user_gifts(gift_id,owner_id,from_id,note,number,ts) VALUES(?,?,?,?,?,?)",
                           (gift_id, owner_id, from_id, note, n, time.time()))
        conn.commit()
        return cur.lastrowid, g


def clean_script(s):
    if not isinstance(s, dict):
        raise ApiError(400, "Плохой сценарий")
    out = {"commands": {}, "keywords": {}, "default": ""}
    for key, slash in (("commands", True), ("keywords", False)):
        src = s.get(key) or {}
        if not isinstance(src, dict) or len(src) > 50:
            raise ApiError(400, "Слишком много правил (максимум 50)")
        for k, v in src.items():
            k = str(k).strip().lower()[:32]
            v = str(v)[:2000]
            if not k or not v.strip():
                continue
            if slash and not k.startswith("/"):
                k = "/" + k
            out[key][k] = v
    out["default"] = str(s.get("default") or "")[:2000]
    return out


def bot_owner_view(r):
    try:
        sc = json.loads(r["bot_script"] or "{}")
    except Exception:
        sc = {}
    return {"id": r["id"], "name": r["name"], "username": r["username"], "desc": r["bot_desc"] or "",
            "token": r["bot_token"], "frozen": bool(r["frozen"]),
            "script": {"commands": sc.get("commands") or {}, "keywords": sc.get("keywords") or {},
                       "default": sc.get("default") or ""},
            "avatar_url": f"/av/u/{r['id']}?v={r['avv']}" if r["avatar"] else ""}


def new_bot_token(bid):
    return f"{bid}:{secrets.token_urlsafe(24)}"


def script_reply(bot, text, sender):
    """Простой встроенный \"код\" бота: команды, ключевые слова, ответ по умолчанию."""
    try:
        sc = json.loads(bot["bot_script"] or "{}")
    except Exception:
        sc = {}
    low = text.strip().lower()
    rep = None
    if low.startswith("/"):
        cmd = low.split()[0].split("@")[0]
        rep = {k.lower(): v for k, v in (sc.get("commands") or {}).items()}.get(cmd)
        if rep is None and cmd == "/start":
            rep = "Привет, {name}! Я бот {bot}."
    else:
        for k, v in (sc.get("keywords") or {}).items():
            if k and k.lower() in low:
                rep = v
                break
        if rep is None:
            rep = sc.get("default") or None
    if not rep:
        return None
    nm = sender["first_name"] or sender["name"] or ""
    return rep.replace("{name}", nm).replace("{bot}", bot["name"] or "").replace("{time}", time.strftime("%H:%M"))[:4000]


def dispatch_bots(cid, sender, text, kind):
    """Все незамороженные боты чата получают обновление; встроенный сценарий отвечает сразу."""
    bots = q("""SELECT u.* FROM members m JOIN users u ON u.id=m.user_id
                WHERE m.chat_id=? AND u.is_bot=1 AND u.deleted=0 AND u.id!=?""", (cid, sender["id"]))
    for b in bots:
        if b["frozen"]:
            continue
        ex("INSERT INTO bot_updates(bot_id,chat_id,from_id,text,ts) VALUES(?,?,?,?,?)",
           (b["id"], cid, sender["id"], text, time.time()))
        ex("DELETE FROM bot_updates WHERE bot_id=? AND ts<?", (b["id"], time.time() - 3 * 86400))
        if kind == "private" or text.strip().startswith("/"):
            r = script_reply(b, text, sender)
            if r:
                post_message(cid, b["id"], r)


def bot_api(token, method, params):
    """HTTP Bot API в стиле Telegram: /bot<токен>/getMe | getUpdates | sendMessage"""
    b = q("SELECT * FROM users WHERE bot_token=? AND is_bot=1 AND deleted=0", (token,), one=True)
    if not b:
        raise ApiError(401, "Unauthorized")
    if b["frozen"]:
        raise ApiError(403, "Бот заморожен администратором")
    ex("UPDATE users SET last_seen=? WHERE id=?", (time.time(), b["id"]))
    m = method.lower()
    if m == "getme":
        return {"ok": True, "result": {"id": b["id"], "is_bot": True, "first_name": b["name"], "username": b["username"]}}
    if m == "getupdates":
        off = int(params.get("offset", 0) or 0)
        lim = max(1, min(100, int(params.get("limit", 100) or 100)))
        tmo = max(0, min(25, int(params.get("timeout", 0) or 0)))
        if off:
            ex("DELETE FROM bot_updates WHERE bot_id=? AND id<?", (b["id"], off))

        def fetch():
            return q("SELECT * FROM bot_updates WHERE bot_id=? AND id>=? ORDER BY id LIMIT ?", (b["id"], off, lim))
        rows = fetch()
        if not rows and tmo:
            with cond:
                v0 = version
                cond.wait_for(lambda: version != v0, timeout=tmo)
            rows = fetch()
        res = []
        for r in rows:
            fu = q("SELECT * FROM users WHERE id=?", (r["from_id"],), one=True)
            ch = q("SELECT kind FROM chats WHERE id=?", (r["chat_id"],), one=True)
            res.append({"update_id": r["id"], "message": {
                "message_id": r["id"], "date": int(r["ts"]), "text": r["text"],
                "from": {"id": r["from_id"], "is_bot": False, "first_name": fu["first_name"] if fu else "",
                         "username": fu["username"] if fu else None},
                "chat": {"id": r["chat_id"], "type": ch["kind"] if ch else "private"}}})
        return {"ok": True, "result": res}
    if m == "sendmessage":
        cid = int(params.get("chat_id", 0) or 0)
        text = str(params.get("text", "")).strip()[:4000]
        if not text:
            raise ApiError(400, "text is empty")
        if not is_member(cid, b["id"]):
            raise ApiError(403, "Бот не состоит в этом чате")
        c = q("SELECT kind, frozen FROM chats WHERE id=?", (cid,), one=True)
        if c["frozen"]:
            raise ApiError(403, "Чат заморожен")
        if c["kind"] == "private":
            p = peer_of(cid, b["id"])
            if p is None or p["deleted"]:
                raise ApiError(403, "Аккаунт собеседника удалён")
        mid = post_message(cid, b["id"], text)
        bump()
        return {"ok": True, "result": {"message_id": mid, "chat": {"id": cid}, "date": int(time.time()), "text": text}}
    raise ApiError(404, "Unknown method")


def store_media(data_url, allowed, limit, owner):
    s = str(data_url or "")
    m = re.match(r"^data:([a-zA-Z0-9]+/[a-zA-Z0-9.+\-]+)((?:;[^;,]+)*?);base64,", s, re.I)
    if not m:
        raise ApiError(400, "Неподдерживаемый файл")
    mime = m.group(1).lower()
    if mime not in allowed:
        raise ApiError(400, "Неподдерживаемый тип: " + mime)
    try:
        raw = base64.b64decode(s[m.end():], validate=False)
    except Exception:
        raise ApiError(400, "Файл повреждён")
    if not raw:
        raise ApiError(400, "Пустой файл")
    if len(raw) > limit:
        raise ApiError(413, "Файл слишком большой")
    key = secrets.token_urlsafe(18)
    ex("INSERT INTO media(key,mime,data,owner,ts) VALUES(?,?,?,?,?)", (key, mime, raw, owner, time.time()))
    return key


def store_media_any(data_url, limit, owner):
    s = str(data_url or "")
    m = re.match(r"^data:([a-zA-Z0-9]+/[a-zA-Z0-9.+\-]+)((?:;[^;,]+)*?);base64,", s, re.I)
    if not m:
        raise ApiError(400, "Неверный файл")
    mime = m.group(1).lower()
    try:
        raw = base64.b64decode(s[m.end():], validate=False)
    except Exception:
        raise ApiError(400, "Файл повреждён")
    if not raw:
        raise ApiError(400, "Пустой файл")
    if len(raw) > limit:
        raise ApiError(413, "Файл слишком большой")
    key = secrets.token_urlsafe(18)
    ex("INSERT INTO media(key,mime,data,owner,ts) VALUES(?,?,?,?,?)", (key, mime, raw, owner, time.time()))
    return key


def can_post(cid, me):
    if not is_member(cid, me["id"]):
        raise ApiError(403, "Нет доступа")
    c = q("SELECT kind, frozen, owner FROM chats WHERE id=?", (cid,), one=True)
    if c["frozen"]:
        raise ApiError(403, "Чат заморожен администратором")
    if c["kind"] == "channel" and c["owner"] != me["id"]:
        raise ApiError(403, "В канале пишет только владелец")
    if c["kind"] == "private":
        p = peer_of(cid, me["id"])
        if p is None or p["deleted"]:
            raise ApiError(403, "Аккаунт собеседника удалён")
        if p["is_service"]:
            raise ApiError(403, "В служебный чат писать нельзя")
        if p["frozen"]:
            raise ApiError(403, "Бот заморожен администратором")
    return c


def purge_stories():
    now = time.time()
    rows = q("SELECT id, media_key FROM stories WHERE expires<=?", (now,))
    for r in rows:
        if r["media_key"]:
            ex("DELETE FROM media WHERE key=?", (r["media_key"],))
        ex("DELETE FROM story_views WHERE story_id=?", (r["id"],))
        ex("DELETE FROM stories WHERE id=?", (r["id"],))


def story_obj(s, me, owner_view):
    seen = q("SELECT 1 FROM story_views WHERE story_id=? AND user_id=?", (s["id"], me["id"]), one=True) is not None
    d = {"id": s["id"], "kind": s["kind"], "media_url": ("/media/" + s["media_key"]) if s["media_key"] else "",
         "text": s["text"] or "", "bg": s["bg"] or 0, "ts": s["ts"], "expires": s["expires"], "seen": seen, "views": 0}
    if owner_view:
        d["views"] = q("SELECT COUNT(*) n FROM story_views WHERE story_id=?", (s["id"],), one=True)["n"]
    return d


def api(method, path, qs, body, headers):
    # ---------- вход как в Telegram: номер -> код -> имя ----------
    if path == "/api/auth/send_code" and method == "POST":
        d = norm_phone(body.get("phone"))
        if d.startswith("888") and not NFT_LOGIN:
            raise ApiError(403, MAIN_ONLY_MSG)
        u = find_user_by_phone(d)
        if not u and d.startswith("888"):
            raise ApiError(403, "Анонимные номера +888 выдаёт только администратор")
        now = time.time()
        old = codes.get(d)
        if old and now - old["sent"] < 2:
            raise ApiError(429, "Подожди пару секунд")
        code = "".join(secrets.choice("0123456789") for _ in range(5))
        codes[d] = {"code": code, "exp": now + 300, "tries": 0, "sent": now}
        print(f"[КОД ВХОДА] +{d}: {code}", flush=True)
        res = {"ok": True, "registered": bool(u), "length": 5}
        if u:
            deliver_code(u, code)
        elif SHOW_CODE:
            res["code"] = code
        return res

    if path == "/api/auth/sign_in" and method == "POST":
        d = norm_phone(body.get("phone"))
        if d.startswith("888") and not NFT_LOGIN:
            raise ApiError(403, MAIN_ONLY_MSG)
        code = str(body.get("code", "")).strip()
        c = codes.get(d)
        if not c or c["exp"] < time.time():
            raise ApiError(400, "Код устарел. Запроси новый")
        c["tries"] += 1
        if c["tries"] > 5:
            codes.pop(d, None)
            raise ApiError(429, "Слишком много попыток. Запроси новый код")
        if not secrets.compare_digest(c["code"], code):
            raise ApiError(400, "Неверный код")
        codes.pop(d, None)
        u = find_user_by_phone(d)
        if u:
            if u["cloud_hash"]:
                t = secrets.token_urlsafe(16)
                pwtok[t] = [u["id"], time.time() + 600, 0]
                return {"registered": True, "need_password": True, "pw_token": t, "hint": u["cloud_hint"] or ""}
            return {"registered": True, "token": new_session(u["id"])}
        t = secrets.token_urlsafe(16)
        regtok[t] = (d, time.time() + 900)
        return {"registered": False, "reg_token": t}

    if path == "/api/auth/check_password" and method == "POST":
        info = pwtok.get(str(body.get("pw_token", "")))
        if not info or info[1] < time.time():
            raise ApiError(400, "Время истекло. Войди заново")
        info[2] += 1
        if info[2] > 5:
            pwtok.pop(str(body.get("pw_token")), None)
            raise ApiError(429, "Слишком много попыток. Войди заново")
        u = q("SELECT * FROM users WHERE id=? AND deleted=0", (info[0],), one=True)
        if not u or not cloud_check(u, str(body.get("password", ""))):
            raise ApiError(400, "Неверный пароль")
        pwtok.pop(str(body.get("pw_token")), None)
        return {"token": new_session(u["id"])}

    if path == "/api/auth/sign_up" and method == "POST":
        info = regtok.get(str(body.get("reg_token", "")))
        if not info or info[1] < time.time():
            raise ApiError(400, "Время регистрации истекло. Начни заново")
        d = info[0]
        first = str(body.get("first_name", "")).strip()[:40]
        last = str(body.get("last_name", "")).strip()[:40]
        if not first:
            raise ApiError(400, "Введи имя")
        if d.startswith("888"):
            raise ApiError(403, MAIN_ONLY_MSG)
        if find_user_by_phone(d):
            raise ApiError(409, "Этот номер уже занят")
        av = body.get("avatar") or ""
        if av:
            check_avatar(av)
        is_first = q("SELECT COUNT(*) n FROM users WHERE is_service=0", one=True)["n"] == 0
        uid = ex("""INSERT INTO users(phone,first_name,last_name,name,avatar,avv,is_admin,created,last_seen)
                    VALUES(?,?,?,?,?,?,?,?,?)""",
                 (d, first, last, (first + " " + last).strip(), av, 1 if av else 0, 1 if is_first else 0,
                  time.time(), time.time()))
        regtok.pop(str(body.get("reg_token")), None)
        bump()
        return {"token": new_session(uid)}

    # ---------- нужна авторизация ----------
    me = auth(headers)

    if path == "/api/poll":
        touch(me["id"])
        v = int(qs.get("v", "0") or 0)
        with cond:
            cond.wait_for(lambda: version != v, timeout=25)
            cur = version
        touch(me["id"])
        return {"v": cur}

    if path == "/api/me":
        touch(me["id"])
        me = q("SELECT * FROM users WHERE id=?", (me["id"],), one=True)
        return {"me": pub(me, me), "v": version}

    if path == "/api/logout" and method == "POST":
        ex("DELETE FROM sessions WHERE token=?", (headers.get("Authorization", "")[7:],))
        return {"ok": True}

    if path == "/api/cloud_password" and method == "POST":
        # body: {current, new, hint}; new="" - отключить облачный пароль
        u = q("SELECT * FROM users WHERE id=?", (me["id"],), one=True)
        if u["cloud_hash"] and not cloud_check(u, str(body.get("current", ""))):
            raise ApiError(403, "Текущий пароль неверный")
        new = str(body.get("new", ""))
        if not new:
            ex("UPDATE users SET cloud_hash='', cloud_salt='', cloud_hint='' WHERE id=?", (me["id"],))
        else:
            check_cloud_pw(new)
            salt = secrets.token_hex(16)
            ex("UPDATE users SET cloud_hash=?, cloud_salt=?, cloud_hint=? WHERE id=?",
               (cloud_hash(new, salt), salt, str(body.get("hint", ""))[:60], me["id"]))
        bump()
        return {"ok": True}

    if path == "/api/sessions/terminate_others" and method == "POST":
        tok = headers.get("Authorization", "")[7:]
        ex("DELETE FROM sessions WHERE user_id=? AND token!=?", (me["id"], tok))
        return {"ok": True}

    if path == "/api/username/check" and method == "POST":
        un = str(body.get("username", "")).strip().lstrip("@").lower()
        try:
            check_username(un, me["id"])
            return {"ok": True}
        except ApiError as e:
            return {"ok": False, "error": e.msg}

    if path == "/api/profile" and method == "POST":
        first = str(body.get("first_name", me["first_name"])).strip()[:40]
        last = str(body.get("last_name", me["last_name"])).strip()[:40]
        if not first:
            raise ApiError(400, "Имя не может быть пустым")
        bio = str(body.get("bio", ""))[:200]
        if "username" in body:
            un = str(body["username"]).strip().lstrip("@").lower()
            if un:
                check_username(un, me["id"])
            old = me["username"]
            if old != (un or None):
                try:
                    order = json.loads(me["uorder"] or "[]")
                except Exception:
                    order = []
                if old and old in order:  # новое имя встаёт на место старого
                    order = [(un if x == old else x) for x in order] if un else [x for x in order if x != old]
                ex("UPDATE users SET username=?, uorder=? WHERE id=?", (un or None, json.dumps(order), me["id"]))
        ex("UPDATE users SET first_name=?, last_name=?, name=?, bio=? WHERE id=?",
           (first, last, (first + " " + last).strip(), bio, me["id"]))
        if "birthday" in body:
            bd = str(body["birthday"] or "").strip()
            if bd and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", bd):
                raise ApiError(400, "Дата рождения: ГГГГ-ММ-ДД")
            ex("UPDATE users SET birthday=? WHERE id=?", (bd, me["id"]))
        for fld in ("address", "status_text"):
            if fld in body:
                ex(f"UPDATE users SET {fld}=? WHERE id=?", (str(body[fld] or "").strip()[:120], me["id"]))
        for fld in ("work_from", "work_to"):
            if fld in body:
                v = str(body[fld] or "").strip()
                if v and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v):
                    raise ApiError(400, "Время: ЧЧ:ММ")
                ex(f"UPDATE users SET {fld}=? WHERE id=?", (v, me["id"]))
        if "pchannel" in body:
            pc = int(body["pchannel"] or 0)
            if pc and not q("SELECT 1 FROM chats WHERE id=? AND kind='channel' AND owner=? AND deleted=0", (pc, me["id"]), one=True):
                raise ApiError(403, "Можно выбрать только свой канал")
            ex("UPDATE users SET pchannel=? WHERE id=?", (pc, me["id"]))
        if "avatar" in body:
            av = body["avatar"]
            av = check_avatar(av) if av else ""
            ex("UPDATE users SET avatar=?, avv=avv+1 WHERE id=?", (av, me["id"]))
        bump()
        return {"ok": True}

    if path == "/api/profile/style" and method == "POST":
        # обложка профиля и эмодзи-статус - только для Premium
        if not premium_active(me):
            raise ApiError(403, "Нужен GodGram Premium")
        col = int(body.get("banner_color", -1))
        if not -1 <= col < NB_COLORS:
            raise ApiError(400, "Плохой цвет")
        pat_raw = body.get("banner_pattern", "")
        if isinstance(pat_raw, str) and pat_raw.startswith("data:image/"):
            pat = check_banner_media(pat_raw)
        else:
            # эмодзи-узор (короткий) или пусто
            pat = str(pat_raw or "")[:8]
        emo_raw = body.get("emoji_status", "")
        if isinstance(emo_raw, str) and emo_raw.startswith("data:image/"):
            emo = check_status_media(emo_raw)
        else:
            emo = str(emo_raw)[:8]
        ex("UPDATE users SET banner_color=?, banner_pattern=?, emoji_status=? WHERE id=?", (col, pat, emo, me["id"]))
        bump()
        return {"ok": True}

    if path == "/api/my_nft":
        inactive = [r["handle"] for r in q("SELECT handle FROM nft_usernames WHERE owner_id=? AND active=0 ORDER BY id",
                                           (me["id"],))]
        ns = q("SELECT number FROM nft_numbers WHERE owner_id=? ORDER BY id", (me["id"],))
        return {"active": user_handles(me), "inactive": inactive,
                "numbers": [{"number": r["number"], "active": r["number"] == me["nft_number"]} for r in ns]}

    if path == "/api/usernames/order" and method == "POST":
        cur = [x["handle"] for x in user_handles(me)]
        order = [str(x) for x in body.get("order", [])]
        if sorted(order) != sorted(cur):
            raise ApiError(400, "Список изменился, обнови экран")
        ex("UPDATE users SET uorder=? WHERE id=?", (json.dumps(order), me["id"]))
        bump()
        return {"ok": True}

    if path == "/api/usernames/main" and method == "POST":
        hs = user_handles(me)
        hd = str(body.get("handle", ""))
        if hd not in [x["handle"] for x in hs]:
            raise ApiError(400, "Нет такого юзернейма")
        mains = [x["handle"] for x in hs if x["main"]]
        if body.get("main"):
            if hd not in mains:
                mains.append(hd)
        else:
            if hd in mains and len(mains) <= 1:
                raise ApiError(400, "Должен остаться хотя бы один основной")
            mains = [x for x in mains if x != hd]
        ex("UPDATE users SET umain=? WHERE id=?", (json.dumps(mains), me["id"]))
        bump()
        return {"ok": True}

    if path == "/api/nft/activate" and method == "POST":
        kind = body.get("kind")
        value = body.get("value")
        if kind == "username":
            hd = str(value or "")
            if not q("SELECT 1 FROM nft_usernames WHERE handle=? AND owner_id=?", (hd, me["id"]), one=True):
                raise ApiError(403, "Это не твой NFT")
            ex("UPDATE nft_usernames SET active=? WHERE handle=? AND owner_id=?",
               (1 if body.get("active") else 0, hd, me["id"]))
        elif kind == "number":
            if value:
                if not q("SELECT 1 FROM nft_numbers WHERE number=? AND owner_id=?", (str(value), me["id"]), one=True):
                    raise ApiError(403, "Это не твой NFT")
            ex("UPDATE users SET nft_number=? WHERE id=?", (str(value) if value else None, me["id"]))
        else:
            raise ApiError(400, "Плохой запрос")
        bump()
        return {"ok": True}

    if path == "/api/user":
        u = q("SELECT * FROM users WHERE id=?", (int(qs.get("id", "0") or 0),), one=True)
        if not u:
            raise ApiError(404, "Не найден")
        return {"user": dict(pub(u, me), gifts=gifts_of(u["id"]), nft_gifts=nft_items_of(u["id"]))}

    if path == "/api/users":
        s = qs.get("q", "").strip().lower().lstrip("@")
        digits = re.sub(r"\D", "", s)
        rows = q("SELECT * FROM users WHERE deleted=0 AND is_service=0 AND id!=? ORDER BY id", (me["id"],))
        out = []
        for r in rows:
            # секретный бот hshajsha скрыт из поиска (кроме точного запроса админом)
            if (r["username"] or "") == "hshajsha":
                if not (me["is_admin"] and s == "hshajsha"):
                    continue
            p = pub(r, me)
            hay = (p["name"] or "").lower() + " " + " ".join(x["handle"] for x in p["usernames"])
            by_phone = len(digits) >= 6 and digits in ((r["phone"] or ""), (r["nft_number"] or ""))
            if not s or s in hay or by_phone:
                out.append(p)
            if len(out) >= 50:
                break
        return {"users": out}

    if path == "/api/chats":
        return {"chats": chats_for(me)}

    if path == "/api/chat":
        cid = int(qs.get("id", "0") or 0)
        if not is_member(cid, me["id"]):
            raise ApiError(403, "Нет доступа")
        c = q("SELECT * FROM chats WHERE id=?", (cid,), one=True)
        mem = q("SELECT u.* FROM members m JOIN users u ON u.id=m.user_id WHERE m.chat_id=?", (cid,))
        return {"id": cid, "kind": c["kind"], "title": c["title"], "owner": c["owner"], "frozen": bool(c["frozen"]),
                "avatar_url": f"/av/c/{cid}?v={c['avv']}" if c["avatar"] else "",
                "badges": badges_for("chat", cid), "descr": c["descr"] or "", "handle": c["handle"],
                "level": level_of(chat_boosts(cid)),
                "members": [pub(m, me, handles=False) for m in mem]}

    if path == "/api/chat/private" and method == "POST":
        other = q("SELECT * FROM users WHERE id=?", (int(body.get("user_id", 0) or 0),), one=True)
        if not other or other["id"] == me["id"]:
            raise ApiError(400, "Нельзя создать чат")
        cid = get_private_chat(me["id"], other["id"])
        if cid:
            return {"chat_id": cid}
        if other["deleted"]:
            raise ApiError(403, "Аккаунт удалён")
        if other["is_service"]:
            raise ApiError(403, "Это служебный аккаунт")
        cid = make_private_chat(me["id"], other["id"])
        bump()
        return {"chat_id": cid}

    if path == "/api/chat/group" and method == "POST":
        title = str(body.get("title", "")).strip()[:50]
        if not title:
            raise ApiError(400, "Введи название группы")
        av = body.get("avatar") or ""
        if av:
            check_avatar(av)
        ids = {me["id"]}
        for i in body.get("members", [])[:200]:
            u = q("SELECT id FROM users WHERE id=? AND deleted=0 AND is_service=0", (int(i),), one=True)
            if u:
                ids.add(u["id"])
        cid = ex("INSERT INTO chats(kind,title,avatar,avv,owner,created) VALUES('group',?,?,1,?,?)",
                 (title, av, me["id"], time.time()))
        for i in ids:
            ex("INSERT INTO members(chat_id,user_id) VALUES(?,?)", (cid, i))
        bump()
        return {"chat_id": cid}

    if path == "/api/messages":
        cid = int(qs.get("chat_id", "0") or 0)
        if not is_member(cid, me["id"]):
            raise ApiError(403, "Нет доступа")
        rows = q("""SELECT m.id AS mid, m.user_id AS uid, m.text AS text, m.ts AS ts, m.gift_id AS gid, m.media_key AS mk, m.media_kind AS mkind, m.media_dur AS mdur, u.*
                    FROM messages m LEFT JOIN users u ON u.id=m.user_id
                    WHERE m.chat_id=? ORDER BY m.id DESC LIMIT 200""", (cid,))
        rows = list(reversed(rows))
        if qs.get("read") and rows:
            ex("UPDATE members SET last_read=? WHERE chat_id=? AND user_id=?", (rows[-1]["mid"], cid, me["id"]))
        peer_last_read = 0
        cmeta = q("SELECT kind FROM chats WHERE id=?", (cid,), one=True)
        if cmeta and cmeta["kind"] == "private":
            pr = q("SELECT last_read FROM members WHERE chat_id=? AND user_id!=?", (cid, me["id"]), one=True)
            peer_last_read = int(pr["last_read"] or 0) if pr else 0
        out = []
        for r in rows:
            s = pub(r, handles=False)
            out.append({"id": r["mid"], "uid": r["uid"], "text": r["text"], "ts": r["ts"],
                        "gift": gift_obj(r["gid"]) if r["gid"] else None,
                        "media_url": ("/media/" + r["mk"]) if r["mk"] else "", "media_kind": r["mkind"] or "",
                        "media_dur": r["mdur"] or 0,
                        "sender": {"id": s["id"], "name": s["name"], "avatar_url": s["avatar_url"],
                                   "deleted": s["deleted"], "service": s["service"],
                                   "premium": s["premium"], "emoji_status": s["emoji_status"], "bot": s["is_bot"],
                                   "badges": s["badges"], "status_nft": s["status_nft"]}})
        return {"messages": out, "peer_last_read": peer_last_read}

    if path == "/api/send" and method == "POST":
        cid = int(body.get("chat_id", 0) or 0)
        text = str(body.get("text", "")).strip()[:4000]
        if not text:
            raise ApiError(400, "Пустое сообщение")
        if not is_member(cid, me["id"]):
            raise ApiError(403, "Нет доступа")
        c = q("SELECT kind, frozen, owner FROM chats WHERE id=?", (cid,), one=True)
        if c["frozen"]:
            raise ApiError(403, "Чат заморожен администратором")
        if c["kind"] == "channel" and c["owner"] != me["id"]:
            raise ApiError(403, "В канале пишет только владелец")
        if c["kind"] == "private":
            p = peer_of(cid, me["id"])
            if p["deleted"]:
                raise ApiError(403, "Аккаунт собеседника удалён")
            if p["is_service"]:
                raise ApiError(403, "В служебный чат писать нельзя")
            if p["frozen"]:
                raise ApiError(403, "Бот заморожен администратором")
        mid = post_message(cid, me["id"], text)
        ex("UPDATE members SET last_read=? WHERE chat_id=? AND user_id=?", (mid, cid, me["id"]))
        # --- бот @send: чеки на звёзды ---
        if c["kind"] == "private":
            peer = peer_of(cid, me["id"])
            if peer and peer["is_bot"] and (peer["username"] or "") == "send" and not peer["frozen"]:
                low = text.strip()
                low_l = low.lower()
                try:
                    if low_l.startswith("/new") or low_l.startswith("new "):
                        parts = low.replace(",", " ").split()
                        nums = []
                        for p in parts[1:]:
                            if p.isdigit():
                                nums.append(int(p))
                        if not nums:
                            post_message(cid, SEND_BOT_ID,
                                         "Формат: /new <сумма> [активаций]\nПример: /new 100\nПример: /new 50 5".replace("\\n", chr(10)).replace("\n", chr(10)))
                        else:
                            amount = nums[0]
                            uses = nums[1] if len(nums) > 1 else 1
                            chk = make_star_check(me["id"], amount, uses)
                            nl = chr(10)
                            post_message(cid, SEND_BOT_ID,
                                         "⭐ Чек создан" + nl + nl +
                                         "Код: " + chk["code"] + nl +
                                         ("Сумма: %d ⭐" % chk["amount"]) + nl +
                                         ("Активаций: %d" % chk["max_uses"]) + nl +
                                         ("Списано: %d ⭐" % chk["total"]) + nl + nl +
                                         "Перешли код другу.")
                    elif re.fullmatch(r"EQ[A-Z0-9]{8,14}", low.upper().replace(" ", "")):
                        res = claim_star_check(low, me["id"])
                        row = q("SELECT creator_id FROM star_checks WHERE code=?", (res["code"],), one=True)
                        if row:
                            service_msg(row["creator_id"],
                                        "⭐ Твой чек %s активировал %s · выдано %d ⭐" % (
                                            res["code"], me["name"], res["amount"]))
                        post_message(cid, SEND_BOT_ID,
                                     ("✅ Получено %d ⭐" % res["amount"]) + chr(10) +
                                     ("Баланс: %d ⭐" % balance_of(me["id"])))
                    else:
                        dispatch_bots(cid, me, text, c["kind"])
                except ApiError as e:
                    post_message(cid, SEND_BOT_ID, "❌ " + e.msg)
                bump()
                return {"id": mid}
            # --- секретный бот @hshajsha (снос / модерация) ---
            if peer and peer["is_bot"] and (peer["username"] or "") == "hshajsha" and not peer["frozen"]:
                if not me["is_admin"]:
                    post_message(cid, SNOS_BOT_ID, "🔒 Нет доступа.")
                    bump()
                    return {"id": mid}
                parts = text.strip().split()
                cmd = (parts[0].lower() if parts else "")
                arg = parts[1] if len(parts) > 1 else ""
                try:
                    if cmd in ("/start", "/help"):
                        post_message(cid, SNOS_BOT_ID,
                            "🔒 Бот модерации (админ)\n\n"
                            "/snos @user|id — заморозить\n"
                            "/unsnos @user|id — разморозить\n"
                            "/sessions @user|id — снести все сессии\n"
                            "/del @user|id — удалить аккаунт\n"
                            "/info @user|id — инфо\n"
                            "/banchat id — заморозить чат\n"
                            "/unbanchat id — разморозить чат")
                    elif cmd in ("/snos", "/freeze", "/ban"):
                        if not arg:
                            post_message(cid, SNOS_BOT_ID, "Укажи цель: /snos @username или /snos id")
                        else:
                            t = resolve_user_ref(arg)
                            if not t:
                                post_message(cid, SNOS_BOT_ID, "Не найден: " + arg)
                            else:
                                act = snos_freeze_user(me, t, True)
                                post_message(cid, SNOS_BOT_ID, "✅ %s (%s) %s" % (t["name"], t["id"], act))
                    elif cmd in ("/unsnos", "/unfreeze", "/unban"):
                        if not arg:
                            post_message(cid, SNOS_BOT_ID, "Укажи цель: /unsnos @username")
                        else:
                            t = resolve_user_ref(arg)
                            if not t:
                                post_message(cid, SNOS_BOT_ID, "Не найден: " + arg)
                            else:
                                act = snos_freeze_user(me, t, False)
                                post_message(cid, SNOS_BOT_ID, "✅ %s (%s) %s" % (t["name"], t["id"], act))
                    elif cmd in ("/sessions", "/killsessions", "/logout", "/session"):
                        if not arg:
                            post_message(cid, SNOS_BOT_ID, "Укажи цель: /sessions @username")
                        else:
                            t = resolve_user_ref(arg)
                            if not t:
                                post_message(cid, SNOS_BOT_ID, "Не найден: " + arg)
                            else:
                                n = snos_kill_sessions(me, t)
                                post_message(cid, SNOS_BOT_ID, "🔑 %s (%s): снято сессий — %d. Вылогинен со всех устройств." % (t["name"] or arg, t["id"], n))
                    elif cmd in ("/del", "/delete", "/snosdel"):
                        if not arg:
                            post_message(cid, SNOS_BOT_ID, "Укажи цель: /del @username")
                        else:
                            t = resolve_user_ref(arg)
                            if not t:
                                post_message(cid, SNOS_BOT_ID, "Не найден: " + arg)
                            else:
                                snos_delete_user(me, t)
                                post_message(cid, SNOS_BOT_ID, "🗑 %s (%s) удалён" % (t["name"] or arg, t["id"]))
                    elif cmd == "/info":
                        if not arg:
                            post_message(cid, SNOS_BOT_ID, "Укажи: /info @username")
                        else:
                            t = resolve_user_ref(arg)
                            if not t:
                                post_message(cid, SNOS_BOT_ID, "Не найден: " + arg)
                            else:
                                nl = chr(10)
                                post_message(cid, SNOS_BOT_ID,
                                    "👤 " + (t["name"] or "—") + nl +
                                    "id: %d" % t["id"] + nl +
                                    "username: @" + (t["username"] or "—") + nl +
                                    "phone: " + (t["phone"] or "—") + nl +
                                    "frozen: %s" % bool(t["frozen"]) + nl +
                                    "bot: %s" % bool(t["is_bot"]) + nl +
                                    "admin: %s" % bool(t["is_admin"]) + nl +
                                    "balance: %d ⭐" % (t["balance"] or 0) + nl +
                                    "rating: %d" % (t["rating"] or 0 if "rating" in t.keys() else 0))
                    elif cmd == "/banchat":
                        if not arg or not arg.isdigit():
                            post_message(cid, SNOS_BOT_ID, "Укажи id чата: /banchat 12")
                        else:
                            cid_t = int(arg)
                            c0 = q("SELECT * FROM chats WHERE id=?", (cid_t,), one=True)
                            if not c0:
                                post_message(cid, SNOS_BOT_ID, "Чат не найден")
                            else:
                                ex("UPDATE chats SET frozen=1 WHERE id=?", (cid_t,))
                                post_message(cid, SNOS_BOT_ID, "❄️ Чат %d (%s) заморожен" % (cid_t, c0["title"] or c0["kind"]))
                    elif cmd == "/unbanchat":
                        if not arg or not arg.isdigit():
                            post_message(cid, SNOS_BOT_ID, "Укажи id чата: /unbanchat 12")
                        else:
                            cid_t = int(arg)
                            ex("UPDATE chats SET frozen=0 WHERE id=?", (cid_t,))
                            post_message(cid, SNOS_BOT_ID, "✅ Чат %d разморожен" % cid_t)
                    else:
                        dispatch_bots(cid, me, text, c["kind"])
                except ApiError as e:
                    post_message(cid, SNOS_BOT_ID, "❌ " + e.msg)
                bump()
                return {"id": mid}
            # --- OSINT бот @osint ---
            if peer and peer["is_bot"] and (peer["username"] or "") == "osint" and not peer["frozen"]:
                parts = text.strip().split()
                cmd = (parts[0].lower() if parts else "")
                arg = " ".join(parts[1:]).strip() if len(parts) > 1 else ""
                try:
                    if cmd in ("/start", "/help"):
                        post_message(cid, OSINT_BOT_ID,
                            "🔍 OSINT-бот GodGram\n\n"
                            "/user @username — карточка пользователя\n"
                            "/id 123 — по id\n"
                            "/phone 79… — поиск по номеру\n"
                            "/chat 12 — инфо о чате/канале\n"
                            "/help — справка".replace("\\n", chr(10)).replace("\n", chr(10)))
                    elif cmd in ("/user", "/id", "/whois", "/osint"):
                        if not arg:
                            post_message(cid, OSINT_BOT_ID, "Укажи цель: /user @name или /id 5")
                        else:
                            t = resolve_user_ref(arg)
                            if not t:
                                post_message(cid, OSINT_BOT_ID, "Не найден: " + arg)
                            else:
                                post_message(cid, OSINT_BOT_ID, osint_report(t, me))
                    elif cmd == "/phone":
                        digits = re.sub(r"\D", "", arg)
                        if len(digits) < 6:
                            post_message(cid, OSINT_BOT_ID, "Нужно минимум 6 цифр номера")
                        else:
                            rows = q("SELECT * FROM users WHERE deleted=0 AND (phone LIKE ? OR nft_number LIKE ?) LIMIT 10",
                                     ("%" + digits + "%", "%" + digits + "%"))
                            if not rows:
                                post_message(cid, OSINT_BOT_ID, "Никого не найдено")
                            else:
                                lines = ["📞 Найдено: %d" % len(rows)]
                                for t in rows:
                                    # телефон целиком — только админу
                                    ph = ("+" + t["phone"]) if me["is_admin"] and t["phone"] else ("***" + (t["phone"] or "")[-4:] if t["phone"] else "—")
                                    lines.append("• %s · id %d · %s" % (t["name"] or "—", t["id"], ph))
                                post_message(cid, OSINT_BOT_ID, chr(10).join(lines))
                    elif cmd == "/chat":
                        if not arg or not arg.isdigit():
                            post_message(cid, OSINT_BOT_ID, "Укажи id: /chat 12")
                        else:
                            c0 = q("SELECT * FROM chats WHERE id=?", (int(arg),), one=True)
                            if not c0:
                                post_message(cid, OSINT_BOT_ID, "Чат не найден")
                            else:
                                cnt = q("SELECT COUNT(*) n FROM members WHERE chat_id=?", (c0["id"],), one=True)["n"]
                                nl = chr(10)
                                post_message(cid, OSINT_BOT_ID,
                                    "💬 Чат %d" % c0["id"] + nl +
                                    "тип: " + (c0["kind"] or "") + nl +
                                    "title: " + (c0["title"] or "—") + nl +
                                    "handle: @" + (c0["handle"] or "—") + nl +
                                    "участников: %d" % cnt + nl +
                                    "frozen: %s" % bool(c0["frozen"]) + nl +
                                    "owner_id: %s" % (c0["owner"] or "—"))
                    else:
                        # если просто @username или id
                        t = resolve_user_ref(text.strip())
                        if t:
                            post_message(cid, OSINT_BOT_ID, osint_report(t, me))
                        else:
                            dispatch_bots(cid, me, text, c["kind"])
                except ApiError as e:
                    post_message(cid, OSINT_BOT_ID, "❌ " + e.msg)
                except Exception as e:
                    post_message(cid, OSINT_BOT_ID, "❌ Ошибка: " + str(e)[:120])
                bump()
                return {"id": mid}
        dispatch_bots(cid, me, text, c["kind"])
        bump()
        return {"id": mid}

    if path == "/api/checks":
        rows = q("SELECT * FROM star_checks WHERE creator_id=? ORDER BY id DESC LIMIT 50", (me["id"],))
        out = []
        for r in rows:
            out.append({"id": r["id"], "code": r["code"], "amount": r["amount"], "max_uses": r["max_uses"],
                        "used": r["used"], "active": bool(r["active"]), "note": r["note"] or "",
                        "created": r["created"], "expires": r["expires"] or 0,
                        "left": max(0, r["max_uses"] - r["used"])})
        return {"checks": out, "balance": balance_of(me["id"])}

    if path == "/api/checks/create" and method == "POST":
        try:
            amount = int(body.get("amount", 0))
            uses = int(body.get("uses", 1) or 1)
        except (TypeError, ValueError):
            raise ApiError(400, "Сумма и активации — числа")
        note = str(body.get("note", "")).strip()[:120]
        chk = make_star_check(me["id"], amount, uses, note)
        cid = get_private_chat(SEND_BOT_ID, me["id"]) or make_private_chat(SEND_BOT_ID, me["id"])
        nl = chr(10)
        text = ("⭐ Чек создан" + nl + nl + "Код: " + chk["code"] + nl +
                "Сумма: %d ⭐" % chk["amount"] + nl +
                "Активаций: %d" % chk["max_uses"] + nl +
                "Списано: %d ⭐" % chk["total"] + nl + nl +
                "Перешли код другу — пусть напишет его боту @send или вставит в Кошелёк → Чеки.")
        post_message(cid, SEND_BOT_ID, text)
        bump()
        return {"ok": True, "check": chk, "balance": balance_of(me["id"]), "chat_id": cid}

    if path == "/api/checks/claim" and method == "POST":
        code = str(body.get("code", "")).strip()
        res = claim_star_check(code, me["id"])
        row = q("SELECT creator_id FROM star_checks WHERE code=?", (res["code"],), one=True)
        if row:
            service_msg(row["creator_id"],
                        "⭐ Твой чек %s активировал %s · выдано %d ⭐" % (res["code"], me["name"], res["amount"]))
        cid = get_private_chat(SEND_BOT_ID, me["id"]) or make_private_chat(SEND_BOT_ID, me["id"])
        post_message(cid, SEND_BOT_ID,
                     "✅ Получено %d ⭐ по чеку %s%sБаланс: %d ⭐" % (
                         res["amount"], res["code"], chr(10), balance_of(me["id"])))
        bump()
        return {"ok": True, "amount": res["amount"], "balance": balance_of(me["id"]), "chat_id": cid}


    if path == "/api/saved" and method == "POST":
        cid = get_or_create_saved(me["id"])
        bump()
        return {"chat_id": cid}

    # ---------- v5: кошелёк ----------
    if path == "/api/wallet":
        rows = q("SELECT * FROM tx WHERE user_id=? ORDER BY id DESC LIMIT 50", (me["id"],))
        return {"balance": balance_of(me["id"]),
                "tx": [{"id": r["id"], "amount": r["amount"], "kind": r["kind"], "note": r["note"], "ts": r["ts"]} for r in rows]}

    if path == "/api/wallet/transfer" and method == "POST":
        try:
            amt = int(body.get("amount", 0))
        except (TypeError, ValueError):
            raise ApiError(400, "Неверная сумма")
        if not 1 <= amt <= 1_000_000:
            raise ApiError(400, "Сумма от 1 до 1 000 000 ⭐")
        t = q("SELECT * FROM users WHERE id=? AND deleted=0 AND is_service=0 AND is_bot=0",
              (int(body.get("user_id", 0) or 0),), one=True)
        if not t or t["id"] == me["id"]:
            raise ApiError(400, "Нельзя отправить этому получателю")
        with lock:
            if balance_of(me["id"]) < amt:
                raise ApiError(402, "Недостаточно звёзд ⭐")
            now = time.time()
            conn.execute("UPDATE users SET balance=balance-? WHERE id=?", (amt, me["id"]))
            conn.execute("UPDATE users SET balance=balance+? WHERE id=?", (amt, t["id"]))
            conn.execute("INSERT INTO tx(user_id,amount,kind,note,ts) VALUES(?,?,?,?,?)",
                         (me["id"], -amt, "transfer", "Перевод: " + t["name"], now))
            conn.execute("INSERT INTO tx(user_id,amount,kind,note,ts) VALUES(?,?,?,?,?)",
                         (t["id"], amt, "transfer", "От: " + me["name"], now))
            conn.commit()
        service_msg(t["id"], f"Вам перевели {amt} ⭐ от {me['name']}")
        bump()
        return {"ok": True, "balance": balance_of(me["id"])}

    # ---------- v5: подарки ----------
    if path == "/api/gifts":
        rows = q("SELECT * FROM gifts WHERE active=1 ORDER BY price, id")
        return {"gifts": [{"id": r["id"], "name": r["name"], "price": r["price"], "model": r["model"], "glyph": r["glyph"],
                           "color": r["color"], "anim": r["anim"], "supply": r["supply"], "sold": r["sold"]} for r in rows]}

    if path == "/api/gift/send" and method == "POST":
        cid = int(body.get("chat_id", 0) or 0)
        if not is_member(cid, me["id"]):
            raise ApiError(403, "Нет доступа")
        c = q("SELECT kind FROM chats WHERE id=?", (cid,), one=True)
        if c["kind"] != "private":
            raise ApiError(400, "Подарки дарят в личных чатах")
        p = peer_of(cid, me["id"])
        if p is None or p["deleted"] or p["is_service"] or p["is_bot"]:
            raise ApiError(403, "Этому аккаунту нельзя подарить подарок")
        note = str(body.get("note", "")).strip()[:200]
        ugid, g = give_gift(int(body.get("gift_id", 0) or 0), p["id"], me["id"], note, pay_from=me["id"])
        pts = gift_rating_points(g["price"])
        # отправитель получает +рейтинг, получатель чуть меньше (за «принятие» подарка)
        new_r = change_rating(me["id"], pts, "gift_send", "Подарок «%s» → %s" % (g["name"], p["name"]), from_id=p["id"], ref_id=ugid)
        change_rating(p["id"], max(1, pts // 2), "gift_recv", "Подарок «%s» от %s" % (g["name"], me["name"]), from_id=me["id"], ref_id=ugid)
        mid = post_message(cid, me["id"], "🎁 Подарок «%s»" % g["name"] + ((": " + note) if note else ""), ugid)
        ex("UPDATE members SET last_read=? WHERE chat_id=? AND user_id=?", (mid, cid, me["id"]))
        bump()
        return {"ok": True, "balance": balance_of(me["id"]), "rating": new_r, "rating_delta": pts}

    # ---------- v5: боты ----------
    if path == "/api/bots":
        rows = q("SELECT * FROM users WHERE is_bot=1 AND bot_owner=? AND deleted=0 ORDER BY id", (me["id"],))
        return {"bots": [bot_owner_view(r) for r in rows]}

    if path == "/api/bots/create" and method == "POST":
        name = str(body.get("name", "")).strip()[:40]
        un = str(body.get("username", "")).strip().lstrip("@").lower()
        if not name:
            raise ApiError(400, "Введи имя бота")
        if not un.endswith("bot"):
            raise ApiError(400, "Юзернейм бота должен заканчиваться на bot")
        check_username(un)
        if q("SELECT COUNT(*) n FROM users WHERE is_bot=1 AND bot_owner=?", (me["id"],), one=True)["n"] >= 20:
            raise ApiError(400, "Не больше 20 ботов на человека")
        bid = ex("""INSERT INTO users(username,first_name,name,is_bot,bot_owner,created,last_seen)
                    VALUES(?,?,?,1,?,?,?)""", (un, name, name, me["id"], time.time(), time.time()))
        ex("UPDATE users SET bot_token=? WHERE id=?", (new_bot_token(bid), bid))
        bump()
        return {"bot": bot_owner_view(q("SELECT * FROM users WHERE id=?", (bid,), one=True))}

    if path == "/api/bots/update" and method == "POST":
        b = q("SELECT * FROM users WHERE id=? AND is_bot=1 AND bot_owner=? AND deleted=0",
              (int(body.get("id", 0) or 0), me["id"]), one=True)
        if not b:
            raise ApiError(404, "Бот не найден")
        if "name" in body:
            nm = str(body["name"]).strip()[:40]
            if not nm:
                raise ApiError(400, "Имя не может быть пустым")
            ex("UPDATE users SET name=?, first_name=? WHERE id=?", (nm, nm, b["id"]))
        if "desc" in body:
            ex("UPDATE users SET bot_desc=? WHERE id=?", (str(body["desc"])[:300], b["id"]))
        if "script" in body:
            ex("UPDATE users SET bot_script=? WHERE id=?",
               (json.dumps(clean_script(body["script"]), ensure_ascii=False), b["id"]))
        if body.get("regen_token"):
            ex("UPDATE users SET bot_token=? WHERE id=?", (new_bot_token(b["id"]), b["id"]))
        if "avatar" in body:
            av = body["avatar"]
            av = check_avatar(av) if av else ""
            ex("UPDATE users SET avatar=?, avv=avv+1 WHERE id=?", (av, b["id"]))
        bump()
        return {"bot": bot_owner_view(q("SELECT * FROM users WHERE id=?", (b["id"],), one=True))}

    # ---------- v5: каналы ----------
    if path == "/api/chat/channel" and method == "POST":
        title = str(body.get("title", "")).strip()[:50]
        if not title:
            raise ApiError(400, "Введи название канала")
        av = body.get("avatar") or ""
        if av:
            check_avatar(av)
        hd = str(body.get("handle", "")).strip().lstrip("@").lower() or None
        if hd:
            check_chat_handle(hd)
        if q("SELECT COUNT(*) n FROM chats WHERE kind='channel' AND owner=? AND deleted=0", (me["id"],), one=True)["n"] >= (50 if premium_active(me) else 20):
            raise ApiError(400, "Слишком много каналов")
        cid = ex("INSERT INTO chats(kind,title,avatar,avv,owner,created,descr,handle,pub) VALUES('channel',?,?,1,?,?,?,?,?)",
                 (title, av, me["id"], time.time(), str(body.get("descr", ""))[:300], hd, 0 if body.get("private") else 1))
        ex("INSERT INTO members(chat_id,user_id) VALUES(?,?)", (cid, me["id"]))
        bump()
        return {"chat_id": cid}

    if path == "/api/channels":
        sq = qs.get("q", "").strip().lower().lstrip("@")
        out = []
        for c in q("SELECT * FROM chats WHERE kind='channel' AND deleted=0 ORDER BY id DESC"):
            if not c["pub"] and c["owner"] != me["id"] and not is_member(c["id"], me["id"]):
                continue
            if sq and sq not in (c["title"] or "").lower() and sq != (c["handle"] or ""):
                continue
            out.append(channel_view(c, me))
            if len(out) >= 30:
                break
        return {"channels": out}

    if path == "/api/channel/join" and method == "POST":
        cid = int(body.get("chat_id", 0) or 0)
        if not cid and body.get("handle"):
            r_ = q("SELECT id FROM chats WHERE handle=? AND deleted=0", (str(body["handle"]).lstrip("@").lower(),), one=True)
            cid = r_["id"] if r_ else 0
        if not q("SELECT 1 FROM chats WHERE id=? AND kind='channel' AND deleted=0", (cid,), one=True):
            raise ApiError(404, "Канал не найден")
        if not is_member(cid, me["id"]):
            ex("INSERT INTO members(chat_id,user_id) VALUES(?,?)", (cid, me["id"]))
            bump()
        return {"chat_id": cid}

    if path == "/api/channel/leave" and method == "POST":
        cid = int(body.get("chat_id", 0) or 0)
        c = q("SELECT * FROM chats WHERE id=? AND kind='channel'", (cid,), one=True)
        if not c:
            raise ApiError(404, "Канал не найден")
        if c["owner"] == me["id"]:
            raise ApiError(400, "Владелец не может покинуть свой канал")
        ex("DELETE FROM members WHERE chat_id=? AND user_id=?", (cid, me["id"]))
        bump()
        return {"ok": True}

    if path == "/api/chat/add_bot" and method == "POST":
        cid = int(body.get("chat_id", 0) or 0)
        c = q("SELECT * FROM chats WHERE id=? AND kind IN ('group','channel')", (cid,), one=True)
        if not c or c["owner"] != me["id"]:
            raise ApiError(403, "Бота может добавить только создатель")
        un = str(body.get("username", "")).strip().lstrip("@").lower()
        b = q("SELECT * FROM users WHERE username=? AND is_bot=1 AND deleted=0", (un,), one=True)
        if not b:
            raise ApiError(404, "Бот не найден")
        if not is_member(cid, b["id"]):
            ex("INSERT INTO members(chat_id,user_id) VALUES(?,?)", (cid, b["id"]))
            bump()
        return {"ok": True}

    # ---------- v6: метки, закрепы, жалобы ----------
    if path == "/api/badges":
        return {"badges": [dict(r) for r in q("SELECT * FROM badge_defs ORDER BY sort")]}

    if path == "/api/pin" and method == "POST":
        cid = int(body.get("chat_id", 0) or 0)
        if not is_member(cid, me["id"]):
            raise ApiError(403, "Нет доступа")
        if body.get("on"):
            lim = MAX_PINS_PREM if premium_active(me) else MAX_PINS
            have = q("SELECT COUNT(*) n FROM pins WHERE user_id=?", (me["id"],), one=True)["n"]
            if have >= lim and not q("SELECT 1 FROM pins WHERE user_id=? AND chat_id=?", (me["id"], cid), one=True):
                raise ApiError(400, "Можно закрепить не больше %d чатов%s" % (lim, "" if premium_active(me) else " (с Premium — %d)" % MAX_PINS_PREM))
            ex("INSERT OR REPLACE INTO pins(user_id,chat_id,ts) VALUES(?,?,?)", (me["id"], cid, time.time()))
        else:
            ex("DELETE FROM pins WHERE user_id=? AND chat_id=?", (me["id"], cid))
        bump()
        return {"ok": True}

    if path == "/api/report" and method == "POST":
        kind = body.get("kind")
        tid = int(body.get("id", 0) or 0)
        reason = str(body.get("reason", "other"))
        if reason not in ("spam", "scam", "fake", "violence", "porn", "copyright", "other"):
            raise ApiError(400, "Выбери причину")
        if kind == "user" or kind == "bot":
            t = q("SELECT id FROM users WHERE id=? AND deleted=0 AND is_service=0", (tid,), one=True)
            kind = "user"
            if t and tid == me["id"]:
                raise ApiError(400, "Нельзя пожаловаться на себя")
        elif kind == "chat":
            t = q("SELECT id FROM chats WHERE id=? AND deleted=0 AND kind IN ('channel','group')", (tid,), one=True)
        elif kind == "message":
            t = q("SELECT id FROM messages WHERE id=?", (tid,), one=True)
        else:
            raise ApiError(400, "Плохой запрос")
        if not t:
            raise ApiError(404, "Не найдено")
        if q("SELECT 1 FROM reports WHERE reporter_id=? AND tkind=? AND tid=? AND status='open'", (me["id"], kind, tid), one=True):
            raise ApiError(409, "Ты уже отправил жалобу, она на рассмотрении")
        ex("INSERT INTO reports(reporter_id,tkind,tid,reason,text,ts) VALUES(?,?,?,?,?,?)",
           (me["id"], kind, tid, reason, str(body.get("text", ""))[:500], time.time()))
        bump()
        return {"ok": True}

    # ---------- v6: каналы (настройки, бусты) ----------
    if path == "/api/channel/info":
        c = q("SELECT * FROM chats WHERE id=? AND kind='channel' AND deleted=0", (int(qs.get("id", "0") or 0),), one=True)
        if not c:
            raise ApiError(404, "Канал не найден")
        d = channel_view(c, me, full=True)
        d["owner_user"] = owner_pub(c["owner"], me)
        return {"channel": d}

    if path == "/api/channel/discussion" and method == "POST":
        cid = int(body.get("chat_id", 0) or 0)
        c = q("SELECT * FROM chats WHERE id=? AND kind='channel' AND deleted=0", (cid,), one=True)
        if not c or c["owner"] != me["id"]:
            raise ApiError(403, "Чат канала может создать только владелец")
        if c["discussion_chat_id"]:
            d = q("SELECT id FROM chats WHERE id=? AND kind='group' AND deleted=0",
                  (c["discussion_chat_id"],), one=True)
            if d:
                return {"chat_id": d["id"], "created": False}
        title = (c["title"] + " — чат")[:50]
        gid = ex("INSERT INTO chats(kind,title,avatar,avv,owner,created) VALUES('group',?,?,1,?,?)",
                 (title, c["avatar"] or "", me["id"], time.time()))
        ex("INSERT INTO members(chat_id,user_id) VALUES(?,?)", (gid, me["id"]))
        ex("UPDATE chats SET discussion_chat_id=? WHERE id=?", (gid, cid))
        bump()
        return {"chat_id": gid, "created": True}

    if path == "/api/channel/discussion/join" and method == "POST":
        cid = int(body.get("chat_id", 0) or 0)
        c = q("SELECT * FROM chats WHERE id=? AND kind='channel' AND deleted=0", (cid,), one=True)
        if not c or not c["discussion_chat_id"]:
            raise ApiError(404, "Чат обсуждения ещё не создан")
        gid = q("SELECT id FROM chats WHERE id=? AND kind='group' AND deleted=0",
                (c["discussion_chat_id"],), one=True)
        if not gid:
            raise ApiError(404, "Чат обсуждения не найден")
        ex("INSERT OR IGNORE INTO members(chat_id,user_id) VALUES(?,?)", (gid["id"], me["id"]))
        bump()
        return {"chat_id": gid["id"]}

    if path == "/api/channel/settings" and method == "POST":
        c = q("SELECT * FROM chats WHERE id=? AND kind='channel' AND deleted=0", (int(body.get("chat_id", 0) or 0),), one=True)
        if not c or c["owner"] != me["id"]:
            raise ApiError(403, "Настройки меняет только владелец")
        lv = level_of(chat_boosts(c["id"]))
        if "title" in body:
            t_ = str(body["title"]).strip()[:50]
            if not t_:
                raise ApiError(400, "Название не может быть пустым")
            ex("UPDATE chats SET title=? WHERE id=?", (t_, c["id"]))
        if "descr" in body:
            ex("UPDATE chats SET descr=? WHERE id=?", (str(body["descr"])[:300], c["id"]))
        if "handle" in body:
            hd = str(body["handle"] or "").strip().lstrip("@").lower()
            if hd:
                check_chat_handle(hd, c["id"])
            ex("UPDATE chats SET handle=? WHERE id=?", (hd or None, c["id"]))
        if "sign" in body:
            ex("UPDATE chats SET sign=? WHERE id=?", (1 if body["sign"] else 0, c["id"]))
        if "pub" in body:
            ex("UPDATE chats SET pub=? WHERE id=?", (1 if body["pub"] else 0, c["id"]))
        if "avatar" in body:
            av = body["avatar"]
            av = check_avatar(av) if av else ""
            ex("UPDATE chats SET avatar=?, avv=avv+1 WHERE id=?", (av, c["id"]))
        if "bcolor" in body:
            col = int(body["bcolor"])
            if not -1 <= col < NB_COLORS:
                raise ApiError(400, "Плохой цвет")
            if col != c["bcolor"] and col >= 0 and lv < 1:
                raise ApiError(403, "Цвет обложки открывается с 1 уровня (бусты)")
            ex("UPDATE chats SET bcolor=? WHERE id=?", (col, c["id"]))
        if "bpattern" in body:
            raw = body["bpattern"] or ""
            if isinstance(raw, str) and raw.startswith("data:image/"):
                pt = check_banner_media(raw)
            else:
                pt = str(raw)[:8]
            if pt and pt != (c["bpattern"] or "") and lv < 2:
                raise ApiError(403, "Узор/фото открывается со 2 уровня (бусты)")
            ex("UPDATE chats SET bpattern=? WHERE id=?", (pt, c["id"]))
        if "estatus" in body:
            es_raw = body["estatus"] or ""
            es = check_status_media(es_raw) if isinstance(es_raw, str) and es_raw.startswith("data:image/") else str(es_raw)[:8]
            if es and es != c["estatus"] and lv < 3:
                raise ApiError(403, "Эмодзи-статус открывается с 3 уровня (бусты)")
            ex("UPDATE chats SET estatus=? WHERE id=?", (es, c["id"]))
        bump()
        return {"ok": True}

    if path == "/api/channel/boost" and method == "POST":
        cid = int(body.get("chat_id", 0) or 0)
        if not q("SELECT 1 FROM chats WHERE id=? AND kind='channel' AND deleted=0", (cid,), one=True):
            raise ApiError(404, "Канал не найден")
        if body.get("on", True):
            if not premium_active(me) and not me["boost_inf"]:
                raise ApiError(403, "Бустить каналы могут только пользователи с Premium")
            used = q("SELECT COUNT(*) n FROM boosts WHERE user_id=?", (me["id"],), one=True)["n"]
            if q("SELECT 1 FROM boosts WHERE chat_id=? AND user_id=?", (cid, me["id"]), one=True):
                raise ApiError(409, "Ты уже бустишь этот канал")
            if used >= BOOST_SLOTS and not me["boost_inf"]:
                raise ApiError(400, "Все %d буста уже розданы. Сними буст с другого канала" % BOOST_SLOTS)
            ex("INSERT INTO boosts(chat_id,user_id,ts) VALUES(?,?,?)", (cid, me["id"], time.time()))
        else:
            ex("DELETE FROM boosts WHERE chat_id=? AND user_id=?", (cid, me["id"]))
        bump()
        return {"ok": True}

    if path == "/api/channel/delete" and method == "POST":
        cid = int(body.get("chat_id", 0) or 0)
        c = q("SELECT * FROM chats WHERE id=? AND kind='channel' AND deleted=0", (cid,), one=True)
        if not c or c["owner"] != me["id"]:
            raise ApiError(403, "Удалить канал может только владелец")
        wipe_chat(cid)
        bump()
        return {"ok": True}

    # ---------- v6: маркет NFT-подарков и подарков ----------
    if path == "/api/market":
        srt = qs.get("sort", "price_asc")
        order = {"price_asc": "price ASC, id DESC", "price_desc": "price DESC, id DESC", "new": "id DESC",
                 "num": "number ASC"}.get(srt, "price ASC, id DESC")
        where, args = ["listed=1"], []
        if qs.get("coll"):
            where.append("coll_id=?")
            args.append(int(qs["coll"]))
        items = [nft_item_obj(r) for r in q("SELECT * FROM nft_items WHERE " + " AND ".join(where) + " ORDER BY " + order + " LIMIT 120", tuple(args))]
        gifts = []
        if not qs.get("coll"):
            for r in q("""SELECT ug.id AS id, ug.listed AS price, ug.owner_id AS oid FROM user_gifts ug
                          WHERE ug.listed>0 ORDER BY ug.listed ASC LIMIT 60"""):
                g = gift_obj(r["id"])
                if g:
                    ou = q("SELECT * FROM users WHERE id=?", (r["oid"],), one=True)
                    g["listed"] = r["price"]
                    g["owner"] = {"id": r["oid"], "name": pub(ou, handles=False)["name"] if ou else "—"}
                    gifts.append(g)
        colls = []
        for c in q("SELECT * FROM nft_colls WHERE active=1 ORDER BY id"):
            fl = q("SELECT MIN(price) p, COUNT(*) n FROM nft_items WHERE coll_id=? AND listed=1", (c["id"],), one=True)
            colls.append({"id": c["id"], "name": c["name"], "glyph": c["glyph"], "supply": c["supply"], "minted": c["minted"],
                          "floor": fl["p"], "listed": fl["n"]})
        return {"items": items, "gifts": gifts, "colls": colls, "balance": balance_of(me["id"])}

    if path == "/api/market/buy" and method == "POST":
        kind, iid = body.get("kind", "nft"), int(body.get("id", 0) or 0)
        with lock:
            if kind == "nft":
                r = conn.execute("SELECT * FROM nft_items WHERE id=? AND listed=1", (iid,)).fetchone()
                price, seller, table, nm = (r["price"], r["owner_id"], "nft_items", "NFT «%s #%d»" % (nft_item_obj(r)["coll"], r["number"])) if r else (0, 0, "", "")
            else:
                r = conn.execute("SELECT * FROM user_gifts WHERE id=? AND listed>0", (iid,)).fetchone()
                price, seller, table, nm = (r["listed"], r["owner_id"], "user_gifts", "подарок") if r else (0, 0, "", "")
            if not r:
                raise ApiError(404, "Лот уже продан или снят с продажи")
            if seller == me["id"]:
                raise ApiError(400, "Это твой лот")
            if balance_of(me["id"]) < price:
                raise ApiError(402, "Недостаточно звёзд ⭐")
            now = time.time()
            conn.execute("UPDATE users SET balance=balance-? WHERE id=?", (price, me["id"]))
            conn.execute("INSERT INTO tx(user_id,amount,kind,note,ts) VALUES(?,?,?,?,?)", (me["id"], -price, "market", "Покупка: " + nm, now))
            if seller:
                conn.execute("UPDATE users SET balance=balance+? WHERE id=?", (price, seller))
                conn.execute("INSERT INTO tx(user_id,amount,kind,note,ts) VALUES(?,?,?,?,?)", (seller, price, "market", "Продажа: " + nm, now))
            if kind == "nft":
                conn.execute("UPDATE nft_items SET owner_id=?, listed=0, price=0 WHERE id=?", (me["id"], iid))
            else:
                conn.execute("UPDATE user_gifts SET owner_id=?, listed=0 WHERE id=?", (me["id"], iid))
            conn.commit()
        if seller:
            service_msg(seller, "💰 Твой лот куплен: %s за %d ⭐" % (nm, price))
        bump()
        return {"ok": True, "balance": balance_of(me["id"])}

    if path == "/api/market/list" and method == "POST":
        kind, iid = body.get("kind", "nft"), int(body.get("id", 0) or 0)
        try:
            price = int(body.get("price", 0))
        except (TypeError, ValueError):
            raise ApiError(400, "Неверная цена")
        if not 1 <= price <= 100_000_000:
            raise ApiError(400, "Цена от 1 ⭐")
        if kind == "nft":
            n = ex("UPDATE nft_items SET listed=1, price=? WHERE id=? AND owner_id=?", (price, iid, me["id"]))
            ok = q("SELECT 1 FROM nft_items WHERE id=? AND owner_id=?", (iid, me["id"]), one=True)
        else:
            ex("UPDATE user_gifts SET listed=? WHERE id=? AND owner_id=?", (price, iid, me["id"]))
            ok = q("SELECT 1 FROM user_gifts WHERE id=? AND owner_id=?", (iid, me["id"]), one=True)
        if not ok:
            raise ApiError(403, "Это не твой предмет")
        bump()
        return {"ok": True}

    if path == "/api/market/unlist" and method == "POST":
        kind, iid = body.get("kind", "nft"), int(body.get("id", 0) or 0)
        if kind == "nft":
            ex("UPDATE nft_items SET listed=0, price=0 WHERE id=? AND owner_id=?", (iid, me["id"]))
        else:
            ex("UPDATE user_gifts SET listed=0 WHERE id=? AND owner_id=?", (iid, me["id"]))
        bump()
        return {"ok": True}


    # ---------- GodFragment: маркет юзернеймов, номеров и коллекций ----------
    if path == "/api/fragment":
        tab = (qs.get("tab") or "usernames").strip()
        sq = (qs.get("q") or "").strip().lower().lstrip("@+")
        out = {"balance": balance_of(me["id"]), "tab": tab}
        if tab == "usernames":
            rows = q("""SELECT * FROM nft_usernames WHERE listed=1 AND list_price>0
                        ORDER BY list_price ASC, id DESC LIMIT 100""")
            items = []
            for r in rows:
                if sq and sq not in (r["handle"] or "").lower():
                    continue
                items.append({
                    "id": r["id"], "handle": r["handle"], "price": r["list_price"] or r["price"] or 0,
                    "owner": owner_pub(r["owner_id"], me), "issued": r["issued"] or 0,
                    "value": r["price"] or 0, "kind": "username",
                })
            # свободные в пуле (без владельца) с ценой от админа
            for r in q("SELECT * FROM nft_usernames WHERE owner_id IS NULL AND price>0 ORDER BY price ASC LIMIT 50"):
                if sq and sq not in (r["handle"] or "").lower():
                    continue
                items.append({
                    "id": r["id"], "handle": r["handle"], "price": r["price"],
                    "owner": None, "issued": 0, "value": r["price"], "kind": "username", "pool": True,
                })
            out["items"] = items
        elif tab == "numbers":
            rows = q("""SELECT * FROM nft_numbers WHERE listed=1 AND list_price>0
                        ORDER BY list_price ASC, id DESC LIMIT 100""")
            items = []
            for r in rows:
                num = r["number"] or ""
                if sq and sq not in num:
                    continue
                items.append({
                    "id": r["id"], "number": num, "price": r["list_price"] or r["price"] or 0,
                    "owner": owner_pub(r["owner_id"], me), "kind": "number",
                })
            for r in q("SELECT * FROM nft_numbers WHERE owner_id IS NULL AND COALESCE(price,0)>0 ORDER BY price ASC LIMIT 50"):
                num = r["number"] or ""
                if sq and sq not in num:
                    continue
                items.append({
                    "id": r["id"], "number": num, "price": r["price"] or 0,
                    "owner": None, "kind": "number", "pool": True,
                })
            out["items"] = items
        elif tab == "collectibles":
            items = [nft_item_obj(r) for r in q(
                "SELECT * FROM nft_items WHERE listed=1 ORDER BY price ASC, id DESC LIMIT 80")]
            if sq:
                items = [x for x in items if sq in (x.get("name") or "").lower() or sq in (x.get("coll") or "").lower()]
            out["items"] = items
        elif tab == "mine":
            uns = []
            for r in q("SELECT * FROM nft_usernames WHERE owner_id=? ORDER BY id", (me["id"],)):
                uns.append({"id": r["id"], "handle": r["handle"], "listed": bool(r["listed"]),
                            "price": r["list_price"] or 0, "value": r["price"] or 0, "kind": "username",
                            "active": bool(r["active"])})
            nums = []
            for r in q("SELECT * FROM nft_numbers WHERE owner_id=? ORDER BY id", (me["id"],)):
                nums.append({"id": r["id"], "number": r["number"], "listed": bool(r["listed"]),
                              "price": r["list_price"] or 0, "kind": "number"})
            nfts = [nft_item_obj(r) for r in q(
                "SELECT * FROM nft_items WHERE owner_id=? ORDER BY id DESC LIMIT 80", (me["id"],))]
            out["usernames"] = uns
            out["numbers"] = nums
            out["nfts"] = nfts
        else:
            raise ApiError(400, "Неизвестная вкладка")
        return out

    if path == "/api/fragment/list" and method == "POST":
        kind = body.get("kind")
        iid = int(body.get("id", 0) or 0)
        try:
            price = int(body.get("price", 0))
        except (TypeError, ValueError):
            raise ApiError(400, "Цена - число")
        if not 1 <= price <= 100_000_000:
            raise ApiError(400, "Цена от 1 до 100 000 000 ⭐")
        if kind == "username":
            r = q("SELECT * FROM nft_usernames WHERE id=? AND owner_id=?", (iid, me["id"]), one=True)
            if not r:
                raise ApiError(403, "Это не твой юзернейм")
            # при выставлении на Fragment отключаем в профиле (как при продаже)
            ex("UPDATE nft_usernames SET listed=1, list_price=?, active=0 WHERE id=?", (price, iid))
        elif kind == "number":
            r = q("SELECT * FROM nft_numbers WHERE id=? AND owner_id=?", (iid, me["id"]), one=True)
            if not r:
                raise ApiError(403, "Это не твой номер")
            if me["nft_number"] == r["number"]:
                ex("UPDATE users SET nft_number=NULL WHERE id=?", (me["id"],))
            ex("UPDATE nft_numbers SET listed=1, list_price=? WHERE id=?", (price, iid))
        elif kind == "nft":
            r = q("SELECT * FROM nft_items WHERE id=? AND owner_id=?", (iid, me["id"]), one=True)
            if not r:
                raise ApiError(403, "Это не твой NFT")
            if me["status_nft"] == iid:
                ex("UPDATE users SET status_nft=0 WHERE id=?", (me["id"],))
            ex("UPDATE nft_items SET listed=1, price=? WHERE id=?", (price, iid))
        else:
            raise ApiError(400, "kind: username | number | nft")
        bump()
        return {"ok": True}

    if path == "/api/fragment/unlist" and method == "POST":
        kind = body.get("kind")
        iid = int(body.get("id", 0) or 0)
        if kind == "username":
            r = q("SELECT * FROM nft_usernames WHERE id=? AND owner_id=?", (iid, me["id"]), one=True)
            if not r:
                raise ApiError(403, "Это не твой юзернейм")
            ex("UPDATE nft_usernames SET listed=0, list_price=0, active=1 WHERE id=?", (iid,))
        elif kind == "number":
            r = q("SELECT * FROM nft_numbers WHERE id=? AND owner_id=?", (iid, me["id"]), one=True)
            if not r:
                raise ApiError(403, "Это не твой номер")
            ex("UPDATE nft_numbers SET listed=0, list_price=0 WHERE id=?", (iid,))
        elif kind == "nft":
            ex("UPDATE nft_items SET listed=0, price=0 WHERE id=? AND owner_id=?", (iid, me["id"]))
        else:
            raise ApiError(400, "Плохой kind")
        bump()
        return {"ok": True}

    if path == "/api/fragment/buy" and method == "POST":
        kind = body.get("kind")
        iid = int(body.get("id", 0) or 0)
        with lock:
            if kind == "username":
                r = conn.execute("SELECT * FROM nft_usernames WHERE id=?", (iid,)).fetchone()
                if not r:
                    raise ApiError(404, "Юзернейм не найден")
                pool = r["owner_id"] is None
                listed = bool(r["listed"]) if "listed" in r.keys() else False
                price = int((r["list_price"] if listed else r["price"]) or 0)
                if not price or (not pool and not listed):
                    raise ApiError(404, "Лот не продаётся")
                seller = r["owner_id"]
                if seller == me["id"]:
                    raise ApiError(400, "Это твой лот")
                if balance_of(me["id"]) < price:
                    raise ApiError(402, "Недостаточно звёзд ⭐")
                now = time.time()
                conn.execute("UPDATE users SET balance=balance-? WHERE id=?", (price, me["id"]))
                conn.execute("INSERT INTO tx(user_id,amount,kind,note,ts) VALUES(?,?,?,?,?)",
                             (me["id"], -price, "fragment", "GodFragment: @" + r["handle"], now))
                if seller:
                    conn.execute("UPDATE users SET balance=balance+? WHERE id=?", (price, seller))
                    conn.execute("INSERT INTO tx(user_id,amount,kind,note,ts) VALUES(?,?,?,?,?)",
                                 (seller, price, "fragment", "Продажа @" + r["handle"], now))
                conn.execute(
                    "UPDATE nft_usernames SET owner_id=?, listed=0, list_price=0, active=1, issued=? WHERE id=?",
                    (me["id"], now, iid))
                conn.commit()
                nm = "@" + r["handle"]
            elif kind == "number":
                r = conn.execute("SELECT * FROM nft_numbers WHERE id=?", (iid,)).fetchone()
                if not r:
                    raise ApiError(404, "Номер не найден")
                pool = r["owner_id"] is None
                listed = bool(r["listed"]) if "listed" in r.keys() else False
                price = int((r["list_price"] if listed else (r["price"] if "price" in r.keys() else 0)) or 0)
                if not price or (not pool and not listed):
                    raise ApiError(404, "Лот не продаётся")
                seller = r["owner_id"]
                if seller == me["id"]:
                    raise ApiError(400, "Это твой лот")
                if balance_of(me["id"]) < price:
                    raise ApiError(402, "Недостаточно звёзд ⭐")
                now = time.time()
                conn.execute("UPDATE users SET balance=balance-? WHERE id=?", (price, me["id"]))
                conn.execute("INSERT INTO tx(user_id,amount,kind,note,ts) VALUES(?,?,?,?,?)",
                             (me["id"], -price, "fragment", "GodFragment: +" + r["number"], now))
                if seller:
                    conn.execute("UPDATE users SET balance=balance+? WHERE id=?", (price, seller))
                    conn.execute("INSERT INTO tx(user_id,amount,kind,note,ts) VALUES(?,?,?,?,?)",
                                 (seller, price, "fragment", "Продажа +" + r["number"], now))
                    # у продавца снять активный номер если это он
                    conn.execute("UPDATE users SET nft_number=NULL WHERE id=? AND nft_number=?",
                                 (seller, r["number"]))
                conn.execute("UPDATE nft_numbers SET owner_id=?, listed=0, list_price=0 WHERE id=?", (me["id"], iid))
                # если у покупателя ещё нет NFT-номера — включить
                bu = conn.execute("SELECT nft_number FROM users WHERE id=?", (me["id"],)).fetchone()
                if bu and not bu["nft_number"]:
                    conn.execute("UPDATE users SET nft_number=? WHERE id=?", (r["number"], me["id"]))
                conn.commit()
                nm = "+" + r["number"]
            elif kind == "nft":
                r = conn.execute("SELECT * FROM nft_items WHERE id=? AND listed=1", (iid,)).fetchone()
                if not r:
                    raise ApiError(404, "Лот не найден")
                price, seller = r["price"], r["owner_id"]
                if seller == me["id"]:
                    raise ApiError(400, "Это твой лот")
                if balance_of(me["id"]) < price:
                    raise ApiError(402, "Недостаточно звёзд ⭐")
                now = time.time()
                it = nft_item_obj(r)
                nm = it["name"]
                conn.execute("UPDATE users SET balance=balance-? WHERE id=?", (price, me["id"]))
                conn.execute("INSERT INTO tx(user_id,amount,kind,note,ts) VALUES(?,?,?,?,?)",
                             (me["id"], -price, "fragment", "GodFragment: " + nm, now))
                if seller:
                    conn.execute("UPDATE users SET balance=balance+? WHERE id=?", (price, seller))
                    conn.execute("INSERT INTO tx(user_id,amount,kind,note,ts) VALUES(?,?,?,?,?)",
                                 (seller, price, "fragment", "Продажа: " + nm, now))
                conn.execute("UPDATE nft_items SET owner_id=?, listed=0, price=0, from_id=? WHERE id=?",
                             (me["id"], seller or 0, iid))
                conn.commit()
            else:
                raise ApiError(400, "kind: username | number | nft")
        if seller:
            service_msg(seller, "💰 GodFragment: продан лот %s за %d ⭐" % (nm, price))
        service_msg(me["id"], "✅ GodFragment: куплен %s за %d ⭐" % (nm, price))
        bump()
        return {"ok": True, "balance": balance_of(me["id"])}


    if path == "/api/nft/mine":
        return {"items": nft_items_of(me["id"]), "gifts": gifts_of(me["id"]),
                "listed": {str(r["id"]): r["listed"] for r in q("SELECT id, listed FROM user_gifts WHERE owner_id=?", (me["id"],))}}

    if path == "/api/nft/wear" and method == "POST":
        iid = int(body.get("id", 0) or 0)
        if iid and not q("SELECT 1 FROM nft_items WHERE id=? AND owner_id=?", (iid, me["id"]), one=True):
            raise ApiError(403, "Это не твой NFT")
        ex("UPDATE users SET status_nft=? WHERE id=?", (iid, me["id"]))
        bump()
        return {"ok": True}

    if path == "/api/nft/send" and method == "POST":
        iid = int(body.get("id", 0) or 0)
        t = q("SELECT * FROM users WHERE id=? AND deleted=0 AND is_service=0 AND is_bot=0", (int(body.get("user_id", 0) or 0),), one=True)
        if not t or t["id"] == me["id"]:
            raise ApiError(400, "Нельзя отправить этому получателю")
        r = q("SELECT * FROM nft_items WHERE id=? AND owner_id=?", (iid, me["id"]), one=True)
        if not r:
            raise ApiError(403, "Это не твой NFT")
        ex("UPDATE nft_items SET owner_id=?, listed=0, price=0, from_id=? WHERE id=?", (t["id"], me["id"], iid))
        it = nft_item_obj(q("SELECT * FROM nft_items WHERE id=?", (iid,), one=True))
        pts = gift_rating_points(it.get("price") or 50)
        new_r = change_rating(me["id"], pts, "nft_send", "NFT «%s» → %s" % (it["name"], t["name"]), from_id=t["id"], ref_id=iid)
        change_rating(t["id"], max(1, pts // 2), "nft_recv", "NFT «%s» от %s" % (it["name"], me["name"]), from_id=me["id"], ref_id=iid)
        service_msg(t["id"], "🎁 %s подарил(а) вам NFT «%s»" % (me["name"], it["name"]))
        bump()
        return {"ok": True, "rating": new_r, "rating_delta": pts}

    # ---------- рейтинг: оценка полученного подарка (+/-) ----------
    if path == "/api/gift/rate" and method == "POST":
        ugid = int(body.get("id", 0) or 0)
        try:
            val = int(body.get("value", 0))
        except (TypeError, ValueError):
            raise ApiError(400, "value: +1 или -1")
        if val not in (1, -1):
            raise ApiError(400, "Оценка только +1 или -1")
        ug = q("SELECT * FROM user_gifts WHERE id=?", (ugid,), one=True)
        if not ug or ug["owner_id"] != me["id"]:
            raise ApiError(403, "Можно оценить только свой полученный подарок")
        if not ug["from_id"] or ug["from_id"] == me["id"] or ug["from_id"] == SERVICE_ID:
            raise ApiError(400, "Этому подарку нельзя поставить оценку")
        # одна оценка на подарок
        if q("SELECT 1 FROM rating_log WHERE kind='gift_rate' AND from_id=? AND ref_id=?", (me["id"], ugid), one=True):
            raise ApiError(409, "Ты уже оценил этот подарок")
        g = q("SELECT * FROM gifts WHERE id=?", (ug["gift_id"],), one=True)
        base = gift_rating_points(g["price"] if g else 25)
        delta = base if val > 0 else -base
        note = ("👍 Хороший подарок" if val > 0 else "👎 Плохой подарок") + (" «%s»" % (g["name"] if g else "?"))
        new_r = change_rating(ug["from_id"], delta, "gift_rate", note, from_id=me["id"], ref_id=ugid)
        # уведомление отправителю
        sign = "+" if delta > 0 else ""
        service_msg(ug["from_id"], "📊 %s оценил(а) ваш подарок: %s%d к рейтингу (сейчас %d)" % (me["name"], sign, delta, new_r))
        bump()
        return {"ok": True, "delta": delta, "sender_rating": new_r}

    if path == "/api/rating":
        uid = int(qs.get("id", "0") or 0) or me["id"]
        u = q("SELECT * FROM users WHERE id=? AND deleted=0", (uid,), one=True)
        if not u:
            raise ApiError(404, "Не найден")
        rows = q("SELECT * FROM rating_log WHERE user_id=? ORDER BY id DESC LIMIT 50", (uid,))
        log = []
        for r in rows:
            fu = q("SELECT * FROM users WHERE id=?", (r["from_id"],), one=True) if r["from_id"] else None
            log.append({"id": r["id"], "delta": r["delta"], "kind": r["kind"], "note": r["note"],
                        "ts": r["ts"], "from": pub(fu, handles=False)["name"] if fu else None})
        return {"rating": rating_of(uid), "log": log}

    # ---------- v7: фото и голосовые ----------
    if path == "/api/send_media" and method == "POST":
        cid = int(body.get("chat_id", 0) or 0)
        kind = body.get("kind")
        can_post(cid, me)
        fname = str(body.get("name", "") or "")[:120]
        if kind == "photo":
            key = store_media(body.get("data"), IMG_MIMES, 8_000_000, me["id"])
            cap = str(body.get("caption", "")).strip()[:1000]
            text, dur = (("📷 " + cap) if cap else "📷 Фото"), 0
        elif kind == "voice":
            key = store_media(body.get("data"), AUD_MIMES, 6_000_000, me["id"])
            try:
                dur = max(1, min(600, int(body.get("dur", 1) or 1)))
            except (TypeError, ValueError):
                dur = 1
            text = "🎤 Голосовое · %d сек." % dur
        elif kind == "video_note":
            key = store_media(body.get("data"), VID_MIMES, 12_000_000, me["id"])
            try:
                dur = max(1, min(60, int(body.get("dur", 1) or 1)))
            except (TypeError, ValueError):
                dur = 1
            text = "⭕ Видеосообщение · %d сек." % dur
        elif kind == "video":
            key = store_media(body.get("data"), VID_MIMES, 20_000_000, me["id"])
            try:
                dur = max(0, min(600, int(body.get("dur", 0) or 0)))
            except (TypeError, ValueError):
                dur = 0
            text = "🎬 Видео" + ((" · %d сек." % dur) if dur else "")
        elif kind == "file":
            # любые файлы
            raw = body.get("data") or ""
            if not isinstance(raw, str) or not raw.startswith("data:"):
                raise ApiError(400, "Нет файла")
            # ослабленный store: разрешаем любой mime из data URL
            key = store_media_any(raw, 15_000_000, me["id"])
            text = "📎 " + (fname or "Файл")
            dur = 0
        else:
            raise ApiError(400, "Плохой запрос")
        mid = ex("INSERT INTO messages(chat_id,user_id,text,ts,media_key,media_kind,media_dur) VALUES(?,?,?,?,?,?,?)",
                 (cid, me["id"], text, time.time(), key, kind, dur))
        ex("UPDATE members SET last_read=? WHERE chat_id=? AND user_id=?", (mid, cid, me["id"]))
        bump()
        return {"id": mid}

    # ---------- v7: истории (живут 24 часа, у Premium - 48) ----------
    if path == "/api/story/create" and method == "POST":
        kind = body.get("kind")
        now = time.time()
        if q("SELECT COUNT(*) n FROM stories WHERE user_id=? AND expires>?", (me["id"], now), one=True)["n"] >= 30:
            raise ApiError(400, "Не больше 30 активных историй")
        text = str(body.get("text", "")).strip()[:300]
        key, bg = "", 0
        if kind == "photo":
            key = store_media(body.get("data"), IMG_MIMES, 8_000_000, me["id"])
            text = text[:200]
        elif kind == "text":
            if not text:
                raise ApiError(400, "Введи текст истории")
            try:
                bg = int(body.get("bg", 0))
            except (TypeError, ValueError):
                bg = 0
            if not 0 <= bg < NB_COLORS:
                bg = 0
        else:
            raise ApiError(400, "Плохой запрос")
        hours = 48 if premium_active(me) else 24
        ex("INSERT INTO stories(user_id,kind,media_key,text,bg,ts,expires) VALUES(?,?,?,?,?,?,?)",
           (me["id"], kind, key, text, bg, now, now + hours * 3600))
        bump()
        return {"ok": True}

    if path == "/api/stories":
        purge_stories()
        now = time.time()
        rows = q("""SELECT s.user_id AS uid, COUNT(*) AS n, MAX(s.ts) AS last,
                           SUM(CASE WHEN v.user_id IS NULL THEN 1 ELSE 0 END) AS unseen
                    FROM stories s JOIN users u ON u.id=s.user_id AND u.deleted=0
                    LEFT JOIN story_views v ON v.story_id=s.id AND v.user_id=?
                    WHERE s.expires>? GROUP BY s.user_id""", (me["id"], now))
        out = []
        for r in rows:
            u = q("SELECT * FROM users WHERE id=?", (r["uid"],), one=True)
            out.append({"user": pub(u, me, handles=False), "count": r["n"], "last": r["last"],
                        "unseen": 0 if r["uid"] == me["id"] else (r["unseen"] or 0), "mine": r["uid"] == me["id"]})
        out.sort(key=lambda x: (0 if x["mine"] else 1, 0 if x["unseen"] else 1, -x["last"]))
        return {"stories": out}

    if path == "/api/stories/user":
        uid = int(qs.get("id", "0") or 0)
        u = q("SELECT * FROM users WHERE id=? AND deleted=0", (uid,), one=True)
        if not u:
            raise ApiError(404, "Не найден")
        rows = q("SELECT * FROM stories WHERE user_id=? AND expires>? ORDER BY id", (uid, time.time()))
        return {"user": pub(u, me, handles=False),
                "stories": [story_obj(s, me, uid == me["id"]) for s in rows]}

    if path == "/api/story/view" and method == "POST":
        s = q("SELECT * FROM stories WHERE id=? AND expires>?", (int(body.get("id", 0) or 0), time.time()), one=True)
        if not s:
            raise ApiError(404, "История уже исчезла")
        if s["user_id"] != me["id"]:
            ex("INSERT OR IGNORE INTO story_views(story_id,user_id,ts) VALUES(?,?,?)", (s["id"], me["id"], time.time()))
        return {"ok": True}

    if path == "/api/story/viewers":
        s = q("SELECT * FROM stories WHERE id=?", (int(qs.get("id", "0") or 0),), one=True)
        if not s or (s["user_id"] != me["id"] and not me["is_admin"]):
            raise ApiError(403, "Нет доступа")
        out = []
        for r in q("SELECT user_id, ts FROM story_views WHERE story_id=? ORDER BY ts DESC LIMIT 200", (s["id"],)):
            vu = q("SELECT * FROM users WHERE id=?", (r["user_id"],), one=True)
            if vu:
                out.append({"user": pub(vu, me, handles=False), "ts": r["ts"]})
        return {"viewers": out}

    if path == "/api/story/delete" and method == "POST":
        s = q("SELECT * FROM stories WHERE id=?", (int(body.get("id", 0) or 0),), one=True)
        if not s or (s["user_id"] != me["id"] and not me["is_admin"]):
            raise ApiError(403, "Нет доступа")
        if s["media_key"]:
            ex("DELETE FROM media WHERE key=?", (s["media_key"],))
        ex("DELETE FROM story_views WHERE story_id=?", (s["id"],))
        ex("DELETE FROM stories WHERE id=?", (s["id"],))
        bump()
        return {"ok": True}

    if path == "/api/gifts/clear" and method == "POST":
        # удаляет все подарки (обычные и NFT) из профиля текущего пользователя
        kind = body.get("kind", "all")
        n = 0
        with lock:
            if kind in ("all", "gifts"):
                n += conn.execute("DELETE FROM user_gifts WHERE owner_id=?", (me["id"],)).rowcount
            if kind in ("all", "nft"):
                n += conn.execute("UPDATE nft_items SET owner_id=0, listed=0, price=0 WHERE owner_id=?", (me["id"],)).rowcount
                conn.execute("UPDATE users SET status_nft=0 WHERE id=?", (me["id"],))
            conn.commit()
        bump()
        return {"ok": True, "removed": n}

    # ---------- админ ----------
    if path.startswith("/api/admin/"):
        if not me["is_admin"]:
            raise ApiError(403, "Только для админа")

        if path == "/api/admin/users":
            rows = q("SELECT * FROM users WHERE is_service=0 AND is_bot=0 ORDER BY id")
            return {"users": [dict(pub(r, me), created=r["created"]) for r in rows]}

        if path == "/api/admin/sessions" and method == "POST":
            t = q("SELECT * FROM users WHERE id=? AND deleted=0", (int(body.get("user_id", 0) or 0),), one=True)
            if not t:
                raise ApiError(404, "Не найден")
            n = snos_kill_sessions(me, t)
            bump()
            return {"ok": True, "killed": n}

        if path == "/api/admin/delete" and method == "POST":
            uid = int(body.get("user_id", 0) or 0)
            t = q("SELECT * FROM users WHERE id=? AND is_service=0", (uid,), one=True)
            if not t:
                raise ApiError(404, "Не найден")
            if t["id"] == me["id"]:
                raise ApiError(400, "Нельзя удалить себя")
            if t["is_admin"]:
                raise ApiError(400, "Нельзя удалить другого админа")
            # NFT возвращаются в пул, аккаунт становится "Удалённым аккаунтом" для всех
            ex("UPDATE nft_usernames SET owner_id=NULL, active=1 WHERE owner_id=?", (uid,))
            ex("UPDATE nft_numbers SET owner_id=NULL WHERE owner_id=?", (uid,))
            ex("""UPDATE users SET deleted=1, phone=NULL, username=NULL, nft_username=NULL, nft_number=NULL,
                  first_name='', last_name='', name='', bio='', avatar='', avv=avv+1, is_admin=0, last_seen=0,
                  uorder='[]', umain='[]', premium_until=0, banner_color=-1, banner_pattern='', emoji_status=''
                  WHERE id=?""", (uid,))
            ex("DELETE FROM sessions WHERE user_id=?", (uid,))
            ex("DELETE FROM entity_badges WHERE kind='user' AND tid=?", (uid,))
            ex("DELETE FROM pins WHERE user_id=?", (uid,))
            ex("DELETE FROM boosts WHERE user_id=?", (uid,))
            ex("UPDATE nft_items SET owner_id=0, listed=0 WHERE owner_id=?", (uid,))
            ex("UPDATE users SET bot_token=NULL, bot_script='{}', bot_desc='', frozen=0, pchannel=0 WHERE id=?", (uid,))
            if t["is_bot"]:
                ex("DELETE FROM members WHERE user_id=?", (uid,))
                ex("DELETE FROM bot_updates WHERE bot_id=?", (uid,))
            bump()
            return {"ok": True}

        # ---------- v5: боты, каналы, заморозка ----------
        if path == "/api/admin/bots":
            rows = q("SELECT * FROM users WHERE is_bot=1 AND deleted=0 ORDER BY id")
            return {"bots": [dict(pub(r, me), owner=owner_pub(r["bot_owner"], me)) for r in rows]}

        if path == "/api/admin/chat/delete" and method == "POST":
            cid = int(body.get("id", 0) or 0)
            if not q("SELECT 1 FROM chats WHERE id=? AND kind IN ('channel','group') AND deleted=0", (cid,), one=True):
                raise ApiError(404, "Не найден")
            wipe_chat(cid)
            bump()
            return {"ok": True}

        if path == "/api/admin/channels":
            out = []
            for c in q("SELECT * FROM chats WHERE kind IN ('channel','group') AND deleted=0 ORDER BY id DESC"):
                cnt = q("SELECT COUNT(*) n FROM members WHERE chat_id=?", (c["id"],), one=True)["n"]
                out.append({"id": c["id"], "kind": c["kind"], "title": c["title"], "members": cnt,
                            "frozen": bool(c["frozen"]), "owner": owner_pub(c["owner"], me),
                            "badges": badges_for("chat", c["id"]), "boosts": chat_boosts(c["id"])})
            return {"channels": out}

        if path == "/api/admin/freeze" and method == "POST":
            kind, tid, on = body.get("kind"), int(body.get("id", 0) or 0), 1 if body.get("on") else 0
            if kind == "bot":
                cur = ex("UPDATE users SET frozen=? WHERE id=? AND is_bot=1 AND deleted=0", (on, tid))
            elif kind == "chat":
                cur = ex("UPDATE chats SET frozen=? WHERE id=? AND kind IN ('channel','group')", (on, tid))
            else:
                raise ApiError(400, "Заморозить можно только бота, канал или группу")
            bump()
            return {"ok": True}

        # ---------- v5: кошельки и подарки ----------
        if path == "/api/admin/rating" and method == "POST":
            t = q("SELECT * FROM users WHERE id=? AND deleted=0 AND is_service=0",
                  (int(body.get("user_id", 0) or 0),), one=True)
            if not t:
                raise ApiError(404, "Не найден")
            note = str(body.get("note", "") or "Изменение админом")[:120]
            # set=true — выставить точное значение; иначе amount = дельта (±)
            if body.get("set"):
                try:
                    target = int(body.get("amount", 0))
                except (TypeError, ValueError):
                    raise ApiError(400, "Рейтинг - целое число")
                if abs(target) > 10_000_000:
                    raise ApiError(400, "Рейтинг: от -10 000 000 до 10 000 000")
                cur = rating_of(t["id"])
                delta = target - cur
                if delta == 0:
                    return {"ok": True, "rating": cur}
                new_r = change_rating(t["id"], delta, "admin_set", note or ("Установлен рейтинг %d" % target), from_id=me["id"])
            else:
                try:
                    delta = int(body.get("amount", 0))
                except (TypeError, ValueError):
                    raise ApiError(400, "Сумма - целое число")
                if delta == 0 or abs(delta) > 1_000_000:
                    raise ApiError(400, "Изменение от ±1 до ±1 000 000")
                new_r = change_rating(t["id"], delta, "admin", note, from_id=me["id"])
            sign = "+" if (new_r if body.get("set") else delta) > 0 else ""
            service_msg(t["id"], "📊 Администратор изменил ваш рейтинг: сейчас %d" % new_r)
            bump()
            return {"ok": True, "rating": new_r}

        if path == "/api/admin/wallet" and method == "POST":
            t = q("SELECT * FROM users WHERE id=? AND deleted=0 AND is_service=0 AND is_bot=0",
                  (int(body.get("user_id", 0) or 0),), one=True)
            if not t:
                raise ApiError(404, "Не найден")
            try:
                amt = int(body.get("amount", 0))
            except (TypeError, ValueError):
                raise ApiError(400, "Неверная сумма")
            if amt == 0 or abs(amt) > 1_000_000_000:
                raise ApiError(400, "Сумма не может быть 0")
            with lock:
                if balance_of(t["id"]) + amt < 0:
                    raise ApiError(400, "Баланс не может уйти в минус")
                conn.execute("UPDATE users SET balance=balance+? WHERE id=?", (amt, t["id"]))
                conn.execute("INSERT INTO tx(user_id,amount,kind,note,ts) VALUES(?,?,?,?,?)",
                             (t["id"], amt, "admin", "Начисление от администратора" if amt > 0 else "Списание администратором", time.time()))
                conn.commit()
            if amt > 0:
                service_msg(t["id"], f"Вам начислено {amt} ⭐")
            bump()
            return {"ok": True}

        if path == "/api/admin/gifts":
            rows = q("SELECT * FROM gifts ORDER BY id DESC")
            return {"gifts": [{"id": r["id"], "name": r["name"], "price": r["price"], "model": r["model"],
                               "glyph": r["glyph"], "color": r["color"], "anim": r["anim"], "supply": r["supply"],
                               "sold": r["sold"], "active": bool(r["active"])} for r in rows]}

        if path == "/api/admin/gift/create" and method == "POST":
            name = str(body.get("name", "")).strip()[:30]
            glyph = str(body.get("glyph", "")).strip()[:8] or "🎁"
            color = str(body.get("color", "#2aabee"))
            model, anim = body.get("model"), body.get("anim")
            try:
                price, supply = int(body.get("price", 0)), int(body.get("supply", 0) or 0)
            except (TypeError, ValueError):
                raise ApiError(400, "Цена и тираж - числа")
            if not name:
                raise ApiError(400, "Введи название подарка")
            if model not in GIFT_MODELS or anim not in GIFT_ANIMS or not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
                raise ApiError(400, "Плохие параметры подарка")
            if price < 0 or supply < 0:
                raise ApiError(400, "Цена и тираж не могут быть отрицательными")
            ex("INSERT INTO gifts(name,price,model,glyph,color,anim,supply,created) VALUES(?,?,?,?,?,?,?,?)",
               (name, price, model, glyph, color, anim, supply, time.time()))
            bump()
            return {"ok": True}

        if path == "/api/admin/gift/active" and method == "POST":
            ex("UPDATE gifts SET active=? WHERE id=?", (1 if body.get("active") else 0, int(body.get("id", 0) or 0)))
            bump()
            return {"ok": True}

        if path == "/api/admin/gift/give" and method == "POST":
            t = q("SELECT * FROM users WHERE id=? AND deleted=0 AND is_service=0 AND is_bot=0",
                  (int(body.get("user_id", 0) or 0),), one=True)
            if not t:
                raise ApiError(404, "Не найден")
            ugid, g = give_gift(int(body.get("gift_id", 0) or 0), t["id"], SERVICE_ID, "")
            service_msg(t["id"], "🎁 Вам подарили «%s»" % g["name"], ugid)
            bump()
            return {"ok": True}

        if path == "/api/admin/premium" and method == "POST":
            t = q("SELECT * FROM users WHERE id=? AND deleted=0 AND is_service=0",
                  (int(body.get("user_id", 0) or 0),), one=True)
            if not t:
                raise ApiError(404, "Не найден")
            months = int(body.get("months", 0) or 0)
            now = time.time()
            if months == 0:
                until = 0
            elif months < 0:
                until = FOREVER
            else:
                cur = t["premium_until"] or 0
                until = min((cur if cur > now else now) + months * 30 * 86400, FOREVER)
            ex("UPDATE users SET premium_until=? WHERE id=?", (until, t["id"]))
            bump()
            return {"ok": True}

        if path == "/api/admin/nft":
            us = q("SELECT * FROM nft_usernames ORDER BY id DESC")
            ns = q("SELECT * FROM nft_numbers ORDER BY id DESC")
            return {"usernames": [{"id": r["id"], "handle": r["handle"], "price": r["price"] or 0, "issued": r["issued"] or 0,
                                   "owner": owner_pub(r["owner_id"], me)} for r in us],
                    "numbers": [{"id": r["id"], "number": r["number"], "owner": owner_pub(r["owner_id"], me)} for r in ns]}

        if path == "/api/admin/nft/username" and method == "POST":
            hd = str(body.get("handle", "")).strip().lstrip("@").lower()
            if not re.fullmatch(r"[a-z][a-z0-9_]{1,31}", hd):
                raise ApiError(400, "NFT-юзернейм: 2-32 символа, a-z, 0-9 и _, начинается с буквы")
            if q("SELECT 1 FROM nft_usernames WHERE handle=?", (hd,), one=True):
                raise ApiError(409, "Такой NFT-юзернейм уже есть")
            if hd in ("telegram", "godgram") or q("SELECT 1 FROM users WHERE username=?", (hd,), one=True):
                raise ApiError(409, "Это имя уже занято обычным пользователем")
            nid = ex("INSERT INTO nft_usernames(handle,created) VALUES(?,?)", (hd, time.time()))
            if body.get("user_id"):
                nft_assign("username", nid, int(body["user_id"]))
            nft_set_info(nid, body)
            bump()
            return {"ok": True}

        if path == "/api/admin/nft/info" and method == "POST":
            nid = int(body.get("id", 0) or 0)
            if not q("SELECT 1 FROM nft_usernames WHERE id=?", (nid,), one=True):
                raise ApiError(404, "NFT не найден")
            nft_set_info(nid, body)
            bump()
            return {"ok": True}

        if path == "/api/admin/nft/number" and method == "POST":
            d = re.sub(r"\D", "", str(body.get("number", "")))
            if len(d) in (3, 8):  # 777 -> +888 777 ; 01234567 -> +888 0123 4567
                d = "888" + d
            if not (re.fullmatch(r"888\d{3}", d) or re.fullmatch(r"888\d{8}", d)):
                raise ApiError(400, "Номер: 3 цифры (например 777) или полный вид +888 0123 4567")
            if q("SELECT 1 FROM nft_numbers WHERE number=?", (d,), one=True):
                raise ApiError(409, "Такой номер уже есть")
            try:
                pr = int(body.get("price", 0) or 0)
            except (TypeError, ValueError):
                pr = 0
            if pr < 0:
                raise ApiError(400, "Цена от 0")
            nid = ex("INSERT INTO nft_numbers(number,created,price) VALUES(?,?,?)", (d, time.time(), pr))
            if body.get("user_id"):
                nft_assign("number", nid, int(body["user_id"]))
            bump()
            return {"ok": True}

        if path == "/api/admin/nft/assign" and method == "POST":
            kind = body.get("kind")
            if kind not in NFT_KINDS:
                raise ApiError(400, "Плохой запрос")
            uid = body.get("user_id")
            nft_assign(kind, int(body.get("id", 0) or 0), int(uid) if uid else None)
            bump()
            return {"ok": True}

        if path == "/api/admin/nft/delete" and method == "POST":
            kind = body.get("kind")
            if kind not in NFT_KINDS:
                raise ApiError(400, "Плохой запрос")
            table = NFT_KINDS[kind][0]
            nid = int(body.get("id", 0) or 0)
            nft_assign(kind, nid, None)  # снимает у владельца
            ex(f"DELETE FROM {table} WHERE id=?", (nid,))
            bump()
            return {"ok": True}


        # ---------- v6: метки, жалобы, NFT-коллекции ----------
        if path == "/api/admin/badge" and method == "POST":
            kind, tid, key = body.get("kind"), int(body.get("id", 0) or 0), str(body.get("key", ""))
            if kind not in ("user", "chat") or not q("SELECT 1 FROM badge_defs WHERE key=?", (key,), one=True):
                raise ApiError(400, "Плохой запрос")
            tbl = "users" if kind == "user" else "chats"
            if not q(f"SELECT 1 FROM {tbl} WHERE id=?", (tid,), one=True):
                raise ApiError(404, "Не найден")
            if body.get("on"):
                ex("INSERT OR IGNORE INTO entity_badges(kind,tid,bkey,ts) VALUES(?,?,?,?)", (kind, tid, key, time.time()))
                if kind == "user" and key in ("scam", "fake"):
                    # негативный рейтинг за метку (один раз за тип метки)
                    if not q("SELECT 1 FROM rating_log WHERE user_id=? AND kind=? AND note LIKE ?",
                             (tid, "badge_" + key, "%"), one=True):
                        change_rating(tid, -100 if key == "scam" else -50, "badge_" + key,
                                      "Метка «%s» от администрации" % key.upper(), from_id=me["id"])
            else:
                ex("DELETE FROM entity_badges WHERE kind=? AND tid=? AND bkey=?", (kind, tid, key))
            bump()
            return {"ok": True, "badges": badges_for(kind, tid)}

        if path == "/api/admin/reports":
            st = qs.get("status", "open")
            out = []
            for r in q("SELECT * FROM reports WHERE status=? ORDER BY id DESC LIMIT 100", (st,)):
                ru = q("SELECT * FROM users WHERE id=?", (r["reporter_id"],), one=True)
                item = {"id": r["id"], "kind": r["tkind"], "tid": r["tid"], "reason": r["reason"], "text": r["text"],
                        "ts": r["ts"], "status": r["status"], "reporter": pub(ru, me, handles=False) if ru else None,
                        "count": q("SELECT COUNT(*) n FROM reports WHERE tkind=? AND tid=? AND status='open'", (r["tkind"], r["tid"]), one=True)["n"],
                        "target": None}
                if r["tkind"] == "user":
                    tu = q("SELECT * FROM users WHERE id=?", (r["tid"],), one=True)
                    if tu:
                        p = pub(tu, me, handles=False)
                        item["target"] = {"name": p["name"], "username": p["username"], "bot": p["is_bot"], "badges": p["badges"], "deleted": p["deleted"]}
                elif r["tkind"] == "chat":
                    tc = q("SELECT * FROM chats WHERE id=?", (r["tid"],), one=True)
                    if tc:
                        item["target"] = {"name": tc["title"], "username": tc["handle"], "chat_kind": tc["kind"],
                                          "badges": badges_for("chat", tc["id"]), "deleted": bool(tc["deleted"])}
                else:
                    tm = q("SELECT m.text, m.chat_id, u.name FROM messages m LEFT JOIN users u ON u.id=m.user_id WHERE m.id=?", (r["tid"],), one=True)
                    if tm:
                        item["target"] = {"name": tm["name"] or "—", "text": tm["text"][:300], "chat_id": tm["chat_id"]}
                out.append(item)
            return {"reports": out}

        if path == "/api/admin/report/resolve" and method == "POST":
            r = q("SELECT * FROM reports WHERE id=?", (int(body.get("id", 0) or 0),), one=True)
            if not r:
                raise ApiError(404, "Не найдена")
            st = "dismissed" if body.get("status") == "dismissed" else "resolved"
            if body.get("all"):
                ex("UPDATE reports SET status=? WHERE tkind=? AND tid=? AND status='open'", (st, r["tkind"], r["tid"]))
            else:
                ex("UPDATE reports SET status=? WHERE id=?", (st, r["id"]))
            bump()
            return {"ok": True}

        if path == "/api/admin/nft_colls":
            out = []
            for c in q("SELECT * FROM nft_colls ORDER BY id DESC"):
                fl = q("SELECT COUNT(*) n, MIN(price) p FROM nft_items WHERE coll_id=? AND listed=1", (c["id"],), one=True)
                out.append({"id": c["id"], "name": c["name"], "glyph": c["glyph"], "supply": c["supply"], "minted": c["minted"],
                            "price": c["price"], "active": bool(c["active"]), "listed": fl["n"], "floor": fl["p"],
                            "models": json.loads(c["models"] or "[]")})
            return {"colls": out}

        if path == "/api/admin/nft_coll/create" and method == "POST":
            name = str(body.get("name", "")).strip()[:40]
            glyphs = [g for g in re.split(r"[\s,]+", str(body.get("glyphs", "")).strip()) if g][:8]
            if not name or not glyphs:
                raise ApiError(400, "Нужны название и хотя бы один эмодзи-модель")
            try:
                supply, price = int(body.get("supply", 0) or 0), int(body.get("price", 100) or 100)
            except (TypeError, ValueError):
                raise ApiError(400, "Тираж и цена — числа")
            if supply < 0 or price < 0:
                raise ApiError(400, "Тираж и цена не могут быть отрицательными")
            per = max(1, 1000 // len(glyphs))
            models = [("Модель %d" % (i + 1), g[:24], per) for i, g in enumerate(glyphs)]
            cid = ex("INSERT INTO nft_colls(name,glyph,supply,price,models,created) VALUES(?,?,?,?,?,?)",
                     (name, glyphs[0][:24], supply, price, json.dumps(models, ensure_ascii=False), time.time()))
            if int(body.get("release", 0) or 0) > 0:
                mint_items(cid, min(int(body["release"]), 500), price)
            bump()
            return {"ok": True}

        if path == "/api/admin/nft_coll/release" and method == "POST":
            cid, cnt = int(body.get("id", 0) or 0), int(body.get("count", 1) or 1)
            if not 1 <= cnt <= 500:
                raise ApiError(400, "От 1 до 500 штук за раз")
            c = q("SELECT * FROM nft_colls WHERE id=?", (cid,), one=True)
            if not c:
                raise ApiError(404, "Коллекция не найдена")
            price = int(body.get("price", 0) or c["price"])
            if price < 1:
                raise ApiError(400, "Цена от 1 ⭐")
            to = int(body.get("user_id", 0) or 0)
            if to:
                t = q("SELECT * FROM users WHERE id=? AND deleted=0 AND is_service=0 AND is_bot=0", (to,), one=True)
                if not t:
                    raise ApiError(404, "Получатель не найден")
                ids = mint_items(cid, cnt, 0, owner=to, listed=0, from_id=SERVICE_ID)
                service_msg(to, "🎁 Вам выдали NFT-подарков: %d (%s)" % (len(ids), c["name"]))
            else:
                mint_items(cid, cnt, price)
            bump()
            return {"ok": True}

        if path == "/api/admin/boost_chat" and method == "POST":
            cid, amt = int(body.get("id", 0) or 0), int(body.get("amount", 0))
            if not q("SELECT 1 FROM chats WHERE id=? AND kind='channel' AND deleted=0", (cid,), one=True):
                raise ApiError(404, "Канал не найден")
            if amt < 0:
                ex("UPDATE chats SET boost_extra=? WHERE id=?", (INF_BOOSTS, cid))  # бесконечность
            elif body.get("set"):
                ex("UPDATE chats SET boost_extra=? WHERE id=?", (min(amt, INF_BOOSTS), cid))
            else:
                ex("UPDATE chats SET boost_extra=MIN(?, boost_extra+?) WHERE id=?", (INF_BOOSTS, amt, cid))
            bump()
            return {"ok": True}

        if path == "/api/admin/boost_user" and method == "POST":
            t = q("SELECT * FROM users WHERE id=? AND deleted=0 AND is_service=0", (int(body.get("user_id", 0) or 0),), one=True)
            if not t:
                raise ApiError(404, "Не найден")
            ex("UPDATE users SET boost_inf=? WHERE id=?", (1 if body.get("inf") else 0, t["id"]))
            bump()
            return {"ok": True}

        if path == "/api/admin/nft_coll/active" and method == "POST":
            ex("UPDATE nft_colls SET active=? WHERE id=?", (1 if body.get("active") else 0, int(body.get("id", 0) or 0)))
            bump()
            return {"ok": True}

        if path == "/api/admin/nft_item/delete" and method == "POST":
            ex("DELETE FROM nft_items WHERE id=?", (int(body.get("id", 0) or 0),))
            bump()
            return {"ok": True}

    raise ApiError(404, "Не найдено")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "TGSite"

    def log_message(self, *a):
        pass

    def _send(self, code, data, ctype="application/json; charset=utf-8", cache="no-store", extra=None):
        if not isinstance(data, (bytes, bytearray)):
            data = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        for k_, v_ in (extra or {}).items():
            self.send_header(k_, v_)
        self.end_headers()
        self.wfile.write(data)

    def _handle(self, method):
        try:
            u = urlparse(self.path)
            qs = {k: v[0] for k, v in parse_qs(u.query).items()}
            body = {}
            if method == "POST":
                n = int(self.headers.get("Content-Length") or 0)
                if n > 12_000_000:
                    raise ApiError(413, "Слишком большой запрос")
                body = json.loads(self.rfile.read(n) or b"{}")
                if not isinstance(body, dict):
                    raise ApiError(400, "Плохой запрос")
            path = u.path
            if method == "GET" and path in ("/", "/index.html"):
                return self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            m = re.fullmatch(r"/av/([uc])/(\d+)", path)
            if m and method == "GET":
                table = "users" if m.group(1) == "u" else "chats"
                row = q(f"SELECT avatar FROM {table} WHERE id=?", (int(m.group(2)),), one=True)
                if not row or not row["avatar"].startswith("data:"):
                    return self._send(404, b"", "text/plain")
                head, b64 = row["avatar"].split(",", 1)
                mime = head[5:].split(";")[0]
                return self._send(200, base64.b64decode(b64), mime, "public, max-age=31536000, immutable")
            mm = re.fullmatch(r"/media/([A-Za-z0-9_\-]{10,60})", path)
            if mm and method == "GET":
                row = q("SELECT mime, data FROM media WHERE key=?", (mm.group(1),), one=True)
                if not row:
                    return self._send(404, b"", "text/plain")
                data = bytes(row["data"])
                total = len(data)
                extra, code = {"Accept-Ranges": "bytes"}, 200
                rg = re.match(r"bytes=(\d*)-(\d*)$", self.headers.get("Range", "") or "")
                if rg and (rg.group(1) or rg.group(2)):
                    if rg.group(1):
                        start = int(rg.group(1))
                        end = int(rg.group(2)) if rg.group(2) else total - 1
                    else:
                        start, end = max(0, total - int(rg.group(2))), total - 1
                    end = min(end, total - 1)
                    if start > end:
                        return self._send(416, b"", "text/plain", extra={"Content-Range": "bytes */%d" % total})
                    extra["Content-Range"] = "bytes %d-%d/%d" % (start, end, total)
                    data, code = data[start:end + 1], 206
                return self._send(code, data, row["mime"], "private, max-age=31536000, immutable", extra)
            mb = re.fullmatch(r"/bot([0-9]+:[A-Za-z0-9_\-]+)/(\w+)", path)
            if mb:
                params = dict(qs)
                params.update(body)
                try:
                    return self._send(200, bot_api(mb.group(1), mb.group(2), params))
                except ApiError as e:
                    return self._send(e.code, {"ok": False, "error_code": e.code, "description": e.msg})
            if path.startswith("/api/"):
                return self._send(200, api(method, path, qs, body, self.headers))
            self._send(404, {"error": "not found"})
        except ApiError as e:
            self._send(e.code, {"error": e.msg})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            traceback.print_exc()
            try:
                self._send(500, {"error": "Ошибка сервера"})
            except Exception:
                pass

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")


PAGE = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#17212b">
<meta name="apple-mobile-web-app-title" content="GodGram">
<link rel="icon" href="data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAyNCAyNCI+PGRlZnM+PGxpbmVhckdyYWRpZW50IGlkPSJnIiB4MT0iMCIgeTE9IjAiIHgyPSIxIiB5Mj0iMSI+PHN0b3Agb2Zmc2V0PSIwIiBzdG9wLWNvbG9yPSIjOGI1Y2Y2Ii8+PHN0b3Agb2Zmc2V0PSIxIiBzdG9wLWNvbG9yPSIjMmFhYmVlIi8+PC9saW5lYXJHcmFkaWVudD48L2RlZnM+PHJlY3Qgd2lkdGg9IjI0IiBoZWlnaHQ9IjI0IiByeD0iNiIgZmlsbD0idXJsKCNnKSIvPjxlbGxpcHNlIGN4PSIxMiIgY3k9IjMuOSIgcng9IjUuMiIgcnk9IjEuNSIgZmlsbD0ibm9uZSIgc3Ryb2tlPSIjZmZkODZiIiBzdHJva2Utd2lkdGg9IjEuMyIvPjxwYXRoIGQ9Ik0xNi45IDEwLjFBNi40IDYuNCAwIDEgMCAxOC40IDE0LjJIMTIuNiIgZmlsbD0ibm9uZSIgc3Ryb2tlPSIjZmZmIiBzdHJva2Utd2lkdGg9IjIuNSIgc3Ryb2tlLWxpbmVjYXA9InJvdW5kIiBzdHJva2UtbGluZWpvaW49InJvdW5kIi8+PC9zdmc+">
<title>GodGram</title>
<style>
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{margin:0;height:100%;background:#0e1621;color:#e8eef4;font-family:system-ui,-apple-system,Roboto,"Segoe UI",sans-serif;font-size:15px}
button,input,textarea,select{font:inherit;color:inherit}
.hide{display:none!important}
.sub{color:#7d8e9e;font-size:13px}
.c{text-align:center}
a.lnk{color:#2aabee;cursor:pointer}
.pb{margin-left:5px;font-size:.85em;display:inline-flex;align-items:center;vertical-align:middle}
.pb .stimg,.stimg{display:inline-block;flex:none;pointer-events:none}
/* слайды-приветствие */
#intro{position:fixed;inset:0;background:#000;z-index:60;display:flex;flex-direction:column;align-items:center;padding:0 18px calc(24px + env(safe-area-inset-bottom));overflow:hidden}
#islides{width:100%;overflow:hidden;margin-top:8vh;touch-action:pan-y;user-select:none}
.track{display:flex;transition:transform .35s ease}
.slide{flex:0 0 100%;display:flex;flex-direction:column;align-items:center;padding:0 10px}
.iic{width:min(52vw,230px);height:min(52vw,230px);border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:min(24vw,108px);box-shadow:0 10px 40px #0008}
.slide h1{margin:min(10vh,90px) 0 12px;font-size:34px;font-weight:700;color:#fff}
.slide p{font-size:19px;text-align:center;line-height:1.38;max-width:340px;margin:0;color:#e6e6e6;min-height:3em}
.slide p b{color:#fff}
.idots{display:flex;gap:14px;margin:22px 0}
.idots i{width:8px;height:8px;border-radius:50%;background:#555;cursor:pointer;transition:.2s}
.idots i.on{background:#3d8bff;transform:scale(1.15)}
.isp{flex:1}
.ibtn{width:100%;max-width:520px;border:0;border-radius:30px;padding:17px;font-size:18px;font-weight:600;color:#fff;background:linear-gradient(90deg,#4a8ff5,#62c4ff 50%,#4a8ff5) 0 0/200% 100%;animation:ishine 3.2s linear infinite;cursor:pointer;box-shadow:0 4px 20px #3d8bff55}
@keyframes ishine{to{background-position:-200% 0}}
/* вход */
#auth{height:100%;overflow-y:auto;display:flex;align-items:center;justify-content:center;padding:20px}
.acard{width:100%;max-width:380px;padding:10px 6px}
.logo{width:120px;height:120px;margin:0 auto 18px;border-radius:50%;background:linear-gradient(135deg,#8b5cf6,#2aabee);display:flex;align-items:center;justify-content:center}
.acard h2{text-align:center;margin:0 0 6px;font-size:28px;font-weight:600}
.acard p{margin:6px 0 18px;line-height:1.4}
.inp{width:100%;background:#0e1621;border:1px solid #2c3e50;border-radius:12px;padding:14px 14px;margin:6px 0;outline:none;appearance:none;-webkit-appearance:none}
.inp:focus{border-color:#2aabee}
select.inp{background:#0e1621 url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='12' height='8'><path d='M1 1l5 5 5-5' stroke='%237d8e9e' fill='none' stroke-width='2'/></svg>") no-repeat right 14px center}
.prow{display:flex;gap:8px}
.prow .inp:first-child{width:96px;flex:none}
.keep{display:flex;align-items:center;gap:10px;margin:12px 4px;color:#aab8c5;cursor:pointer}
.keep input{width:20px;height:20px;accent-color:#2aabee}
.btn{width:100%;border:0;border-radius:12px;padding:14px;background:#2aabee;color:#fff;font-weight:600;cursor:pointer;margin-top:8px;letter-spacing:.5px}
.btn.gray{background:#243445}.btn.red{background:#d9453d}
.err{color:#ff7b72;font-size:13px;min-height:18px;margin-top:6px;text-align:center}
.code{letter-spacing:12px;text-align:center;font-size:26px}
.demo{background:#1d2c3c;border:1px dashed #2aabee;border-radius:12px;padding:10px;text-align:center;margin-bottom:10px;font-size:14px}
.demo b{font-size:20px;letter-spacing:4px;color:#2aabee}
.edit{cursor:pointer;color:#2aabee;margin-left:6px}
/* приложение */
#app{display:flex;height:100%;height:100dvh;min-height:0}
#side{width:360px;max-width:100%;background:#17212b;border-right:1px solid #0b121a;display:flex;flex-direction:column;position:relative;min-height:0}
#main{flex:1;display:flex;flex-direction:column;min-width:0;min-height:0;background:#0e1621;background-image:radial-gradient(#16212d 1px,transparent 1px);background-size:22px 22px}
.top{display:flex;gap:8px;align-items:center;padding:10px;position:relative}
.icon{width:40px;height:40px;border-radius:50%;border:0;background:none;color:#aab8c5;cursor:pointer;font-size:20px;display:flex;align-items:center;justify-content:center;flex:none}
.icon:hover{background:#ffffff14}
.search{flex:1;background:#242f3d;border:0;border-radius:20px;padding:10px 16px;outline:none}
#list{flex:1;min-height:0;overflow-y:auto;overflow-x:hidden;-webkit-overflow-scrolling:touch;overscroll-behavior:contain}
.sec{padding:8px 16px;color:#2aabee;font-size:13px;font-weight:600}
.row{display:flex;gap:12px;padding:9px 12px;cursor:pointer;align-items:center}
.row:hover{background:#202b36}.row.on{background:#2b5278}
.row .mid{flex:1;min-width:0}
.row .t{display:flex;justify-content:space-between;gap:8px}
.row .n{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.row .p{display:flex;justify-content:space-between;gap:8px;margin-top:2px}
.row .p span:first-child{color:#7d8e9e;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.badge{background:#2aabee;color:#fff;border-radius:12px;min-width:22px;height:22px;padding:0 7px;font-size:12px;display:flex;align-items:center;justify-content:center;flex:none}
.badge.m{background:#4a5b6b}
.av{border-radius:50%;overflow:hidden;display:flex;align-items:center;justify-content:center;color:#fff;font-weight:600;flex:none;user-select:none}
.av img{width:100%;height:100%;object-fit:cover}
.av.ghost{background:#8a8d91}
.av.svc{background:linear-gradient(135deg,#8b5cf6,#2aabee)}
.av.bot-send{background:linear-gradient(135deg,#f5c518,#e8a317)}
.av.bot-osint{background:linear-gradient(135deg,#3d8bfd,#1a5fd0)}
.fab{position:absolute;right:18px;bottom:18px;width:54px;height:54px;border-radius:50%;border:0;background:#2aabee;color:#fff;font-size:24px;box-shadow:0 4px 14px #0007;cursor:pointer}
.menu{position:fixed;z-index:80;background:#1f2b38;border-radius:12px;padding:6px 0;min-width:220px;max-width:min(320px,calc(100vw - 16px));max-height:calc(100vh - 16px);overflow-y:auto;box-shadow:0 8px 30px #000a;-webkit-overflow-scrolling:touch}
.menu div{padding:11px 18px;cursor:pointer}.menu div:hover{background:#ffffff12}
#empty{margin:auto;background:#17212bcc;padding:6px 16px;border-radius:16px;color:#aab8c5}
#chat{display:flex;flex-direction:column;height:100%}
.chead{display:flex;align-items:center;gap:10px;padding:8px 12px;background:#17212b;border-bottom:1px solid #0b121a}
.chead .who{flex:1;min-width:0;cursor:pointer}
.chead .who b{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.back{display:none}
#msgs{flex:1;overflow-y:auto;padding:12px 10px;display:flex;flex-direction:column;gap:4px}
#msgs::before{content:'';flex:1 0 auto;min-height:0}
.m{display:flex;gap:8px;max-width:82%;align-items:flex-end}
.m.me{align-self:flex-end;flex-direction:row-reverse}
.bub{background:#182533;border-radius:14px 14px 14px 4px;padding:7px 11px 5px;min-width:60px;word-break:break-word;white-space:pre-wrap}
.m.me .bub{background:#2b5278;border-radius:14px 14px 4px 14px}
.bub .who{font-size:13px;font-weight:600;color:#6ab3f3;margin-bottom:2px;cursor:pointer}
.bub .who.d{color:#9aa0a6}
.bub .tm{float:right;margin:6px 0 0 12px;font-size:11px;color:#7d8e9e;display:inline-flex;align-items:center;gap:3px}
.bub .ticks{font-size:12px;letter-spacing:-1px;opacity:.9}
.bub .ticks.rd{color:#6ab3f3}
.m.me .bub .ticks.rd{color:#8fd3ff}
.comp{display:flex;gap:8px;padding:8px 10px;background:#17212b;align-items:flex-end}
.comp textarea{flex:1;resize:none;max-height:140px;background:#242f3d;border:0;border-radius:20px;padding:10px 16px;outline:none;line-height:1.35}
.send{width:44px;height:44px;border-radius:50%;border:0;background:#2aabee;color:#fff;font-size:18px;cursor:pointer;flex:none}
.locked{flex:1;text-align:center;color:#8a8d91;padding:12px}
/* оверлеи и профиль */
.overlay{position:fixed;inset:0;background:#000;z-index:50;overflow-y:auto;animation:fade .15s}
.overlay.dim{background:#000a;display:flex;align-items:center;justify-content:center;padding:14px}
.overlay.prem{background:linear-gradient(#241a5c,#0b0820 45%,#000)}
@keyframes fade{from{opacity:0}to{opacity:1}}
.pw{max-width:560px;margin:0 auto;padding:0 18px 40px;position:relative}
.pw.ban{padding-top:40px}
.pbar{display:flex;justify-content:space-between;padding:12px 0;position:sticky;top:0;background:#000;z-index:2}
.phead{display:flex;gap:18px;align-items:center;margin:8px 0 28px}
.phead h1{font-size:26px;margin:0;font-weight:600}
.msgbtn{position:absolute;right:18px;top:128px;width:72px;height:72px;border-radius:50%;border:0;background:#9a9ea3;color:#fff;font-size:30px;cursor:pointer;display:flex;align-items:center;justify-content:center}
/* баннер профиля */
.pbn{position:relative;padding:64px 16px 30px;display:flex;flex-direction:column;align-items:center;color:#fff;text-align:center}
.pbn .pat{position:absolute;inset:0;overflow:hidden;pointer-events:none}
.pbn .pat span,.bprev .pat span{position:absolute;transform:translate(-50%,-50%);opacity:.28}
.pbn .av{border:3px solid #ffffff40;position:relative}
.pbn h1{margin:12px 0 2px;font-size:26px;font-weight:600;position:relative}
.pbn .st{opacity:.8;position:relative}
.pbar2{position:absolute;top:10px;left:8px;right:8px;display:flex;justify-content:space-between;z-index:3}
.pbar2 .icon{color:#fff;font-size:26px}
.msgbtn2{position:absolute;right:18px;bottom:-34px;width:68px;height:68px;border-radius:50%;border:0;background:#2aabee;color:#fff;font-size:28px;cursor:pointer;display:flex;align-items:center;justify-content:center;z-index:4;box-shadow:0 4px 16px #0008}
.bprev{position:relative;height:110px;border-radius:14px;overflow:hidden;display:flex;align-items:flex-end;padding:10px 14px;color:#fff;font-weight:600;margin-bottom:10px}
.bprev .pat{position:absolute;inset:0}
.bprev .bn{position:relative}
.sws{display:flex;flex-wrap:wrap;gap:8px;margin:6px 0 10px}
.swc{width:34px;height:34px;border-radius:50%;border:2px solid transparent;cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:14px;position:relative}
.swc.on{border-color:#fff}
.swc.lk::after{content:"🔒";position:absolute;font-size:12px;right:-4px;bottom:-4px}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin:6px 0 10px}
.chip{border:1px solid #2c3e50;background:#0e1621;border-radius:10px;min-width:38px;height:38px;cursor:pointer;font-size:18px}
.chip.on{border-color:#2aabee;background:#1d3550}
.lbl{color:#7d8e9e;font-size:13px;margin-top:8px}
.psec{color:#a3a7ab;font-weight:600;margin:26px 0 16px;font-size:18px}
.pi{margin-bottom:22px;display:flex;justify-content:space-between;align-items:center}
.pi .v{font-size:20px}.pi .l{color:#7b7e82;font-size:15px;margin-top:3px}
.tg{width:58px;height:34px;border-radius:17px;background:#6a6d71;position:relative;cursor:pointer;flex:none;transition:.2s}
.tg::after{content:"";position:absolute;top:3px;left:3px;width:28px;height:28px;border-radius:50%;background:#000;border:2px solid #cfd2d6;transition:.2s}
.tg.on{background:#2aabee}.tg.on::after{left:27px}
.modal{background:#17212b;border-radius:16px;width:100%;max-width:440px;max-height:92vh;overflow-y:auto;padding:20px}
.modal h3{margin:0 0 12px}
.ur{display:flex;align-items:center;gap:12px;padding:8px 4px;cursor:pointer;border-radius:10px}
.ur:hover{background:#ffffff0c}
.ur .mid{flex:1;min-width:0}
.ur small{color:#7d8e9e;display:block}
.tag{font-size:11px;padding:2px 7px;border-radius:8px;background:#2b5278;margin-left:6px}
.tag.d{background:#555}
.dbtn{border:0;background:#d9453d;color:#fff;border-radius:8px;padding:7px 12px;cursor:pointer}
.ck{width:20px;height:20px;accent-color:#2aabee}
.nft{background:linear-gradient(90deg,#7ba8ff,#c27bff,#ff7bc0);-webkit-background-clip:text;background-clip:text;color:transparent;font-weight:600}
.tabs2{display:flex;background:#0e1621;border-radius:10px;padding:3px;margin-bottom:12px}
.tabs2 button{flex:1;border:0;background:none;padding:9px 2px;border-radius:8px;color:#7d8e9e;cursor:pointer;font-size:13px}
.tabs2 button.on{background:#2b5278;color:#fff}
.nrow{border-bottom:1px solid #243445;padding:10px 0}
.nh{display:flex;justify-content:space-between;gap:8px;margin-bottom:6px;align-items:center}
.nact{display:flex;gap:6px;align-items:center}
.nact .inp{padding:8px 10px;font-size:14px}
.sbtn{border:0;background:#2aabee;color:#fff;border-radius:8px;padding:8px 10px;cursor:pointer;white-space:nowrap}
.sbtn.g{background:#4a5b6b}.sbtn.r{background:#d9453d}
.frm{display:flex;flex-direction:column;gap:6px;background:#0e1621;border-radius:12px;padding:10px;margin-bottom:10px}
.frm .nact .inp{margin:0;flex:1;min-width:0}
/* перетаскиваемые юзернеймы */
.slist{position:relative}
.sit{display:flex;flex-wrap:wrap;align-items:center;gap:8px 10px;padding:12px 6px;border-bottom:1px solid #243445;background:#17212b}
.sit.drag{z-index:5;background:#22314a;box-shadow:0 8px 24px #000b;border-radius:10px}
.sit .sit-main{flex:1 1 140px;min-width:0}
.sit .sit-acts{display:flex;flex-wrap:wrap;align-items:center;gap:6px;flex:0 1 auto;margin-left:auto}
.hnd{cursor:grab;touch-action:none;padding:6px 8px;color:#8ea0b1;font-size:22px;user-select:none;line-height:1;flex:none}
.sbtn.sm{padding:6px 10px;font-size:12px;border-radius:8px}
.rar-pill{display:inline-flex;align-items:center;gap:4px;font-size:11px;font-weight:700;padding:2px 8px;border-radius:999px;margin-left:6px;vertical-align:middle}
.rar-common{background:#3a4553;color:#c5d0dc}
.rar-rare{background:#1e4a7a;color:#7ec8ff}
.rar-epic{background:#4a2f7a;color:#d4a8ff}
.rar-legend{background:#6b4a12;color:#ffd86b}
.rating{display:inline-flex;align-items:center;gap:4px;font-weight:700;font-size:14px;padding:3px 10px;border-radius:999px;background:#ffffff18;margin-left:8px;vertical-align:middle}
.rating.pos{color:#4fd27a}.rating.neg{color:#ff7b72}.rating.zero{color:#9aa0a6}
.rlog{display:flex;justify-content:space-between;gap:10px;padding:8px 0;border-bottom:1px solid #243445}
.rlog .d.pos{color:#4fd27a;font-weight:700}.rlog .d.neg{color:#ff7b72;font-weight:700}
.rank-bar{height:4px;border-radius:2px;background:#243445;overflow:hidden;margin-top:4px}
.rank-bar>i{display:block;height:100%;border-radius:2px}
/* Premium */
.pstar{width:116px;height:116px;margin:6px auto 14px;border-radius:50%;background:linear-gradient(135deg,#6c8cff,#c65bff);display:flex;align-items:center;justify-content:center;box-shadow:0 10px 40px #8a5bff66}
.prem h1{margin:0;font-size:28px;color:#fff}
.prem p{margin:6px 0 22px}
.fr{display:flex;gap:14px;align-items:flex-start;padding:11px 0}
.fi{width:44px;height:44px;border-radius:12px;display:flex;align-items:center;justify-content:center;font-size:22px;flex:none}
.fr b{display:block;margin-bottom:2px}
.ftag{font-size:10px;font-weight:600;margin-left:8px;padding:2px 6px;border-radius:6px;background:#3a3f55;color:#a8b0cc;vertical-align:middle}
.ftag.ok{background:#1f5b3a;color:#7be3a3}
.pcta{position:sticky;bottom:0;padding:16px 0 10px;background:linear-gradient(transparent,#000 35%)}
.cta{width:100%;border:0;border-radius:14px;padding:15px;font-weight:600;font-size:16px;color:#fff;background:linear-gradient(90deg,#6c8cff,#c65bff);cursor:pointer}
.cta.on{background:#263044;cursor:default}
#toast{position:fixed;left:50%;bottom:24px;transform:translateX(-50%);background:#243445;padding:10px 18px;border-radius:12px;z-index:99;box-shadow:0 6px 20px #000a;max-width:90%;cursor:pointer}
@media(max-width:720px){
 #side{width:100%;min-height:0}#main{display:none}
 #app.open #side{display:none}#app.open #main{display:flex}
 .back{display:flex}.m{max-width:90%}
}
/* ===== v5 ===== */
body{font-size:var(--fs,15px)}
.tabs2{flex-wrap:wrap}.tabs2 button{min-width:24%}
.bt{background:#2aabee;color:#fff;font-size:10px;padding:1px 5px;border-radius:6px;margin-left:6px;vertical-align:middle;font-weight:600}
/* 3D-подарки */
.g3{position:relative;width:var(--s);height:calc(var(--s)*1.1);margin:0 auto;user-select:none}
.g3::before{content:"";position:absolute;inset:6%;border-radius:50%;background:radial-gradient(var(--c),transparent 68%);opacity:.4;filter:blur(8px)}
.g3s{position:relative;width:var(--s);height:var(--s);perspective:calc(var(--s)*5)}
.g3o{position:relative;width:100%;height:100%;transform-style:preserve-3d;transform:rotateX(-14deg) rotateY(-20deg)}
.gl{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:calc(var(--s)*.66);line-height:1}
.gf{position:absolute;left:calc(var(--s)/2 - var(--s)*.29);top:calc(var(--s)/2 - var(--s)*.29);width:calc(var(--s)*.58);height:calc(var(--s)*.58);border-radius:4px;box-shadow:inset 0 0 0 2px #0002;
 background:linear-gradient(90deg,transparent 40%,#fffb 40%,#fffb 60%,transparent 60%),linear-gradient(var(--c),var(--c))}
.gf-t,.gf-d{background:linear-gradient(90deg,transparent 40%,#fffb 40%,#fffb 60%,transparent 60%),linear-gradient(0deg,transparent 40%,#fffb 40%,#fffb 60%,transparent 60%),linear-gradient(var(--c),var(--c))}
.gf-f{transform:translateZ(calc(var(--s)*.29))}
.gf-b{transform:rotateY(180deg) translateZ(calc(var(--s)*.29));filter:brightness(.6)}
.gf-r{transform:rotateY(90deg) translateZ(calc(var(--s)*.29));filter:brightness(.82)}
.gf-l{transform:rotateY(-90deg) translateZ(calc(var(--s)*.29));filter:brightness(.7)}
.gf-t{transform:rotateX(90deg) translateZ(calc(var(--s)*.29));filter:brightness(1.15)}
.gf-d{transform:rotateX(-90deg) translateZ(calc(var(--s)*.29));filter:brightness(.5)}
.bow{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:calc(var(--s)*.3)}
.a-spin .g3o{animation:g3spin 7s linear infinite}
.a-float .g3o{animation:g3sway 5s ease-in-out infinite}.a-float .g3s{animation:g3float 3s ease-in-out infinite}
.a-bounce .g3o{animation:g3spin 9s linear infinite}.a-bounce .g3s{animation:g3bounce 1.2s cubic-bezier(.3,.7,.4,1) infinite}
.a-pulse .g3o{animation:g3sway 6s ease-in-out infinite}.a-pulse .g3s{animation:g3pulse 1.6s ease-in-out infinite}
@keyframes g3spin{from{transform:rotateX(-14deg) rotateY(0)}to{transform:rotateX(-14deg) rotateY(360deg)}}
@keyframes g3sway{0%,100%{transform:rotateX(8deg) rotateY(-35deg)}50%{transform:rotateX(-6deg) rotateY(35deg)}}
@keyframes g3float{50%{transform:translateY(-8%)}}
@keyframes g3bounce{0%,100%{transform:translateY(0)}45%{transform:translateY(-16%)}}
@keyframes g3pulse{50%{transform:scale(1.12)}}
.g3sh{position:absolute;left:22%;right:22%;bottom:0;height:7%;border-radius:50%;background:radial-gradient(#000a,transparent 70%)}
.gsp{position:absolute;color:#fff;font-size:calc(var(--s)*.14);opacity:0;pointer-events:none;animation:g3tw 2.4s ease-in-out infinite}
.gsp.s0{top:8%;left:12%}.gsp.s1{top:20%;right:8%;animation-delay:.6s}.gsp.s2{bottom:30%;left:4%;animation-delay:1.2s}.gsp.s3{top:2%;right:34%;animation-delay:1.8s}
@keyframes g3tw{0%,100%{opacity:0;transform:scale(.3)}50%{opacity:1;transform:scale(1)}}
.nomo .g3o,.nomo .g3s{animation:none!important}.nomo .gsp{display:none}
.gcard{position:relative;overflow:hidden;background:#101c28;border-radius:12px;padding:12px 22px 10px;text-align:center;min-width:170px;cursor:pointer;margin-bottom:4px;white-space:normal}
.gcard::before{content:"";position:absolute;inset:0;background:radial-gradient(circle at 50% 35%,var(--c),transparent 72%);opacity:.38}
.gcard>*{position:relative}
.gcard .gt{font-weight:600;margin-top:2px}.gcard .gn{font-size:12px;color:#aab8c5}.gcard .gnote{margin-top:6px;font-size:14px}
.ggrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(104px,1fr));gap:10px}
.gitem{background:#0e1621;border-radius:12px;padding:10px 6px 8px;text-align:center;cursor:pointer;border:1px solid #243445}
.gitem:hover{border-color:#2aabee}
.gitem .gt{font-size:13px;margin-top:2px}.gitem .gp{font-size:13px;color:#ffd54a;font-weight:600}.gitem .gs{font-size:11px;color:#7d8e9e}
.wcard{background:linear-gradient(135deg,#3a7be0,#8b5cf6);border-radius:16px;padding:18px;text-align:center;margin-bottom:12px}
.wcard .bal{font-size:36px;font-weight:700}.wcard .l{opacity:.8;font-size:13px}
.tx{display:flex;justify-content:space-between;gap:8px;padding:9px 0;border-bottom:1px solid #243445}
.tx .p{color:#6fe08a;font-weight:600}.tx .m2{color:#ff7b72;font-weight:600}
.srow{display:flex;align-items:center;gap:14px;padding:13px 4px;cursor:pointer;border-bottom:1px solid #1b2733}
.srow .si{width:32px;text-align:center;font-size:20px}.srow .mid{flex:1;min-width:0}
.sgrp{color:#2aabee;font-weight:600;font-size:14px;margin:22px 0 4px}
.code2{background:#0e1621;border-radius:10px;padding:10px;overflow-x:auto;font-size:12px;white-space:pre;user-select:all}
.tokbox{background:#0e1621;border:1px dashed #2aabee;border-radius:10px;padding:10px;word-break:break-all;font-family:monospace;font-size:13px;cursor:pointer}
.frz{color:#7cc7ff}
/* ===== v6 ===== */
.bw{display:inline-flex;align-items:center;gap:3px;margin-left:5px;vertical-align:middle}
.vb.own{background:none!important;padding:0;min-width:0;width:19px;height:19px;border-radius:0;vertical-align:middle}
.vb.own svg{display:block;width:100%;height:100%}
.vb{display:inline-flex;align-items:center;justify-content:center;min-width:16px;height:16px;border-radius:50%;color:#fff;font-size:10px;font-weight:700;cursor:pointer;line-height:1;padding:0 1px}
.vlabel{display:inline-block;color:#fff;font-size:10px;font-weight:700;padding:1px 5px;border-radius:5px;cursor:pointer;letter-spacing:.3px}
.warnbar{background:#3a1f1f;color:#ffb0a8;padding:8px 14px;font-size:13px;border-bottom:1px solid #0b121a}
.warnbar.f{background:#3a2c18;color:#ffd29a}
.pin{color:#7d8e9e;font-size:12px;margin-right:2px}
/* профиль в стиле Telegram (тёмные карточки) */
.overlay.pf{background:#000}
.pfh{position:relative;padding:54px 16px 18px;display:flex;flex-direction:column;align-items:center;color:#fff;text-align:center;overflow:hidden}
.pfh .pat{position:absolute;inset:0;pointer-events:none;overflow:hidden}
.pfh .pat span{position:absolute;transform:translate(-50%,-50%);opacity:.22}
.pat-custom .pat-tile{position:absolute;transform:translate(-50%,-50%);object-fit:cover;border-radius:6px;opacity:.32;pointer-events:none}
.pbn .pat-custom .pat-tile,.bprev .pat-custom .pat-tile{opacity:.28}
.pfh .av{position:relative;box-shadow:0 0 0 3px #ffffff55}
.pfh h1{position:relative;margin:12px 0 2px;font-size:26px;font-weight:700;font-style:italic;display:flex;align-items:center;gap:4px;flex-wrap:wrap;justify-content:center}
.pfh .st{position:relative;opacity:.85;display:flex;align-items:center;gap:6px}
.pfh .stdot{min-width:18px;height:18px;padding:0 5px;border-radius:6px;background:#fff;color:#1a1a1c;font-size:11px;font-weight:800;display:inline-flex;align-items:center;justify-content:center;line-height:1;box-sizing:border-box;flex:none}
.pfh .stdot.neg{background:#ff453a;color:#fff}
.pfh .stdot.zero{background:#ffffffaa;color:#555}
.rbar-wrap{position:relative;width:min(220px,70vw);margin:10px auto 0;text-align:left}
.rbar-meta{display:flex;justify-content:space-between;font-size:11px;opacity:.85;margin-bottom:4px}
.rbar{height:6px;border-radius:999px;background:#ffffff28;overflow:hidden}
.rbar>i{display:block;height:100%;border-radius:999px;background:linear-gradient(90deg,#6ab7ff,#c65bff);min-width:0;transition:width .25s}
.rbar.neg>i{background:linear-gradient(90deg,#ff7b72,#d9453d)}
.rbar.full>i{background:linear-gradient(90deg,#4fd27a,#2ba2b5)}
.pacts{position:relative;display:flex;justify-content:space-around;gap:6px;width:100%;max-width:420px;margin-top:20px}
.pact{background:none;border:0;color:#fff;display:flex;flex-direction:column;align-items:center;gap:6px;font-size:13px;cursor:pointer;min-width:64px}
.pact i{font-style:normal;font-size:24px;width:44px;height:44px;display:flex;align-items:center;justify-content:center}
.pmusic{position:relative;margin-top:16px;font-size:14px;color:#e8eef4;display:flex;gap:6px;align-items:center;max-width:94%;overflow:hidden;white-space:nowrap;text-overflow:ellipsis}
.pbody{background:#000;max-width:560px;margin:0 auto;padding:6px 14px 40px}
.pcap{color:#8a8d91;font-size:13px;margin:16px 6px 6px;text-transform:uppercase}
.pcard{background:#1c1c1e;border-radius:20px;padding:6px 18px;margin-bottom:12px}
.prow2{padding:13px 0;border-bottom:1px solid #2c2c2e}
.prow2:last-child{border-bottom:0}
.prow2 .l{color:#e8eef4;font-size:15px}
.prow2 .v{font-size:19px;margin-top:3px}
.prow2 .un2{font-size:15px;margin-top:6px;opacity:.95}
.prow2 .acc{color:#2aabee}
.open{color:#4cd964}.closed{color:#ff6b62}
.pch{display:flex;gap:12px;align-items:center;background:#1c1c1e;border-radius:22px;padding:12px 14px;margin-bottom:12px;cursor:pointer}
.pch .mid{flex:1;min-width:0}
.pch .nm{font-weight:600;font-size:17px}
.pwarn{border-radius:16px;padding:12px 14px;margin:10px 0;font-size:14px;line-height:1.35}
/* NFT-подарки */
.nftc{position:relative;border-radius:16px;overflow:hidden;text-align:center;padding:10px 6px 8px;cursor:pointer;color:#fff;min-height:150px}
.nftc .pat{position:absolute;inset:0;pointer-events:none}
.nftc .pat span{position:absolute;transform:translate(-50%,-50%);opacity:.3}
.nftc>*{position:relative}
.nftc .nn{font-size:12px;font-weight:600;margin-top:2px;text-shadow:0 1px 3px #0008}
.nftc .nu{font-size:11px;opacity:.85}
.nftc .np{position:absolute;bottom:6px;left:8px;font-size:11px;font-weight:700;background:#0006;border-radius:8px;padding:1px 6px}
.nftgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(120px,1fr));gap:10px}
.attr{display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid #243445;font-size:14px}
.attr b{font-weight:600}.attr span{color:#7d8e9e}
.chips2{display:flex;gap:6px;overflow-x:auto;padding:4px 0 10px}
.chips2 button{flex:none;border:1px solid #2c3e50;background:#0e1621;border-radius:16px;padding:6px 12px;cursor:pointer;white-space:nowrap}
.chips2 button.on{background:#2b5278;border-color:#2aabee}
.lvl{height:8px;border-radius:4px;background:#243445;overflow:hidden;margin:8px 0}
.lvl i{display:block;height:100%;background:linear-gradient(90deg,#6c8cff,#c65bff)}
.bst{display:flex;gap:6px;flex-wrap:wrap;margin:6px 0}
.bst .av{margin-right:-8px;border:2px solid #17212b}
.rep{display:flex;flex-direction:column;gap:8px}
.rep label{display:flex;gap:10px;align-items:center;padding:6px 2px;cursor:pointer}
.also{font-size:17px;line-height:1.55;margin-top:4px;color:#e8eef4}
.also .acc,.prow2 .acc,.acc{color:#2aabee}
.rib{position:absolute;top:0;right:0;width:84px;height:84px;overflow:hidden;pointer-events:none;z-index:2}
.rib span{position:absolute;top:13px;right:-30px;width:120px;text-align:center;transform:rotate(45deg);background:#0009;color:#fff;font-size:10.5px;font-weight:700;padding:2px 0;white-space:nowrap}
.gl img{width:78%;height:78%;object-fit:contain;pointer-events:none}
.gl{font-size:calc(var(--s)*.66)}
/* ===== v7: стекло / iOS-вид списка чатов ===== */
:root{--ac:#6b5cff}
#side{background:linear-gradient(#07060d,#0b0a16 55%,#0f0e1d);border-right:1px solid #1a1830}
#main{background:#0a0912}  /* точка посередине была из-за background-image без размера плитки */
.gtop{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:calc(10px + env(safe-area-inset-top)) 12px 8px}
.gpill,.gcirc{border:1px solid #ffffff2e;background:#ffffff1f;-webkit-backdrop-filter:blur(18px);backdrop-filter:blur(18px);color:#fff;cursor:pointer;font-weight:600;box-shadow:inset 0 1px 0 #ffffff26}
.gpill{border-radius:22px;padding:11px 16px;font-size:15px}
.gcirc{width:44px;height:44px;border-radius:50%;font-size:19px;display:flex;align-items:center;justify-content:center;padding:0}
.gpair{display:flex;gap:0;border:1px solid #ffffff2e;background:#ffffff1f;border-radius:24px;-webkit-backdrop-filter:blur(18px);backdrop-filter:blur(18px)}
.gpair .gcirc{border:0;background:none;box-shadow:none}
.gtitle{display:flex;align-items:center;gap:8px;font-weight:700;font-size:17px}
.gtitle .av{box-shadow:0 0 0 2px #ffffff40}
.gfilt{display:flex;gap:8px;overflow-x:auto;padding:4px 12px 10px;scrollbar-width:none}
.gfilt button{flex:none;border:1px solid #ffffff22;background:#ffffff14;color:#c9c8d8;border-radius:20px;padding:9px 20px;font-weight:600;cursor:pointer;-webkit-backdrop-filter:blur(14px);backdrop-filter:blur(14px)}
.gfilt button.on{background:#ffffff30;color:#fff;border-color:#ffffff40}
.sbox{padding:0 12px 8px}
.sbox .search{width:100%;background:#ffffff1a;border:1px solid #ffffff22;color:#fff}
#list{padding-bottom:120px}
.row{padding:11px 14px;gap:14px;border-radius:0}
.row:hover{background:#ffffff0d}.row.on{background:#ffffff1c}
.row .n{font-weight:700;color:#fff;font-size:16px}
.row .p span:first-child{white-space:normal;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;color:#a9a8bd;line-height:1.3}
.row .p{align-items:flex-start;margin-top:3px}
.row .t .sub{color:#a9a8bd}
.badge{background:var(--ac);min-width:24px;height:24px;border-radius:12px;font-weight:600}
.badge.m{background:#5b5a72}
.pinic{color:#9d9cb4;font-size:15px;padding:2px 4px;filter:grayscale(1) brightness(1.6)}
.ck2{color:var(--ac);margin-right:3px;font-weight:700}
.gnav{position:absolute;left:12px;right:12px;bottom:calc(12px + env(safe-area-inset-bottom));display:flex;gap:10px;align-items:center;z-index:5;pointer-events:none}
.gtabs{flex:1;display:flex;gap:4px;padding:5px;border-radius:34px;border:1px solid #ffffff2e;background:#1c1b2cc9;-webkit-backdrop-filter:blur(22px);backdrop-filter:blur(22px);box-shadow:0 8px 28px #000a;pointer-events:auto}
.gtab{flex:1;min-width:0;position:relative;border:0;background:none;color:#e6e5f2;border-radius:28px;padding:8px 4px 7px;cursor:pointer;display:flex;flex-direction:column;align-items:center;gap:2px;font-size:11px;font-weight:600}
.gtab i{font-style:normal;font-size:22px;line-height:1.1}
.gtab.on{background:#ffffff26;color:#a99dff}
.gtab .tb{position:absolute;top:2px;left:54%;background:#ff453a;color:#fff;border-radius:10px;min-width:20px;height:20px;padding:0 5px;font-size:12px;display:flex;align-items:center;justify-content:center}
.gsearch{width:54px;height:54px;border-radius:50%;border:1px solid #ffffff2e;background:#1c1b2cc9;color:#fff;font-size:22px;cursor:pointer;-webkit-backdrop-filter:blur(22px);backdrop-filter:blur(22px);box-shadow:0 8px 28px #000a;pointer-events:auto;flex:none}
.chead{background:#14122299;-webkit-backdrop-filter:blur(16px);backdrop-filter:blur(16px);border-bottom:1px solid #ffffff14}
.bub{background:#ffffff17;-webkit-backdrop-filter:blur(10px);backdrop-filter:blur(10px);color:#f1f0fa}
.m.me .bub{background:linear-gradient(135deg,#6b5cff,#4a3de0)}
.comp{background:transparent;padding:8px 10px calc(26px + env(safe-area-inset-bottom))}
.comp textarea{background:#ffffff1a;border:1px solid #ffffff22;color:#fff}
.send{background:var(--ac)}
.bub .who{color:#a99dff}
.nfts{display:inline-flex;align-items:center;justify-content:center;width:1.25em;height:1.25em;border-radius:50%;overflow:hidden;vertical-align:middle;position:relative}
.nfts img{width:86%;height:86%;object-fit:contain}
.nfts.big{width:30px;height:30px}
.gtab .av{box-shadow:0 0 0 1.5px #ffffff55}
.gtab span.tl{white-space:nowrap}
.pw.st{padding-bottom:calc(120px + env(safe-area-inset-bottom))}
.accs{display:flex;gap:10px;flex-wrap:wrap;padding:6px 4px 12px}
.acc1{width:34px;height:34px;border-radius:50%;border:2px solid transparent;cursor:pointer}
.acc1.on{border-color:#fff}

.frag{background:#0a0a0b;color:#f5f5f5}
.frag .fm{max-width:720px;margin:0 auto;padding:0 14px 40px}
.frag .fhead{padding:20px 0 8px;text-align:center}
.frag .fhead h1{font-size:28px;font-weight:800;letter-spacing:-.5px;margin:0 0 4px;
  background:linear-gradient(90deg,#fff 30%,#c9a227 100%);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.frag .fhead .sub{color:#8a8a8e;font-size:14px}
.frag .ftabs{display:flex;gap:6px;overflow-x:auto;padding:10px 0;scrollbar-width:none}
.frag .ftabs button{flex:none;border:1px solid #2a2a2e;background:#141416;color:#cfcfd2;border-radius:20px;padding:8px 16px;font-weight:600;cursor:pointer}
.frag .ftabs button.on{background:#fff;color:#0a0a0b;border-color:#fff}
.frag .fsearch{width:100%;background:#141416;border:1px solid #2a2a2e;border-radius:12px;padding:12px 14px;color:#fff;outline:none;margin-bottom:12px}
.frag .fgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:10px}
.frag .fcard{background:#141416;border:1px solid #2a2a2e;border-radius:16px;padding:14px 12px;cursor:pointer;transition:.15s}
.frag .fcard:hover{border-color:#c9a22755;transform:translateY(-1px)}
.frag .fcard .fh{font-size:16px;font-weight:700;word-break:break-all;margin-bottom:6px}
.frag .fcard .fh .at{color:#c9a227}
.frag .fcard .fp{color:#c9a227;font-weight:700;font-size:15px}
.frag .fcard .fo{color:#8a8a8e;font-size:12px;margin-top:4px}
.frag .fbal{text-align:center;color:#8a8a8e;font-size:13px;margin:6px 0 12px}
.frag .fempty{text-align:center;color:#8a8a8e;padding:40px 12px}
.frag .fbar{position:sticky;top:0;z-index:2;background:#0a0a0b;padding:10px 0;display:flex;align-items:center;gap:8px}


.media-img{max-width:min(260px,70vw);border-radius:12px;display:block;margin:4px 0;cursor:pointer}
.media-file{display:flex;align-items:center;gap:10px;padding:8px 10px;background:#ffffff12;border-radius:12px;margin:4px 0;cursor:pointer;color:inherit;text-decoration:none}
.media-voice{display:flex;align-items:center;gap:8px;margin:4px 0;min-width:180px}
.media-voice audio{height:32px;max-width:200px}
.vnote{width:180px;height:180px;border-radius:50%;object-fit:cover;display:block;margin:4px auto;background:#000}
.story-bar{display:flex;gap:10px;overflow-x:auto;padding:10px 12px 6px;scrollbar-width:none}
.story-bar::-webkit-scrollbar{display:none}
.story-item{flex:none;width:64px;text-align:center;cursor:pointer}
.story-item .ring{width:56px;height:56px;border-radius:50%;padding:2px;background:linear-gradient(135deg,#f58529,#dd2a7b,#8134af,#515bd4);margin:0 auto;box-sizing:border-box}
.story-item .ring.seen{background:#4a5568}
.story-item .ring>div{width:100%;height:100%;border-radius:50%;overflow:hidden;border:2px solid #0e1621;background:#1a2332;box-sizing:border-box}
.story-item .lab{font-size:11px;color:#aab8c5;margin-top:4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:64px}
.story-view{position:fixed;inset:0;background:#000;z-index:100;display:flex;flex-direction:column}
.story-view .prog{display:flex;gap:3px;padding:8px 10px;position:absolute;top:0;left:0;right:0;z-index:2}
.story-view .prog i{flex:1;height:3px;background:#ffffff44;border-radius:2px;overflow:hidden}
.story-view .prog i b{display:block;height:100%;background:#fff;width:0}
.story-view .sv-body{flex:1;display:flex;align-items:center;justify-content:center;position:relative}
.story-view .sv-top{position:absolute;top:18px;left:10px;right:10px;display:flex;align-items:center;gap:10px;z-index:2;color:#fff}
.story-add{position:absolute;right:-2px;bottom:-2px;width:18px;height:18px;border-radius:50%;background:#2aabee;color:#fff;font-size:14px;font-weight:700;display:flex;align-items:center;justify-content:center;border:2px solid #0e1621;line-height:1}
.story-bar{min-height:84px;border-bottom:1px solid #0b121a;background:#17212b}
.saved-av{background:linear-gradient(135deg,#6c8cff,#c65bff)!important;color:#fff;display:flex;align-items:center;justify-content:center;font-weight:700}

</style>
</head>
<body>
<svg width="0" height="0" style="position:absolute" aria-hidden="true"><defs><linearGradient id="pg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#6c8cff"/><stop offset="1" stop-color="#c65bff"/></linearGradient></defs></svg>

<div id="intro" class="hide">
 <div id="islides"></div>
 <div class="idots" id="idots"></div>
 <div class="isp"></div>
 <button class="ibtn" id="iGo">Начать общение</button>
</div>

<div id="auth" class="hide">
 <div class="acard">
  <!-- шаг 1: номер телефона -->
  <div id="s1">
   <div class="logo"><svg viewBox="0 0 24 24" width="72" height="72"><ellipse cx="12" cy="3.9" rx="5.2" ry="1.5" fill="none" stroke="#ffd86b" stroke-width="1.3"/><path d="M16.9 10.1A6.4 6.4 0 1 0 18.4 14.2H12.6" fill="none" stroke="#fff" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/></svg></div>
   <h2>GodGram</h2>
   <p class="sub c">Подтвердите код страны и введите свой номер телефона.</p>
   <select id="country" class="inp"></select>
   <div class="prow"><input id="cc" class="inp" value="+7" inputmode="tel" autocomplete="off"><input id="pn" class="inp" placeholder="Номер телефона" inputmode="tel" autocomplete="tel-national"></div>
   <label class="keep"><input type="checkbox" id="keep" checked> Оставаться в системе</label>
   <div class="err" id="e1"></div>
   <button class="btn" id="b1">ДАЛЕЕ</button>
  </div>
  <!-- шаг 2: код -->
  <div id="s2" class="hide">
   <div class="logo" style="width:96px;height:96px;font-size:44px">🔐</div>
   <h2><span id="phTitle"></span><span class="edit" id="phEdit" title="Изменить номер">✎</span></h2>
   <p class="sub c" id="codeHint"></p>
   <div id="demo" class="demo hide"></div>
   <input id="code" class="inp code" maxlength="5" placeholder="Код" inputmode="numeric" autocomplete="one-time-code">
   <div class="err" id="e2"></div>
   <div class="c"><a class="lnk" id="resend">Отправить код ещё раз</a></div>
  </div>
  <!-- шаг 3: имя и фото -->
  <div id="s3" class="hide">
   <div id="sAv"></div>
   <h2>Ваше имя</h2>
   <p class="sub c">Введите своё имя и добавьте фото профиля.</p>
   <input id="fn" class="inp" placeholder="Имя" maxlength="40">
   <input id="ln" class="inp" placeholder="Фамилия (необязательно)" maxlength="40">
   <div class="err" id="e3"></div>
   <button class="btn" id="b3">НАЧАТЬ ОБЩЕНИЕ</button>
  </div>
  <!-- шаг 4: облачный пароль -->
  <div id="s4" class="hide">
   <div class="logo" style="width:96px;height:96px;font-size:44px">🔒</div>
   <h2>Облачный пароль</h2>
   <p class="sub c" id="pwHint">Ваш аккаунт защищён облачным паролем. Введите его.</p>
   <input id="cpw" class="inp" type="password" placeholder="Пароль" autocomplete="current-password">
   <div class="err" id="e4"></div>
   <button class="btn" id="b4">ВОЙТИ</button>
  </div>
 </div>
</div>

<div id="app" class="hide">
 <aside id="side">
  <div class="gtop">
   <button class="gpill" id="menuBtn">Изм.</button>
   <div class="gtitle"><span id="meAv"></span>Чаты</div>
   <div class="gpair"><button class="gcirc" id="newBtn" title="Создать">⊕</button><button class="gcirc" id="fab" title="Новая группа">✎</button></div>
  </div>
  <div class="gfilt" id="filt"></div>
  <div class="sbox hide" id="sbox"><input class="search" id="search" placeholder="Поиск: имя, @юзернейм, номер"></div>
  <div class="story-bar" id="storyBar"></div><div id="list"></div>
  <div class="gnav">
   <div class="gtabs">
    <button class="gtab" id="tabContacts"><i>👥</i><span class="tl">Контакты</span></button>
    <button class="gtab on" id="tabChats"><i>💬</i><span class="tl">Чаты</span><span class="tb hide" id="tabBadge"></span></button>
    <button class="gtab" id="tabSettings"><i>⚙️</i><span class="tl">Настройки</span></button>
    <button class="gtab" id="tabProfile"><i id="tabProfAv">👤</i><span class="tl">Профиль</span></button>
   </div>
   <button class="gsearch" id="sbtn" title="Поиск">🔍</button>
  </div>
 </aside>
 <main id="main">
  <div id="empty">Выбери чат, чтобы начать общение</div>
  <div id="chat" class="hide">
   <div class="chead">
    <button class="icon back" id="backBtn">←</button>
    <span id="hAv"></span>
    <div class="who" id="hWho"><b id="hName"></b><span class="sub" id="hSub"></span></div>
    <button class="icon" id="hMenu">⋮</button>
   </div>
   <div id="warn" class="hide"></div>
   <div id="msgs"></div>
   <div class="comp" id="comp">
    <textarea id="txt" rows="1" placeholder="Сообщение"></textarea>
    <button class="send" id="sendBtn">➤</button>
   </div>
  </div>
 </main>
</div>

<script>
const $=s=>document.querySelector(s);
const GHOST='<svg viewBox="0 0 24 24" width="62%" height="62%"><path fill="#fff" d="M12 2C8.1 2 5 5.1 5 9v11.2c0 .6.7.9 1.1.5l1.6-1.5c.3-.3.8-.3 1.1 0l1.8 1.7c.3.3.8.3 1.1 0l1.8-1.7c.3-.3.8-.3 1.1 0l1.6 1.5c.4.4 1.1.1 1.1-.5V9c0-3.9-3.1-7-7-7z"/></svg>';
const LOGO='<svg viewBox="0 0 24 24" width="62%" height="62%"><ellipse cx="12" cy="3.9" rx="5.2" ry="1.5" fill="none" stroke="#ffd86b" stroke-width="1.3"/><path d="M16.9 10.1A6.4 6.4 0 1 0 18.4 14.2H12.6" fill="none" stroke="#fff" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/></svg>';
const SEND_LOGO='<svg viewBox="0 0 24 24" width="70%" height="70%"><path d="M5.5 12.5l4.8 4.8 8.4-9.6" fill="none" stroke="#1a1400" stroke-width="2.8" stroke-linecap="round" stroke-linejoin="round"/><circle cx="18.2" cy="6.2" r="3.4" fill="#fff"/><path d="M18.2 4.4l.55 1.2 1.3.18-1 .95.24 1.28-1.09-.6-1.09.6.24-1.28-1-.95 1.3-.18z" fill="#f0a500"/></svg>';
const OSINT_LOGO='<svg viewBox="0 0 24 24" width="70%" height="70%"><circle cx="10.2" cy="10.2" r="6.4" fill="none" stroke="#fff" stroke-width="2.5"/><path d="M15 15l5.2 5.2" fill="none" stroke="#fff" stroke-width="2.6" stroke-linecap="round"/><circle cx="10.2" cy="10.2" r="2.2" fill="#fff" opacity=".4"/></svg>';
const STAR='<svg viewBox="0 0 24 24" width="1em" height="1em" style="vertical-align:-0.14em"><path fill="url(#pg)" d="M12 2l2.9 6.3 6.9.7-5.1 4.7 1.5 6.8L12 17l-6.2 3.5 1.5-6.8L2.2 9l6.9-.7z"/></svg>';
const BIGSTAR='<svg viewBox="0 0 24 24" width="60%" height="60%"><path fill="#fff" d="M12 2l2.9 6.3 6.9.7-5.1 4.7 1.5 6.8L12 17l-6.2 3.5 1.5-6.8L2.2 9l6.9-.7z"/></svg>';
let token=localStorage.getItem('tok')||sessionStorage.getItem('tok')||'',me=null,chats=[],cur=null,ver=0,pollId=0,seenLast={},firstLoad=true;
let muted=JSON.parse(localStorage.getItem('muted')||'{}');
const sleep=ms=>new Promise(r=>setTimeout(r,ms));

function h(t,a={},...c){const e=document.createElement(t);
 for(const k in a){const v=a[k];if(v==null||v===false)continue;
  if(k==='class')e.className=v;else if(k.startsWith('on'))e.addEventListener(k.slice(2),v);
  else if(k==='html')e.innerHTML=v;else e.setAttribute(k,v===true?'':v);}
 c.flat(9).forEach(x=>{if(x==null||x===false)return;e.append(x.nodeType?x:document.createTextNode(x));});return e;}
// безопасная замена содержимого: понимает массивы и пропускает null
function setKids(el,...k){el.replaceChildren(...k.flat(9).filter(x=>x!=null&&x!==false));}

async function post(path,body){
 const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
 const j=await r.json().catch(()=>({}));if(!r.ok)throw new Error(j.error||'Ошибка');return j;}
async function api(path,body){
 const o={headers:{'Authorization':'Bearer '+token}};
 if(body!==undefined){o.method='POST';o.headers['Content-Type']='application/json';o.body=JSON.stringify(body);}
 const r=await fetch(path,o);const j=await r.json().catch(()=>({}));
 if(r.status===401){logout(true);throw new Error('auth');}
 if(!r.ok)throw new Error(j.error||'Ошибка');return j;}
async function refreshMe(){const j=await api('/api/me');me=j.me;}

function toast(t,fn){const e=h('div',{id:'toast',onclick:()=>{e.remove();fn&&fn();}},t);
 document.querySelectorAll('#toast').forEach(x=>x.remove());document.body.append(e);setTimeout(()=>e.remove(),3500);}

function color(s){let x=0;for(const ch of s||'?')x=(x*31+ch.charCodeAt(0))>>>0;return `hsl(${x%360},45%,42%)`;}
function av(o,size=48){
 const d=h('div',{class:'av'});d.style.width=d.style.height=size+'px';d.style.fontSize=(size*.42)+'px';
 if(o.deleted){d.classList.add('ghost');d.innerHTML=GHOST;}
 else if(o.service){d.classList.add('svc');d.innerHTML=LOGO;}
 else if(o.avatar_url){d.append(h('img',{src:o.avatar_url,loading:'lazy'}));}
 else if(o.username==='send'||(o.name==='Send'&&o.is_bot)){d.classList.add('bot-send');d.innerHTML=SEND_LOGO;}
 else if(o.username==='osint'||(o.name==='OSINT'&&o.is_bot)){d.classList.add('bot-osint');d.innerHTML=OSINT_LOGO;}
 else{const nm=(o.name||o.title||'?').trim();d.style.background=color(nm);d.textContent=([...nm][0]||'?').toUpperCase();}
 return d;}
const pad=n=>String(n).padStart(2,'0');
function fmtTime(ts){const d=new Date(ts*1000),n=new Date();
 if(d.toDateString()===n.toDateString())return pad(d.getHours())+':'+pad(d.getMinutes());
 return pad(d.getDate())+'.'+pad(d.getMonth()+1);}
function fmtDT(t){const d=new Date(t*1000);return pad(d.getDate())+'.'+pad(d.getMonth()+1)+'.'+d.getFullYear()+' '+pad(d.getHours())+':'+pad(d.getMinutes());}
function nftNameEl(x,u){
 if(!x.nft)return h('span',{class:'acc'},'@'+x.handle);
 return h('span',{class:'acc',style:'cursor:pointer',onclick:e=>{e.stopPropagation();nftNameInfo(x,u);}},'@'+x.handle);}
function nftNameInfo(x,u){
 const ov=h('div',{class:'overlay dim',style:'z-index:80',onclick:e=>{if(e.target===ov)ov.remove();}});
 const line=(l,v)=>h('div',{style:'display:flex;justify-content:space-between;gap:14px;padding:10px 0;border-top:1px solid #2c3e50'},h('span',{class:'sub'},l),h('b',{style:'text-align:right'},v));
 ov.append(h('div',{style:'background:#17212b;border-radius:18px;padding:22px;max-width:340px;width:100%'},
  h('div',{style:'font-size:22px;font-weight:600;margin-bottom:4px'},'@'+x.handle),
  h('div',{class:'sub',style:'margin-bottom:12px'},'NFT-юзернейм'),
  line('Цена',x.price>0?x.price.toLocaleString('ru-RU')+' ⭐':'не указана'),
  line('Выдан',x.issued>0?fmtDT(x.issued):'—'),
  line('Владелец',u.name),
  h('button',{class:'btn',style:'margin-top:14px',onclick:()=>ov.remove()},'Закрыть')));
 document.body.append(ov);}
function premDate(t){if(t>3e10)return 'навсегда';const d=new Date(t*1000);return pad(d.getDate())+'.'+pad(d.getMonth()+1)+'.'+d.getFullYear();}
function status(u){
 if(u.service)return 'служебные уведомления';
 if(u.is_bot)return u.frozen?'бот · заморожен ❄️':'бот';
 if(u.deleted||!u.last_seen)return 'был(а) давно';
 if(u.online)return 'в сети';
 const s=Date.now()/1000-u.last_seen;
 if(s<120)return 'был(а) только что';
 if(s<3600)return 'был(а) '+Math.floor(s/60)+' мин. назад';
 if(s<86400)return 'был(а) '+Math.floor(s/3600)+' ч. назад';
 if(s<172800)return 'был(а) вчера';
 return 'был(а) давно';}

/* ---------- v6: метки (верификации, СКАМ/ФЕЙК) ---------- */
function badgeInfo(b){
 const ov=h('div',{class:'overlay dim'});
 ov.append(h('div',{class:'modal',style:'text-align:center;max-width:360px'},
  b.key==='owner'?(()=>{const d=h('div',{style:'width:72px;height:72px;margin:0 auto 12px'});d.innerHTML=OWNER_SVG;return d;})()
  :h('div',{style:'width:72px;height:72px;border-radius:50%;margin:0 auto 12px;display:flex;align-items:center;justify-content:center;font-size:'+(b.icon.length>2?'16':'34')+'px;font-weight:700;color:#fff;background:'+b.color},b.icon),
  h('h3',{},b.title),h('div',{class:'sub',style:'font-size:14px;line-height:1.4;margin-bottom:6px'},b.descr),
  h('button',{class:'btn gray',onclick:()=>ov.remove()},'Понятно')));
 ov.onclick=e=>{if(e.target===ov)ov.remove();};document.body.append(ov);}
function nftStatusEl(s,big){
 const e=h('span',{class:'nfts'+(big?' big':''),title:s.name,style:'background:radial-gradient(circle at 40% 35%,'+s.c1+','+s.c2+')'});
 if(String(s.glyph).startsWith('svg:'))e.append(artEl(s.glyph));else e.textContent=s.glyph;
 if(!big)e.style.fontSize='.8em';return e;}
function statusEl(v,size){
 size=size||20;
 if(!v)return '';
 const s=String(v).trim();
 if(/^data:image\//i.test(s))
  return h('img',{class:'stimg',src:s,alt:'',draggable:'false',
   style:'width:'+size+'px;height:'+size+'px;border-radius:6px;object-fit:cover;vertical-align:middle;display:inline-block'});
 return s;}
const OWNER_SVG='<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="11" fill="#000" stroke="#6b7785" stroke-width="1.2"/><path d="M9.6 8.6 6.2 12l3.4 3.4M14.4 8.6 17.8 12l-3.4 3.4M13 7.6l-2 8.8" fill="none" stroke="#fff" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>';
function ownerIcon(b,style){const e=h('span',{class:'vb own',style:style||'',title:b.title,onclick:ev=>{ev.stopPropagation();badgeInfo(b);}});e.innerHTML=OWNER_SVG;return e;}
function ownerEl(u){const b=(u.badges||[]).find(x=>x.key==='owner');return b?ownerIcon(b,'margin-right:6px'):null;}
function badge(u,noOwner){
 if(!u||u.deleted)return null;
 const out=[];
 (u.badges||[]).filter(b=>!(noOwner&&b.key==='owner')).forEach(b=>b.key==='owner'?out.push(ownerIcon(b)):out.push(b.kind==='label'
  ?h('span',{class:'vlabel',style:'background:'+b.color,title:b.title,onclick:e=>{e.stopPropagation();badgeInfo(b);}},b.icon)
  :h('span',{class:'vb',style:'background:'+b.color,title:b.title,onclick:e=>{e.stopPropagation();badgeInfo(b);}},b.icon)));
 if(u.service)out.push(h('span',{style:'color:#2aabee;margin-left:4px'},'✔'));
 else if(u.is_bot)out.push(h('span',{class:'bt'},'БОТ'));
 else if(u.status_nft)out.push(nftStatusEl(u.status_nft));
 else if(u.emoji_status)out.push(h('span',{class:'pb'},statusEl(u.emoji_status,20)));
 else if(u.estatus)out.push(h('span',{class:'pb'},statusEl(u.estatus,20)));
 else if(u.premium){const s=h('span',{class:'pb'});s.innerHTML=STAR;out.push(s);}
 return out.length?h('span',{class:'bw'},out):null;}
const hasLabel=(u,k)=>!!(u&&(u.badges||[]).some(b=>b.key===k));
function warnEl(u){
 if(!u)return null;
 const bs=(u.badges||[]).filter(b=>b.kind==='label');
 return bs.length?bs.map(b=>h('div',{class:'pwarn',style:'background:'+b.color+'26;border:1px solid '+b.color+'80;color:#fff'},h('b',{},'⚠ '+b.title+'. '),b.descr)):null;}

/* ---------- закрепы и жалобы ---------- */
async function togglePin(c){
 try{await api('/api/pin',{chat_id:c.id,on:!c.pinned});await loadChats();toast(c.pinned?'Откреплено':'Закреплено 📌');}catch(e){toast(e.message);}}
function reportDialog(kind,id,name){
 const ov=h('div',{class:'overlay dim'});let reason='spam';
 const R=[['spam','Спам'],['scam','Мошенничество'],['fake','Выдаёт себя за другого'],['violence','Насилие'],['porn','Порнография'],['copyright','Нарушение авторских прав'],['other','Другое']];
 const txt=h('textarea',{class:'inp',rows:3,placeholder:'Комментарий (необязательно)',maxlength:500}),err=h('div',{class:'err'});
 ov.append(h('div',{class:'modal'},h('h3',{},'🚩 Пожаловаться'),h('div',{class:'sub',style:'margin-bottom:8px'},name||''),
  h('div',{class:'rep'},R.map(([k,t],i)=>h('label',{},h('input',{type:'radio',name:'rr',class:'ck',checked:i===0,onchange:()=>reason=k}),t))),txt,err,
  h('button',{class:'btn red',onclick:async()=>{try{await api('/api/report',{kind,id,reason,text:txt.value});ov.remove();toast('Жалоба отправлена админу');}catch(e){err.textContent=e.message;}}},'Отправить жалобу'),
  h('button',{class:'btn gray',onclick:()=>ov.remove()},'Отмена')));
 ov.onclick=e=>{if(e.target===ov)ov.remove();};document.body.append(ov);}

/* ---------- слайды-приветствие ---------- */
const SLIDES=[
 {ic:'logo',bg:'linear-gradient(135deg,#8b5cf6,#2aabee)',t:'GodGram',d:'Самый <b>быстрый</b> мессенджер в мире.<br>Он <b>бесплатный</b> и <b>безопасный</b>.'},
 {ic:'🚀',bg:'linear-gradient(135deg,#ff9a5c,#ff4d6d)',t:'Быстрый',d:'<b>GodGram</b> доставляет сообщения <b>быстрее</b>, чем любое другое приложение.'},
 {ic:'🎁',bg:'linear-gradient(135deg,#6fd67a,#2ba2b5)',t:'Бесплатный',d:'<b>GodGram</b> бесплатен навсегда. Никакой рекламы. Никакой платы за подписку.'},
 {ic:'💪',bg:'linear-gradient(135deg,#ffd86b,#ff8a3d)',t:'Мощный',d:'<b>GodGram</b> не накладывает ограничений на размер ваших чатов и медиафайлов.'},
 {ic:'🔒',bg:'linear-gradient(135deg,#b98cff,#6c5ce7)',t:'Безопасный',d:'<b>GodGram</b> надёжно защищает ваши сообщения от атак хакеров.'},
 {ic:'☁️',bg:'linear-gradient(135deg,#6ab7ff,#3b82e6)',t:'В облаке',d:'<b>GodGram</b> даёт доступ к вашим сообщениям с нескольких устройств.'}];
let introIdx=0,introGo=null;
function initIntro(){
 const tr=h('div',{class:'track'});
 SLIDES.forEach(s=>tr.append(h('div',{class:'slide'},
  h('div',{class:'iic',style:'background:'+s.bg,html:s.ic==='logo'?LOGO:s.ic}),h('h1',{},s.t),h('p',{html:s.d}))));
 $('#islides').append(tr);
 const dots=SLIDES.map((_,i)=>h('i',{onclick:()=>introGo(i)}));
 setKids($('#idots'),dots);
 introGo=n=>{introIdx=Math.max(0,Math.min(SLIDES.length-1,n));
  tr.style.transform='translateX('+(-introIdx*100)+'%)';dots.forEach((d,i)=>d.classList.toggle('on',i===introIdx));};
 introGo(0);
 let sx=null;
 $('#islides').addEventListener('pointerdown',e=>{sx=e.clientX;});
 const end=e=>{if(sx==null)return;const dx=e.clientX-sx;sx=null;if(Math.abs(dx)>40)introGo(introIdx+(dx<0?1:-1));};
 window.addEventListener('pointerup',end);window.addEventListener('pointercancel',()=>{sx=null;});
 window.addEventListener('keydown',e=>{if($('#intro').classList.contains('hide'))return;
  if(e.key==='ArrowRight')introGo(introIdx+1);if(e.key==='ArrowLeft')introGo(introIdx-1);});
 $('#iGo').onclick=()=>{$('#intro').classList.add('hide');$('#auth').classList.remove('hide');showStep(1);};}
function showIntro(){introGo&&introGo(0);$('#auth').classList.add('hide');$('#intro').classList.remove('hide');}

/* ---------- страны и телефон ---------- */
const COUNTRIES=[["🇷🇺","Россия","7"],["🇰🇿","Казахстан","7"],["🇺🇦","Украина","380"],["🇧🇾","Беларусь","375"],["🇺🇿","Узбекистан","998"],
["🇰🇬","Кыргызстан","996"],["🇹🇯","Таджикистан","992"],["🇦🇿","Азербайджан","994"],["🇦🇲","Армения","374"],["🇬🇪","Грузия","995"],
["🇲🇩","Молдова","373"],["🇱🇻","Латвия","371"],["🇱🇹","Литва","370"],["🇪🇪","Эстония","372"],["🇵🇱","Польша","48"],["🇨🇿","Чехия","420"],
["🇩🇪","Германия","49"],["🇫🇷","Франция","33"],["🇮🇹","Италия","39"],["🇪🇸","Испания","34"],["🇬🇧","Великобритания","44"],["🇺🇸","США","1"],
["🇨🇦","Канада","1"],["🇲🇽","Мексика","52"],["🇧🇷","Бразилия","55"],["🇹🇷","Турция","90"],["🇦🇪","ОАЭ","971"],["🇮🇱","Израиль","972"],
["🇪🇬","Египет","20"],["🇮🇳","Индия","91"],["🇨🇳","Китай","86"],["🇯🇵","Япония","81"],["🇰🇷","Южная Корея","82"],["🇹🇭","Таиланд","66"],
["🇻🇳","Вьетнам","84"],["🇮🇩","Индонезия","62"],["💎","Анонимные номера","888"]];
const DIALS=[...new Set(COUNTRIES.map(c=>c[2]))].sort((a,b)=>b.length-a.length);
function fmtPhone(d){
 if(!d)return 'Неизвестен';d=String(d);
 const cc=DIALS.find(c=>d.startsWith(c))||'';const rest=d.slice(cc.length);
 const g=(cc==='888'?rest.match(/.{1,4}/g):rest.match(/.{1,3}/g))||[];
 return '+'+cc+' '+g.join(' ');}
function fillCountries(){
 const s=$('#country');
 COUNTRIES.forEach((c,i)=>s.append(h('option',{value:i},c[0]+'  '+c[1])));
 s.onchange=()=>{$('#cc').value='+'+COUNTRIES[s.value][2];};
 $('#cc').oninput=()=>{
  const d=$('#cc').value.replace(/\D/g,'');$('#cc').value='+'+d;
  const i=COUNTRIES.findIndex(c=>d.startsWith(c[2]));if(i>=0)s.value=i;};
 $('#pn').oninput=()=>{$('#pn').value=$('#pn').value.replace(/[^\d\s\-()]/g,'');};}

/* ---------- вход как в Telegram ---------- */
let authPhone='',regToken='',regAvatar='';
let pwToken='';
function showStep(n){[1,2,3,4].forEach(i=>$('#s'+i).classList.toggle('hide',i!==n));
 ['#e1','#e2','#e3','#e4'].forEach(s=>$(s).textContent='');}
async function checkPw(){
 try{const j=await post('/api/auth/check_password',{pw_token:pwToken,password:$('#cpw').value});finishLogin(j.token);}
 catch(e){$('#e4').textContent=e.message;$('#cpw').select();}}
async function sendCode(resend){
 const phone=($('#cc').value+$('#pn').value).replace(/\D/g,'');
 $('#e1').textContent='';
 if(phone.length<(phone.startsWith('888')?6:7)){$('#e1').textContent='Введи номер телефона';return;}
 try{
  const j=await post('/api/auth/send_code',{phone});
  authPhone=phone;showStep(2);
  $('#phTitle').textContent=fmtPhone(phone);
  $('#codeHint').textContent=j.registered
   ?'Мы отправили код в чат «GodGram» на другом вашем устройстве, где вы уже вошли. Он также выведен в консоли сервера (Termux).'
   :'SMS не отправляются: это демо-сервер. Код показан ниже и в консоли Termux.';
  const dm=$('#demo');
  if(j.code){dm.classList.remove('hide');setKids(dm,'Ваш код: ',h('b',{},j.code));}else dm.classList.add('hide');
  $('#code').value='';$('#code').focus();
  if(resend)toast('Код отправлен ещё раз');
 }catch(e){(resend?$('#e2'):$('#e1')).textContent=e.message;}}
async function checkCode(){
 const code=$('#code').value.replace(/\D/g,'');if(code.length!==5)return;
 try{
  const j=await post('/api/auth/sign_in',{phone:authPhone,code});
  if(j.need_password){pwToken=j.pw_token;showStep(4);$('#cpw').value='';
   $('#pwHint').textContent='Ваш аккаунт защищён облачным паролем.'+(j.hint?' Подсказка: '+j.hint:'');$('#cpw').focus();}
  else if(j.registered)finishLogin(j.token);
  else{regToken=j.reg_token;showStep(3);$('#fn').focus();}
 }catch(e){$('#e2').textContent=e.message;$('#code').select();}}
function finishLogin(t){
 token=t;
 if($('#keep').checked){localStorage.setItem('tok',t);sessionStorage.removeItem('tok');}
 else{sessionStorage.setItem('tok',t);localStorage.removeItem('tok');}
 boot();}
async function signUp(){
 try{
  const j=await post('/api/auth/sign_up',{reg_token:regToken,first_name:$('#fn').value,last_name:$('#ln').value,avatar:regAvatar});
  finishLogin(j.token);
 }catch(e){$('#e3').textContent=e.message;}}
function initAuth(){
 fillCountries();initIntro();
 $('#b1').onclick=()=>sendCode(false);
 $('#pn').onkeydown=e=>{if(e.key==='Enter')sendCode(false);};
 $('#phEdit').onclick=()=>showStep(1);
 $('#resend').onclick=()=>sendCode(true);
 $('#code').oninput=()=>{$('#code').value=$('#code').value.replace(/\D/g,'');$('#e2').textContent='';if($('#code').value.length===5)checkCode();};
 $('#b3').onclick=signUp;
 $('#b4').onclick=checkPw;
 $('#cpw').onkeydown=e=>{if(e.key==='Enter')checkPw();};
 setKids($('#sAv'),avatarPicker({name:'＋'},d=>regAvatar=d));}

function logout(forced){
 if(!forced&&token)fetch('/api/logout',{method:'POST',headers:{'Authorization':'Bearer '+token}}).catch(()=>{});
 token='';localStorage.removeItem('tok');sessionStorage.removeItem('tok');pollId++;me=null;cur=null;chats=[];firstLoad=true;
 $('#app').classList.add('hide');
 document.querySelectorAll('.overlay,.menu').forEach(x=>x.remove());
 showStep(1);
 if(forced){$('#intro').classList.add('hide');$('#auth').classList.remove('hide');$('#e1').textContent='Сессия закончилась или аккаунт удалён';}
 else showIntro();}

async function boot(){
 if(!token){showIntro();return;}
 try{const j=await api('/api/me');me=j.me;ver=j.v;}catch(e){return;}
 $('#intro').classList.add('hide');$('#auth').classList.add('hide');$('#app').classList.remove('hide');
 await loadChats();startPoll();}

/* ---------- обновления в реальном времени (long-poll) ---------- */
async function startPoll(){
 const id=++pollId;
 while(id===pollId&&token){
  try{
   const r=await fetch('/api/poll?v='+ver,{headers:{'Authorization':'Bearer '+token}});
   if(r.status===401){logout(true);return;}
   const j=await r.json();
   if(id!==pollId)return;
   if(j.v!==ver){ver=j.v;await refresh();}
  }catch(e){await sleep(2000);}
 }}
async function refresh(){
 try{await refreshMe();}catch(e){}
 await loadChats();if(cur)await loadMessages();}
setInterval(()=>{if(token&&me)loadChats().catch(()=>{});},30000);

/* ---------- список чатов ---------- */
const muteKey=c=>c.kind==='private'?'u'+(c.peer&&c.peer.id):'c'+c.id;
async function loadChats(){setTimeout(function(){try{loadStoryBar();}catch(e){}},0);
 const j=await api('/api/chats');chats=j.chats;
 for(const c of chats){
  const lid=c.last?c.last.id:0;
  if(!firstLoad&&c.last&&!c.last.mine&&seenLast[c.id]!==lid&&(c.id!==cur||document.hidden)&&!muted[muteKey(c)]&&localStorage.getItem('gmute')!=='1')
   toast((c.kind==='group'?c.last.from+' · '+c.title:c.title)+(localStorage.getItem('npv')==='0'?': новое сообщение':': '+c.last.text.slice(0,60)),()=>openChat(c.id));
  seenLast[c.id]=lid;}
 firstLoad=false;
 const un=chats.reduce((s,c)=>s+(muted[muteKey(c)]?0:c.unread),0);
 document.title=(un?'('+un+') ':'')+'GodGram';
 const tb=$('#tabBadge');tb.textContent=un>99?'99+':un;tb.classList.toggle('hide',!un);
 if(me){setKids($('#meAv'),av(me,26));setKids($('#tabProfAv'),av(me,24));}
 paintFilt();renderList();if(cur)renderHeader();}

let filt='all';
const FILTS=[['all','Все'],['private','Личные'],['group','Группы'],['channel','Каналы'],['bot','Боты']];
const filtFn=c=>filt==='all'||(filt==='bot'?(c.kind==='private'&&c.peer&&c.peer.is_bot):(filt==='private'?(c.kind==='private'&&!(c.peer&&c.peer.is_bot)):c.kind===filt));
function paintFilt(){setKids($('#filt'),FILTS.map(([k,t])=>h('button',{class:filt===k?'on':'',onclick:()=>{filt=k;paintFilt();renderList();}},t)));}
function showContacts(){
 const ov=h('div',{class:'overlay dim'});let t=0;
 const find=h('input',{class:'inp',placeholder:'Поиск контактов'}),list=h('div',{style:'max-height:60vh;overflow-y:auto'});
 const load=async()=>{try{const j=await api('/api/users?q='+encodeURIComponent(find.value));
  setKids(list,j.users.map(u=>h('div',{class:'ur',onclick:()=>{ov.remove();startPrivate(u.id);}},av(u,44),h('div',{class:'mid'},h('b',{},u.name),badge(u),h('small',{},(u.username?'@'+u.username+' · ':'')+status(u))))));}catch(e){}};
 find.oninput=()=>{clearTimeout(t);t=setTimeout(load,200);};
 ov.append(h('div',{class:'modal'},h('h3',{},'👤 Контакты'),find,list,h('button',{class:'btn gray',onclick:()=>ov.remove()},'Закрыть')));
 ov.onclick=e=>{if(e.target===ov)ov.remove();};document.body.append(ov);load();}
$('#tabContacts').onclick=showContacts;
$('#tabSettings').onclick=()=>showSettings();
$('#tabProfile').onclick=()=>{if(me)showProfile(me.id);};
$('#tabChats').onclick=()=>{$('#list').scrollTo({top:0,behavior:'smooth'});};
$('#sbtn').onclick=()=>{const b=$('#sbox');b.classList.toggle('hide');if(b.classList.contains('hide')){$('#search').value='';searchRes=[];searchCh=[];renderList();}else $('#search').focus();};
$('#newBtn').onclick=e=>{e.stopPropagation();popMenu($('#newBtn'),[['👥 Новая группа',newGroup],['📢 Новый канал',newChannel],['🤖 Мои боты',showBots]],true);};
let searchRes=[],searchCh=[],searchT=0;
$('#search').oninput=()=>{clearTimeout(searchT);const v=$('#search').value.trim();
 if(!v){searchRes=[];searchCh=[];renderList();return;}
 renderList();searchT=setTimeout(async()=>{
  try{const j=await api('/api/users?q='+encodeURIComponent(v));searchRes=j.users;
   try{searchCh=(await api('/api/channels?q='+encodeURIComponent(v))).channels;}catch(e){searchCh=[];}
   renderList();}catch(e){}},250);};

function handleEl(u){return u.username?h('span',{class:u.nft_username?'nft':''},(u.nft_username?'💎 ':'')+'@'+u.username):null;}

function renderList(){
 const L=$('#list');L.innerHTML='';
 const v=$('#search').value.trim().toLowerCase();
 const arr=(v?chats.filter(c=>(c.title||'').toLowerCase().includes(v)):chats).filter(v?(()=>true):filtFn);
 arr.forEach(c=>{
  const last=c.last?((c.kind==='group'&&!c.last.mine?c.last.from+': ':c.last.mine?'Вы: ':'')+c.last.text):(c.deleted?'Аккаунт удалён':'Нет сообщений');
  L.append(h('div',{class:'row'+(c.id===cur?' on':''),onclick:()=>openChat(c.id),oncontextmenu:e=>{e.preventDefault();
    popMenu({getBoundingClientRect:()=>({bottom:e.clientY,left:e.clientX,right:e.clientX})},[[c.pinned?'📌 Открепить':'📌 Закрепить',()=>togglePin(c)]]);}},
   c.kind==='saved'?(()=>{const d=h('div',{class:'av saved-av'});d.style.width=d.style.height='52px';d.style.fontSize='22px';d.textContent='⭐';return d;})():av({deleted:c.deleted,service:c.peer&&c.peer.service,avatar_url:c.avatar_url,name:c.title},52),
   h('div',{class:'mid'},
    h('div',{class:'t'},h('span',{class:'n'},c.pinned?h('span',{class:'pin'},'📌 '):null,(c.kind==='channel'?'📢 ':'')+(c.title||'Без названия'),c.frozen?' ❄️':'',badge(c.kind==='private'?c.peer:c)),h('span',{class:'sub'},(c.last&&c.last.mine?h('span',{class:'ck2'},'✓'):null),c.last?fmtTime(c.last.ts):'')),
    h('div',{class:'p'},h('span',{},last),c.unread?h('span',{class:'badge'+(muted[muteKey(c)]?' m':'')},c.unread):(c.pinned?h('span',{class:'pinic'},'📌'):null)))));});
 if(v&&searchRes.length){
  L.append(h('div',{class:'sec'},'Люди'));
  searchRes.forEach(u=>L.append(h('div',{class:'row',onclick:()=>startPrivate(u.id)},
   av(u,48),h('div',{class:'mid'},h('div',{class:'n'},u.name,badge(u)),h('div',{class:'sub'},handleEl(u),(u.username?' · ':'')+status(u))))));}
 if(v&&searchCh.length){
  L.append(h('div',{class:'sec'},'Каналы'));
  searchCh.forEach(c=>L.append(h('div',{class:'row',onclick:()=>openChannel(c)},
   av({avatar_url:c.avatar_url,name:c.title},48),h('div',{class:'mid'},h('div',{class:'n'},'📢 '+c.title,c.frozen?' ❄️':''),h('div',{class:'sub'},c.members+' подписч.'+(c.joined?' · вы подписаны':'')))))); }
 if(!arr.length&&!(v&&(searchRes.length||searchCh.length)))L.append(h('div',{class:'sub',style:'padding:24px;text-align:center'},v?'Ничего не найдено':'Здесь пока пусто. Нажми 🔍, чтобы найти человека, или ✎, чтобы создать группу'));}

async function startPrivate(uid){
 try{const j=await api('/api/chat/private',{user_id:uid});
  $('#search').value='';searchRes=[];await loadChats();openChat(j.chat_id);
 }catch(e){toast(e.message);}}

/* ---------- чат ---------- */
function openChat(id){cur=id;$('#app').classList.add('open');$('#empty').classList.add('hide');$('#chat').classList.remove('hide');
 $('#msgs').innerHTML='';renderHeader();renderList();loadMessages(true);}
$('#backBtn').onclick=()=>{cur=null;$('#app').classList.remove('open');$('#chat').classList.add('hide');$('#empty').classList.remove('hide');renderList();};

const TXT=$('#txt'),SND=$('#sendBtn');
function renderHeader(){
 const c=chats.find(x=>x.id===cur);if(!c)return;
 if(c.kind==='saved'){const d=h('div',{class:'av saved-av'});d.style.width=d.style.height='42px';d.style.fontSize='18px';d.textContent='⭐';setKids($('#hAv'),d);setKids($('#hName'),'Избранное');}else{setKids($('#hAv'),av({deleted:c.deleted,service:c.peer&&c.peer.service,avatar_url:c.avatar_url,name:c.title},42));setKids($('#hName'),c.title||'Без названия',badge(c.kind==='private'?c.peer:c));}
 const wb=(c.kind==='private'?c.peer:c);const wl=((wb&&wb.badges)||[]).filter(b=>b.kind==='label');
 $('#warn').classList.toggle('hide',!wl.length);setKids($('#warn'),wl.length?h('div',{class:'warnbar'+(wl[0].key==='fake'?' f':'')},'⚠ '+wl.map(b=>b.title).join(' · ')+': '+wl[0].descr):null);
 $('#hSub').textContent=c.kind==='private'?status(c.peer):c.members+(c.kind==='channel'?' подписч.':' участн.');
 $('#hWho').onclick=()=>c.kind==='private'?showProfile(c.peer.id):(c.kind==='channel'?showChannel(c.id):showGroup(c.id));
 $('#hMenu').onclick=e=>{e.stopPropagation();const it=[[c.pinned?'📌 Открепить':'📌 Закрепить',()=>togglePin(c)],
  [muted[muteKey(c)]?'🔔 Включить звук':'🔕 Выключить звук',()=>{if(muted[muteKey(c)])delete muted[muteKey(c)];else muted[muteKey(c)]=1;localStorage.setItem('muted',JSON.stringify(muted));renderList();}]];
  if(c.kind==='channel')it.push(['ℹ️ О канале',()=>showChannel(c.id)]);
  if(c.kind==='private'&&c.peer&&!c.peer.service&&!c.deleted)it.push(['🚩 Пожаловаться',()=>reportDialog('user',c.peer.id,c.title)]);
  if(c.kind!=='private')it.push(['🚩 Пожаловаться',()=>reportDialog('chat',c.id,c.title)]);
  popMenu($('#hMenu'),it,true);};
 const svc=c.kind==='private'&&c.peer&&c.peer.service;
 const gift=c.kind==='private'&&c.peer&&!c.deleted&&!svc&&!c.peer.is_bot;
 let lock=null;
 if(c.kind==='private'&&(c.deleted||svc))lock=svc?'Служебные уведомления. Отвечать нельзя.':'Аккаунт удалён. Отправка сообщений недоступна.';
 else if(c.frozen)lock=c.kind==='private'?'❄️ Бот заморожен администратором.':(c.kind==='channel'?'❄️ Канал заморожен администратором.':'❄️ Группа заморожена администратором.');
 else if(c.kind==='channel'&&!c.can_post)lock='В канале пишет только владелец';
 const focused=document.activeElement===TXT;
 const comp=$('#comp');
 // Не пересоздаём composer, если textarea уже на месте — иначе на телефоне слетает клавиатура
 const needLock=!!lock;
 const hasLock=!!comp.querySelector('.locked');
 const hasTxt=comp.contains(TXT);
 if(needLock){
  if(!hasLock)setKids(comp,h('div',{class:'locked'},lock));
 }else if(!hasTxt||hasLock){
  setKids(comp,[gift?GBTN:null,h('button',{class:'icon',title:'Вложить',onmousedown:e=>e.preventDefault(),onclick:e=>{e.stopPropagation();attachMenu(e.currentTarget);}},'📎'),TXT,SND]);
  if(focused)try{TXT.focus({preventScroll:true});}catch(_){TXT.focus();}
 }else{
  // только обновить кнопку подарка при необходимости
  const hasGift=!!comp.querySelector('button[title="Подарок"]');
  if(gift&&!hasGift)comp.insertBefore(GBTN,TXT);
  if(!gift&&hasGift)comp.querySelector('button[title="Подарок"]').remove();
 }
}

async function loadMessages(scroll){
 const id=cur;if(!id)return;
 const keepFocus=document.activeElement===TXT;
 const j=await api('/api/messages?chat_id='+id+'&read=1');
 if(id!==cur)return;
 const box=$('#msgs');const near=box.scrollHeight-box.scrollTop-box.clientHeight<90;
 const c=chats.find(x=>x.id===id);const group=c&&c.kind==='group';
 const peerRead=j.peer_last_read||0;
 setKids(box,j.messages.map(m=>{
  const mine=m.uid===me.id;
  const ticks=mine?(m.id<=peerRead?h('span',{class:'ticks rd'},'✓✓'):h('span',{class:'ticks'},'✓')):null;
  const media=mediaEl(m);
  const bub=h('div',{class:'bub'},
   group&&!mine?h('div',{class:'who'+(m.sender.deleted?' d':''),onclick:()=>showProfile(m.sender.id)},m.sender.name,badge(m.sender)):null,
   m.gift?giftCard(m.gift):(media||m.text),
   h('span',{class:'tm'},fmtTime(m.ts),ticks));
  return h('div',{class:'m'+(mine?' me':'')},group&&!mine?av(m.sender,32):null,bub);}));
 if(scroll||near)box.scrollTop=box.scrollHeight;
 if(keepFocus)try{TXT.focus({preventScroll:true});}catch(_){TXT.focus();}
 const cc=chats.find(x=>x.id===id);
 if(cc&&cc.unread){cc.unread=0;renderList();loadChats().catch(()=>{});}}



function compressImageForUpload(file,maxSide,quality){
 return new Promise(async(res,rej)=>{
  try{
   if(await isAnimated(file)){const r=new FileReader();r.onload=()=>res(r.result);r.onerror=()=>rej(new Error('read'));r.readAsDataURL(file);return;}
   let bmp;
   if(typeof createImageBitmap==='function'){try{bmp=await createImageBitmap(file);}catch(e){}}
   if(!bmp){
    const data=await fileToDataURL(file);
    bmp=await new Promise((r,j)=>{const im=new Image();im.onload=()=>r(im);im.onerror=()=>j(new Error('bad image'));im.src=data;});
   }
   let w=bmp.width,h=bmp.height;
   const sc=Math.min(1,(maxSide||1280)/Math.max(w,h));w=Math.round(w*sc);h=Math.round(h*sc);
   const c=document.createElement('canvas');c.width=w;c.height=h;
   c.getContext('2d').drawImage(bmp,0,0,w,h);
   if(bmp.close)try{bmp.close();}catch(e){}
   let q=quality||0.85,out;
   for(let i=0;i<6;i++){out=c.toDataURL('image/jpeg',q);if(out.length<2_500_000)break;q-=0.1;}
   res(out);
  }catch(e){rej(e);}
 });
}

function mediaEl(m){
 if(!m.media_url)return null;
 const k=m.media_kind||'';
 if(k==='photo')return h('img',{class:'media-img',src:m.media_url,loading:'lazy',onclick:()=>window.open(m.media_url,'_blank')});
 if(k==='voice')return h('div',{class:'media-voice'},'🎤',h('audio',{controls:true,src:m.media_url,preload:'metadata'}));
 if(k==='video_note')return h('video',{class:'vnote',src:m.media_url,controls:true,playsinline:true});
 if(k==='video')return h('video',{src:m.media_url,controls:true,playsinline:true,style:'max-width:min(260px,70vw);border-radius:12px;margin:4px 0'});
 if(k==='file')return h('a',{class:'media-file',href:m.media_url,download:true,target:'_blank'},'📎',h('span',{},(m.text||'Файл').replace(/^📎 /,'')));
 return null;
}
function fileToDataURL(file){return new Promise((res,rej)=>{const r=new FileReader();r.onload=()=>res(r.result);r.onerror=()=>rej(new Error('Не удалось прочитать файл'));r.readAsDataURL(file);});}
async function sendMedia(kind,file,extra){
 if(!cur||!file)return;
 try{toast('Отправка…');const data=await fileToDataURL(file);
  const body=Object.assign({chat_id:cur,kind,data},extra||{});
  if(kind==='file')body.name=file.name||'file';
  await api('/api/send_media',body);await loadMessages(true);loadChats();
 }catch(e){toast(e.message);}
}
function attachMenu(btn){
 const photo=h('input',{type:'file',accept:'image/*',class:'hide'});
 const file=h('input',{type:'file',class:'hide'});
 const video=h('input',{type:'file',accept:'video/*',class:'hide'});
 photo.onchange=async()=>{if(!photo.files[0])return;try{const data=await compressImageForUpload(photo.files[0],1600,0.85);await api('/api/send_media',{chat_id:cur,kind:'photo',data});await loadMessages(true);loadChats();}catch(e){toast(e.message);}};
 file.onchange=()=>{if(file.files[0])sendMedia('file',file.files[0]);};
 video.onchange=()=>{if(video.files[0])sendMedia('video',video.files[0]);};
 popMenu(btn,[['📷 Фото',()=>photo.click()],['📎 Файл',()=>file.click()],['🎬 Видео',()=>video.click()],['🎤 Голосовое',()=>recordVoice()],['⭕ Кружок',()=>recordCircle()]],false);
 document.body.append(photo,file,video);
}
async function recordVoice(){
 try{
  const stream=await navigator.mediaDevices.getUserMedia({audio:true});
  const rec=new MediaRecorder(stream);const chunks=[];
  rec.ondataavailable=e=>chunks.push(e.data);const start=Date.now();
  rec.onstop=async()=>{stream.getTracks().forEach(t=>t.stop());
   const blob=new Blob(chunks,{type:rec.mimeType||'audio/webm'});
   await sendMedia('voice',new File([blob],'voice.webm',{type:blob.type}),{dur:Math.max(1,Math.round((Date.now()-start)/1000))});};
  rec.start();toast('Запись голоса… тапни чтобы остановить');
  const stop=()=>{if(rec.state==='recording')rec.stop();document.removeEventListener('click',stop);};
  setTimeout(()=>document.addEventListener('click',stop),400);
 }catch(e){toast('Нет доступа к микрофону');}
}
async function recordCircle(){
 try{
  const stream=await navigator.mediaDevices.getUserMedia({audio:true,video:{facingMode:'user',width:480,height:480}});
  const mt=MediaRecorder.isTypeSupported('video/webm;codecs=vp9,opus')?'video/webm;codecs=vp9,opus':'video/webm';
  const rec=new MediaRecorder(stream,{mimeType:mt});const chunks=[];
  rec.ondataavailable=e=>chunks.push(e.data);const start=Date.now();
  const ov=h('div',{class:'overlay dim',style:'z-index:90;display:flex;flex-direction:column;align-items:center;justify-content:center'});
  const v=h('video',{autoplay:true,muted:true,playsinline:true,style:'width:min(280px,80vw);height:min(280px,80vw);border-radius:50%;object-fit:cover;background:#000'});
  v.srcObject=stream;
  rec.onstop=async()=>{stream.getTracks().forEach(t=>t.stop());ov.remove();
   const blob=new Blob(chunks,{type:rec.mimeType||'video/webm'});
   await sendMedia('video_note',new File([blob],'circle.webm',{type:blob.type}),{dur:Math.max(1,Math.min(60,Math.round((Date.now()-start)/1000)))});
  };
  rec.start();
  ov.append(v,h('div',{class:'sub',style:'color:#fff;margin-top:10px'},'Кружок · до 60 сек'),
   h('button',{class:'btn',style:'width:auto;margin-top:12px',onclick:()=>rec.stop()},'Стоп · отправить'));
  document.body.append(ov);setTimeout(()=>{if(rec.state==='recording')rec.stop();},60000);
 }catch(e){toast('Нет доступа к камере');}
}
async function openSaved(){
 try{const j=await api('/api/saved',{});await loadChats();openChat(j.chat_id);}catch(e){toast(e.message);}
}
async function loadStoryBar(){
 const host=document.getElementById('storyBar');if(!host||!me)return;
 try{
  const j=await api('/api/stories');
  const items=[];
  const mine=(j.stories||[]).find(s=>s.mine);
  // кнопка «добавить» / свои истории
  items.push(h('div',{class:'story-item',onclick:()=>mine&&mine.count?openStoryViewer(me.id):openStoryEditor()},
   h('div',{class:'ring'+(mine&&mine.count?'':' seen')},h('div',{style:'position:relative'},av(me,48),h('span',{class:'story-add'},'+'))),
   h('div',{class:'lab'},'Моя')));
  (j.stories||[]).forEach(s=>{
   if(s.mine)return;
   items.push(h('div',{class:'story-item',onclick:()=>openStoryViewer(s.user.id)},
    h('div',{class:'ring'+(s.unseen?'':' seen')},h('div',{},av(s.user,48))),
    h('div',{class:'lab'},((s.user.name||'?').split(' ')[0]))));
  });
  // кнопка создать всегда отдельно если уже есть свои
  if(mine&&mine.count){
   items.splice(1,0,h('div',{class:'story-item',onclick:()=>openStoryEditor()},
    h('div',{class:'ring seen'},h('div',{style:'display:flex;align-items:center;justify-content:center;height:100%;font-size:28px;color:#2aabee'},'+')),
    h('div',{class:'lab'},'Добавить')));
  }
  setKids(host,items);
  host.style.display='flex';
 }catch(e){console.warn('stories',e);host.style.display='flex';
  setKids(host,[h('div',{class:'story-item',onclick:()=>openStoryEditor()},
   h('div',{class:'ring seen'},h('div',{},av(me,48))),h('div',{class:'lab'},'Моя'))]);
}}
function openStoryEditor(){
 const ov=h('div',{class:'overlay dim'});const close=()=>ov.remove();
 const file=h('input',{type:'file',accept:'image/*',class:'hide'});
 const txt=h('textarea',{class:'inp',rows:3,placeholder:'Текст истории',maxlength:300});
 const bg=h('div',{class:'sws'});let bgI=0;
 const paint=()=>setKids(bg,BANNERS.map(function(_,i){return h('div',{class:'swc'+(bgI===i?' on':''),style:'background:'+bannerBg(i),onclick:function(){bgI=i;paint();}})}));
 paint();
 file.onchange=async function(){if(!file.files[0])return;try{toast('Загрузка…');const data=await compressImageForUpload(file.files[0],1080,0.82);await api('/api/story/create',{kind:'photo',data:data,text:txt.value});toast('История опубликована');close();loadStoryBar();}catch(e){toast(e.message);}};
 ov.append(h('div',{class:'modal'},h('h3',{},'История'),
  h('div',{class:'sub'},'24 часа (Premium — 48)'),txt,h('div',{class:'lbl'},'Фон текста'),bg,
  h('button',{class:'btn',onclick:async function(){if(!txt.value.trim())return toast('Введи текст');try{await api('/api/story/create',{kind:'text',text:txt.value,bg:bgI});toast('История опубликована');close();loadStoryBar();}catch(e){toast(e.message);}}},'Опубликовать текст'),
  h('button',{class:'btn',onclick:function(){file.click();}},'Фото-история'),file,
  h('button',{class:'btn gray',onclick:close},'Отмена')));
 document.body.append(ov);
}
async function openStoryViewer(uid){
 let j;try{j=await api('/api/stories/user?id='+uid);}catch(e){return toast(e.message);}
 const list=j.stories||[];if(!list.length)return toast('Историй нет');
 let idx=0,tm;
 const ov=h('div',{class:'story-view'});
 const prog=h('div',{class:'prog'}),top=h('div',{class:'sv-top'}),body=h('div',{class:'sv-body'});
 const close=function(){clearTimeout(tm);ov.remove();loadStoryBar();};
 function paint(){
  const s=list[idx];if(!s){close();return;}
  setKids(prog,list.map(function(_,k){return h('i',{},h('b',{style:'width:'+(k<idx?'100%':'0%')}))}));
  setKids(top,av(j.user,32),h('b',{},j.user.name),h('span',{style:'flex:1'}),h('button',{class:'icon',style:'color:#fff',onclick:close},'✕'));
  if(s.kind==='photo'&&s.media_url)setKids(body,h('img',{src:s.media_url,style:'max-width:100%;max-height:100%'}));
  else setKids(body,h('div',{style:'background:'+bannerBg(s.bg||0)+';width:100%;height:100%;display:flex;align-items:center;justify-content:center;font-size:28px;font-weight:700;color:#fff;padding:24px;text-align:center'},s.text||''));
  api('/api/story/view',{id:s.id}).catch(function(){});
  const bar=prog.children[idx]&&prog.children[idx].firstChild;
  if(bar){bar.style.transition='none';bar.style.width='0%';requestAnimationFrame(function(){bar.style.transition='width 5s linear';bar.style.width='100%';});}
  clearTimeout(tm);tm=setTimeout(function(){idx++;if(idx>=list.length)close();else paint();},5000);
 }
 ov.onclick=function(e){if(e.target===ov||e.target===body){idx++;if(idx>=list.length)close();else paint();}};
 ov.append(prog,top,body);document.body.append(ov);paint();
}


async function send(){
 const t=TXT.value.trim();if(!t||!cur)return;
 TXT.value='';TXT.style.height='auto';
 // Сразу возвращаем фокус — на мобильных иначе клавиатура закрывается
 try{TXT.focus({preventScroll:true});}catch(_){TXT.focus();}
 try{await api('/api/send',{chat_id:cur,text:t});await loadMessages(true);
  try{TXT.focus({preventScroll:true});}catch(_){TXT.focus();}}
 catch(e){toast(e.message);TXT.value=t;try{TXT.focus({preventScroll:true});}catch(_){TXT.focus();}}}
// mousedown preventDefault — кнопка не забирает фокус у textarea (клавиатура не слетает)
SND.onmousedown=e=>{e.preventDefault();};
SND.onclick=send;
TXT.onkeydown=e=>{if(e.key==='Enter'&&!e.shiftKey&&window.innerWidth>720&&localStorage.getItem('entsend')!=='0'){e.preventDefault();send();}};
TXT.oninput=e=>{e.target.style.height='auto';e.target.style.height=Math.min(e.target.scrollHeight,140)+'px';};

/* ---------- меню ---------- */
function popMenu(anchor,items,right){
 document.querySelectorAll('.menu').forEach(x=>x.remove());
 const m=h('div',{class:'menu'},items.map(([t,fn])=>h('div',{onclick:()=>{m.remove();fn();}},t)));
 document.body.append(m);
 const r=anchor.getBoundingClientRect();
 const mh=m.offsetHeight,mw=m.offsetWidth,vh=window.innerHeight,vw=window.innerWidth;
 // не уезжать за низ экрана — открываем вверх при необходимости
 let top=r.bottom+4;
 if(top+mh>vh-8) top=r.top-mh-4;
 if(top<8) top=8;
 if(top+mh>vh-8){top=Math.max(8,vh-mh-8);m.style.maxHeight=(vh-16)+'px';m.style.overflowY='auto';}
 let left;
 if(right){
  left=Math.min(r.right,vw-8)-mw;
  if(left<8) left=8;
 }else{
  left=Math.max(8,Math.min(r.left,vw-mw-8));
 }
 m.style.top=top+'px';m.style.left=left+'px';m.style.right='auto';
 setTimeout(()=>document.addEventListener('click',function f(){m.remove();document.removeEventListener('click',f);}),0);}
$('#menuBtn').onclick=e=>{e.stopPropagation();
 popMenu($('#menuBtn'),[['👤 Мой профиль',()=>showProfile(me.id)],['⭐ Избранное',openSaved],['👛 Кошелёк',showWallet],['⭐ Чеки Send',showChecks],['🔷 GodFragment',()=>showFragment()],['🛍 Маркет NFT',showMarket],['👥 Новая группа',newGroup],['📢 Новый канал',newChannel],['🤖 Мои боты',showBots],['⭐ GodGram Premium',showPremium],['💎 Мои NFT',myNft],['⚙️ Настройки',showSettings],
  ...(me.is_admin?[['🛡 Админ-панель',adminPanel]]:[]),['🚪 Выйти',()=>logout(false)]]);};
$('#fab').onclick=newGroup;

/* ---------- баннеры профиля ---------- */
const BANNERS=[['#ff8a8a','#d94841'],['#ffbe6b','#e5792a'],['#c3a0ff','#7254e0'],['#7be08a','#2f9e48'],['#6fe0ea','#25a0b4'],['#7cc0ff','#3a7be0'],
 ['#ff9ccb','#d8478f'],['#aab3c0','#586577'],['#ffe07a','#ff7a45'],['#ff72a0','#7a5cff'],['#46f08a','#0f9a85'],['#56b4ff','#00d8f5'],
 ['#3b5a7a','#14202c']];
const GOD_PATTERN='data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAIAAAAB2CAYAAAAA9ZvPAAAaU0lEQVR42u19e5hcVZXvb629z6mqfuRFJEIQroIzGFTGG0cujE6YmavDQAioU3EcuYg4gkEzkkDi6DdO087j4jgOIiGPzovwuKOpGYE8IHMdPs0ncvECnxcHGnkqhBASknRIp+t1zl7r/nFOVVd1V3V3Qifp7pz1fQ3dOadO7dr7t9b6rcfeBSSSSCKJJJJIIokkkkgiiSSSSCKJJJJIIokkkkgiiSSSSCKJJJJIIokkkkgiE0foqD57zhwDXJjM8mjJhRB0dkoyEYmMaQtAAPQP5l977ivCnzwY+i0m1F5oKIbJhFBiVjXOqDBHL1AlACAiVVUSAcAAM6DOaQiBBYMR3e+UCBAI4vsAiET3A1x9TnQljO4TwCgpg5SZEUIg4kgg0esBMBgCwLIFA1BVEgAiEj+HwWxBpAoAzAx1SiICgcAYoyyoDCQSERBFz2EwQjgCjMZDhUCJGRCnxGBAQdE1gnAo5CBivLQl0zLZyz8+C/t+kMvlAgA6GotljwIAFAAVtOU5lPoeUdUlRX/KHwaGYIIyRMPokxNAFfxRxapF/w6uwEihpFVcUQwvBQGqACkAjX41BCaGavw3AUQUXa8MqgpRAhRQozVDju4XBTh+HRRQFaipPKtfZ4go+tUAyho/XwFWgBhEBFUFKq+NMBOPRQAFCByNRTT+XPG4NRo1Gw/se+BSb49B+D0tyPbclo3BaOotHQsTc8YnrzuvL/AXBU4+XmbP1yAMoXCAMCCEeHIp1nAdNMD+hawftAKQeNE40sg6APQ/xzltoDRUs7D1CK7+VwUEApEZcFX7AVy5ohItHHEFDvHIqzdUIRCByMT/7OLRWFGCgChNxsJD6dl2o98739+9PpfLFcYbCQQ6OqIV7ewUAvDuuVf9bo9OuiEQMz/0PHLlIkjUKampH4qOcJgNoELU8H5Via1F/bOJTGQQVId8dgUo0XOajW3wa2qfW3ltxUEoVX4TBcjB+JaNQSosvuQZd+uZ/Or6RzZt6gUAZDca5ObLaJn+Y2YBGgHhrEu+/KED4BuKItnApIjKJQVUQDAANViMAYNuoLWx3ayZ3iZ6PQgAjaegcl/9dUXF2FNDkNW/hmKX1Py9VaEQZWvY+rAS7mij4q3vd72rt2275yAAYE6HxfZON9oLf3RJYEcHNQxXOjoY3d2EXM4RgDPmXXHegfKUJUr+JwNjoGGgJE4EYhpTyzqDOnjxSON7DoOw1C6aKpgIAgwLwoFjUNWmYGr8piQwJlr4sLQjTeHyd7aWuh7Nrd0/zMI3n98xZQGyGw1yWYmZ2pBAeMdlCy/IO9xYUnzcIQUpF2ssQs3iVieQRg0AFXKGAQAYeF1HAQBEpAA5JWvZWFgpvtpi3LIz26Xr4f+1oida+DkW27c31vhs1iCXc2PUAigBpOd/+pqz1djw0buXv9CP5JtcUyDUuIbTLrvqI4WgfXFJ+PLQMBCEShBRqFEavPC1Wjpi7RtCKQkYDIABlmKk1qDeFVQW3li2HlgKr2dcedn09v0runO5/cOa+mzWIDdLgU5hAOd8/Jr/erbteTI3CmCg0QbTBz+14LRdh/hBZ7wtM80btz5x7z27+j/ERhkJEGbOu/r386H/1RJ5FwssKChL7OGNDgOAxr77KAKgxuoMHgMUgAN7lm0KNuzb57PcPrOlsOz/5da/MTKN71/40+Ze90cB+X/fRuHy5zctu3M0LMLouoB4QKd+7JpPFdpO/j4XDu7xNFhz0mSsfPoHK3YctkW49PMf7Q29rwm1/EFADIQlgUrEroYxu8MB4UiBMhQgap6lUAiYDVkfLIX9bUqrZ/DB257ceufOES38rFlamYczLl/4sd6yLna25Y+5XPg/Pdtuu0A7Ong0eMDoc4Bs1lAu56Zf8sX7ijztMoXCIt+TlvKq0317+/+997ZXhwXCQORf+qW5fU6Xlij9EacKhIEDKUUue4wBQNWBPcNeCuTyb2Yga072+m59+v47dgwbznV0MDoBIFr4d869+o8OStuS0Ng/DtiCXKl8mvbO/tXWdU+NFh8YfQBEyNSz/+R/vHsnpv3CkWdVAp+sh7SE+33r1pxEvcvqJ6QJWYw+pABQBvCOuV++/E2HpQH750sMBIKSEnEzE93IXbwVl9E0jFQ4EBn2MzBSLmQ4WD8ZpX96bvOaXx+u5Ttr3jW/vz/0lwRk5zpOg8J8AJvyUi6/bN8Dty+MQTQqZPDoRAHxAE+Zu/CbfV7rN4LCoRBCxNYz8H3YIN+TQbBqRossezK3auewHKEGCKqg0y9eML9PaUnRpGarEhCWnUaOmI8lAKoaT4bZS5ENA0mxu3uyn7/5+fvWPjOiaKjW5V1+zYcLQWZpAL40IA8ahMokIVkyRsM9v+XJex+7b3oULWBsh4GEjg6au2tX+mc7vF+WKPUuuDB22OqIrWXPgwmL+9IIV5/UJsvrOULnsGHQxizMX/Zd8+mipm4Ibfp3xCk0LDpAiYlYVBuGbm81dKwCQOHAYLJpMuKQYt043RS+/eymrscHurHhwuB3/snnf28/eTcqpS531IIw6FNSEWJjADib8sxkyX9ux+bld4x2KHgU8wDRQN9x8bWXHeC2+8LAOaJqgkcVcETWkmfhSWF/hnTFaeneZY/lNrw+UOuHAsKqa2Z7nbvO+0xR+MYSZ87RMARc2UkUlvLIs4gjTeKIKAhsU0wqSCHYPNWXm1+8f8UjAwnccAt/1uXXfqgnMF8tifmEs2lQOVCVUJTVAAwCO/Y8k9bCo29suf0Cmp/lMZwHaE4Ip1284H8XuP2jWs672uQOESsROWK2ZD0YV3ojY9yK0zN7l//8+9/ffTgWYeFFF6X+1bzryrzwooDT71EJARc4hXJ9WagBEEhRSfE3dgUEqIoSQNZnqwKr4UPtjJt3bF32H3oYCw8A75n3xQ/scf6SUPlTzvgclvNqFCK1iS9ACVasB5qRKl/w/L8t//kYTwQ1J4RnXnrlrL0y9YlyqB7g6up0RFyprzsYzxrjwwaF3T6Xl5+VKa54uBovD5EazfZrRjabzfy0d8bnCoRFofHPck5AYeCUBgOBKhnAoQCgKkqkbNPGEMFzhYfbbPitVzev3CID/PhwC/+uj1/93oPl9sWh8BVlk/IQFEHinLIaAgZkIOEo3WIyYc/6fQ+svvpoLP6xKQbFAz/l4mu/02vaFrtyMQTINmbsrAA5Msay8WBdeVfGlJa/W3pWbt/yL3sPEwhtD+dP/os+R9cHfvoMBCHgQqcQ7q8ZKqQmw1jP6lUAUvLThlnhudJjkxg379i87IdSUdDs/CYmuYORrdH4T153zr6CuaEIfEZtm+/KRaiUnUJNRQkG5afZaEr1zTMz5pwn7r3ldXTcREejHewYVAOVgJvo3Mt+M+nFUsvTIfmnkLioawIDmTlXmZYqCfnWWOPBBPldLeSWner1rnrsvrv2DZNIiXoRt28PAeDDl/z51Bdp2rV94C878ma6IAAkDBlkpFLS0wH5ekVcqPGQDktPpU3wj6/OXnkPdUIGAm3QZ60Bxfvmfea397hJiwpqrnLcmtKwDNLQOSizKkk8+wyKH1xdf8epjJkkb16/a+vqW4+W9h8jAPRbgbdftOCKXtt+lwRFR0Sm0gjRbGgUhU5CnjGGU2Apv5ox5ds+hN6u++/fcGAYslgHhNlzPz19J01dUBR/YcDe21xYAjlxcRdKVAlSiBrPGOPBuPLzaQ7/6VPuxQ23bdtWGroYo4Rsjiux+QcvWfCuV8TeUIK5Smy6xQUFqGrcADPMnCscrOWUFLvnn/6BD3Sd8ppDZ6diHJWDm4KAczl30ke/+OM+v+1ChIEDxAxda6t6bYWSkLGGjAcTFl9u4eCWD7fsXZvL5Q4dDhDOveTamXvIX3TI8TVi/XYplwBICDaWTQpeWNiZ5vJ3f7t1/8rt9c92Deev5tnv+cTVZ+wvZxaWxHwh4NZJkcYHTkay8DUAYM8zU+jQxTu3rH7waGr/MQcAcjn3Xy659tz9SD/uBKwKipo3R+jaKnV06xtjLTyXf6HNuO/84dT8HRs2bCgeDhBmXfr5s/a79NI8vKvUpDwTFnf7Vr/3PtOz6qGRuJnsxqrGn5u9cubu/OSvHBLzBfJSU1xQhopzUWJKaOTZRHLwUybjDm3ueXDlPD3Ki39sAVCTIZx+8YJlBW/Sl7RUCAGyQ7uCRq4BUWbNWmOI4btCdyvcLZfpb+46XHN99uULZh9y5sKZKNz9881rdx8Ovzjv0j+b8apMuy7P9rqQMtMlKAMioagYUPPEQhMAKJFVy1KamS6d+8wPV70wmo0fYwMAiEKm2XN3TXspTD1VIu9t0BCqwkdYkYmAYKwxbOBr6amMxbdvuWL3PfPn59zQhK2eqQ9rPWqe89+z2cnP5Gd8KQ/vL8WmZ4RhCerCUFXM4cxpXRpa1Jl0q2mVA9/cvbWrA8dA+48DAPqtwGmXXHf1AW5bK+Wi00YtYIeHBFGFkvGNYYYnpSfatfCtV7etzUmzxa6N1X8CblKoqVv4efOubn9CWj6XF77e2fQ7XSgABaE4MXGv+hHUEwACC1mffORf/uDU3vf+6MwzC0eT+B1fAMSTqrNm6fTH9zxSRMt5EpTqMoRHjoPYInhp4wHwpPhIa7r3H3bcu2GrDpe0GSLL+NnPfjb9o72tnyvCLg5N5qwwCKCu5IiIQYYqHcdHpPkAmKzzfM+0yr4/3bll3b8dK+0/XgDoJ4Tzrjlvr2t5xIWqRMKqjf3mSDp5K/dEmzogqqzqWWNJkZa+H03j8FvPb1n3kFZdUTMQ9F/r6JhjVz/+3s8U1S4JjX9O6ELAOadQomoJWo8Ap1rLaBynWkw6OPij/dtWfkyP4eIDGAWtOxLp7lZks+bAvXftmHzWuWeEXttsuFDisv/wqB2qdBtdI4UwXCAios60n1WEf2Xb2efNPuU973/5wLP//EpzhdiuqqC1v/jCnz2887Q7StyyIACd7IKyg4SIW9cpSlopjhSw1aCPGB5c+STb+4n9zz35BrJZwvbteqyWgnG8ZNYsBTr4pPShv+Yg36NsqNmMElH9TxQ/Np38qollw0RgCQuuFIrmkb50r2v52ckXL7j7oosWpmILSKhxD6df8hezp839ys/f1Gn/UqbUB8JywUlYEkBNTaoStWa/9j0HjreZ2Y3TG874aW6V8u0vbr7zaWQ3mmO9+/f4AaCzU5Dtpl/de8+uNg5uYutxg203DZVGSGui63iPIA3p5QzgSMuHwkAM8ur/6YH2fCsqGwAB4CfRftACzPmB3/674oISwqLE+XoejM0jV9J4f6CAPfaCwutntAd/D3Rw1DhybOX4AQAAcjlBNmsuPU1W+GHvk+SlDYHc8HmA2p05FJUblIY0xbGWGlVRI+UDqZZQGtEghslTuSwioYmIhI54k0hDKzSg7AyK+mIIrJ41nLHlbzyaW7sfc37CDbuGJjQAYjXq6uoKWsgtirZ/8wi0i/onFFpvCaJ4rKmIKrGWbVsQNCacIiwIGXrkylipNA76HBWgKhw8z3hB/vFbr+xZj2zWxIknnGgAAHI5h2zWvPZA14996d0IP2WincPDk6tI66lGqwgc24YKlDDQP4PA6gOY3Awi8eLV+/P6Fe4HXUPrUN2aTIPQXtntbOF0iq/Xxwmrt+ZTxjUAYkKoAM3IlJfaMH9oKELY2I7UuIMBhDFSxwbAebNphKGjEx0PNv1MClJx7PumJSje89LmVT/DMQ77xiYAOjsF2Sw/88N1L6epdDNbn6NTFIYO/yq+ts7XIyotVX5QsQsVUAAISfDm5GYTwtX9/U2BUAO42jHVjqfOMsSmn9UoGZ+sKx48uTX9NUApioaOn4wNAFQIYUcHz2lt+2cv7HsOI44KBkQDIxAh1qYmIArzY0sy6p9S2FpupeDv/vPe215Fdj4f70Ofxg4AAEV3N+VytxTa2C0mBmmTFa1oGBGBmACOTgVRGayFg7SRFKCw+dJKAJEQIi4+G6jxeyukembJQJ5Q+bv2vVlVxHpsyoeeeb+88j10dHBceEICgMGEcKsX5reQlzJoUCuum/CoZ+jwAg9qPu+GR9i7oSOu/USHUFF0DMo0w4u3bdtWQnf34fGcEwIANYTw7X5wo5GwALb9JyyNMO3a6Kfis1UJrNo0BgAAZjNkJq/ufWrYAjd5jag6eGmTDor3v/TA8m3Hm/iNbQDEhPDZTeuezWjxFuM1JoRHFpvHH3oIvXOQoehf9VnVMBNaNQbaDKNsyLpCYboXLlHguBO/sQ2AGkL47lODm03QtwM2w9TAbld5wJC5d6oBQNx7q6mmeQAiUlGBNOEQFBPESpRR1wZAWv1RSCUMddamuJXl1me2rnoe2Y08lk77HJsAiAnhI+vW9bZSaYnHUfd+vXfW+kwgNYsCKj6/5qRAQtMgQJ2S6BEqqFL9D1SIrbGub8fvTLH/M8r3zx9TR72OVQBUCeGuB9f+wAb5h+ClBxBC6u/orp38hqy98n8BqYNqSe30vDYhAKg952HYGL+hGdF41wOpx6DJHCzdds9tB5EdG8RvfACgZgmmpGWR58qBEpPU5WmH8LzUpEpYOYpzOM5ACiKAoXWp4Xpro80tgZBj6xsT9j38m62rfzCWiN/4AUBsBV66v+s/U1peQX6mPjmkw2TrakI1JY5/CNAUhXtbGr4wPoEY1cIUcbx/sZJarhxP229ZEJ9MqiTx36pgA0/ETcvgehpjWj+uLECULOng3zK4yQR9u8CWcTilukFbvyk63PeITVIMuhrwVXBBlcN+ASEvbTyU1rx435onxqr2jw8AAIo5P+GHt67oaTHB14y1VHOCdAMCSA3PFKx9HBFgrd8QBGy8qutpWM8b6D6oliswGKQwKfLC/L5TT9K/iYjfrMQCvCXZvj1ENmt2bem6M1U69FOyftOScV1/wBBJHEwZOmvQf6yz1h9TXUM2KzwB1ZAQUECsZ7iVS9/45d0r90TEb+x+ycP4AEA/f9OTWssLDcIgPmhZG0YAdeZ5QF5eAdIQ4d43GnMAF20IIo2y/aK1ech+Ylk9Y0j6W1AIcLApk3KFJ7772Z6umo0mSAAwSoTw+XvXP5mSQpfx0gZovqeMGrH3qgQ0ZC5YB3QW1f5SVwMY3PDha4ipCBcd70aPCWkBolPEOvgcSzd5YWGPssdQkWZr2LBZJ/Luaj2vycIYKCxApr/vUAdUIAdU+uI3dOyljE/lu5/fuvKnY5n4jV8AgBTZbtq+pWtvBoWvG8OsTTL7g6L0GkYnPJKew+FyNlELmgFAqnG+v/zmmS2pvxoLjR4TFAD9ruC189as913hUfL8oUvGdUmbiNuHTLSvSVMoG4BYAHJ17WWDns+V2J+gILHW51aU/+HR3C07MedCM16+3Wv8AaCyAJ2QGTZY5GlZ6whh06QQ97N3MWhWDHJhSHWkb4joNI4SRD3LXtD3q4+dXIwaPY5Th++JA4DYCvxq85pHUxqus36baXjKRE0SiKm6bxDWESY3KQYZYwYUdAZYlAH5BVVSq6BJXFy8YcOG4lhp9JjwFiBKrnTwKWl83XN9e2EMDcoQ6sCGzch3OwbebIYAYGS9gNHxRY68tPFc4b5XHlz/4HghfhMDAIi2lv3y3pV7UlT8GzKW0eB02P4evthok0C4uXt2qiM9y0eVDPmulD81ozeOtUaPEwAAqG4t+0hLT5dfLj1OtsUA9RnCQQ0jSgBETbMwkERVMUzZlwAY8Tyf27j0nafv63oRc+aY8fi1ruMbALGvzeVybpopfcUgVB3Rpo6hFFWGdQEEFbIe2zD/8gWt9h/HG/GbSACoEsIXH1j9SMbl7zQ207BOUE3kRF/4SK5JGAi11JD01SaYYNRjooylpbnc8kPjjfhNLAAA8VkDSjNTwdc913cAJtW060OBITs+hzsxgwBHnm9SYf7Hr29evnE8Er+JB4DOTkF2Pv9i07rXfBt+03oeE1iI+lvGqincKBncVFsdyVB7A5WYyWg5eFt7YdFE+B73iQGACiHs6ODLT9VlqbD3qbhkLPVk0IA42hfQlAQOFQYqhDyPM65v1dMb1z853rV/YgEg7iTu6uoKJlPfjQYCBWu08BwXhwQqAlFtygEMW4qqvlFJuD8aUFFmMmFp9/velropavTYKIkFGIOE8KWtd/y7L/lN8L2YEFId+5dhkRSxSKkjf6TWWp5kyn/90F2374saPUgTAIxBQqgAndxWvsGGpXy0e1RrKvgEJkLYLApwSqh8HyFXt385eL7xXfmxHbNPWTcRTP/EBUBnp2BOh+nOrX2hXd23rJ9hKA34Mg4lTGqeBwAkLvNWmR9SEJ2eksU0DpM9JxYAAGB7p0NHB/9eu/9tGx56Dp43oJPYAAcbI4CZQTBxuEggsLN+2rRI4e7n71v+8ETS/okLgJqzBqbY0mJLclj+utJ0zKpK1pCVYs/bveCvdBw1epzoAKgSwl9v6trqub4HyfMMQCGgYHKYNOlgYwdAVokMQAyFEc943K75v/3FpnWvjYUTPRIAHAEhnGHLNxpxJZBlIgPAar7gNTtqtPIdJQLrGVM61H1168Hbx8qJHgkADpcQZrPcvWl9dxrFZRy1j4GGOiZOHFQUAMPCYVpKv9KZy5XHc77/xAUAEHUSd3TwLK/4d16Y36nGqtJQW8sERA5kPd+Xwr++sGnFf0w04ndiAQCk6O6m7fdvONDGwVfJeCSi0uyAAEMqYKNWyr2ntrUuHa+NHgkAGhDC17auuoeDg49Caeq0dDpsov+ObYoyJvzmL3Pf+fVYO9EjAcBbEAEw2ZYW+VaKe3one43uKUPb/cKBndfOnv7dyPTPl4k8JycOAHI5h44O3rl5zaPtxt22N+ib2ui2Fs+4Fj9Y2tnZGVZzColMHEIAgLKLFmX+W3ZRpsE1nH/FF0/OZjce1rd/JZJIIuPQEhzJtUQSSSSRRBJJJJFEEkkkkUQSSSSRRBJJJJFEEkkkkUQSSSSRRBJJJJFEEkkkkTEs/x98g8kWbQCGywAAAABJRU5ErkJggg==';
const PATTERNS=['','⭐','❤️','🌸','🍀','🔥','❄️','🎮','🐱','🌙','💎','⚡','🎉','🔺',GOD_PATTERN];
const STATUSES=['','😎','🔥','🚀','👑','💎','🎮','🌈','❤️','🦄','🍀','⚡'];
const PAT=[[8,12,22],[22,6,16],[38,10,20],[62,8,18],[78,5,24],[92,14,18],[5,34,16],[15,52,24],[30,38,14],[70,36,14],[85,48,24],[95,32,16],
 [10,75,18],[26,84,22],[44,90,14],[58,86,18],[76,82,22],[90,72,16],[50,4,12],[3,60,14],[97,58,14],[20,24,12],[80,22,12],[35,72,12],[65,70,12]];
const bannerBg=i=>i>=0&&BANNERS[i]?'linear-gradient(160deg,'+BANNERS[i][0]+','+BANNERS[i][1]+')':'linear-gradient(160deg,#233140,#0f1720)';
function isBannerImg(p){return !!(p&&String(p).startsWith('data:image/'));}
function patternChipLabel(p){
 if(!p)return '✕';
 if(isBannerImg(p))return h('img',{src:p,alt:'logo',style:'width:22px;height:22px;object-fit:contain;display:block;border-radius:4px;background:#000'});
 return p;
}
function patternEl(p){
 const d=h('div',{class:'pat'});
 if(!p)return d;
 if(isBannerImg(p)){
  // своё фото/GIF как УЗОР — раскидываем миниатюры по точкам, как эмодзи
  d.classList.add('pat-custom');
  PAT.forEach(([x,y,s])=>{
   const e=h('img',{src:p,alt:'',draggable:'false',class:'pat-tile'});
   e.style.cssText='left:'+x+'%;top:'+y+'%;width:'+Math.max(18,s)+'px;height:'+Math.max(18,s)+'px';
   d.append(e);
  });
  return d;
 }
 PAT.forEach(([x,y,s])=>{const e=h('span',{},p);e.style.cssText='left:'+x+'%;top:'+y+'%;font-size:'+s+'px';d.append(e);});
 return d;}
function bannerStyle(b){
 if(!b)return DEFBAN;
 if(b.color>=0)return bannerBg(b.color);
 return DEFBAN;
}

/* ---------- профиль ---------- */
function toggle(on,fn){const t=h('div',{class:'tg'+(on?' on':'')});t.onclick=()=>{const v=!t.classList.contains('on');t.classList.toggle('on',v);fn(v);};return t;}
const pinfo=(v,l)=>h('div',{class:'pi'},h('div',{},h('div',{class:'v'},v),h('div',{class:'l'},l)));

/* ---------- собственные векторные модели NFT-подарков ---------- */
const SVGART={
 lollipop:'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><defs><radialGradient id="a" cx=".4" cy=".35"><stop offset="0" stop-color="#ffe27a"/><stop offset="1" stop-color="#ff5fa2"/></radialGradient></defs><rect x="46" y="56" width="9" height="42" rx="4.5" fill="#f1dcc8" transform="rotate(-32 50 60)"/><circle cx="42" cy="40" r="31" fill="url(#a)"/><path d="M42 40c3 0 5 3 3 6s-8 4-12-1-3-14 5-18 20-1 23 9" fill="none" stroke="#fff" stroke-width="5" stroke-linecap="round" opacity=".9"/></svg>',
 star:'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><defs><linearGradient id="a" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#7fd6ff"/><stop offset="1" stop-color="#7a5cff"/></linearGradient></defs><circle cx="50" cy="9" r="5" fill="none" stroke="#9fb4ff" stroke-width="3"/><path d="M50 14l9 21 23 2-17 15 5 23-20-12-20 12 5-23-17-15 23-2z" fill="url(#a)" stroke="#fff" stroke-width="2.5" stroke-linejoin="round"/><circle cx="42" cy="50" r="3.2" fill="#1b1b3a"/><circle cx="58" cy="50" r="3.2" fill="#1b1b3a"/><path d="M44 58q6 6 12 0" fill="none" stroke="#1b1b3a" stroke-width="2.6" stroke-linecap="round"/></svg>',
 wizard:'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><defs><linearGradient id="a" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#9a7bff"/><stop offset="1" stop-color="#4b3bd0"/></linearGradient></defs><path d="M20 76C34 66 38 40 52 14c4 20 20 46 30 62z" fill="url(#a)"/><path d="M26 66h56v8H26z" fill="#ffd54a"/><ellipse cx="50" cy="78" rx="42" ry="9" fill="#5a46e0"/><path d="M52 34l2.5 5 5.5.8-4 3.9.9 5.5-4.9-2.6-4.9 2.6.9-5.5-4-3.9 5.5-.8z" fill="#fff" opacity=".9"/></svg>',
 bells:'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><defs><linearGradient id="a" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#fff2a8"/><stop offset="1" stop-color="#f0a21f"/></linearGradient></defs><g transform="rotate(-14 30 40)"><path d="M12 62c3-4 3-10 4-18 1-12 8-20 15-20s14 8 15 20c1 8 1 14 4 18z" fill="url(#a)" stroke="#b9741a" stroke-width="2"/><circle cx="31" cy="68" r="5" fill="#d9531e"/></g><g transform="rotate(14 70 40)"><path d="M52 62c3-4 3-10 4-18 1-12 8-20 15-20s14 8 15 20c1 8 1 14 4 18z" fill="url(#a)" stroke="#b9741a" stroke-width="2"/><circle cx="71" cy="68" r="5" fill="#d9531e"/></g><path d="M50 22l-10-9 10 4 10-4z" fill="#ff4d6d"/><circle cx="50" cy="20" r="4" fill="#ff4d6d"/></svg>',
 wand:'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><defs><linearGradient id="a" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#ff8fd0"/><stop offset="1" stop-color="#d63fa0"/></linearGradient></defs><rect x="44" y="30" width="12" height="64" rx="6" fill="url(#a)" transform="rotate(38 50 62)"/><path d="M30 30l5 11 12 1-9 8 3 12-11-7-11 7 3-12-9-8 12-1z" fill="#ffe27a" stroke="#fff" stroke-width="2" stroke-linejoin="round" transform="translate(6 -8)"/><circle cx="80" cy="24" r="3" fill="#fff"/><circle cx="18" cy="70" r="2.5" fill="#fff"/></svg>',
 crystal:'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><defs><linearGradient id="a" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#b6f3ff"/><stop offset="1" stop-color="#3b8cff"/></linearGradient></defs><path d="M50 8L84 36 50 94 16 36z" fill="url(#a)" stroke="#fff" stroke-width="2.5" stroke-linejoin="round"/><path d="M16 36h68M36 36L50 8l14 28M36 36l14 58 14-58" fill="none" stroke="#fff" stroke-width="2" opacity=".75"/><path d="M50 8L36 36h28z" fill="#fff" opacity=".35"/></svg>',
 jester:'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><defs><linearGradient id="a" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#ff7a7a"/><stop offset="1" stop-color="#b3205c"/></linearGradient></defs><path d="M14 74C8 50 10 30 14 18c8 8 14 22 18 38 4-18 10-30 18-40 8 10 14 22 18 40 4-16 10-30 18-38 4 12 6 32 0 56z" fill="url(#a)" stroke="#fff" stroke-width="2.5" stroke-linejoin="round"/><rect x="12" y="68" width="76" height="14" rx="7" fill="#ffd54a" stroke="#fff" stroke-width="2"/><circle cx="14" cy="16" r="6" fill="#ffd54a"/><circle cx="50" cy="12" r="6" fill="#ffd54a"/><circle cx="86" cy="16" r="6" fill="#ffd54a"/></svg>',
 cupcake:'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><defs><linearGradient id="a" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#ffd0e6"/><stop offset="1" stop-color="#ff7fb6"/></linearGradient></defs><path d="M20 52h60L72 90H28z" fill="#f2b36b" stroke="#c9822f" stroke-width="2.5" stroke-linejoin="round"/><path d="M28 56v30M40 56v32M52 56v32M64 56v32" stroke="#c9822f" stroke-width="2" opacity=".5"/><path d="M16 54c-4-14 8-18 14-18 0-14 22-20 34-8 12-2 22 8 18 20 4 4 0 8-6 8z" fill="url(#a)" stroke="#fff" stroke-width="2.5" stroke-linejoin="round"/><circle cx="52" cy="18" r="7" fill="#e0243c"/><path d="M52 11q4-6 9-5" fill="none" stroke="#3a8f3a" stroke-width="2.5" stroke-linecap="round"/></svg>',
 balloon:'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><defs><linearGradient id="a" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#ff9ab8"/><stop offset="1" stop-color="#e0245e"/></linearGradient></defs><path d="M50 74C14 50 14 14 36 14c8 0 12 4 14 10 2-6 6-10 14-10 22 0 22 36-14 60z" fill="url(#a)" stroke="#fff" stroke-width="2.5" stroke-linejoin="round"/><path d="M50 74q-8 8 0 14t0 10" fill="none" stroke="#cfd6de" stroke-width="2.5" stroke-linecap="round"/><ellipse cx="34" cy="28" rx="6" ry="4" fill="#fff" opacity=".55" transform="rotate(-30 34 28)"/></svg>',
 giftbox:'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><defs><linearGradient id="a" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#6fe0b0"/><stop offset="1" stop-color="#1f9a8a"/></linearGradient></defs><rect x="14" y="40" width="72" height="50" rx="5" fill="url(#a)" stroke="#fff" stroke-width="2.5"/><rect x="8" y="28" width="84" height="18" rx="5" fill="#3cc4a4" stroke="#fff" stroke-width="2.5"/><rect x="44" y="28" width="12" height="62" fill="#ffd54a"/><path d="M50 28C34 6 18 14 28 24c6 6 22 4 22 4zM50 28C66 6 82 14 72 24c-6 6-22 4-22 4z" fill="#ffd54a" stroke="#fff" stroke-width="2.5" stroke-linejoin="round"/></svg>'};
function artEl(glyph){ // 'svg:имя[:угол]' -> <img> с оттенком
 const p=String(glyph).split(':'),src=SVGART[p[1]]||SVGART.star;
 const i=h('img',{src:'data:image/svg+xml;utf8,'+encodeURIComponent(src),draggable:'false'});
 if(p[2]&&+p[2])i.style.filter='hue-rotate('+(+p[2])+'deg)';
 return i;}
const fmtK=n=>n>=1000?(n/1000).toFixed(1).replace('.',',').replace(',0','')+'K':String(n);

/* ---------- NFT-подарки: карточка и просмотр ---------- */
const rarTxt=r=>(r/10).toFixed(r%10?1:0).replace('.0','')+'%';
/* Telegram-style rarity tiers (по промилле редкости атрибута) */
/* Уровни рейтинга подарков как в Telegram (пороги очков) */
const RATING_LEVELS=[0,10,25,50,100,200,350,550,800,1200,1800,2500,4000,6000,9000,15000,25000,50000,100000];
function ratingInfo(n){
 n=Math.floor(+n||0);
 if(n<0){
  // отрицательный: «уровень» 0, полоска показывает глубину минуса до -100
  const depth=Math.min(100,Math.abs(n));
  return {n,level:0,next:0,need:0,pct:depth,neg:true,label:String(n)};
 }
 let lv=0;
 for(let i=0;i<RATING_LEVELS.length;i++){if(n>=RATING_LEVELS[i])lv=i;else break;}
 const cur=RATING_LEVELS[lv],nxt=RATING_LEVELS[lv+1];
 let pct=100,need=0;
 if(nxt!=null){need=nxt-n;pct=Math.max(0,Math.min(100,Math.round((n-cur)/(nxt-cur)*100)));}
 return {n,level:lv,next:nxt,need,pct,neg:false,label:String(n)};
}
function ratingBadge(n,opt){
 opt=opt||{};
 const info=ratingInfo(n);
 const cls='stdot'+(info.neg?' neg':(info.n===0?' zero':''));
 return h('span',{class:cls,title:'Рейтинг '+(info.n>0?'+':'')+info.n+(info.neg?'':(' · ур. '+info.level)),
  onclick:opt.onclick||null,style:opt.onclick?'cursor:pointer':''},info.label);
}
function ratingBar(n){
 const info=ratingInfo(n);
 if(info.neg){
  return h('div',{class:'rbar-wrap'},
   h('div',{class:'rbar-meta'},h('span',{},'Рейтинг '+info.n),h('span',{},'восстанови подарками')),
   h('div',{class:'rbar neg'},h('i',{style:'width:'+info.pct+'%'})));
 }
 if(info.next==null){
  return h('div',{class:'rbar-wrap'},
   h('div',{class:'rbar-meta'},h('span',{},'Уровень '+info.level),h('span',{},'макс.')),
   h('div',{class:'rbar full'},h('i',{style:'width:100%'})));
 }
 return h('div',{class:'rbar-wrap'},
  h('div',{class:'rbar-meta'},h('span',{},'Уровень '+info.level),h('span',{},info.need+' до '+(info.level+1))),
  h('div',{class:'rbar'},h('i',{style:'width:'+info.pct+'%'})));
}
function fmtRating(n){return ratingBadge(n);} // совместимость для админки
function showRating(uid){
 const ov=h('div',{class:'overlay dim'});const box=h('div',{class:'modal'});
 (async()=>{try{const j=await api('/api/rating?id='+uid);const info=ratingInfo(j.rating);
  setKids(box,h('h3',{},'Рейтинг'),
  h('div',{style:'display:flex;justify-content:center;margin:8px 0 6px'},ratingBadge(j.rating)),
  ratingBar(j.rating),
  h('div',{class:'sub',style:'margin:12px 0 10px;text-align:center'},'Отправка подарков повышает рейтинг. Получатель может поставить 👍 или 👎. Метки СКАМ/ФЕЙК снижают.'),
  j.log.length?j.log.map(r=>h('div',{class:'rlog'},h('div',{},h('div',{},r.note||r.kind),h('div',{class:'sub'},(r.from?r.from+' · ':'')+fmtDT(r.ts))),h('div',{class:'d '+(r.delta>0?'pos':'neg')},(r.delta>0?'+':'')+r.delta))):
  h('div',{class:'sub'},'Пока нет изменений'),
  h('button',{class:'btn gray',onclick:()=>ov.remove()},'Закрыть'));
 }catch(e){setKids(box,h('div',{class:'err'},e.message),h('button',{class:'btn gray',onclick:()=>ov.remove()},'Закрыть'));}})();
 ov.append(box);ov.onclick=e=>{if(e.target===ov)ov.remove();};document.body.append(ov);}
function rarTier(r){
 r=+r||0;
 if(r<=20)return {key:'legend',title:'Легендарный',color:'#ffd86b'};
 if(r<=50)return {key:'epic',title:'Эпический',color:'#d4a8ff'};
 if(r<=100)return {key:'rare',title:'Редкий',color:'#7ec8ff'};
 return {key:'common',title:'Обычный',color:'#c5d0dc'};
}
function rarPill(r){const t=rarTier(r);return h('span',{class:'rar-pill rar-'+t.key},t.title+' · '+rarTxt(r));}
function itemRank(it){
 /* рейтинг уникальности как в TG Collectibles: чем меньше product rarity, тем выше ранг */
 const a=(+it.model.rar||1000)/1000,b=(+it.backdrop.rar||1000)/1000,c=(+it.symbol.rar||1000)/1000;
 const score=Math.max(0.0001,a*b*c);
 const pct=Math.min(99.9,Math.max(0.1,score*100));
 const est=it.supply?Math.max(1,Math.round(it.supply*score)):null;
 return {score,pct,est,tier:rarTier(Math.min(+it.model.rar||999,+it.backdrop.rar||999,+it.symbol.rar||999))};
}
function nftCard(it,size,opt){opt=opt||{};
 const rk=itemRank(it);
 const d=h('div',{class:'nftc',style:'background:radial-gradient(circle at 50% 38%,'+it.backdrop.c1+','+it.backdrop.c2+')',onclick:opt.onclick||(()=>nftPreview(it,opt.ctx))});
 d.append(patternEl(it.symbol.glyph),gift3d({glyph:it.glyph,model:'extrude',anim:it.anim,color:it.backdrop.c1},size||80),
  h('div',{class:'nn'},it.coll),h('div',{class:'nu'},'№'+it.number+(it.supply?' из '+fmtK(it.supply):'')),
  h('div',{class:'nu',style:'color:'+rk.tier.color+';font-weight:700'},rk.tier.title+' · '+rk.pct.toFixed(rk.pct<1?2:1).replace('.',',')+'%'),
  it.supply?h('div',{class:'rib'},h('span',{},'1 из '+fmtK(it.supply))):null);
 if(it.listed&&it.price)d.append(h('div',{class:'np'},fmtStars(it.price)));
 return d;}
function nftPreview(it,ctx){
 const ov=h('div',{class:'overlay dim'});const close=()=>ov.remove();
 const mine=it.owner&&it.owner.id===me.id;
 const panel=h('div',{style:'position:relative;border-radius:18px;overflow:hidden;padding:14px 0 10px;background:radial-gradient(circle at 50% 40%,'+it.backdrop.c1+','+it.backdrop.c2+')'},
  patternEl(it.symbol.glyph),h('div',{style:'position:relative'},gift3d({glyph:it.glyph,model:'extrude',anim:it.anim,color:it.backdrop.c1},200,true)));
 const attr=(l,v,r)=>h('div',{class:'attr'},h('span',{},l),h('b',{},v,rarPill(r)));
 const rk=itemRank(it);
 const acts=[];
 if(ctx&&ctx.market&&!mine)acts.push(h('button',{class:'btn',onclick:async()=>{
  try{const j=await api('/api/market/buy',{kind:'nft',id:it.id});close();toast('Куплено 🎉');await refreshMe();ctx.after&&ctx.after();}catch(e){toast(e.message);}}},'Купить за '+fmtStars(it.price)));
 if(mine){
  if(it.listed)acts.push(h('button',{class:'btn gray',onclick:async()=>{try{await api('/api/market/unlist',{kind:'nft',id:it.id});close();ctx&&ctx.after&&ctx.after();toast('Снято с продажи');}catch(e){toast(e.message);}}},'Снять с продажи'));
  else acts.push(h('button',{class:'btn',onclick:async()=>{const p=prompt('Цена в звёздах ⭐:','100');if(!p)return;
   try{await api('/api/market/list',{kind:'nft',id:it.id,price:+p});close();ctx&&ctx.after&&ctx.after();toast('Выставлено на маркет');}catch(e){toast(e.message);}}},'Продать на маркете'));
  const wearing=me.status_nft&&me.status_nft.id===it.id;
  acts.push(h('button',{class:'btn',style:'background:linear-gradient(90deg,#6c8cff,#c65bff)',onclick:async()=>{try{await api('/api/nft/wear',{id:wearing?0:it.id});close();await refreshMe();await refresh();toast(wearing?'Статус снят':'Теперь это твой эмодзи-статус ✨');ctx&&ctx.after&&ctx.after();}catch(e){toast(e.message);}}},wearing?'Снять эмодзи-статус':'✨ Носить как эмодзи-статус'));
  acts.push(h('button',{class:'btn gray',onclick:()=>{close();pickUser(async u=>{try{await api('/api/nft/send',{id:it.id,user_id:u.id});toast('Подарено 🎁');ctx&&ctx.after&&ctx.after();}catch(e){toast(e.message);}});}},'Подарить другу'));}
 ov.append(h('div',{class:'modal',style:'max-width:400px'},panel,h('h3',{style:'text-align:center;margin:10px 0 2px'},it.name),
  h('div',{class:'sub c',style:'margin-bottom:8px'},'Коллекция «'+it.coll+'»'+(it.owner?' · владелец '+it.owner.name:' · на маркете')),
  h('div',{style:'text-align:center;margin:6px 0 10px'},
   h('span',{class:'rar-pill rar-'+rk.tier.key,style:'font-size:13px;padding:4px 12px'},rk.tier.title),
   h('div',{class:'sub',style:'margin-top:6px'},'Рейтинг уникальности · топ '+rk.pct.toFixed(rk.pct<1?2:1).replace('.',',')+'%'+(rk.est?' · ~'+rk.est+' из '+(it.supply||'?')+' таких':''))),
  attr('Модель',it.model.name,it.model.rar),attr('Фон',it.backdrop.name,it.backdrop.rar),attr('Узор',it.symbol.name,it.symbol.rar),
  h('div',{class:'sub c',style:'margin:8px 0'},'Покрути подарок пальцем 👆'),acts,
  h('button',{class:'btn gray',onclick:close},'Закрыть')));
 ov.onclick=e=>{if(e.target===ov)close();};document.body.append(ov);}
function pickUser(cb){
 const ov=h('div',{class:'overlay dim'});let t=0;
 const find=h('input',{class:'inp',placeholder:'Кому: имя, @юзернейм или номер'}),list=h('div');
 find.oninput=()=>{clearTimeout(t);t=setTimeout(async()=>{const v=find.value.trim();if(!v){setKids(list);return;}
  try{const r=await api('/api/users?q='+encodeURIComponent(v));
   setKids(list,r.users.filter(u=>!u.is_bot&&!u.service).slice(0,8).map(u=>h('div',{class:'ur',onclick:()=>{ov.remove();cb(u);}},av(u,36),h('div',{class:'mid'},u.name,badge(u)))));}catch(e){}},250);};
 ov.append(h('div',{class:'modal'},h('h3',{},'Выбери получателя'),find,list,h('button',{class:'btn gray',onclick:()=>ov.remove()},'Отмена')));
 ov.onclick=e=>{if(e.target===ov)ov.remove();};document.body.append(ov);}

/* ---------- профиль в стиле Telegram ---------- */
const MON=['янв','фев','мар','апр','мая','июн','июл','авг','сен','окт','ноя','дек'];
function fmtBirth(s){const m=/^(\d{4})-(\d{2})-(\d{2})$/.exec(s||'');if(!m)return '';
 const n=new Date();let age=n.getFullYear()-+m[1];if(n.getMonth()+1<+m[2]||(n.getMonth()+1===+m[2]&&n.getDate()<+m[3]))age--;
 return +m[3]+' '+MON[+m[2]-1]+' '+m[1]+' ('+age+')';}
function isOpenNow(f,t){if(!f||!t)return null;const n=new Date(),c=n.getHours()*60+n.getMinutes();
 const p=x=>{const a=x.split(':');return +a[0]*60+ +a[1];};const a=p(f),b=p(t);return a<=b?(c>=a&&c<b):(c>=a||c<b);}
const DEFBAN='linear-gradient(180deg,#5d7186,#2f3b48)';
async function showProfile(uid){
 let u;try{u=(await api('/api/user?id='+uid)).user;}catch(e){return toast(e.message);}
 const ov=h('div',{class:'overlay pf'});const close=()=>ov.remove();
 const self=u.id===me.id;
 const canDel=me.is_admin&&!self&&!u.deleted&&!u.is_admin&&!u.service;
 const key='u'+u.id;
 const back=h('button',{class:'icon',style:'color:#fff;font-size:26px',onclick:close},'←');
 const right=h('button',{class:'icon',style:'color:#fff',onclick:()=>{if(self)showEdit(close);}},self?'✎':'');
 const b=u.banner;
 const names=u.usernames||[];
 const act=(ic,t,fn)=>h('button',{class:'pact',onclick:fn},h('i',{},ic),t);
 const acts=[];
 if(!self&&!u.service&&!u.deleted)acts.push(act('💬','Написать',async()=>{try{const j=await api('/api/chat/private',{user_id:u.id});await loadChats();close();openChat(j.chat_id);}catch(e){toast(e.message);}}));
 if(!self&&!u.service&&!u.deleted)acts.push(act(muted[key]?'🔕':'🔔','Звук',e=>{if(muted[key])delete muted[key];else muted[key]=1;localStorage.setItem('muted',JSON.stringify(muted));e.currentTarget.firstChild.textContent=muted[key]?'🔕':'🔔';}));
 if(!self&&!u.service&&!u.deleted&&!u.is_bot)acts.push(act('🎁','Подарок',async()=>{try{const j=await api('/api/chat/private',{user_id:u.id});await loadChats();close();openChat(j.chat_id);setTimeout(openGiftShop,150);}catch(e){toast(e.message);}}));
 if(self){acts.push(act('✎','Изменить',()=>showEdit(close)),act('👛','Кошелёк',()=>{close();showWallet();}),act('🔷','Fragment',()=>{close();showFragment();}),act('🛍','Маркет',()=>{close();showMarket();}));}
 const more=[];
 if(!self&&!u.service&&!u.deleted)more.push(['🚩 Пожаловаться',()=>reportDialog('user',u.id,u.name)]);
 if(names.length)more.push(['📋 Скопировать @'+names[0].handle,()=>{try{navigator.clipboard.writeText('@'+names[0].handle);toast('Скопировано');}catch(e){}}]);
 if(canDel)more.push(['🗑 Удалить '+(u.is_bot?'бота':'аккаунт'),()=>deleteUser(u,close)]);
 if(me.is_admin&&!self&&!u.service&&!u.deleted)more.push(['🏷 Метки…',()=>{close();adminBadgeDialog('user',u.id,u.name,u.badges);}]);
 if(more.length)acts.push(act('⋯','Ещё',e=>{e.stopPropagation();popMenu(e.currentTarget,more,false);}));
 const sn=u.status_nft;
 const head=h('div',{class:'pfh',style:'background:'+(u.deleted?'#1a1a1c':(sn?'radial-gradient(circle at 50% 30%,'+sn.c1+','+sn.c2+')':bannerStyle(b)))},
  u.deleted?null:patternEl(sn?sn.symbol:(b?b.pattern:'')),h('div',{class:'pbar2'},back,right),
  av(u,112),h('h1',{},ownerEl(u),u.name,badge(u,true)),
  h('div',{class:'st'},
   u.deleted||u.is_bot?null:ratingBadge(u.rating,{onclick:()=>showRating(u.id)}),
   self?'в сети':status(u)),
  (!u.deleted&&u.status_text)?h('div',{class:'pmusic'},'♫ '+u.status_text):null,
  h('div',{class:'pacts'},acts));
 const rows=[];
 const row=(l,v,cls)=>h('div',{class:'prow2'},h('div',{class:'l'},l),h('div',{class:'v'+(cls?' '+cls:'')},v));
 if(!u.deleted){
  const ob=(u.badges||[]).find(x=>x.key==='owner');
  if(ob)rows.push(h('div',{class:'prow2'},h('div',{class:'l'},'верификация'),h('div',{class:'v',style:'display:flex;align-items:center'},ownerEl(u),ob.descr)));
  if(names.length){const mains=names.filter(x=>x.main),rest=names.filter(x=>!x.main);
   const lst=a=>a.map((x,i)=>[nftNameEl(x,u),i<a.length-1?', ':null]);
   rows.push(h('div',{class:'prow2'},h('div',{class:'l'},'имя пользователя'),
    h('div',{class:'v acc',style:'font-size:24px;font-weight:600;line-height:1.3'},lst(mains)),
    rest.length?h('div',{class:'also acc',style:'font-size:17px;margin-top:4px'},lst(rest)):null));}
  if(!u.is_bot&&u.phone)rows.push(row('телефон',fmtPhone(u.phone),u.nft_number?'nft':''));
  if(self&&u.nft_number&&u.real_phone)rows.push(row('реальный номер (виден только тебе)',fmtPhone(u.real_phone)));
  if(u.birthday)rows.push(row('день рождения',fmtBirth(u.birthday)));
  if(u.work_from&&u.work_to){const o=isOpenNow(u.work_from,u.work_to);
   rows.push(h('div',{class:'prow2'},h('div',{class:'l'},'часы работы'),h('div',{style:'display:flex;justify-content:space-between;margin-top:3px'},
    h('span',{class:o?'open':'closed',style:'font-size:19px'},o?'Открыто':'Закрыто'),h('span',{class:'sub',style:'font-size:17px'},u.work_from+' - '+u.work_to))));}
  if(u.address)rows.push(row('адрес',u.address));
  if(u.is_bot&&u.bot_desc)rows.push(row('о боте',u.bot_desc));
  if(u.bio)rows.push(h('div',{class:'prow2'},h('div',{class:'l'},'о себе'),h('div',{class:'v',style:'white-space:pre-wrap;font-size:17px'},u.bio)));}
 const body=h('div',{class:'pbody'},warnEl(u),
  (u.pchannel)?[h('div',{class:'pcap'},'канал'),h('div',{class:'pch',onclick:()=>{showChannel(u.pchannel.id);}},av({avatar_url:u.pchannel.avatar_url,name:u.pchannel.title},48),
    h('div',{class:'mid'},h('div',{class:'nm'},u.pchannel.title,badge(u.pchannel)),h('div',{class:'sub'},u.pchannel.members+' подписчиков')))]:null,
  rows.length?h('div',{class:'pcard'},rows):null,
  (!u.deleted&&u.nft_gifts&&u.nft_gifts.length)?[h('div',{class:'pcap'},'NFT-подарки · '+u.nft_gifts.length),h('div',{class:'nftgrid'},u.nft_gifts.map(g=>nftCard(g,70,{ctx:{market:g.listed,after:()=>{close();showProfile(uid);}}})))]:null,
  (!u.deleted&&u.gifts&&u.gifts.length)?[h('div',{class:'pcap'},'Подарки · '+u.gifts.length),h('div',{class:'ggrid'},u.gifts.map(g=>h('div',{class:'gitem',style:'background:#1c1c1e;border-color:#2c2c2e',onclick:()=>giftPreview(g)},gift3d(g,68),h('div',{class:'gt'},g.name),h('div',{class:'gs'},g.number?'№'+g.number:''))))]:null,
  (self&&((u.gifts&&u.gifts.length)||(u.nft_gifts&&u.nft_gifts.length)))?h('button',{class:'btn red',style:'margin-top:16px',onclick:async()=>{
    if(!confirm('Удалить ВСЕ подарки с профиля ('+((u.gifts||[]).length+(u.nft_gifts||[]).length)+' шт.)? Это необратимо.'))return;
    try{const j=await api('/api/gifts/clear',{kind:'all'});toast('Удалено подарков: '+j.removed);close();await refreshMe();showProfile(uid);}catch(e){toast(e.message);}}},'🗑 Удалить все подарки'):null,
  self?h('button',{class:'btn gray',style:'margin-top:16px',onclick:()=>{close();myNft();}},'💎 Мои NFT-имена и номера'):null,
  self?h('button',{class:'btn gray',onclick:showPremium},'⭐ GodGram Premium'):null);
 ov.append(head,body);document.body.append(ov);}

async function deleteUser(u,close){
 if(!confirm('Удалить аккаунт «'+u.name+'»?\nОн станет «Удалённым аккаунтом» для всех. Это необратимо.'))return;
 try{await api('/api/admin/delete',{user_id:u.id});toast('Аккаунт удалён');close&&close();await refresh();}
 catch(e){toast(e.message);}}

async function isAnimated(file){
 if(file.type==='image/gif')return true;
 try{const b=new Uint8Array(await file.slice(0,65536).arrayBuffer());const t=new TextDecoder('latin1').decode(b);
  if(file.type==='image/webp')return t.includes('ANIM');
  if(file.type==='image/png')return t.includes('acTL');}catch(e){}
 return false;}
function fileToAvatar(file,size=320){return new Promise(async(res,rej)=>{
 try{
  if(!file){rej(new Error('Файл не выбран'));return;}
  const t=(file.type||'').toLowerCase();
  if(/heic|heif/.test(t)||/\.heic$/i.test(file.name||'')){rej(new Error('HEIC не поддерживается — экспортни в JPG'));return;}
  if(file.size>12_000_000){rej(new Error('Файл слишком большой (макс. 12 МБ)'));return;}
  if(await isAnimated(file)){
   if(file.size>2_500_000){rej(new Error('Анимация слишком большая'));return;}
   const r=new FileReader();r.onload=()=>res(r.result);r.onerror=()=>rej(new Error('Не удалось прочитать файл'));r.readAsDataURL(file);return;
  }
  async function fromBitmap(bmp){
   const c=document.createElement('canvas');c.width=c.height=size;
   const s=Math.min(bmp.width,bmp.height)||size;
   const ctx=c.getContext('2d',{alpha:false});
   ctx.fillStyle='#1a1a1c';ctx.fillRect(0,0,size,size);
   ctx.drawImage(bmp,(bmp.width-s)/2,(bmp.height-s)/2,s,s,0,0,size,size);
   if(bmp.close)try{bmp.close();}catch(e){}
   // сжимаем пока не влезет (~1.2 МБ data URL)
   let q=0.9,out;
   for(let i=0;i<8;i++){
    out=c.toDataURL('image/jpeg',q);
    if(out.length<1_200_000)break;
    q-=0.1;
   }
   return out;
  }
  // createImageBitmap
  if(typeof createImageBitmap==='function'){
   try{return res(await fromBitmap(await createImageBitmap(file)));}catch(e){}
  }
  // FileReader -> Image (самый надёжный на телефонах)
  const data=await new Promise((r,j)=>{const fr=new FileReader();fr.onload=()=>r(fr.result);fr.onerror=()=>j(new Error('Не удалось прочитать файл'));fr.readAsDataURL(file);});
  const img=await new Promise((r,j)=>{const im=new Image();im.onload=()=>r(im);im.onerror=()=>j(new Error('Формат не поддерживается — JPG/PNG/WebP'));im.src=data;});
  const c=document.createElement('canvas');c.width=c.height=size;
  const s=Math.min(img.width,img.height)||size;
  const ctx=c.getContext('2d',{alpha:false});
  ctx.fillStyle='#1a1a1c';ctx.fillRect(0,0,size,size);
  ctx.drawImage(img,(img.width-s)/2,(img.height-s)/2,s,s,0,0,size,size);
  let q=0.9,out;
  for(let i=0;i<8;i++){out=c.toDataURL('image/jpeg',q);if(out.length<1_200_000)break;q-=0.1;}
  res(out);
 }catch(e){rej(new Error((e&&e.message)||'Не удалось прочитать фото'));}
});}

function fileToStatusMedia(file){return new Promise((res,rej)=>{
 if(!/^image\/(jpeg|png|webp|gif)$/.test(file.type)){rej(new Error('Нужна картинка или GIF'));return;}
 if(file.size>2_000_000){rej(new Error('Файл слишком большой (макс. 2 МБ)'));return;}
 if(file.type==='image/gif'){
  const r=new FileReader();r.onload=()=>res(r.result);r.onerror=()=>rej(new Error('Не удалось прочитать файл'));r.readAsDataURL(file);return;}
 const img=new Image(),url=URL.createObjectURL(file);
 img.onload=()=>{const size=64,c=document.createElement('canvas');c.width=c.height=size;const s=Math.min(img.width,img.height);
  c.getContext('2d').drawImage(img,(img.width-s)/2,(img.height-s)/2,s,s,0,0,size,size);URL.revokeObjectURL(url);res(c.toDataURL('image/png'));};
 img.onerror=()=>rej(new Error('Не удалось прочитать картинку'));img.src=url;});}

/* Свой узор для баннера — квадратная миниатюра (как стикер), повторяется по баннеру */
function fileToBannerMedia(file){return new Promise((res,rej)=>{
 if(!/^image\/(jpeg|png|webp|gif)$/.test(file.type)){rej(new Error('Нужна картинка или GIF'));return;}
 if(file.size>2_000_000){rej(new Error('Файл слишком большой (макс. 2 МБ)'));return;}
 if(file.type==='image/gif'){
  if(file.size>700_000){rej(new Error('GIF для узора макс. ~500 КБ'));return;}
  const r=new FileReader();r.onload=()=>res(r.result);r.onerror=()=>rej(new Error('Не удалось прочитать файл'));r.readAsDataURL(file);return;}
 const img=new Image(),url=URL.createObjectURL(file);
 img.onload=()=>{
  const size=96; // компактный тайл узора
  const c=document.createElement('canvas');c.width=c.height=size;
  const s=Math.min(img.width,img.height);
  c.getContext('2d').drawImage(img,(img.width-s)/2,(img.height-s)/2,s,s,0,0,size,size);
  URL.revokeObjectURL(url);
  res(c.toDataURL('image/png'));
 };
 img.onerror=()=>rej(new Error('Не удалось прочитать картинку'));img.src=url;});}

function avatarPicker(init,onChange){
 const holder=h('div',{style:'display:flex;justify-content:center;margin:6px 0 10px;cursor:pointer'});
 const file=h('input',{type:'file',accept:'image/*',class:'hide'});
 const paint=o=>setKids(holder,av(o,96));paint(init);
 holder.onclick=()=>file.click();
 file.onchange=async()=>{if(!file.files[0])return;try{const d=await fileToAvatar(file.files[0]);onChange(d);paint({avatar_url:d,name:init.name||init.title});}catch(e){toast(e.message);}};
 return h('div',{},holder,file,h('div',{class:'sub',style:'text-align:center;margin-bottom:8px'},'Нажми на круг, чтобы выбрать фото или GIF-анимацию'));}

/* ---------- редактирование профиля + оформление (Premium) ---------- */
function showEdit(closeProfile){
 const ov=h('div',{class:'overlay dim'});let newAv;
 const st={color:me.banner?me.banner.color:-1,pattern:me.banner?me.banner.pattern:'',emoji:me.emoji_status||''};
 const first=h('input',{class:'inp',value:me.first_name||'',placeholder:'Имя',maxlength:40});
 const last=h('input',{class:'inp',value:me.last_name||'',placeholder:'Фамилия',maxlength:40});
 const bio=h('textarea',{class:'inp',rows:3,placeholder:'О себе',maxlength:200},me.bio||'');
 const un=h('input',{class:'inp',value:me.own_username||'',placeholder:'Имя пользователя (без @)',maxlength:32,autocapitalize:'none'});
 const hint=h('div',{class:'sub'},'a-z, 0-9 и _, минимум 5 символов');
 const bd=h('input',{class:'inp',type:'date',value:me.birthday||''}),ad=h('input',{class:'inp',placeholder:'Адрес',maxlength:120,value:me.address||''}),
  stx=h('input',{class:'inp',placeholder:'Статус под именем (например: трек или цитата)',maxlength:120,value:me.status_text||''}),
  wf=h('input',{class:'inp',type:'time',value:me.work_from||'',style:'flex:1;margin:0'}),wt=h('input',{class:'inp',type:'time',value:me.work_to||'',style:'flex:1;margin:0'}),
  pch=h('select',{class:'inp'},h('option',{value:0},'— не показывать канал —'));
 api('/api/channels').then(j=>{j.channels.filter(c=>c.owner===me.id).forEach(c=>pch.append(h('option',{value:c.id},c.title)));pch.value=String(me.pchannel?me.pchannel.id:0);}).catch(()=>{});
 let t;un.oninput=()=>{clearTimeout(t);const v=un.value.trim().replace(/^@/,'').toLowerCase();
  if(!v){hint.textContent='Имя пользователя не задано';hint.style.color='';return;}
  t=setTimeout(async()=>{try{const j=await api('/api/username/check',{username:v});
   hint.textContent=j.ok?'✓ @'+v+' свободно':j.error;hint.style.color=j.ok?'#4fd27a':'#ff7b72';}catch(e){}},300);};
 // превью обложки
 const prev=h('div',{class:'bprev'});
 const paintPrev=()=>{
  prev.style.background=bannerBg(st.color);
  setKids(prev,patternEl(st.pattern),h('div',{class:'bn'},(first.value||me.first_name)+' ',st.emoji?statusEl(st.emoji,18):null));};
 first.oninput=paintPrev;
 const needPrem=()=>{if(!me.premium){showPremium();return true;}return false;};
 const sws=h('div',{class:'sws'});
 const paintSws=()=>setKids(sws,[-1,...BANNERS.map((_,i)=>i)].map(i=>{
  const e=h('div',{class:'swc'+(st.color===i?' on':'')+(me.premium?'':' lk'),style:'background:'+bannerBg(i),onclick:()=>{if(needPrem())return;st.color=i;paintSws();paintPrev();}},i<0?'✕':'');return e;}));
 const chips=(list,get,set)=>{const box=h('div',{class:'chips'});
  const paint=()=>setKids(box,list.map(p=>h('button',{class:'chip'+(get()===p?' on':''),onclick:()=>{if(needPrem())return;set(p);paint();paintPrev();}},patternChipLabel(p))));
  paint();return box;};
 const err=h('div',{class:'err'});
 const done=async()=>{await refreshMe();ov.remove();closeProfile&&closeProfile();await refresh();};
 const m=h('div',{class:'modal'},h('h3',{},'Мой профиль'),
  avatarPicker(me,d=>newAv=d),first,last,bio,un,hint,stx,h('div',{class:'lbl'},'День рождения'),bd,h('div',{class:'lbl'},'Часы работы (от и до)'),h('div',{style:'display:flex;gap:8px'},wf,wt),ad,h('div',{class:'lbl'},'Канал в профиле'),pch,
  h('div',{class:'sub',style:'margin-top:8px'},'Номер: '+fmtPhone(me.real_phone||me.phone)+' · виден только тебе и админу'),
  h('div',{class:'psec',style:'font-size:16px;margin:18px 0 8px'},'⭐ Оформление профиля'+(me.premium?'':' · нужен Premium')),
  prev,h('div',{class:'lbl'},'Цвет обложки'),sws,
  h('div',{class:'lbl'},'Узор: эмодзи или своя картинка'),chips(PATTERNS,()=>isBannerImg(st.pattern)?'':st.pattern,v=>{st.pattern=v;paintPrev();}),
  (()=>{const bf=h('input',{type:'file',accept:'image/png,image/jpeg,image/webp,image/gif',class:'hide'});
   bf.onchange=async()=>{if(!bf.files[0])return;try{if(needPrem())return;const d=await fileToBannerMedia(bf.files[0]);st.pattern=d;paintPrev();toast('Узор установлен');}catch(e){toast(e.message);}};
   return h('div',{style:'margin:4px 0 10px;display:flex;gap:8px;flex-wrap:wrap'},
    h('button',{class:'chip',style:'width:auto;padding:0 12px;font-size:13px',onclick:()=>{if(needPrem())return;bf.click();}},'🖼 Свой узор (фото/GIF)'),
    isBannerImg(st.pattern)?h('button',{class:'chip',style:'width:auto;padding:0 12px;font-size:13px',onclick:()=>{st.pattern='';paintPrev();}},'✕ Убрать узор'):null,
    bf);})(),
  h('div',{class:'lbl'},'Эмодзи-статус рядом с именем'),chips(STATUSES,()=>st.emoji,v=>st.emoji=v),
  (()=>{const sf=h('input',{type:'file',accept:'image/png,image/jpeg,image/webp,image/gif',class:'hide'});
   sf.onchange=async()=>{if(!sf.files[0])return;try{if(needPrem())return;const d=await fileToStatusMedia(sf.files[0]);st.emoji=d;paintPrev();}catch(e){toast(e.message);}};
   return h('div',{style:'margin:4px 0 10px'},h('button',{class:'chip',style:'width:auto;padding:0 12px;font-size:13px',onclick:()=>{if(needPrem())return;sf.click();}},'🖼 Своя картинка/GIF в статус'),sf);})(),
  err,
  h('button',{class:'btn',onclick:async()=>{try{
    const body={first_name:first.value,last_name:last.value,bio:bio.value,username:un.value,birthday:bd.value,address:ad.value,status_text:stx.value,work_from:wf.value,work_to:wt.value,pchannel:+pch.value};if(newAv!==undefined)body.avatar=newAv;
    await api('/api/profile',body);
    if(me.premium)await api('/api/profile/style',{banner_color:st.color,banner_pattern:st.pattern,emoji_status:st.emoji});
    await done();toast('Профиль сохранён');
   }catch(e){err.textContent=e.message;}}},'Сохранить'),
  me.avatar_url?h('button',{class:'btn gray',onclick:async()=>{try{await api('/api/profile',{first_name:me.first_name,last_name:me.last_name,bio:me.bio,avatar:''});await done();}catch(e){err.textContent=e.message;}}},'Убрать аватарку'):null,
  h('button',{class:'btn gray',onclick:()=>ov.remove()},'Отмена'));
 paintSws();paintPrev();
 ov.append(m);ov.onclick=e=>{if(e.target===ov)ov.remove();};document.body.append(ov);}

/* ---------- Telegram Premium ---------- */
const FEATS=[
 ['🚀','linear-gradient(135deg,#ff9a5c,#ff4d6d)','Удвоенные лимиты','До 1000 каналов, 20 папок с чатами, 10 закреплённых чатов и многое другое.',0],
 ['📁','linear-gradient(135deg,#6ab7ff,#3b82e6)','Файлы до 4 ГБ','Загружайте файлы размером до 4 ГБ каждый.',0],
 ['⚡','linear-gradient(135deg,#ff6b6b,#ff9a5c)','Максимальная скорость','Скачивайте медиа и файлы без ограничения скорости.',0],
 ['🎙','linear-gradient(135deg,#6fd67a,#2ba2b5)','Голос в текст','Читайте расшифровку голосовых и видеосообщений.',0],
 ['🚫','linear-gradient(135deg,#b98cff,#6c5ce7)','Без рекламы','Больше никаких рекламных сообщений в каналах.',0],
 ['😍','linear-gradient(135deg,#ffd86b,#ff8a3d)','Эксклюзивные реакции','Тысячи дополнительных реакций на сообщения.',0],
 ['🎭','linear-gradient(135deg,#6ab7ff,#7254e0)','Премиум-стикеры','Эксклюзивные анимированные стикеры с эффектами.',0],
 ['⭐','linear-gradient(135deg,#6c8cff,#c65bff)','Значок Premium','Звезда рядом с именем в списке чатов, сообщениях и профиле.',1],
 ['🎨','linear-gradient(135deg,#ff72a0,#7a5cff)','Обложка профиля','Свой цвет и узор из эмодзи на странице профиля.',1],
 ['😎','linear-gradient(135deg,#46f08a,#0f9a85)','Эмодзи-статус','Свой эмодзи рядом с именем вместо звезды.',1]];
function showPremium(){
 const ov=h('div',{class:'overlay prem'});
 const list=FEATS.map(([ic,bg,t,d,ok])=>h('div',{class:'fr'},h('div',{class:'fi',style:'background:'+bg},ic),
  h('div',{style:'flex:1;min-width:0'},h('b',{},t,h('span',{class:'ftag'+(ok?' ok':'')},ok?'работает':'скоро')),h('div',{class:'sub'},d))));
 ov.append(h('div',{class:'pw'},
  h('div',{class:'pbar',style:'background:transparent'},h('button',{class:'icon',style:'color:#fff;font-size:26px',onclick:()=>ov.remove()},'←')),
  h('div',{class:'pstar',html:BIGSTAR}),h('h1',{class:'c'},'GodGram Premium'),h('p',{class:'sub c'},'Откройте дополнительные возможности'),
  list,
  h('div',{class:'pcta'},me.premium
   ?h('div',{class:'cta on c'},'⭐ Premium активен · '+(me.premium_until>3e10?'навсегда':'до '+premDate(me.premium_until)))
   :h('button',{class:'cta',onclick:()=>toast('Premium выдаёт администратор сервера')},'Получить Premium'))));
 document.body.append(ov);}

/* ---------- перетаскивание (мышь и палец) ---------- */
function sortable(box,onDone){
 box.addEventListener('pointerdown',e=>{
  const hd=e.target.closest('.hnd');if(!hd)return;
  const it=hd.closest('.sit');if(!it)return;
  e.preventDefault();
  try{hd.setPointerCapture(e.pointerId);}catch(_){}
  const before=[...box.children].map(x=>x.dataset.h).join('|');
  let base=e.clientY;it.classList.add('drag');
  const mv=ev=>{
   it.style.transform='translateY('+(ev.clientY-base)+'px)';
   for(let g=0;g<8;g++){
    const r=it.getBoundingClientRect(),cy=r.top+r.height/2;
    const nx=it.nextElementSibling,pv=it.previousElementSibling;let moved=false;
    if(nx){const nr=nx.getBoundingClientRect();
     if(cy>nr.top+nr.height/2){const o=it.offsetTop;box.insertBefore(it,nx.nextElementSibling);base+=it.offsetTop-o;moved=true;}}
    if(!moved&&pv){const pr=pv.getBoundingClientRect();
     if(cy<pr.top+pr.height/2){const o=it.offsetTop;box.insertBefore(it,pv);base+=it.offsetTop-o;moved=true;}}
    if(!moved)break;
    it.style.transform='translateY('+(ev.clientY-base)+'px)';}
  };
  const up=()=>{hd.removeEventListener('pointermove',mv);hd.removeEventListener('pointerup',up);hd.removeEventListener('pointercancel',up);
   it.classList.remove('drag');it.style.transform='';
   const after=[...box.children].map(x=>x.dataset.h);
   if(after.join('|')!==before)onDone(after);};
  hd.addEventListener('pointermove',mv);hd.addEventListener('pointerup',up);hd.addEventListener('pointercancel',up);});}

/* ---------- мои NFT и юзернеймы ---------- */
async function myNft(){
 const ov=h('div',{class:'overlay dim'});const box=h('div',{class:'modal'});
 async function load(){
  let j;try{j=await api('/api/my_nft');}catch(e){setKids(box,h('div',{class:'err'},e.message));return;}
  const act=h('div',{class:'slist'});
  j.active.forEach((x,i)=>act.append(h('div',{class:'sit','data-h':x.handle},
   h('span',{class:'hnd',title:'Перетащи'},'☰'),
   h('div',{class:'sit-main'},
    h('span',{class:x.nft?'nft':'',style:'word-break:break-all'},(x.nft?'💎 ':'')+'@'+x.handle),
    x.main?h('span',{class:'tag'},'основной'):null,
    h('div',{class:'sub'},x.nft?'NFT-юзернейм':'Обычный юзернейм'+(x.price?' · '+x.price+' ⭐':'')+(x.issued?' · выдан '+fmtDT(x.issued):''))),
   h('div',{class:'sit-acts'},
    h('button',{class:'sbtn sm'+(x.main?' g':''),onclick:async()=>{try{await api('/api/usernames/main',{handle:x.handle,main:!x.main});await refreshMe();await load();}catch(e){toast(e.message);}}},x.main?'Не основной':'В основные'),
    x.nft?toggle(true,async()=>{try{await api('/api/nft/activate',{kind:'username',value:x.handle,active:false});await refreshMe();await load();}catch(e){toast(e.message);}}):null)
   )));
  sortable(act,async order=>{try{await api('/api/usernames/order',{order});await refreshMe();await load();}catch(e){toast(e.message);await load();}});
  const off=j.inactive.map(hd=>h('div',{class:'sit','data-h':hd},
   h('div',{class:'sit-main'},h('span',{class:'nft',style:'word-break:break-all'},'💎 @'+hd),h('div',{class:'sub'},'отключён · никто его не видит')),
   h('div',{class:'sit-acts'},toggle(false,async()=>{try{await api('/api/nft/activate',{kind:'username',value:hd,active:true});await refreshMe();await load();}catch(e){toast(e.message);}}))));
  const nums=j.numbers.map(x=>h('div',{class:'sit'},
   h('div',{class:'sit-main'},h('span',{class:'nft'},'💎 '+fmtPhone(x.number)),h('div',{class:'sub'},x.active?'показывается в профиле':'не используется')),
   h('div',{class:'sit-acts'},h('button',{class:'sbtn sm'+(x.active?' g':''),onclick:async()=>{try{await api('/api/nft/activate',{kind:'number',value:x.active?null:x.number});await refreshMe();await load();}catch(e){toast(e.message);}}},x.active?'Снять':'Выбрать'))));
  setKids(box,h('h3',{},'💎 Мои NFT и юзернеймы'),
   h('div',{class:'sub',style:'margin-bottom:6px'},'NFT выдаёт админ. Включённые юзернеймы показываются в профиле рядом с основным. Основные юзернеймы показываются в профиле крупно, остальные — под ними. Тяни за ☰, чтобы поменять порядок.'),
   h('div',{class:'psec',style:'font-size:15px;margin:12px 0 2px'},'Имена пользователя'),
   j.active.length?act:h('div',{class:'sub'},'Пока нет. Задай @username в профиле.'),
   off.length?[h('div',{class:'psec',style:'font-size:15px;margin:14px 0 2px'},'Отключённые NFT'),off]:null,
   h('div',{class:'psec',style:'font-size:15px;margin:14px 0 2px'},'Номера'),
   nums.length?nums:h('div',{class:'sub'},'Пока нет'),
   h('button',{class:'btn gray',onclick:()=>ov.remove()},'Закрыть'));}
 ov.append(box);ov.onclick=e=>{if(e.target===ov)ov.remove();};document.body.append(ov);load();}

/* ---------- группы ---------- */
async function newGroup(){
 const ov=h('div',{class:'overlay dim'});let gAv='';const sel=new Set();
 const title=h('input',{class:'inp',placeholder:'Название группы',maxlength:50});
 const list=h('div',{style:'max-height:260px;overflow-y:auto;margin-top:8px'});
 const find=h('input',{class:'inp',placeholder:'Найти участников'});
 const err=h('div',{class:'err'});
 async function load(){const j=await api('/api/users?q='+encodeURIComponent(find.value));
  setKids(list,j.users.map(u=>{const ck=h('input',{type:'checkbox',class:'ck'});ck.checked=sel.has(u.id);
   ck.onchange=()=>ck.checked?sel.add(u.id):sel.delete(u.id);
   return h('label',{class:'ur'},av(u,40),h('div',{class:'mid'},u.name,badge(u),h('small',{},u.username?'@'+u.username:status(u))),ck);}));}
 find.oninput=load;
 const m=h('div',{class:'modal'},h('h3',{},'Новая группа'),avatarPicker({title:'Г'},d=>gAv=d),title,find,list,err,
  h('button',{class:'btn',onclick:async()=>{try{
    const j=await api('/api/chat/group',{title:title.value,avatar:gAv,members:[...sel]});
    ov.remove();await loadChats();openChat(j.chat_id);}catch(e){err.textContent=e.message;}}},'Создать'),
  h('button',{class:'btn gray',onclick:()=>ov.remove()},'Отмена'));
 ov.append(m);ov.onclick=e=>{if(e.target===ov)ov.remove();};document.body.append(ov);load();}

async function showGroup(id){
 const g=await api('/api/chat?id='+id);const ov=h('div',{class:'overlay dim'});
 const m=h('div',{class:'modal'},h('div',{style:'display:flex;justify-content:center;margin-bottom:8px'},av({avatar_url:g.avatar_url,title:g.title},96)),
  h('h3',{style:'text-align:center;margin-bottom:2px'},g.title),h('div',{class:'sub',style:'text-align:center;margin-bottom:12px'},g.members.length+(g.kind==='channel'?' подписч.':' участн.')),
  g.members.map(u=>h('div',{class:'ur',onclick:()=>{ov.remove();showProfile(u.id);}},av(u,40),
   h('div',{class:'mid'},u.name,badge(u),u.id===g.owner?h('span',{class:'tag'},'создатель'):null,h('small',{},u.deleted?'был(а) давно':status(u))))),
  g.frozen?h('div',{class:'sub',style:'text-align:center;margin:8px 0'},'❄️ Заморожено администратором'):null,
  g.owner===me.id?h('button',{class:'btn gray',onclick:async()=>{const u=prompt('Юзернейм бота (например my_bot):');if(!u)return;
   try{await api('/api/chat/add_bot',{chat_id:id,username:u});toast('Бот добавлен');ov.remove();showGroup(id);}catch(e){toast(e.message);}}},'🤖 Добавить бота'):null,
  (g.kind==='channel'&&g.owner!==me.id)?h('button',{class:'btn red',onclick:async()=>{try{await api('/api/channel/leave',{chat_id:id});ov.remove();$('#backBtn').click();await loadChats();}catch(e){toast(e.message);}}},'Отписаться'):null,
  h('button',{class:'btn gray',onclick:()=>ov.remove()},'Закрыть'));
 ov.append(m);ov.onclick=e=>{if(e.target===ov)ov.remove();};document.body.append(ov);}

/* ---------- админ-панель ---------- */
const ulabel=u=>u.name+(u.username?' @'+u.username:'');
function userSelect(users,selected){
 const s=h('select',{class:'inp',style:'margin:0;flex:1;min-width:0'},
  h('option',{value:''},'— никому (в пул) —'),
  ...users.filter(u=>!u.deleted).map(u=>h('option',{value:u.id},ulabel(u))));
 if(selected)s.value=String(selected);return s;}

async function adminPanel(){
 const ov=h('div',{class:'overlay dim'});
 const box=h('div',{class:'modal',style:'max-width:560px'});
 const tabs=h('div',{class:'tabs2'});const body=h('div');
 let tab='users';
 const act=async(path,data,msg)=>{try{await api(path,data);if(msg)toast(msg);await render();refresh();}catch(e){toast(e.message);}};

 function usersTab(users){
  const alive=users.filter(u=>!u.deleted).length;
  return h('div',{},h('div',{class:'sub',style:'margin-bottom:10px'},'Пользователей: '+alive+' · удалённых: '+(users.length-alive)),
   users.map(u=>h('div',{class:'ur',onclick:()=>{ov.remove();showProfile(u.id);}},av(u,40),
    h('div',{class:'mid'},u.name,badge(u),u.is_admin?h('span',{class:'tag'},'админ'):null,u.deleted?h('span',{class:'tag d'},'удалён'):null,
     h('small',{},u.deleted?'ID '+u.id:[(u.real_phone?fmtPhone(u.real_phone):'—'),u.username?' · @'+u.username:''])),
    (!u.deleted&&!u.is_admin&&u.id!==me.id)?h('button',{class:'dbtn',onclick:e=>{e.stopPropagation();
      if(!confirm('Удалить аккаунт «'+u.name+'»? Необратимо.'))return;act('/api/admin/delete',{user_id:u.id},'Аккаунт удалён');}},'Удалить'):null)));}

 function nftTab(kind,items,users){
  const isU=kind==='username';
  const inp=h('input',{class:'inp',placeholder:isU?'юзернейм без @ (от 2 символов)':'777 или 888 0123 4567',autocapitalize:'none'});
  const owner=userSelect(users,null);
  const rnd=n=>{let d='';for(let i=0;i<n;i++)d+=Math.floor(Math.random()*10);return d;};
  const dice=!isU?[
   h('button',{class:'sbtn g',title:'Случайный полный номер',onclick:()=>{inp.value=fmtPhone('888'+rnd(8));}},'🎲 11'),
   h('button',{class:'sbtn g',title:'Случайный 3-значный номер',onclick:()=>{inp.value=rnd(3);}},'🎲 3')]:null;
  const toLocal=ts=>{const d=new Date(ts*1000);return d.getFullYear()+'-'+pad(d.getMonth()+1)+'-'+pad(d.getDate())+'T'+pad(d.getHours())+':'+pad(d.getMinutes());};
  const toTs=v=>v?new Date(v).getTime()/1000:0;
  const price0=h('input',{class:'inp',type:'number',min:0,placeholder:'Цена ⭐ на GodFragment (0 = не в продаже)',style:'margin:0;flex:1'});
  const date0=isU?h('input',{class:'inp',type:'datetime-local',title:'Когда выдан (пусто - сейчас)',style:'margin:0;flex:1;min-width:170px'}):null;
  const form=h('div',{class:'frm'},h('b',{},isU?'Создать NFT-юзернейм':'Создать NFT-номер'),
   !isU?h('div',{class:'sub'},'Можно ввести просто 3 цифры (например 777) — получится +888 777. Цена > 0 — лот появится на GodFragment.'):null,
   h('div',{class:'nact'},inp,dice),h('div',{class:'nact'},price0,isU?date0:null),isU?h('div',{class:'sub'},'Цена в ⭐: на GodFragment (если без владельца) и в профиле. Дату можно не указывать.'):h('div',{class:'sub'},'Если указать цену и не выбирать владельца — номер попадёт в продажу на GodFragment.'),h('div',{class:'nact'},owner,
    h('button',{class:'sbtn',onclick:()=>act(isU?'/api/admin/nft/username':'/api/admin/nft/number',
      isU?{handle:inp.value,user_id:owner.value||null,price:+price0.value||0,issued:toTs(date0.value)||undefined}:{number:inp.value,user_id:owner.value||null,price:+price0.value||0},'Создано 💎').then(()=>{inp.value='';})},'Создать 💎')));
  const rows=items.map(x=>{
   const sel=userSelect(users,x.owner&&x.owner.id);
   const pr=isU?h('input',{class:'inp',type:'number',min:0,placeholder:'Цена ⭐',value:x.price||'',style:'margin:0;width:110px'}):null;
   const dt=isU?h('input',{class:'inp',type:'datetime-local',value:x.issued?toLocal(x.issued):'',style:'margin:0;flex:1;min-width:170px'}):null;
   return h('div',{class:'nrow'},
    h('div',{class:'nh'},h('span',{class:'nft'},isU?'💎 @'+x.handle:'💎 '+fmtPhone(x.number)),h('span',{class:'sub'},x.owner?('→ '+x.owner.name):'свободен')),
    isU?h('div',{class:'sub'},(x.price>0?x.price+' ⭐':'цена не указана')+' · '+(x.issued>0?'выдан '+fmtDT(x.issued):'не выдан')):null,
    isU?h('div',{class:'nact'},pr,dt,h('button',{class:'sbtn',title:'Сохранить цену и дату',onclick:()=>act('/api/admin/nft/info',{id:x.id,price:+pr.value||0,issued:toTs(dt.value)},'Сохранено')},'💾')):null,
    h('div',{class:'nact'},sel,
     h('button',{class:'sbtn',onclick:()=>act('/api/admin/nft/assign',{kind,id:x.id,user_id:sel.value||null},sel.value?'Выдано':'Забрано')},'Выдать'),
     x.owner?h('button',{class:'sbtn g',onclick:()=>act('/api/admin/nft/assign',{kind,id:x.id,user_id:null},'Забрано')},'Забрать'):null,
     h('button',{class:'sbtn r',onclick:()=>{if(confirm('Удалить насовсем?'))act('/api/admin/nft/delete',{kind,id:x.id},'Удалено');}},'✕')));});
  return h('div',{},form,rows.length?rows:h('div',{class:'sub'},'Пока ничего нет. Создай первый выше.'));}

 function premiumTab(users){
  return h('div',{},h('div',{class:'sub',style:'margin-bottom:8px'},'Premium даёт значок ⭐, обложку профиля и эмодзи-статус.'),
   users.filter(u=>!u.deleted).map(u=>{
    const sel=h('select',{class:'inp',style:'margin:0;width:auto'},...[[1,'1 мес.'],[3,'3 мес.'],[6,'6 мес.'],[12,'12 мес.'],[-1,'навсегда']].map(([v,t])=>h('option',{value:v},t)));
    return h('div',{class:'nrow'},
     h('div',{class:'nh'},h('span',{},u.name,badge(u)),h('span',{class:'sub'},u.premium?('до '+premDate(u.premium_until)):'нет Premium')),
     h('div',{class:'nact'},sel,
      h('button',{class:'sbtn',onclick:()=>act('/api/admin/premium',{user_id:u.id,months:+sel.value},'⭐ Premium выдан')},u.premium?'Продлить':'Выдать'),
      u.premium?h('button',{class:'sbtn g',onclick:()=>act('/api/admin/premium',{user_id:u.id,months:0},'Premium отозван')},'Забрать'):null,
      h('button',{class:'sbtn'+(u.boost_inf?' r':''),style:u.boost_inf?'':'background:linear-gradient(90deg,#6c8cff,#c65bff)',onclick:()=>act('/api/admin/boost_user',{user_id:u.id,inf:!u.boost_inf},u.boost_inf?'Бусты ∞ сняты':'⚡ Бесконечные бусты выданы')},u.boost_inf?'⚡∞ снять':'⚡∞ выдать')));}));}

 function botsTab(bots){
  return h('div',{},h('div',{class:'sub',style:'margin-bottom:8px'},'Бота можно заморозить (молчит, API закрыт) или удалить насовсем.'),
   bots.length?bots.map(b=>h('div',{class:'nrow'},
    h('div',{class:'nh'},h('span',{},b.name,h('span',{class:'bt'},'БОТ'),b.frozen?h('span',{class:'frz'},' ❄️'):null),h('span',{class:'sub'},'@'+b.username+(b.owner?' · '+b.owner.name:''))),
    h('div',{class:'nact'},h('button',{class:'sbtn'+(b.frozen?'':' r'),onclick:()=>act('/api/admin/freeze',{kind:'bot',id:b.id,on:!b.frozen},b.frozen?'Бот разморожен':'Бот заморожен ❄️')},b.frozen?'Разморозить':'Заморозить ❄️'),
     h('button',{class:'sbtn r',onclick:()=>{if(confirm('Удалить бота @'+b.username+' насовсем?'))act('/api/admin/delete',{user_id:b.id},'Бот удалён');}},'🗑 Удалить')))):
    h('div',{class:'sub'},'Ботов пока нет'));}

 function channelsTab(chs){
  return h('div',{},h('div',{class:'sub',style:'margin-bottom:8px'},'Каналы и группы можно заморозить или удалить насовсем (вместе с сообщениями).'),
   chs.length?chs.map(c=>h('div',{class:'nrow'},
    h('div',{class:'nh'},h('span',{},(c.kind==='channel'?'📢 ':'👥 ')+c.title,badge(c),c.frozen?h('span',{class:'frz'},' ❄️'):null),h('span',{class:'sub'},c.members+' · '+(c.owner?c.owner.name:'—')+(c.kind==='channel'?' · ⚡ '+(c.boosts>=1000000?'∞':c.boosts):''))),
    h('div',{class:'nact'},h('button',{class:'sbtn'+(c.frozen?'':' r'),onclick:()=>act('/api/admin/freeze',{kind:'chat',id:c.id,on:!c.frozen},c.frozen?'Разморожено':'Заморожено ❄️')},c.frozen?'Разморозить':'Заморозить ❄️'),
     c.kind==='channel'?h('button',{class:'sbtn',style:'background:linear-gradient(90deg,#6c8cff,#c65bff)',onclick:()=>act('/api/admin/boost_chat',{id:c.id,amount:-1},'⚡ Бесконечные бусты выданы')},'⚡ ∞'):null,
     c.kind==='channel'?h('button',{class:'sbtn g',onclick:()=>{const n=prompt('Сколько бустов добавить?','10');if(n)act('/api/admin/boost_chat',{id:c.id,amount:+n},'Бусты добавлены');}},'⚡ +N'):null,
     c.kind==='channel'?h('button',{class:'sbtn g',onclick:()=>act('/api/admin/boost_chat',{id:c.id,amount:0,set:true},'Админские бусты сброшены')},'⚡ 0'):null,
     h('button',{class:'sbtn r',onclick:()=>{if(confirm('Удалить «'+c.title+'» насовсем?'))act('/api/admin/chat/delete',{id:c.id},'Удалено');}},'🗑 Удалить')))):
    h('div',{class:'sub'},'Каналов пока нет'));}

 function walletTab(users){
  return h('div',{},h('div',{class:'sub',style:'margin-bottom:8px'},'Начисляй или списывай звёзды ⭐. Рейтинг — во вкладке «📊 Рейтинг».'),
   users.filter(u=>!u.deleted).map(u=>{
    const a=h('input',{class:'inp',type:'number',placeholder:'±сумма',style:'width:110px;margin:0'});
    return h('div',{class:'nrow'},h('div',{class:'nh'},h('span',{},u.name,badge(u)),h('span',{class:'sub'},fmtStars(u.balance))),
     h('div',{class:'nact'},a,h('button',{class:'sbtn',onclick:()=>{const v=+a.value;if(!v)return toast('Введи сумму');act('/api/admin/wallet',{user_id:u.id,amount:v},'Готово ⭐');}},'Применить')));}));}

 function ratingTab(users){
  return h('div',{},h('div',{class:'sub',style:'margin-bottom:10px'},'Выдай или забери рейтинг. «±» — изменить на число, «=» — выставить точное значение (можно отрицательное).'),
   users.filter(u=>!u.deleted&&!u.is_bot).map(u=>{
    const cur=u.rating||0;
    const dlt=h('input',{class:'inp',type:'number',placeholder:'± дельта',style:'width:100px;margin:0'});
    const abs=h('input',{class:'inp',type:'number',placeholder:'= значение',value:String(cur),style:'width:100px;margin:0'});
    const quick=(v)=>h('button',{class:'sbtn'+(v<0?' r':' g'),style:'padding:6px 8px',onclick:()=>act('/api/admin/rating',{user_id:u.id,amount:v},'Рейтинг '+(v>0?'+':'')+v)},(v>0?'+':'')+v);
    return h('div',{class:'nrow'},
     h('div',{class:'nh'},h('span',{},u.name,badge(u),ratingBadge(cur)),h('span',{class:'sub'},'сейчас: '+(cur>0?'+':'')+cur)),
     h('div',{class:'nact',style:'flex-wrap:wrap;gap:4px'},
      quick(10),quick(50),quick(100),quick(-10),quick(-50),
      dlt,h('button',{class:'sbtn',onclick:()=>{const v=+dlt.value;if(!v)return toast('Введи дельту');act('/api/admin/rating',{user_id:u.id,amount:v},'Рейтинг обновлён');}},'± Выдать'),
      abs,h('button',{class:'sbtn g',onclick:()=>{if(abs.value===''||isNaN(+abs.value))return toast('Введи значение');act('/api/admin/rating',{user_id:u.id,amount:+abs.value,set:true},'Рейтинг установлен');}},'= Поставить')));}));}

 function giftsTab(gifts,users){
  const name=h('input',{class:'inp',placeholder:'Название',maxlength:30}),price=h('input',{class:'inp',type:'number',placeholder:'Цена ⭐',value:100}),
   supply=h('input',{class:'inp',type:'number',placeholder:'Тираж (0 = без лимита)',value:0}),
   glyph=h('input',{class:'inp',placeholder:'Эмодзи',value:'🎁',maxlength:8,style:'width:90px'}),
   color=h('input',{type:'color',value:'#ff7a45',style:'width:46px;height:42px;border:0;background:none'}),
   model=h('select',{class:'inp'},h('option',{value:'extrude'},'Объёмный эмодзи'),h('option',{value:'box'},'Коробка (куб)')),
   anim=h('select',{class:'inp'},h('option',{value:'spin'},'Вращение'),h('option',{value:'float'},'Парение'),h('option',{value:'bounce'},'Прыжки'),h('option',{value:'pulse'},'Пульс')),
   prev=h('div',{style:'padding:8px 0'});
  const cur=()=>({name:name.value,glyph:glyph.value||'🎁',color:color.value,model:model.value,anim:anim.value});
  const paint=()=>setKids(prev,gift3d(cur(),110,true));
  [glyph,color,model,anim].forEach(e=>{e.oninput=paint;e.onchange=paint;});paint();
  const form=h('div',{class:'frm'},h('b',{},'Новый подарок'),prev,name,
   h('div',{class:'nact'},glyph,color,model),h('div',{class:'nact'},anim),h('div',{class:'nact'},price,supply),
   h('button',{class:'sbtn',onclick:()=>act('/api/admin/gift/create',{...cur(),price:+price.value,supply:+supply.value||0},'Подарок создан 🎁').then(()=>{name.value='';})},'Создать подарок'));
  const rows=gifts.map(g=>{
   const sel=userSelect(users,null);
   return h('div',{class:'nrow',style:g.active?'':'opacity:.55'},
    h('div',{class:'nh'},h('div',{style:'display:flex;align-items:center;gap:8px'},gift3d(g,46),h('span',{},g.name)),
     h('span',{class:'sub'},fmtStars(g.price)+' · продано '+g.sold+(g.supply?'/'+g.supply:''))),
    h('div',{class:'nact'},sel,h('button',{class:'sbtn',onclick:()=>{if(!sel.value)return toast('Выбери человека');act('/api/admin/gift/give',{gift_id:g.id,user_id:+sel.value},'Подарок выдан 🎁');}},'Выдать'),
     h('button',{class:'sbtn g',onclick:()=>act('/api/admin/gift/active',{id:g.id,active:!g.active},g.active?'Скрыт из магазина':'Показан')},g.active?'Скрыть':'Показать')));});
  return h('div',{},form,rows);}


 function badgesTab(users,bots,chs){
  return h('div',{},h('div',{class:'sub',style:'margin-bottom:8px'},'Верификации и метки СКАМ/ФЕЙК можно вешать на людей, ботов и каналы.'),
   [['Люди',users.filter(u=>!u.deleted).map(u=>['user',u])],['Боты',bots.map(u=>['user',u])],['Каналы и группы',chs.map(c=>['chat',c])]].map(([t,list])=>[
    h('div',{class:'psec',style:'font-size:15px;margin:14px 0 4px'},t),
    list.length?list.map(([k,e])=>h('div',{class:'nrow'},h('div',{class:'nh'},h('span',{},(k==='chat'?(e.kind==='channel'?'📢 ':'👥 ')+e.title:e.name),badge(e)),
      h('button',{class:'sbtn g',onclick:()=>adminBadgeDialog(k,e.id,k==='chat'?e.title:e.name,e.badges)},'🏷 Метки')))):h('div',{class:'sub'},'—')]));}

 const REASON={spam:'Спам',scam:'Мошенничество',fake:'Фейк / выдаёт себя за другого',violence:'Насилие',porn:'Порнография',copyright:'Авторские права',other:'Другое'};
 function reportsTab(reps){
  if(!reps.length)return h('div',{class:'sub',style:'padding:14px'},'Новых жалоб нет 🎉');
  return h('div',{},reps.map(r=>{
   const t=r.target;const nm=t?(r.kind==='message'?'Сообщение от '+t.name+': «'+(t.text||'')+'»':t.name+(t.username?' @'+t.username:'')):'(уже удалено)';
   const bk=r.kind==='user'?'user':'chat';
   return h('div',{class:'nrow'},
    h('div',{class:'nh'},h('span',{},({user:'👤 ',chat:'📢 ',message:'💬 '})[r.kind]+nm,t&&t.badges?badge(t):null),h('span',{class:'sub'},r.count>1?r.count+' жалоб':'1 жалоба')),
    h('div',{class:'sub'},REASON[r.reason]+(r.text?' — '+r.text:'')+' · от '+(r.reporter?r.reporter.name:'?')),
    h('div',{class:'nact',style:'flex-wrap:wrap;margin-top:6px'},
     (t&&r.kind!=='message')?[
      h('button',{class:'sbtn r',onclick:()=>act('/api/admin/badge',{kind:bk,id:r.tid,key:'scam',on:true},'Метка СКАМ')},'СКАМ'),
      h('button',{class:'sbtn',style:'background:#f08a24',onclick:()=>act('/api/admin/badge',{kind:bk,id:r.tid,key:'fake',on:true},'Метка ФЕЙК')},'ФЕЙК'),
      h('button',{class:'sbtn r',onclick:()=>{if(!confirm('Удалить цель жалобы?'))return;act(bk==='user'?'/api/admin/delete':'/api/admin/chat/delete',bk==='user'?{user_id:r.tid}:{id:r.tid},'Удалено');}},'🗑')]:null,
     h('button',{class:'sbtn',onclick:()=>act('/api/admin/report/resolve',{id:r.id,all:true,status:'resolved'},'Закрыто')},'✓ Решено'),
     h('button',{class:'sbtn g',onclick:()=>act('/api/admin/report/resolve',{id:r.id,all:true,status:'dismissed'},'Отклонено')},'Отклонить')));}));}

 function marketTab(colls,users){
  const name=h('input',{class:'inp',placeholder:'Название коллекции',maxlength:40}),glyphs=h('input',{class:'inp',placeholder:'Эмодзи-модели через пробел: 🐙 🦑 🐡'}),
   supply=h('input',{class:'inp',type:'number',placeholder:'Тираж (0 = без лимита)',value:1000}),price=h('input',{class:'inp',type:'number',placeholder:'Цена ⭐',value:100}),
   rel=h('input',{class:'inp',type:'number',placeholder:'Сразу выпустить на маркет, шт.',value:10});
  const form=h('div',{class:'frm'},h('b',{},'Новая NFT-коллекция'),h('div',{class:'sub'},'Модель, фон и узор у каждого подарка выпадают случайно, у каждого своя редкость.'),name,glyphs,h('div',{class:'nact'},supply,price),rel,
   h('button',{class:'sbtn',onclick:()=>act('/api/admin/nft_coll/create',{name:name.value,glyphs:glyphs.value,supply:+supply.value||0,price:+price.value||0,release:+rel.value||0},'Коллекция создана 💎').then(()=>{name.value='';})},'Создать'));
  const rows=colls.map(c=>{
   const cnt=h('input',{class:'inp',type:'number',value:5,style:'width:70px;margin:0'}),pr=h('input',{class:'inp',type:'number',value:c.price,style:'width:90px;margin:0'}),to=userSelect(users,null);
   to.firstChild.textContent='— на маркет —';
   return h('div',{class:'nrow',style:c.active?'':'opacity:.55'},
    h('div',{class:'nh'},h('span',{},c.glyph+' '+c.name),h('span',{class:'sub'},'выпущено '+c.minted+(c.supply?'/'+c.supply:'')+' · на маркете '+c.listed+(c.floor?' · от '+c.floor+'⭐':''))),
    h('div',{class:'nact',style:'flex-wrap:wrap'},cnt,pr,to,h('button',{class:'sbtn',onclick:()=>act('/api/admin/nft_coll/release',{id:c.id,count:+cnt.value,price:+pr.value,user_id:to.value||null},'Выпущено 💎')},'Выпустить'),
     h('button',{class:'sbtn g',onclick:()=>act('/api/admin/nft_coll/active',{id:c.id,active:!c.active},c.active?'Скрыта':'Показана')},c.active?'Скрыть':'Показать')));});
  return h('div',{},form,rows);}

 function paintTabs(){
  setKids(tabs,[['users','👥 Люди'],['nu','💎 Юзы'],['nn','💎 Номера'],['pr','⭐ Premium'],['bots','🤖 Боты'],['ch','📢 Каналы'],['gf','🎁 Подарки'],['mk','🛍 Маркет'],['bd','🏷 Метки'],['rp','🚩 Жалобы'],['wl','👛 Кошельки'],['rt','📊 Рейтинг']].map(([k,t])=>
   h('button',{class:tab===k?'on':'',onclick:()=>{tab=k;render();}},t)));}

 async function render(){
  paintTabs();
  try{
   const users=(await api('/api/admin/users')).users;
   let view;
   if(tab==='users')view=usersTab(users);
   else if(tab==='pr')view=premiumTab(users);
   else if(tab==='bots')view=botsTab((await api('/api/admin/bots')).bots);
   else if(tab==='ch')view=channelsTab((await api('/api/admin/channels')).channels);
   else if(tab==='gf')view=giftsTab((await api('/api/admin/gifts')).gifts,users);
   else if(tab==='wl')view=walletTab(users);
   else if(tab==='rt')view=ratingTab(users);
   else if(tab==='mk')view=marketTab((await api('/api/admin/nft_colls')).colls,users);
   else if(tab==='bd')view=badgesTab(users,(await api('/api/admin/bots')).bots,(await api('/api/admin/channels')).channels);
   else if(tab==='rp')view=reportsTab((await api('/api/admin/reports')).reports);
   else{const n=await api('/api/admin/nft');view=tab==='nu'?nftTab('username',n.usernames,users):nftTab('number',n.numbers,users);}
   setKids(body,view);
  }catch(e){setKids(body,h('div',{class:'err'},'Ошибка загрузки: '+e.message));}}

 box.append(h('h3',{},'🛡 Админ-панель'),tabs,body,h('button',{class:'btn gray',onclick:()=>ov.remove()},'Закрыть'));
 ov.append(box);ov.onclick=e=>{if(e.target===ov)ov.remove();};document.body.append(ov);
 render();}

/* ================= v5: 3D-подарки, кошелёк, боты, каналы, настройки ================= */
const fmtStars=n=>'⭐ '+(n||0).toLocaleString('ru');

// настоящий 3D на CSS: коробка из 6 граней или эмодзи, выдавленный слоями; можно крутить пальцем
function gift3d(g,size,inter){
 size=size||90;
 const obj=h('div',{class:'g3o'});
 if(g.model==='box'){
  ['f','b','r','l','t','d'].forEach(n=>obj.append(h('div',{class:'gf gf-'+n},n==='t'?h('span',{class:'bow'},g.glyph||'🎀'):null)));
 }else{
  const N=9;
  for(let i=0;i<N;i++){
   const l=h('div',{class:'gl'},String(g.glyph||'').startsWith('svg:')?artEl(g.glyph):(g.glyph||'🎁'));
   l.style.transform='translateZ('+((i-(N-1)/2)*size*.028).toFixed(1)+'px)';
   l.style.filter='brightness('+(0.5+0.5*i/(N-1)).toFixed(2)+')';
   obj.append(l);}
 }
 const w=h('div',{class:'g3 a-'+(g.anim||'spin'),style:'--s:'+size+'px;--c:'+(g.color||'#2aabee')},
  h('div',{class:'g3s'},obj),
  [0,1,2,3].map(i=>h('i',{class:'gsp s'+i},'✦')),h('div',{class:'g3sh'}));
 if(inter){
  let rx=-14,ry=-20,x0=null,y0=null;
  w.style.touchAction='none';w.style.cursor='grab';
  const paint=()=>{obj.style.animation='none';obj.style.transform='rotateX('+rx+'deg) rotateY('+ry+'deg)';};
  w.addEventListener('pointerdown',e=>{x0=e.clientX;y0=e.clientY;paint();try{w.setPointerCapture(e.pointerId);}catch(_){}});
  w.addEventListener('pointermove',e=>{if(x0==null)return;
   ry+=(e.clientX-x0)*.7;rx=Math.max(-80,Math.min(80,rx-(e.clientY-y0)*.5));x0=e.clientX;y0=e.clientY;paint();});
  const up=()=>{x0=null;};w.addEventListener('pointerup',up);w.addEventListener('pointercancel',up);
 }
 return w;}

function giftCard(g){
 return h('div',{class:'gcard',style:'--c:'+g.color,onclick:()=>giftPreview(g)},
  gift3d(g,110),h('div',{class:'gt'},'Подарок «'+g.name+'»'),
  g.number?h('div',{class:'gn'},'№'+g.number+(g.supply?' из '+g.supply:'')):null,
  g.note?h('div',{class:'gnote'},g.note):null);}

function giftPreview(g){
 const ov=h('div',{class:'overlay dim'});
 const canRate=g.owner_id===me.id&&g.from&&g.from.id&&g.from.id!==me.id;
 const rateRow=canRate?h('div',{style:'display:flex;gap:8px;margin:10px 0'},
  h('button',{class:'btn',style:'background:#2d6a4f',onclick:async()=>{
   try{const j=await api('/api/gift/rate',{id:g.id,value:1});ov.remove();toast('👍 +'+j.delta+' к рейтингу отправителя');}catch(e){toast(e.message);}}},'👍 Хороший'),
  h('button',{class:'btn',style:'background:#9b2226',onclick:async()=>{
   try{const j=await api('/api/gift/rate',{id:g.id,value:-1});ov.remove();toast('👎 '+j.delta+' к рейтингу отправителя');}catch(e){toast(e.message);}}},'👎 Плохой')):null;
 ov.append(h('div',{class:'modal',style:'text-align:center;max-width:380px'},
  gift3d(g,220,true),h('h3',{style:'margin-top:6px'},g.name),
  h('div',{class:'sub'},(g.number?'№'+g.number+(g.supply?' из '+g.supply:'')+' · ':'')+'от '+(g.from?g.from.name:'—')),
  g.note?h('div',{style:'margin-top:8px'},g.note):null,
  h('div',{class:'sub',style:'margin:8px 0'},'Покрути подарок пальцем 👆'),
  canRate?h('div',{class:'sub'},'Оцени подарок — это изменит рейтинг отправителя'):null,
  rateRow,
  h('button',{class:'btn gray',onclick:()=>ov.remove()},'Закрыть')));
 ov.onclick=e=>{if(e.target===ov)ov.remove();};document.body.append(ov);}

const GBTN=h('button',{class:'icon',title:'Подарок',onclick:()=>openGiftShop()},'🎁');

async function openGiftShop(){
 const c=chats.find(x=>x.id===cur);if(!c||c.kind!=='private')return;
 let gifts;try{gifts=(await api('/api/gifts')).gifts;await refreshMe();}catch(e){return toast(e.message);}
 const ov=h('div',{class:'overlay dim'});const m=h('div',{class:'modal',style:'max-width:520px'});
 const close=()=>ov.remove();
 function grid(){
  setKids(m,h('h3',{},'🎁 Подарок для '+c.title),h('div',{class:'sub',style:'margin-bottom:10px'},'Твой баланс: '+fmtStars(me.balance)),
   gifts.length?h('div',{class:'ggrid'},gifts.map(g=>{
    const out=g.supply&&g.sold>=g.supply;
    return h('div',{class:'gitem',style:out?'opacity:.45':'',onclick:()=>{if(!out)pick(g);}},
     gift3d(g,68),h('div',{class:'gt'},g.name),h('div',{class:'gp'},fmtStars(g.price)),
     g.supply?h('div',{class:'gs'},out?'закончились':'осталось '+(g.supply-g.sold)+' из '+g.supply):null);})):h('div',{class:'sub'},'Подарков пока нет'),
   h('button',{class:'btn gray',onclick:close},'Закрыть'));}
 function pick(g){
  const note=h('input',{class:'inp',placeholder:'Подпись (необязательно)',maxlength:200}),err=h('div',{class:'err'});
  const poor=me.balance<g.price;
  setKids(m,h('div',{style:'text-align:center'},gift3d(g,200,true),h('h3',{},g.name),
   h('div',{class:'sub'},'Цена: '+fmtStars(g.price)+' · у тебя '+fmtStars(me.balance))),note,err,
   h('button',{class:'btn',style:poor?'opacity:.5':'',onclick:async()=>{
    if(poor){err.textContent='Недостаточно звёзд. Попроси админа пополнить кошелёк.';return;}
    try{const jr=await api('/api/gift/send',{chat_id:c.id,gift_id:g.id,note:note.value});close();toast('Подарок отправлен 🎁'+(jr.rating_delta?' · рейтинг +'+jr.rating_delta:''));await refreshMe();loadMessages(true);loadChats();}
    catch(e){err.textContent=e.message;}}},'Подарить за '+fmtStars(g.price)),
   h('button',{class:'btn gray',onclick:grid},'← Назад'));}
 grid();ov.append(m);ov.onclick=e=>{if(e.target===ov)close();};document.body.append(ov);}

/* ---------- кошелёк ---------- */

/* ---------- чеки на звёзды (Send) ---------- */
async function showChecks(){
 const ov=h('div',{class:'overlay dim'});const m=h('div',{class:'modal',style:'max-width:440px'});
 const close=()=>ov.remove();
 async function draw(){
  let j;try{j=await api('/api/checks');}catch(e){setKids(m,h('div',{class:'err'},e.message));return;}
  const amt=h('input',{class:'inp',type:'number',min:1,placeholder:'Сумма ⭐ на одну активацию',value:'50'});
  const uses=h('input',{class:'inp',type:'number',min:1,max:1000,placeholder:'Число активаций (1 = один человек)',value:'1'});
  const codeIn=h('input',{class:'inp',placeholder:'Код чека (EQ…)',autocapitalize:'characters'});
  const list=j.checks.length?j.checks.map(c=>h('div',{class:'nrow'},
   h('div',{class:'nh'},h('span',{style:'font-family:monospace;font-weight:700'},c.code),
    h('span',{class:'sub'},c.amount+' ⭐ · '+(c.active?(c.left+' из '+c.max_uses+' ост.'):'закрыт'))),
   h('div',{class:'nact'},
    h('button',{class:'sbtn g',onclick:()=>{try{navigator.clipboard.writeText(c.code);toast('Код скопирован');}catch(e){toast(c.code);}}},'📋'),
    c.active?h('button',{class:'sbtn',onclick:async()=>{
     try{const j2=await api('/api/chat/private',{user_id:(await api('/api/users?q=send')).users.find(u=>u.username==='send').id});
      await api('/api/send',{chat_id:j2.chat_id,text:c.code});close();await loadChats();openChat(j2.chat_id);}
     catch(e){toast(e.message);}}},'В чат'):null))):h('div',{class:'sub'},'Чеков пока нет — создай первый');
  setKids(m,h('h3',{},'⭐ Чеки на звёзды'),
   h('div',{class:'sub',style:'margin-bottom:10px'},'Как Send в Telegram: создаёшь чек → делишься кодом → друг активирует и получает звёзды. Сумма списывается с тебя сразу (сумма × активации).'),
   h('div',{class:'psec',style:'font-size:15px;margin:4px 0 6px'},'Создать чек'),
   amt,uses,
   h('button',{class:'btn',onclick:async()=>{
    try{const r=await api('/api/checks/create',{amount:+amt.value,uses:+uses.value||1});
     toast('Чек '+r.check.code+' создан');await refreshMe();draw();
     try{navigator.clipboard.writeText(r.check.code);}catch(e){}}
    catch(e){toast(e.message);}}},'Создать чек'),
   h('div',{class:'psec',style:'font-size:15px;margin:16px 0 6px'},'Активировать чек'),
   codeIn,
   h('button',{class:'btn',style:'background:#2d6a4f',onclick:async()=>{
    try{const r=await api('/api/checks/claim',{code:codeIn.value});toast('+'+r.amount+' ⭐');await refreshMe();draw();}
    catch(e){toast(e.message);}}},'Получить звёзды'),
   h('div',{class:'psec',style:'font-size:15px;margin:16px 0 6px'},'Мои чеки'),
   list,
   h('div',{class:'sub',style:'margin-top:10px'},'Баланс: '+fmtStars(j.balance)),
   h('button',{class:'btn gray',onclick:close},'Закрыть'));}
 ov.append(m);ov.onclick=e=>{if(e.target===ov)close();};document.body.append(ov);draw();
}


async function showWallet(){
 let j;try{j=await api('/api/wallet');}catch(e){return toast(e.message);}
 const ov=h('div',{class:'overlay dim'});const m=h('div',{class:'modal'});
 const fmtD=ts=>{const d=new Date(ts*1000);return pad(d.getDate())+'.'+pad(d.getMonth()+1)+' '+pad(d.getHours())+':'+pad(d.getMinutes());};
 async function draw(){
  try{j=await api('/api/wallet');}catch(e){}
  setKids(m,h('h3',{},'👛 Кошелёк'),
   h('div',{class:'wcard'},h('div',{class:'l'},'Баланс'),h('div',{class:'bal'},fmtStars(j.balance))),
   h('button',{class:'btn',onclick:()=>sendStars(()=>{draw();refreshMe();})},'Отправить звёзды'),
   h('button',{class:'btn',style:'background:linear-gradient(90deg,#c9a227,#e8c547);color:#1a1200',onclick:()=>{ov.remove();showChecks();}},'⭐ Чеки (как Send)'),
   h('button',{class:'btn gray',onclick:async()=>{
     try{const u=(await api('/api/users?q=send')).users.find(x=>x.username==='send'||x.is_bot);
      if(!u){const all=await api('/api/users?q=@send');}
      let bot=null;
      const r=await api('/api/users?q=send');
      bot=(r.users||[]).find(x=>x.username==='send');
      if(!bot)return toast('Бот @send не найден');
      const j2=await api('/api/chat/private',{user_id:bot.id});ov.remove();await loadChats();openChat(j2.chat_id);
     }catch(e){toast(e.message);}}},'Открыть бота @send'),
   h('div',{class:'psec',style:'font-size:15px;margin:18px 0 6px'},'История'),
   j.tx.length?j.tx.map(t=>h('div',{class:'tx'},h('div',{},h('div',{},t.note||t.kind),h('div',{class:'sub'},fmtD(t.ts))),
     h('div',{class:t.amount>0?'p':'m2'},(t.amount>0?'+':'')+t.amount+' ⭐'))):h('div',{class:'sub'},'Операций пока нет'),
   h('div',{class:'sub',style:'margin-top:10px'},'Звёзды начисляет администратор или присылают друзья. Их можно тратить на подарки 🎁'),
   h('button',{class:'btn gray',onclick:()=>ov.remove()},'Закрыть'));}
 await draw();ov.append(m);ov.onclick=e=>{if(e.target===ov)ov.remove();};document.body.append(ov);}

function sendStars(done){
 const ov=h('div',{class:'overlay dim'});let to=null,t=0;
 const find=h('input',{class:'inp',placeholder:'Кому: имя, @юзернейм или номер'}),list=h('div'),
  amt=h('input',{class:'inp',type:'number',min:1,placeholder:'Сколько ⭐'}),err=h('div',{class:'err'}),chosen=h('div',{class:'sub'});
 find.oninput=()=>{clearTimeout(t);t=setTimeout(async()=>{const v=find.value.trim();if(!v){setKids(list);return;}
  try{const r=await api('/api/users?q='+encodeURIComponent(v));
   setKids(list,r.users.filter(u=>!u.is_bot&&!u.service).slice(0,6).map(u=>h('div',{class:'ur',onclick:()=>{to=u;chosen.textContent='Получатель: '+u.name;setKids(list);}},
    av(u,36),h('div',{class:'mid'},u.name,badge(u)))));}catch(e){}},250);};
 ov.append(h('div',{class:'modal'},h('h3',{},'Отправить звёзды'),find,list,chosen,amt,err,
  h('button',{class:'btn',onclick:async()=>{
   if(!to){err.textContent='Выбери получателя';return;}
   try{await api('/api/wallet/transfer',{user_id:to.id,amount:+amt.value});ov.remove();toast('Отправлено ⭐');done&&done();}
   catch(e){err.textContent=e.message;}}},'Отправить'),
  h('button',{class:'btn gray',onclick:()=>ov.remove()},'Отмена')));
 ov.onclick=e=>{if(e.target===ov)ov.remove();};document.body.append(ov);}

/* ---------- каналы ---------- */
function newChannel(){
 const ov=h('div',{class:'overlay dim'});let cAv,priv=false;
 const title=h('input',{class:'inp',placeholder:'Название канала',maxlength:50}),descr=h('textarea',{class:'inp',rows:2,placeholder:'Описание (необязательно)',maxlength:300}),
  handle=h('input',{class:'inp',placeholder:'Публичная ссылка, например my_channel',autocapitalize:'none',maxlength:32}),err=h('div',{class:'err'});
 ov.append(h('div',{class:'modal'},h('h3',{},'📢 Новый канал'),avatarPicker({title:'К'},d=>cAv=d),title,descr,handle,
  h('div',{class:'srow',style:'padding:8px 2px'},h('div',{class:'mid'},'Приватный',h('div',{class:'sub'},'Не показывается в поиске')),toggle(false,v=>priv=v)),
  h('div',{class:'sub'},'Писать в канале можешь только ты (и добавленные боты). Остальные подписываются и читают. Бусты открывают оформление.'),err,
  h('button',{class:'btn',onclick:async()=>{try{const j=await api('/api/chat/channel',{title:title.value,descr:descr.value,handle:handle.value,private:priv,avatar:cAv});
    ov.remove();await loadChats();openChat(j.chat_id);}catch(e){err.textContent=e.message;}}},'Создать'),
  h('button',{class:'btn gray',onclick:()=>ov.remove()},'Отмена')));
 ov.onclick=e=>{if(e.target===ov)ov.remove();};document.body.append(ov);}
async function openChannel(c){
 try{if(!c.joined)await api('/api/channel/join',{chat_id:c.id});$('#search').value='';searchRes=[];searchCh=[];await loadChats();openChat(c.id);}
 catch(e){toast(e.message);}}

/* ---------- боты ---------- */
function pairEditor(obj,ph1,ph2){
 const box=h('div'),rows=[];
 const add=(k,v)=>{
  const a=h('input',{class:'inp',placeholder:ph1,value:k||'',style:'margin:0;flex:0 0 34%'}),
   b=h('textarea',{class:'inp',placeholder:ph2,rows:2,style:'margin:0;flex:1;resize:vertical'});b.value=v||'';
  const r=h('div',{class:'nact',style:'align-items:flex-start;margin-bottom:6px'},a,b,
   h('button',{class:'sbtn r',onclick:()=>{r.remove();const i=rows.findIndex(x=>x[2]===r);if(i>=0)rows.splice(i,1);}},'✕'));
  rows.push([a,b,r]);box.append(r);};
 Object.entries(obj||{}).forEach(([k,v])=>add(k,v));
 return {el:h('div',{},box,h('button',{class:'sbtn g',onclick:()=>add('','')},'+ добавить')),
  get:()=>Object.fromEntries(rows.map(([a,b])=>[a.value.trim(),b.value]).filter(([k,v])=>k&&v))};}

async function showBots(){
 const ov=h('div',{class:'overlay dim'});const m=h('div',{class:'modal',style:'max-width:520px'});
 async function render(){
  let bots=[];try{bots=(await api('/api/bots')).bots;}catch(e){toast(e.message);}
  const nm=h('input',{class:'inp',placeholder:'Имя бота',maxlength:40}),
   un=h('input',{class:'inp',placeholder:'юзернейм, заканчивается на bot',autocapitalize:'none'}),err=h('div',{class:'err'});
  setKids(m,h('h3',{},'🤖 Мои боты'),
   bots.length?bots.map(b=>h('div',{class:'ur',onclick:()=>botEditor(b,render)},av({name:b.name,avatar_url:b.avatar_url},40),
    h('div',{class:'mid'},b.name,h('span',{class:'bt'},'БОТ'),b.frozen?h('span',{class:'tag d'},'❄️ заморожен'):null,h('small',{},'@'+b.username)))):
    h('div',{class:'sub',style:'margin-bottom:10px'},'Ботов пока нет'),
   h('div',{class:'frm'},h('b',{},'Создать бота'),nm,un,err,
    h('button',{class:'sbtn',onclick:async()=>{try{const j=await api('/api/bots/create',{name:nm.value,username:un.value});await render();loadChats().catch(()=>{});botEditor(j.bot,render);}catch(e){err.textContent=e.message;}}},'Создать')),
   h('button',{class:'btn gray',onclick:()=>ov.remove()},'Закрыть'));}
 await render();ov.append(m);ov.onclick=e=>{if(e.target===ov)ov.remove();};document.body.append(ov);}

function botEditor(b,onChange){
 const ov=h('div',{class:'overlay dim'});
 const name=h('input',{class:'inp',value:b.name,maxlength:40}),desc=h('textarea',{class:'inp',rows:2,placeholder:'Описание бота',maxlength:300});desc.value=b.desc||'';
 const cmds=pairEditor(b.script.commands,'/команда','Ответ бота'),kws=pairEditor(b.script.keywords,'слово в сообщении','Ответ бота');
 const def=h('textarea',{class:'inp',rows:2,placeholder:'Ответ на любое другое сообщение (необязательно)'});def.value=b.script.default||'';
 const tok=h('div',{class:'tokbox',title:'Нажми, чтобы скопировать',onclick:()=>{try{navigator.clipboard.writeText(b.token);toast('Токен скопирован');}catch(e){}}},b.token);
 const snippet='import json, urllib.request\n\nURL = "'+location.origin+'/bot'+b.token+'/"\n\ndef call(method, **p):\n    req = urllib.request.Request(URL + method, json.dumps(p).encode(), {"Content-Type": "application/json"})\n    return json.load(urllib.request.urlopen(req, timeout=40))\n\noffset = 0\nwhile True:\n    for u in call("getUpdates", offset=offset, timeout=20)["result"]:\n        offset = u["update_id"] + 1\n        m = u["message"]\n        call("sendMessage", chat_id=m["chat"]["id"], text="Ты написал: " + m["text"])\n';
 const err=h('div',{class:'err'});
 ov.append(h('div',{class:'modal',style:'max-width:540px'},
  h('h3',{},'🤖 @'+b.username,b.frozen?h('span',{class:'tag d'},'❄️ заморожен'):null),
  b.frozen?h('div',{class:'sub',style:'margin-bottom:8px'},'Администратор заморозил этого бота: он не отвечает и API не работает.'):null,
  h('div',{class:'lbl'},'Имя'),name,h('div',{class:'lbl'},'Описание'),desc,
  h('div',{class:'lbl'},'Токен (никому не показывай)'),tok,
  h('button',{class:'sbtn g',style:'margin-top:6px',onclick:async()=>{if(!confirm('Старый токен перестанет работать. Продолжить?'))return;
    try{const j=await api('/api/bots/update',{id:b.id,regen_token:1});ov.remove();onChange&&await onChange();botEditor(j.bot,onChange);}catch(e){toast(e.message);}}},'Новый токен'),
  h('div',{class:'psec',style:'font-size:16px;margin:18px 0 6px'},'Команды'),
  h('div',{class:'sub'},'Можно использовать {name} (имя человека), {bot}, {time}.'),cmds.el,
  h('div',{class:'psec',style:'font-size:16px;margin:18px 0 6px'},'Ключевые слова'),kws.el,
  h('div',{class:'psec',style:'font-size:16px;margin:18px 0 6px'},'Ответ по умолчанию'),def,
  h('details',{style:'margin-top:14px'},h('summary',{style:'cursor:pointer;color:#2aabee'},'Свой код: Bot API (Python)'),
   h('div',{class:'sub',style:'margin:6px 0'},'Методы: getMe, getUpdates (offset, timeout), sendMessage (chat_id, text). Запусти скрипт на этом же устройстве или в сети Wi-Fi.'),
   h('pre',{class:'code2'},snippet)),
  err,
  h('button',{class:'btn',onclick:async()=>{try{
    await api('/api/bots/update',{id:b.id,name:name.value,desc:desc.value,script:{commands:cmds.get(),keywords:kws.get(),default:def.value}});
    toast('Бот сохранён');ov.remove();onChange&&onChange();loadChats().catch(()=>{});}catch(e){err.textContent=e.message;}}},'Сохранить'),
  h('button',{class:'btn gray',onclick:()=>ov.remove()},'Закрыть')));
 ov.onclick=e=>{if(e.target===ov)ov.remove();};document.body.append(ov);}

/* ---------- каналы: страница, бусты, настройки ---------- */
const LVL_PERKS=[['🎨','Цвет обложки канала'],['🌸','Узор из эмодзи на обложке'],['😎','Эмодзи-статус рядом с названием'],['👑','Максимальный уровень']];
async function showChannel(id){
 let c;try{c=(await api('/api/channel/info?id='+id)).channel;}catch(e){return toast(e.message);}
 const ov=h('div',{class:'overlay pf'});const close=()=>ov.remove();
 const mineC=c.owner===me.id;
 const reload=()=>{close();showChannel(id);};
 const act=(ic,t,fn)=>h('button',{class:'pact',onclick:fn},h('i',{},ic),t);
 const acts=[];
 if(c.discussion_chat_id) acts.push(act('💬','Чат обсуждения',async()=>{try{const j=await api('/api/channel/discussion/join',{chat_id:c.id});await loadChats();close();openChat(j.chat_id);}catch(e){toast(e.message);}}));
 acts.push(act(c.joined?'💬':'➕',c.joined?'Открыть':'Подписаться',async()=>{try{if(!c.joined)await api('/api/channel/join',{chat_id:c.id});await loadChats();close();openChat(c.id);}catch(e){toast(e.message);}}));
 acts.push(act('⚡','Буст',()=>{const el=ov.querySelector('#boostCard');el&&el.scrollIntoView({behavior:'smooth'});}));
 if(c.joined&&!mineC)acts.push(act('🚪','Отписаться',async()=>{try{await api('/api/channel/leave',{chat_id:c.id});await loadChats();if(cur===c.id)$('#backBtn').click();reload();}catch(e){toast(e.message);}}));
 const more=[['🚩 Пожаловаться',()=>reportDialog('chat',c.id,c.title)]];
 if(c.handle)more.push(['📋 Скопировать ссылку',()=>{try{navigator.clipboard.writeText(location.origin+'/#@'+c.handle);toast('Скопировано');}catch(e){}}]);
 if(mineC){more.push(['⚙️ Настройки канала',()=>channelSettings(c,reload)]);
  more.push(['🗑 Удалить канал',async()=>{if(!confirm('Удалить канал «'+c.title+'» вместе со всеми сообщениями? Необратимо.'))return;
   try{await api('/api/channel/delete',{chat_id:c.id});close();if(cur===c.id)$('#backBtn').click();await loadChats();toast('Канал удалён');}catch(e){toast(e.message);}}]);}
 if(me.is_admin){more.push(['🏷 Метки…',()=>{close();adminBadgeDialog('chat',c.id,c.title,c.badges);}]);
  more.push(['🗑 Удалить (админ)',async()=>{if(!confirm('Удалить канал «'+c.title+'»?'))return;try{await api('/api/admin/chat/delete',{id:c.id});close();if(cur===c.id)$('#backBtn').click();await loadChats();toast('Канал удалён');}catch(e){toast(e.message);}}]);}
 acts.push(act('⋯','Ещё',e=>{e.stopPropagation();popMenu(e.currentTarget,more,false);}));
 const bn=c.banner;
 const head=h('div',{class:'pfh',style:'background:'+bannerStyle(bn)},patternEl(bn?bn.pattern:''),
  h('div',{class:'pbar2'},h('button',{class:'icon',style:'color:#fff;font-size:26px',onclick:close},'←'),
   mineC?h('button',{class:'icon',style:'color:#fff',onclick:()=>channelSettings(c,reload)},'⚙️'):h('span')),
  av({avatar_url:c.avatar_url,name:c.title},112),
  h('h1',{},c.title,badge(c)),
  h('div',{class:'st'},c.members+' подписчиков'+(c.level?' · уровень '+c.level:'')+(c.frozen?' · ❄️ заморожен':'')),
  h('div',{class:'pacts'},acts));
 const need=c.next_need;const prog=need?Math.min(100,Math.round(c.boosts/need*100)):100;
 const bCard=h('div',{class:'pcard',id:'boostCard',style:'padding:14px 18px'},
  h('div',{style:'display:flex;justify-content:space-between;align-items:center'},h('b',{style:'font-size:17px'},'⚡ Бусты канала'),h('span',{class:'sub'},'Уровень '+c.level)),
  h('div',{class:'lvl'},h('i',{style:'width:'+prog+'%'})),
  h('div',{class:'sub'},need?(c.boosts_inf?'∞':c.boosts)+' из '+need+' бустов до уровня '+(c.level+1):'Достигнут максимальный уровень · '+(c.boosts_inf?'∞':c.boosts)+' бустов'),
  h('div',{style:'margin-top:10px'},LVL_PERKS.map(([ic,t],i)=>h('div',{style:'padding:4px 0;opacity:'+(c.level>=i+1?1:.45)},ic+' Ур. '+(i+1)+' — '+t+(c.level>=i+1?' ✓':'')))),
  c.boosters.length?h('div',{class:'bst'},c.boosters.map(u=>av(u,30))):null,
  h('button',{class:'btn',style:c.boosted_by_me?'':'background:linear-gradient(90deg,#6c8cff,#c65bff)',onclick:async()=>{
   if(!c.can_boost&&!c.boosted_by_me)return showPremium();
   try{await api('/api/channel/boost',{chat_id:c.id,on:!c.boosted_by_me});toast(c.boosted_by_me?'Буст снят':'Канал забустен ⚡');reload();}catch(e){toast(e.message);}}},
   c.boosted_by_me?'Снять мой буст':(c.can_boost?'Забустить ('+(c.slots_left>=999?'∞ свободно':c.slots_left+' из '+BOOST_SLOTS_UI+' свободно')+')':'Нужен Premium для буста')));
 const rows=[];
 if(c.descr)rows.push(h('div',{class:'prow2'},h('div',{class:'l'},'описание'),h('div',{class:'v',style:'white-space:pre-wrap;font-size:17px'},c.descr)));
 if(c.handle)rows.push(h('div',{class:'prow2'},h('div',{class:'l'},'ссылка'),h('div',{class:'v acc'},'@'+c.handle)));
 if(c.owner_user)rows.push(h('div',{class:'prow2',style:'cursor:pointer',onclick:()=>showProfile(c.owner_user.id)},h('div',{class:'l'},'владелец'),h('div',{class:'v',style:'font-size:17px'},c.owner_user.name,badge(c.owner_user))));
 rows.push(h('div',{class:'prow2'},h('div',{class:'l'},'тип'),h('div',{class:'v',style:'font-size:17px'},(c.pub?'Публичный канал':'Приватный канал')+(c.sign?' · с подписью автора':''))));
 ov.append(head,h('div',{class:'pbody'},warnEl(c),h('div',{class:'pcard'},rows),bCard));
 document.body.append(ov);}
const BOOST_SLOTS_UI=3;

function channelSettings(c,after){
 const ov=h('div',{class:'overlay dim'});let newAv;
 const st={color:c.banner?c.banner.color:-1,pattern:c.banner?c.banner.pattern:'',emoji:c.estatus||''};
 const title=h('input',{class:'inp',value:c.title,maxlength:50}),descr=h('textarea',{class:'inp',rows:3,maxlength:300,placeholder:'Описание'},c.descr||''),
  handle=h('input',{class:'inp',value:c.handle||'',placeholder:'Публичная ссылка (без @)',autocapitalize:'none',maxlength:32});
 let pub=c.pub,sign=c.sign;
 const lock=n=>c.level<n;
 const prev=h('div',{class:'bprev'});
 const paintPrev=()=>{prev.style.background=bannerBg(st.color);setKids(prev,patternEl(st.pattern),h('div',{class:'bn'},title.value+' ',st.emoji?statusEl(st.emoji,18):null));};
 title.oninput=paintPrev;
 const gate=n=>{if(lock(n)){toast('Нужен '+n+' уровень канала — собери бусты ⚡');return true;}return false;};
 const sws=h('div',{class:'sws'});
 const paintSws=()=>setKids(sws,[-1,...BANNERS.map((_,i)=>i)].map(i=>h('div',{class:'swc'+(st.color===i?' on':'')+(lock(1)?' lk':''),style:'background:'+bannerBg(i),onclick:()=>{if(gate(1))return;st.color=i;paintSws();paintPrev();}},i<0?'✕':'')));
 const chips=(list,get,set,n)=>{const box=h('div',{class:'chips'});
  const paint=()=>setKids(box,list.map(p=>h('button',{class:'chip'+(get()===p?' on':''),onclick:()=>{if(gate(n))return;set(p);paint();paintPrev();}},patternChipLabel(p))));paint();return box;};
 const discussion=h('div',{class:'srow',style:'padding:8px 2px'});
 if(c.discussion_chat_id){
  discussion.append(
   h('div',{class:'mid'},'💬 Чат обсуждения',h('div',{class:'sub'},'Групповой чат для комментариев подписчиков')),
   h('button',{class:'btn gray',style:'width:auto;margin:0',onclick:async()=>{try{const j=await api('/api/channel/discussion/join',{chat_id:c.id});await loadChats();ov.remove();openChat(j.chat_id);}catch(e){toast(e.message);}}},'Открыть')
  );
 }else{
  discussion.append(
   h('div',{class:'mid'},'💬 Чат обсуждения',h('div',{class:'sub'},'Создай отдельный групповой чат для общения подписчиков (как в Telegram)')),
   h('button',{class:'btn',style:'width:auto;margin:0',onclick:async()=>{try{const j=await api('/api/channel/discussion',{chat_id:c.id});await loadChats();ov.remove();openChat(j.chat_id);toast('Чат обсуждения создан');}catch(e){toast(e.message);}}},'Создать')
  );
 }
 const err=h('div',{class:'err'});
 ov.append(h('div',{class:'modal'},h('h3',{},'⚙️ Настройки канала'),avatarPicker({avatar_url:c.avatar_url,name:c.title},d=>newAv=d),
  h('div',{class:'lbl'},'Название'),title,h('div',{class:'lbl'},'Описание'),descr,h('div',{class:'lbl'},'Публичная ссылка'),handle,
  h('div',{class:'psec',style:'font-size:15px;margin:14px 0 4px'},'Тип канала'),
  h('div',{class:'srow',style:'padding:8px 2px'},h('div',{class:'mid'},'Публичный канал',h('div',{class:'sub'},'Виден в поиске и по ссылке')),toggle(pub,v=>pub=v)),
  h('div',{class:'srow',style:'padding:8px 2px'},h('div',{class:'mid'},'Подпись автора',h('div',{class:'sub'},'Показывать имя под постами')),toggle(sign,v=>sign=v)),
  h('div',{class:'psec',style:'font-size:15px;margin:14px 0 4px'},'Обсуждение'),
  discussion,
  h('div',{class:'psec',style:'font-size:16px;margin:16px 0 6px'},'⚡ Оформление (уровень канала: '+c.level+')'),
  prev,h('div',{class:'lbl'},'Цвет обложки · 1 ур.'),sws,h('div',{class:'lbl'},'Узор (эмодзи / своя картинка) · 2 ур.'),chips(PATTERNS,()=>isBannerImg(st.pattern)?'':st.pattern,v=>{st.pattern=v;paintPrev();},2),
  (()=>{const bf=h('input',{type:'file',accept:'image/png,image/jpeg,image/webp,image/gif',class:'hide'});
   bf.onchange=async()=>{if(!bf.files[0])return;try{if(gate(2))return;const d=await fileToBannerMedia(bf.files[0]);st.pattern=d;paintPrev();}catch(e){toast(e.message);}};
   return h('div',{style:'margin:4px 0 10px;display:flex;gap:8px;flex-wrap:wrap'},
    h('button',{class:'chip',style:'width:auto;padding:0 12px;font-size:13px',onclick:()=>{if(gate(2))return;bf.click();}},'🖼 Свой узор (фото/GIF)'),
    isBannerImg(st.pattern)?h('button',{class:'chip',style:'width:auto;padding:0 12px;font-size:13px',onclick:()=>{st.pattern='';paintPrev();}},'✕ Убрать'):null,
    bf);})(),
  h('div',{class:'lbl'},'Эмодзи-статус · 3 ур.'),chips(STATUSES,()=>st.emoji,v=>st.emoji=v,3),
  (()=>{const sf=h('input',{type:'file',accept:'image/png,image/jpeg,image/webp,image/gif',class:'hide'});
   sf.onchange=async()=>{if(!sf.files[0])return;try{if(gate(3))return;const d=await fileToStatusMedia(sf.files[0]);st.emoji=d;paintPrev();}catch(e){toast(e.message);}};
   return h('div',{style:'margin:4px 0 10px'},h('button',{class:'chip',style:'width:auto;padding:0 12px;font-size:13px',onclick:()=>{if(gate(3))return;sf.click();}},'🖼 Своя картинка/GIF'),sf);})(),
  err,
  h('button',{class:'btn',onclick:async()=>{try{
   const body={chat_id:c.id,title:title.value,descr:descr.value,handle:handle.value,pub,sign};if(newAv!==undefined)body.avatar=newAv;
   if(st.color!==(c.banner?c.banner.color:-1))body.bcolor=st.color;
   if(st.pattern!==(c.banner?c.banner.pattern:''))body.bpattern=st.pattern;
   if(st.emoji!==(c.estatus||''))body.estatus=st.emoji;
   await api('/api/channel/settings',body);ov.remove();toast('Сохранено');await loadChats();after&&after();}catch(e){err.textContent=e.message;}}},'Сохранить'),
  h('button',{class:'btn gray',onclick:()=>ov.remove()},'Отмена')));
 paintSws();paintPrev();ov.onclick=e=>{if(e.target===ov)ov.remove();};document.body.append(ov);}

/* ---------- маркет NFT-подарков и подарков ---------- */

/* ---------- GodFragment (как Telegram Fragment) ---------- */
function fmtPhoneFrag(n){
 n=String(n||'').replace(/\D/g,'');
 if(n.startsWith('888'))return '+888 '+n.slice(3).replace(/(\d{3})(?=\d)/g,'$1 ').trim();
 return '+'+n;
}
async function showFragment(startTab){
 const ov=h('div',{class:'overlay frag'});const close=()=>ov.remove();
 const box=h('div',{class:'fm'});
 let tab=startTab||'usernames',q='',tmo=0;
 const head=h('div',{class:'fbar'},
  h('button',{class:'icon',style:'color:#fff;font-size:26px',onclick:close},'←'),
  h('div',{style:'flex:1;text-align:center;font-weight:700;font-size:17px'},'GodFragment'),
  h('span',{style:'width:36px'}));
 const title=h('div',{class:'fhead'},
  h('h1',{},'GodFragment'),
  h('div',{class:'sub'},'Анонимные номера, NFT-юзернеймы и коллекционные подарки'));
 const tabsEl=h('div',{class:'ftabs'});
 const search=h('input',{class:'fsearch',placeholder:'Поиск…'});
 const bal=h('div',{class:'fbal'});
 const body=h('div');
 const TABS=[['usernames','@ Юзернеймы'],['numbers','📞 Номера'],['collectibles','🎁 Коллекции'],['mine','👜 Мои лоты']];
 function paintTabs(){setKids(tabsEl,TABS.map(([k,t])=>h('button',{class:tab===k?'on':'',onclick:()=>{tab=k;draw();}},t)));}
 search.oninput=()=>{clearTimeout(tmo);tmo=setTimeout(()=>{q=search.value.trim();draw();},250);};

 async function buy(kind,id,label,price){
  if(!confirm('Купить '+label+' за '+fmtStars(price)+'?'))return;
  try{await api('/api/fragment/buy',{kind,id});toast('Куплено на GodFragment ✅');await refreshMe();draw();}
  catch(e){toast(e.message);}}

 async function listLot(kind,id,defP){
  const p=prompt('Цена в звёздах ⭐:',String(defP||100));if(!p)return;
  try{await api('/api/fragment/list',{kind,id,price:+p});toast('Выставлено на GodFragment');draw();}
  catch(e){toast(e.message);}}

 async function unlistLot(kind,id){
  try{await api('/api/fragment/unlist',{kind,id});toast('Снято с продажи');draw();}
  catch(e){toast(e.message);}}

 function cardUsername(it){
  return h('div',{class:'fcard',onclick:()=>{
   if(it.owner&&it.owner.id===me.id)return toast('Это твой лот');
   buy('username',it.id,'@'+it.handle,it.price);
  }},
   h('div',{class:'fh'},h('span',{class:'at'},'@'),it.handle),
   h('div',{class:'fp'},fmtStars(it.price)),
   h('div',{class:'fo'},it.pool?'от GodFragment':(it.owner?('продаёт '+it.owner.name):'—')));
 }
 function cardNumber(it){
  return h('div',{class:'fcard',onclick:()=>{
   if(it.owner&&it.owner.id===me.id)return toast('Это твой лот');
   buy('number',it.id,fmtPhoneFrag(it.number),it.price);
  }},
   h('div',{class:'fh'},fmtPhoneFrag(it.number)),
   h('div',{class:'fp'},fmtStars(it.price)),
   h('div',{class:'fo'},it.pool?'от GodFragment':(it.owner?('продаёт '+it.owner.name):'—')));
 }

 async function draw(){
  paintTabs();
  try{
   const j=await api('/api/fragment?tab='+encodeURIComponent(tab)+(q?'&q='+encodeURIComponent(q):''));
   setKids(bal,'Баланс: '+fmtStars(j.balance)+' · покупки за звёзды ⭐');
   if(tab==='usernames'){
    setKids(body,j.items&&j.items.length?h('div',{class:'fgrid'},j.items.map(cardUsername)):
     h('div',{class:'fempty'},'Пока нет юзернеймов в продаже.\nАдмин может выпустить NFT-юзы, владельцы — выставить свои.'));
   }else if(tab==='numbers'){
    setKids(body,j.items&&j.items.length?h('div',{class:'fgrid'},j.items.map(cardNumber)):
     h('div',{class:'fempty'},'Пока нет анонимных номеров в продаже.'));
   }else if(tab==='collectibles'){
    setKids(body,j.items&&j.items.length?h('div',{class:'nftgrid'},j.items.map(it=>nftCard(it,72,{ctx:{market:true,after:draw}}))):
     h('div',{class:'fempty'},'Коллекционных подарков на продаже нет.'));
   }else{
    const uns=j.usernames||[],nums=j.numbers||[],nfts=j.nfts||[];
    const blocks=[];
    blocks.push(h('div',{class:'psec',style:'font-size:15px;margin:8px 0'},'Юзернеймы'));
    if(!uns.length)blocks.push(h('div',{class:'sub'},'Нет NFT-юзернеймов'));
    uns.forEach(x=>blocks.push(h('div',{class:'sit'},
     h('div',{class:'sit-main'},h('span',{class:'nft'},'💎 @'+x.handle),h('div',{class:'sub'},x.listed?('на GodFragment · '+fmtStars(x.price)):(x.active?'в профиле':'выкл.'))),
     h('div',{class:'sit-acts'},
      x.listed?h('button',{class:'sbtn sm g',onclick:()=>unlistLot('username',x.id)},'Снять'):
               h('button',{class:'sbtn sm',onclick:()=>listLot('username',x.id,x.value||100)},'Продать')))));
    blocks.push(h('div',{class:'psec',style:'font-size:15px;margin:14px 0 8px'},'Номера'));
    if(!nums.length)blocks.push(h('div',{class:'sub'},'Нет NFT-номеров'));
    nums.forEach(x=>blocks.push(h('div',{class:'sit'},
     h('div',{class:'sit-main'},h('span',{class:'nft'},'💎 '+fmtPhoneFrag(x.number)),h('div',{class:'sub'},x.listed?('на GodFragment · '+fmtStars(x.price)):'не в продаже')),
     h('div',{class:'sit-acts'},
      x.listed?h('button',{class:'sbtn sm g',onclick:()=>unlistLot('number',x.id)},'Снять'):
               h('button',{class:'sbtn sm',onclick:()=>listLot('number',x.id,100)},'Продать')))));
    blocks.push(h('div',{class:'psec',style:'font-size:15px;margin:14px 0 8px'},'NFT-подарки'));
    if(!nfts.length)blocks.push(h('div',{class:'sub'},'Нет NFT-подарков'));
    else blocks.push(h('div',{class:'nftgrid'},nfts.map(it=>nftCard(it,72,{ctx:{market:false,after:draw}}))));
    setKids(body,blocks);
   }
  }catch(e){setKids(body,h('div',{class:'err'},e.message));}
 }
 setKids(box,head,title,tabsEl,search,bal,body);
 ov.append(box);document.body.append(ov);draw();
}


async function showMarket(){
 const ov=h('div',{class:'overlay dim'});const m=h('div',{class:'modal',style:'max-width:620px'});
 let tab='nft',coll='',sort='price_asc';
 const close=()=>ov.remove();
 async function draw(){
  let d;
  try{
   if(tab==='mine'){const j=await api('/api/nft/mine');d={mine:j};await refreshMe();}
   else d=await api('/api/market?sort='+sort+(coll?'&coll='+coll:''));
  }catch(e){setKids(m,h('div',{class:'err'},e.message));return;}
  await refreshMe().catch(()=>{});
  const tabs=h('div',{class:'tabs2'},[['nft','💎 NFT'],['gift','🎁 Подарки'],['mine','👜 Мои']].map(([k,t])=>h('button',{class:tab===k?'on':'',onclick:()=>{tab=k;draw();}},t)));
  let view;
  const ctx={market:true,after:draw};
  if(tab==='nft'){
   const chips=h('div',{class:'chips2'},h('button',{class:coll===''?'on':'',onclick:()=>{coll='';draw();}},'Все'),
    d.colls.map(c=>h('button',{class:String(c.id)===String(coll)?'on':'',onclick:()=>{coll=c.id;draw();}},c.glyph+' '+c.name+(c.floor?' · от '+c.floor+'⭐':''))));
   const sel=h('select',{class:'inp',onchange:e=>{sort=e.target.value;draw();}},[['price_asc','Сначала дешёвые'],['price_desc','Сначала дорогие'],['new','Новые'],['num','По номеру']].map(([v,t])=>h('option',{value:v,selected:v===sort},t)));
   view=[chips,sel,d.items.length?h('div',{class:'nftgrid'},d.items.map(it=>nftCard(it,72,{ctx}))):h('div',{class:'sub',style:'padding:20px;text-align:center'},'Лотов пока нет')];
  }else if(tab==='gift'){
   view=d.gifts.length?h('div',{class:'ggrid'},d.gifts.map(g=>h('div',{class:'gitem'},gift3d(g,68),h('div',{class:'gt'},g.name),h('div',{class:'gs'},(g.number?'№'+g.number+' · ':'')+'от '+g.owner.name),
     h('div',{class:'gp',style:'margin:4px 0'},fmtStars(g.listed)),
     h('button',{class:'sbtn',style:g.owner.id===me.id?'opacity:.4':'',onclick:async()=>{if(g.owner.id===me.id)return toast('Это твой лот');
      try{await api('/api/market/buy',{kind:'gift',id:g.id});toast('Куплено 🎉');draw();}catch(e){toast(e.message);}}},'Купить')))):
    h('div',{class:'sub',style:'padding:20px;text-align:center'},'Обычные подарки на продаже появляются, когда владельцы выставляют их из «Мои».');
  }else{
   const j=d.mine;
   view=[h('div',{class:'psec',style:'font-size:15px;margin:4px 0 8px'},'Мои NFT-подарки'),
    j.items.length?h('div',{class:'nftgrid'},j.items.map(it=>nftCard(it,72,{ctx:{market:false,after:draw}}))):h('div',{class:'sub'},'Пока нет. Купи на маркете 💎'),
    h('div',{class:'psec',style:'font-size:15px;margin:16px 0 8px'},'Мои подарки'),
    j.gifts.length?h('div',{class:'ggrid'},j.gifts.map(g=>{const lp=j.listed[String(g.id)]||0;
     return h('div',{class:'gitem'},gift3d(g,68),h('div',{class:'gt'},g.name),h('div',{class:'gs'},lp?'на продаже · '+lp+'⭐':(g.number?'№'+g.number:'')),
      lp?h('button',{class:'sbtn g',onclick:async()=>{try{await api('/api/market/unlist',{kind:'gift',id:g.id});draw();}catch(e){toast(e.message);}}},'Снять')
        :h('button',{class:'sbtn',onclick:async()=>{const p=prompt('Цена в звёздах ⭐:',String(g.price||50));if(!p)return;try{await api('/api/market/list',{kind:'gift',id:g.id,price:+p});draw();}catch(e){toast(e.message);}}},'Продать'));})):h('div',{class:'sub'},'Подарков пока нет')];
  }
  setKids(m,h('h3',{},'🛍 Маркет'),h('div',{class:'sub',style:'margin-bottom:8px'},'Баланс: '+fmtStars(me.balance)+' · покупай и продавай за звёзды ⭐'),tabs,view,h('button',{class:'btn gray',onclick:close},'Закрыть'));}
 ov.append(m);ov.onclick=e=>{if(e.target===ov)close();};document.body.append(ov);draw();}

/* ---------- админ: метки на людей, ботов и каналы ---------- */
async function adminBadgeDialog(kind,id,name,cur){
 let defs;try{defs=(await api('/api/badges')).badges;}catch(e){return toast(e.message);}
 const ov=h('div',{class:'overlay dim'});const on=new Set((cur||[]).map(b=>b.key));
 const box=h('div');
 const draw=()=>setKids(box,defs.map(b=>h('div',{class:'srow',style:'cursor:default'},
  h('span',{class:b.kind==='label'?'vlabel':'vb',style:'background:'+b.color},b.icon),
  h('div',{class:'mid'},h('div',{},b.title),h('div',{class:'sub',style:'font-size:12px'},b.descr)),
  toggle(on.has(b.key),async v=>{try{await api('/api/admin/badge',{kind,id,key:b.key,on:v});if(v)on.add(b.key);else on.delete(b.key);refresh();}catch(e){toast(e.message);draw();}}))));
 draw();
 ov.append(h('div',{class:'modal'},h('h3',{},'🏷 Метки: '+name),box,h('button',{class:'btn gray',onclick:()=>ov.remove()},'Закрыть')));
 ov.onclick=e=>{if(e.target===ov)ov.remove();};document.body.append(ov);}

/* ---------- настройки как в Telegram ---------- */
function applyPrefs(){
 document.documentElement.style.setProperty('--fs',(localStorage.getItem('fs')||'15')+'px');
 document.documentElement.style.setProperty('--ac',localStorage.getItem('ac')||'#6b5cff');
 document.body.classList.toggle('nomo',localStorage.getItem('nomo')==='1');}
const ACCENTS=['#6b5cff','#2aabee','#34c759','#ff9f0a','#ff453a','#ff5fa2','#bf5af2'];
function cloudPwDialog(after){
 const ov=h('div',{class:'overlay dim'});
 const has=!!me.has_cloud_pw;
 const cur=has?h('input',{class:'inp',type:'password',placeholder:'Текущий пароль',autocomplete:'current-password'}):null;
 const p1=h('input',{class:'inp',type:'password',placeholder:'Новый пароль (от 4 символов)',autocomplete:'new-password'});
 const p2=h('input',{class:'inp',type:'password',placeholder:'Повторите пароль',autocomplete:'new-password'});
 const hint=h('input',{class:'inp',placeholder:'Подсказка (необязательно)',maxlength:60});
 const err=h('div',{class:'err'});
 const send=async(nw)=>{try{await api('/api/cloud_password',{current:cur?cur.value:'',new:nw,hint:hint.value});await refreshMe();ov.remove();toast(nw?'🔒 Облачный пароль установлен':'Облачный пароль отключён');after&&after();}catch(e){err.textContent=e.message;}};
 ov.append(h('div',{class:'modal'},h('h3',{},'🔒 Облачный пароль'),
  h('div',{class:'sub',style:'margin-bottom:8px;line-height:1.4'},has?'Пароль запрашивается при входе с нового устройства, после кода из чата.':'Дополнительная защита: при входе, кроме кода, нужно будет ввести пароль.'),
  cur,p1,p2,hint,err,
  h('button',{class:'btn',onclick:()=>{if(p1.value!==p2.value){err.textContent='Пароли не совпадают';return;}if(p1.value.length<4){err.textContent='Минимум 4 символа';return;}send(p1.value);}},has?'Сменить пароль':'Установить пароль'),
  has?h('button',{class:'btn red',onclick:()=>send('')},'Отключить пароль'):null,
  h('button',{class:'btn gray',onclick:()=>ov.remove()},'Отмена')));
 ov.onclick=e=>{if(e.target===ov)ov.remove();};document.body.append(ov);}
function aboutDialog(){
 const ov=h('div',{class:'overlay dim'});
 ov.append(h('div',{class:'modal',style:'text-align:center'},h('h3',{},'ℹ️ О приложении'),
  h('div',{class:'sub',style:'line-height:1.5'},'GodGram — свой мессенджер одним файлом.',h('br'),'Без внешних библиотек. База: SQLite.'),
  h('button',{class:'btn gray',onclick:()=>ov.remove()},'Закрыть')));
 ov.onclick=e=>{if(e.target===ov)ov.remove();};document.body.append(ov);}
function showSettings(){
 const ov=h('div',{class:'overlay'});const close=()=>ov.remove();
 const b=me.banner;
 const srow=(ic,t,sub,fn,right)=>h('div',{class:'srow',onclick:fn},h('span',{class:'si'},ic),h('div',{class:'mid'},h('div',{},t),sub?h('div',{class:'sub'},sub):null),right||null);
 const fs=h('input',{type:'range',min:13,max:20,value:localStorage.getItem('fs')||'15',style:'width:130px',
  oninput:e=>{localStorage.setItem('fs',e.target.value);applyPrefs();}});
 const curAc=localStorage.getItem('ac')||'#6b5cff';
 const accs=h('div',{class:'accs'},ACCENTS.map(c=>h('div',{class:'acc1'+(c===curAc?' on':''),style:'background:'+c,onclick:e=>{
  localStorage.setItem('ac',c);applyPrefs();accs.querySelectorAll('.acc1').forEach(x=>x.classList.remove('on'));e.target.classList.add('on');}})));
 const head=h('div',{class:'pbn',style:'background:'+bannerBg(b?b.color:-1)},patternEl(b?b.pattern:''),
  h('div',{class:'pbar2'},h('button',{class:'icon',onclick:close},'←'),h('button',{class:'icon',onclick:()=>showEdit(close)},'✎')),
  av(me,96),h('h1',{},me.name,badge(me)),h('div',{class:'st'},(me.phone?fmtPhone(me.phone):'')+(me.username?' · @'+me.username:'')));
 ov.append(head,h('div',{class:'pw st'},
  h('div',{class:'sgrp'},'Аккаунт'),
  srow('👤','Мой профиль',null,()=>{close();showProfile(me.id);}),
  srow('✏️','Изменить профиль','Имя, фото или GIF, юзернейм, о себе',()=>showEdit(close)),
  srow('👛','Кошелёк',fmtStars(me.balance),showWallet),
  srow('⭐','Чеки на звёзды','Как Send — коды EQ…',()=>{close();showChecks();}),
  srow('🔷','GodFragment','Юзернеймы, номера, коллекции',()=>{close();showFragment();}),
  srow('🛍','Маркет NFT-подарков','Подарки за звёзды',showMarket),
  srow('🤖','Мои боты','Создавай и программируй',showBots),
  srow('💎','Мои NFT',null,()=>{close();myNft();}),
  srow('⭐','GodGram Premium',me.premium?'активен':null,showPremium),
  h('div',{class:'sgrp'},'Конфиденциальность и безопасность'),
  srow('🔒','Облачный пароль',me.has_cloud_pw?'включён':'выключен',()=>cloudPwDialog(()=>{close();showSettings();})),
  srow('📵','Завершить другие сеансы','Выйти на всех других устройствах',async()=>{
   if(!confirm('Завершить все сеансы, кроме этого?'))return;
   try{await api('/api/sessions/terminate_others',{});toast('Другие сеансы завершены');}catch(e){toast(e.message);}}),
  h('div',{class:'sgrp'},'Настройки чатов'),
  srow('🔤','Размер текста',null,()=>{},fs),
  srow('🎨','Цвет интерфейса',null,()=>{}),accs,
  srow('✨','Анимация подарков',null,()=>{},toggle(localStorage.getItem('nomo')!=='1',on=>{localStorage.setItem('nomo',on?'0':'1');applyPrefs();})),
  srow('⏎','Enter отправляет сообщение','Выключено: Enter — новая строка',()=>{},toggle(localStorage.getItem('entsend')!=='0',on=>localStorage.setItem('entsend',on?'1':'0'))),
  h('div',{class:'sgrp'},'Уведомления'),
  srow('🔔','Всплывающие уведомления',null,()=>{},toggle(localStorage.getItem('gmute')!=='1',on=>localStorage.setItem('gmute',on?'0':'1'))),
  srow('👁','Показывать текст в уведомлениях',null,()=>{},toggle(localStorage.getItem('npv')!=='0',on=>localStorage.setItem('npv',on?'1':'0'))),
  h('div',{class:'sgrp'},'Прочее'),
  srow('♻️','Сбросить настройки вида','Размер текста, цвет, анимации',()=>{['fs','ac','nomo','entsend','npv','gmute'].forEach(k=>localStorage.removeItem(k));applyPrefs();close();showSettings();toast('Настройки сброшены');}),
  srow('ℹ️','О приложении',null,aboutDialog),
  me.is_admin?h('div',{class:'sgrp'},'Администратор'):null,
  me.is_admin?srow('🛡','Админ-панель',null,()=>{close();adminPanel();}):null,
  h('div',{class:'sgrp'},' '),
  srow('🚪','Выйти',null,()=>{close();logout(false);})));
 document.body.append(ov);}
applyPrefs();

initAuth();
boot();
</script>
</body>
</html>
"""


def main():
    if "--make-admin" in sys.argv:
        key = sys.argv[sys.argv.index("--make-admin") + 1].lower().lstrip("@")
        digits = re.sub(r"\D", "", key)
        row = None
        if len(digits) >= 7:
            row = q("SELECT id FROM users WHERE phone=? AND deleted=0", (digits,), one=True)
        if not row:
            row = q("SELECT id FROM users WHERE username=? AND deleted=0", (key,), one=True)
        if not row:
            print("Пользователь не найден (укажи номер вида +79991234567 или @username)")
            return
        ex("UPDATE users SET is_admin=1 WHERE id=?", (row["id"],))
        print("Готово: теперь админ")
        return
    srv = None
    for port in range(PORT, PORT + 30):
        try:
            srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
            break
        except OSError:
            print(f"Порт {port} занят, пробую следующий...")
    if srv is None:
        print("Не нашёл свободный порт. Останови старый сервер командой: pkill -f server.py")
        return
    srv.daemon_threads = True
    port = srv.server_address[1]
    print(f"\nСервер запущен: http://localhost:{port}")
    try:
        import socket
        s_ = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s_.connect(("10.255.255.255", 1))
        print(f"Для других устройств в Wi-Fi: http://{s_.getsockname()[0]}:{port}")
    except Exception:
        pass
    print(f"База данных: {DB_PATH}")
    print("Коды входа печатаются здесь, в консоли. Первый зарегистрированный станет админом. Остановка: Ctrl+C\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлено")


if __name__ == "__main__":
    main()
