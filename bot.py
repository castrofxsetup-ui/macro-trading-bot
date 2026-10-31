import os
import sys
import asyncio
import logging
import time
import json
import traceback
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

import aiohttp
from aiohttp import web
import discord
from discord.ext import commands, tasks
from curl_cffi import requests as async_requests
import asyncpg
from aiohttp_socks import ProxyConnector

# ---------------------------------------------------------------------------
# LOGGING SETUP
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("LegacyBot")

# ---------------------------------------------------------------------------
# CONFIGURATION & ENVIRONMENT VARIABLES
# ---------------------------------------------------------------------------
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
PROXY_URL = os.getenv("PROXY_URL")
DATABASE_URL = os.getenv("DATABASE_URL")
PORT = int(os.getenv("PORT", 10000))

# --- Автопереключение способа подключения к Discord -------------------------
# Порядок: платные прокси (PROXY_URL, затем PROXY_URLS через запятую) ->
# прямое подключение -> бесплатные публичные прокси (самый ненадёжный вариант,
# только чтобы бот не лежал совсем). Отключить части можно переменными:
#   USE_DIRECT_FALLBACK=0   — не пробовать подключение напрямую
#   USE_FREE_PROXIES=0      — не пробовать бесплатные прокси
#   FAILOVER_AFTER_SECONDS  — сколько секунд без связи терпеть до переключения (по умолчанию 120)
PROXY_URLS_RAW = os.getenv("PROXY_URLS", "")
PAID_PROXIES = []
for _p in [PROXY_URL or ""] + PROXY_URLS_RAW.replace("\n", ",").split(","):
    _p = _p.strip()
    if _p and _p not in PAID_PROXIES:
        PAID_PROXIES.append(_p)
USE_DIRECT_FALLBACK = os.getenv("USE_DIRECT_FALLBACK", "1") != "0"
USE_FREE_PROXIES = os.getenv("USE_FREE_PROXIES", "1") != "0"
FAILOVER_AFTER_SECONDS = int(os.getenv("FAILOVER_AFTER_SECONDS", "120"))
PRIMARY_RECHECK_SECONDS = 600
FREE_PROXY_SOURCES = [
    ("socks5", "https://api.proxyscrape.com/v2/?request=getproxies&protocol=socks5&timeout=6000&country=all"),
    ("socks5", "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt"),
    ("socks5", "https://raw.githubusercontent.com/hookzof/socks5_list/master/proxy.txt"),
    ("http", "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt"),
]
CONN_STATE = {"down_since": None}

# --- Платный доступ (paywall) ----------------------------------------------
# Ветка/канал, куда бот пишет приветствие-оповещение о платной подписке.
PAYWALL_THREAD_ID = 1566025906340761761
# Ссылка на оплату (необязательно). Если задана — у сообщения появится кнопка «Оплатить».
PAYMENT_URL = ""   # прямых оплат в боте нет: ссылки на оплату владелец отправляет только лично в ЛС
# Как связаться для оплаты, если ссылки нет (например "@username в Telegram").
PAYMENT_CONTACT = "@alchemistcastro"   # Discord владельца сервера
# Кого пингуем, когда человек жмёт «Я оплатил(а)» (ID через запятую). Владелец сервера пингуется всегда.
PAYWALL_ADMIN_IDS = [int(x) for x in os.getenv("PAYWALL_ADMIN_IDS", "").replace(" ", "").split(",") if x.isdigit()]
# Роли (имена через запятую), которые сохраняют доступ ко всем разделам помимо Legacy (модераторы и т.п.).
PAYWALL_EXTRA_ROLES = [x.strip() for x in os.getenv("PAYWALL_EXTRA_ROLES", "Lite Moderator").split(",") if x.strip()]
PAYWALL_PRICES = [("1 месяц", 75, 30), ("3 месяца", 180, 90), ("12 месяцев", 550, 360)]
# Роли без подписки: видят только открытые каналы (как @everyone). Полный доступ — только у Legacy.
RESTRICTED_ROLE_NAMES = ["Trader", "Girl trader", "student by Castro", "macro-trader"]
ANNOUNCE_CHANNEL_ID = 1449056905988149454
# Каналы, которые остаются видимыми для всех без подписки: id -> (название, только чтение)
PAYWALL_OPEN = {
    1566025906340761761: ("подписка / оплата", True),
    1534103088099496058: ("результаты", True),
    1502292137889501235: ("lounge", False),
    1449056905988149454: ("announcement", True),
    1551834834639261766: ("фидбек", False),
    1493200920304160891: ("Open Hall (общие стримы)", False),
    1447282181297672314: ("медиа", False),
}
PAYWALL_STATE = {"enabled": False}
PAYWALL_BACKUP = []          # снимок прав каналов до включения paywall (для отката !paywall off)
PAID_REQUEST_COOLDOWN = {}

TARGET_NEWS_THREAD_ID = 1528319066513604688
TARGET_EVENTS_THREAD_ID = 1528506824687485118
TARGET_STREAMS_CHANNEL_ID = 1528319066513604688

YOUTUBE_CHANNEL_ID = os.getenv("YOUTUBE_CHANNEL_ID", "")

MSK_TZ = timezone(timedelta(hours=3))

COUNTRY_FLAGS = {
    "USD": "🇺🇸", "EUR": "🇪🇺", "GBP": "🇬🇧", "JPY": "🇯🇵",
    "AUD": "🇦🇺", "CAD": "🇨🇦", "CHF": "🇨🇭", "NZD": "🇳🇿",
    "CNY": "🇨🇳", "ALL": "🌐"
}

MONTHS_RU = {
    1: "Января", 2: "Февраля", 3: "Марта", 4: "Апреля", 5: "Мая", 6: "Июня",
    7: "Июля", 8: "Августа", 9: "Сентября", 10: "Октября", 11: "Ноября", 12: "Декабря"
}
DAYS_RU = {
    0: "Пн", 1: "Вт", 2: "Ср", 3: "Чт", 4: "Пт", 5: "Сб", 6: "Вс"
}

if not DISCORD_TOKEN:
    logger.critical("❌ Ошибка: Переменная окружения DISCORD_TOKEN не найдена!")
    sys.exit(1)

if not PROXY_URL:
    logger.warning(
        "⚠️ PROXY_URL не указан. Если хостинг работает из дата-центра (Render/AWS/GCP), "
        "Discord/Cloudflare может блокировать прямые подключения challenge-страницей."
    )

NEWS_CACHE = {"data": [], "last_fetch": 0, "last_attempt": 0}
# После неудачной загрузки новостей не долбим источники каждую минуту
# (цикл алертов вызывает fetch раз в минуту) — иначе faireconomy отвечает 429.
NEWS_FAIL_COOLDOWN_SECONDS = 600
SENT_NEWS_ALERTS = set()
SENT_DISCORD_EVENTS = set()
SENT_YOUTUBE_STREAMS = set()
CACHE_TTL_SECONDS = 1800

LAST_TOP_THREE = []
LAST_OVERTAKE_TIME = 0
GLOBAL_OVERTAKE_COOLDOWN = 180

LAST_OVERTAKE_PAIR = (None, None)
PAIR_OVERTAKE_TIME = 0
PAIR_OVERTAKE_COOLDOWN = 300

bot = None
db_pool = None
USER_XP = {}

# ---------------------------------------------------------------------------
# POSTGRESQL DATABASE HELPERS
# ---------------------------------------------------------------------------
async def init_db():
    global db_pool, USER_XP
    if not DATABASE_URL:
        logger.warning("⚠️ DATABASE_URL не указан. База данных не подключена.")
        return

    try:
        url = DATABASE_URL.replace("postgres://", "postgresql://", 1)
        db_pool = await asyncpg.create_pool(dsn=url)

        async with db_pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS user_xp (
                    user_id BIGINT PRIMARY KEY,
                    xp BIGINT NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS subscriptions (
                    user_id BIGINT PRIMARY KEY,
                    expires_at TEXT NOT NULL,
                    days_total INT NOT NULL,
                    notified_1day BOOLEAN NOT NULL DEFAULT FALSE
                );
                CREATE TABLE IF NOT EXISTS news_cache (
                    id INT PRIMARY KEY,
                    fetched_at DOUBLE PRECISION NOT NULL,
                    data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paywall_backup (
                    channel_id BIGINT NOT NULL,
                    target_id BIGINT NOT NULL,
                    target_type TEXT NOT NULL,
                    allow_bits BIGINT NOT NULL,
                    deny_bits BIGINT NOT NULL,
                    existed BOOLEAN NOT NULL,
                    PRIMARY KEY (channel_id, target_id)
                );
            """)

            val = await conn.fetchval("SELECT value FROM settings WHERE key = 'paywall_enabled';")
            PAYWALL_STATE["enabled"] = (val == "1")
            brows = await conn.fetch("SELECT * FROM paywall_backup;")
            PAYWALL_BACKUP.clear()
            PAYWALL_BACKUP.extend(dict(r) for r in brows)

            rows = await conn.fetch("SELECT user_id, xp FROM user_xp;")
            USER_XP = {row["user_id"]: row["xp"] for row in rows}

        logger.info("✅ Успешное подключение к PostgreSQL БД и загрузка данных!")
    except Exception as e:
        logger.error(f"❌ Ошибка подключения к базе данных: {e}")
        return

    await db_load_news_cache()

# --- Кэш новостей в БД: переживает перезапуски/деплои, чтобы бот не долбил
# --- источники (faireconomy отвечает 429 при частых запросах с одного IP).
def _news_to_json(events: list) -> str:
    return json.dumps([
        {**ev, "dt_utc": ev["dt_utc"].isoformat() if ev.get("dt_utc") else None}
        for ev in events
    ])

def _news_from_json(text: str) -> list:
    events = []
    for ev in json.loads(text):
        raw = ev.get("dt_utc")
        ev["dt_utc"] = datetime.fromisoformat(raw) if raw else None
        events.append(ev)
    return events

async def db_save_news_cache(events: list, fetched_at: float):
    if not db_pool:
        return
    try:
        async with db_pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO news_cache (id, fetched_at, data) VALUES (1, $1, $2)
                ON CONFLICT (id) DO UPDATE SET fetched_at = $1, data = $2;
            """, fetched_at, _news_to_json(events))
    except Exception as e:
        logger.error(f"[DB NEWS CACHE SAVE ERROR] {e}")

async def db_load_news_cache():
    if not db_pool:
        return
    try:
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT fetched_at, data FROM news_cache WHERE id = 1;")
        if row:
            NEWS_CACHE["data"] = _news_from_json(row["data"])
            NEWS_CACHE["last_fetch"] = row["fetched_at"]
            NEWS_CACHE["last_attempt"] = row["fetched_at"]
            age_min = int((time.time() - row["fetched_at"]) / 60)
            logger.info(f"📰 Кэш новостей загружен из БД ({len(NEWS_CACHE['data'])} событий, возраст {age_min} мин).")
    except Exception as e:
        logger.error(f"[DB NEWS CACHE LOAD ERROR] {e}")

async def add_user_xp(user_id: int, amount: int = 5):
    global USER_XP
    USER_XP[user_id] = USER_XP.get(user_id, 0) + amount
    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO user_xp (user_id, xp) VALUES ($1, $2)
                    ON CONFLICT (user_id) DO UPDATE SET xp = $2;
                """, user_id, USER_XP[user_id])
        except Exception as e:
            logger.error(f"[DB XP SAVE ERROR] {e}")

async def db_load_subs() -> dict:
    if not db_pool:
        return {}
    try:
        async with db_pool.acquire() as conn:
            rows = await conn.fetch("SELECT user_id, expires_at, days_total, notified_1day FROM subscriptions;")
            return {
                str(row["user_id"]): {
                    "expires_at": row["expires_at"],
                    "days_total": row["days_total"],
                    "notified_1day": row["notified_1day"]
                }
                for row in rows
            }
    except Exception as e:
        logger.error(f"[DB SUBS LOAD ERROR] {e}")
        return {}

async def db_save_sub(user_id: int, expires_at: str, days_total: int, notified: bool):
    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO subscriptions (user_id, expires_at, days_total, notified_1day)
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (user_id) DO UPDATE SET
                        expires_at = $2, days_total = $3, notified_1day = $4;
                """, user_id, expires_at, days_total, notified)
        except Exception as e:
            logger.error(f"[DB SUB SAVE ERROR] {e}")

async def db_delete_sub(user_id: int):
    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                await conn.execute("DELETE FROM subscriptions WHERE user_id = $1;", user_id)
        except Exception as e:
            logger.error(f"[DB SUB DELETE ERROR] {e}")

# ---------------------------------------------------------------------------
# PROXY DIAGNOSTIC TEST (чтобы отличить "прокси не работает" от "discord.py
# не может подружиться с этим коннектором")
# ---------------------------------------------------------------------------
async def test_proxy_connector(connector) -> bool:
    if connector is None:
        return False
    try:
        timeout = aiohttp.ClientTimeout(total=12)
        # connector_owner=False — ВАЖНО: иначе aiohttp закроет коннектор
        # вместе с этой тестовой сессией, и discord.py получит уже мёртвый коннектор.
        async with aiohttp.ClientSession(connector=connector, connector_owner=False, timeout=timeout) as session:
            async with session.get("https://discord.com/api/v10/gateway") as resp:
                body = (await resp.text())[:200]
                if resp.status == 200:
                    logger.info(f"🔎 Тест прокси ОК: discord.com ответил {resp.status} через прокси.")
                    return True
                else:
                    logger.error(f"🔎 Тест прокси: discord.com ответил статусом {resp.status}, тело: {body!r}")
                    return False
    except Exception as e:
        logger.error(
            f"🔎 Тест прокси (aiohttp_socks, 'голый' Python TLS) ПРОВАЛЕН [{type(e).__name__}]: "
            f"{e or '(пустое сообщение об ошибке)'}\n"
            f"{traceback.format_exc()}"
        )
        return False

async def test_proxy_curl_cffi() -> bool:
    """
    Тот же самый прокси, но через curl_cffi с impersonate='chrome120' —
    то есть с TLS-отпечатком (JA3) настоящего Chrome, а не 'голого' Python.
    Если ЭТОТ тест проходит, а test_proxy_connector() выше — нет, значит
    проблема не в прокси как таковом, а в том, что Discord/Cloudflare
    режет хендшейк именно по TLS-отпечатку стандартного aiohttp/ssl.
    Если падают оба — дело в самом прокси (или его политике по Discord).
    """
    if not PROXY_URL:
        return False
    try:
        proxies = {"http": PROXY_URL, "https": PROXY_URL}
        r = await asyncio.to_thread(
            async_requests.get,
            "https://discord.com/api/v10/gateway",
            impersonate="chrome120",
            timeout=12,
            proxies=proxies,
        )
        if r.status_code == 200:
            logger.info(f"🔎 Тест прокси (curl_cffi, TLS-отпечаток Chrome) ОК: статус {r.status_code}.")
            return True
        else:
            logger.error(f"🔎 Тест прокси (curl_cffi, TLS-отпечаток Chrome): статус {r.status_code}, тело: {r.text[:200]!r}")
            return False
    except Exception as e:
        logger.error(
            f"🔎 Тест прокси (curl_cffi, TLS-отпечаток Chrome) ПРОВАЛЕН [{type(e).__name__}]: "
            f"{e or '(пустое сообщение об ошибке)'}\n"
            f"{traceback.format_exc()}"
        )
        return False

# ---------------------------------------------------------------------------
# PAYWALL: платный доступ к серверу (оповещение в ветке, кнопки, закрытие каналов)
# ---------------------------------------------------------------------------
async def db_set_setting(key: str, value: str):
    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO settings (key, value) VALUES ($1, $2) "
                    "ON CONFLICT (key) DO UPDATE SET value = $2;", key, value)
        except Exception as e:
            logger.error(f"[DB SETTING ERROR] {e}")

async def db_replace_paywall_backup(rows: list):
    """Полностью заменяет сохранённый снимок прав (rows=[] — очистить)."""
    PAYWALL_BACKUP.clear()
    PAYWALL_BACKUP.extend(rows)
    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute("DELETE FROM paywall_backup;")
                    if rows:
                        await conn.executemany(
                            "INSERT INTO paywall_backup (channel_id, target_id, target_type, allow_bits, deny_bits, existed) "
                            "VALUES ($1, $2, $3, $4, $5, $6) ON CONFLICT (channel_id, target_id) DO NOTHING;",
                            [(r["channel_id"], r["target_id"], r["target_type"], r["allow_bits"], r["deny_bits"], r["existed"]) for r in rows])
        except Exception as e:
            logger.error(f"[DB PAYWALL BACKUP ERROR] {e}")

def build_paywall_embed(member=None, guild=None) -> discord.Embed:
    owner_id = (guild.owner_id if guild else None)
    owner = f"<@{owner_id}> (**{PAYMENT_CONTACT}**)" if owner_id else f"**{PAYMENT_CONTACT}**"
    lines = []
    if member is not None:
        lines.append(f"{member.mention}, ты попал(а) на сервер **Discord-комьюнити по трейдингу The Legacy**.")
    else:
        lines.append("Добро пожаловать на сервер **Discord-комьюнити по трейдингу The Legacy**.")
    lines.append("")
    lines.append("Чтобы получить доступ к **полноценному функционалу сервера**, нужно оформить подписку **Legacy**. "
                 "До этого остальные разделы закрыты.")
    embed = discord.Embed(title="🔒 Доступ к The Legacy — по подписке", description="\n".join(lines),
                          color=discord.Color.from_rgb(214, 178, 116))
    base_month = PAYWALL_PRICES[0][1]
    price_lines = []
    for title, price, days in PAYWALL_PRICES:
        months = max(1, round(days / 30))
        saving = base_month * months - price
        extra = f"  ·  выгода **{saving}$**" if saving > 0 else ""
        price_lines.append(f"• **{title}** — **{price}$**{extra}")
    embed.add_field(name="💎 Тарифы", value="\n".join(price_lines), inline=False)
    embed.add_field(name="Как получить доступ", value=(
        f"1️⃣ Свяжись с админом сервера: {owner}\n"
        "2️⃣ Выбери срок — ссылку на оплату админ отправит **лично в личных сообщениях**\n"
        "3️⃣ После оплаты нажми **«Я оплатил(а)»** — админ выдаст роль Legacy и откроет все разделы"), inline=False)
    embed.add_field(name="⚠️ Важно", value=(
        "В боте и на сервере **нет прямых оплат**. Ссылки на оплату выдаёт только владелец сервера в личных сообщениях. "
        "По всем вопросам пиши **лично ему** — не доверяй никому другому."), inline=False)
    embed.set_footer(text="Подписка на 30 / 90 / 360 дней · с ролью Legacy открывается доступ ко всему Discord-серверу")
    return embed

class PaywallView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        if PAYMENT_URL:
            self.add_item(discord.ui.Button(label="💳 Оплатить подписку", style=discord.ButtonStyle.link, url=PAYMENT_URL))

    @discord.ui.button(label="✅ Я оплатил(а)", style=discord.ButtonStyle.success, custom_id="legacy_paywall_paid")
    async def paid_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        user = interaction.user
        now = time.time()
        if now - PAID_REQUEST_COOLDOWN.get(user.id, 0) < 600:
            await interaction.response.send_message(
                "⏳ Заявка уже отправлена — администратор скоро проверит оплату.", ephemeral=True)
            return
        PAID_REQUEST_COOLDOWN[user.id] = now
        await interaction.response.send_message(
            "✅ Спасибо! Админ проверит оплату и откроет доступ. Если вопросы — пиши лично @alchemistcastro.",
            ephemeral=True)
        try:
            ids = []
            if interaction.guild and interaction.guild.owner_id:
                ids.append(interaction.guild.owner_id)
            for i in PAYWALL_ADMIN_IDS:
                if i not in ids:
                    ids.append(i)
            mentions = " ".join(f"<@{i}>" for i in ids)
            mod = discord.utils.get(interaction.guild.roles, name="Lite Moderator") if interaction.guild else None
            if mod:
                mentions += f" {mod.mention}"
            await interaction.channel.send(
                f"{mentions}\n💰 {user.mention} сообщает об оплате. Проверьте платёж и выдайте доступ: "
                f"`!sub {user.id} 30` / `90` / `360`")
        except Exception as e:
            logger.error(f"[PAYWALL PAID BUTTON] {e}")

async def send_paywall_notice(client_bot, member=None, attempts=(0, 5, 20)) -> bool:
    """Пишет оповещение о платной подписке в ветку PAYWALL_THREAD_ID (с повторами — прокси иногда рвёт запросы)."""
    last_err = None
    for delay in attempts:
        if delay:
            await asyncio.sleep(delay)
        try:
            ch = await get_target_channel(PAYWALL_THREAD_ID, client_bot)
            if ch is None:
                last_err = "ветка не найдена"
                continue
            if isinstance(ch, discord.Thread) and ch.archived:
                await ch.edit(archived=False)
            await ch.send(
                content=member.mention if member else None,
                embed=build_paywall_embed(member, getattr(ch, "guild", None)),
                view=PaywallView(),
            )
            return True
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            logger.warning(f"[PAYWALL NOTICE] попытка не удалась: {last_err}")
    logger.error(f"[PAYWALL NOTICE] не удалось отправить оповещение: {last_err}")
    return False

async def paywall_resolve(guild, client_bot) -> list:
    """Находит открытые каналы. Если ID — ветка (thread), права задаются на её родительском канале."""
    out = []
    for cid, (label, readonly) in PAYWALL_OPEN.items():
        ch = await get_target_channel(cid, client_bot)
        item = {"id": cid, "label": label, "readonly": readonly, "obj": ch, "target": None, "is_thread": False}
        if ch is not None:
            if isinstance(ch, discord.Thread):
                item["is_thread"] = True
                item["target"] = ch.parent
            else:
                item["target"] = ch
        out.append(item)
    return out

def _expand_open(items: list) -> dict:
    """id канала -> описание. Если открыта категория, открываются и все её каналы."""
    out = {}
    for it in items:
        t = it["target"]
        if t is None:
            continue
        out[t.id] = it
        if isinstance(t, discord.CategoryChannel):
            for child in t.channels:
                out.setdefault(child.id, it)
    return out

def _copy_overwrite(ows: dict, target) -> discord.PermissionOverwrite:
    if target in ows:
        return discord.PermissionOverwrite.from_pair(*ows[target].pair())
    return discord.PermissionOverwrite()

def _paywall_restricted_roles(guild) -> tuple:
    """(найденные роли, не найденные имена) — поиск без учёта регистра."""
    found, missing = [], []
    for name in RESTRICTED_ROLE_NAMES:
        r = next((x for x in guild.roles if x.name.lower() == name.lower()), None)
        if r:
            found.append(r)
        else:
            missing.append(name)
    return found, missing

def _paywall_keep_roles(guild) -> list:
    roles = []
    legacy = discord.utils.get(guild.roles, name="Legacy")
    if legacy:
        roles.append(legacy)
    for name in PAYWALL_EXTRA_ROLES:
        r = discord.utils.get(guild.roles, name=name)
        if r and r not in roles:
            roles.append(r)
    return roles

async def paywall_plan(guild, client_bot) -> dict:
    keep_roles = _paywall_keep_roles(guild)
    legacy = discord.utils.get(guild.roles, name="Legacy")
    restricted, missing_roles = _paywall_restricted_roles(guild)
    open_items = await paywall_resolve(guild, client_bot)
    open_targets = set(_expand_open(open_items).keys())
    explicit = {}
    for ch in guild.channels:
        if ch.id in open_targets:
            continue
        for target, ow in ch.overwrites.items():
            if (isinstance(target, discord.Role) and target != guild.default_role
                    and target not in keep_roles and target not in restricted
                    and not target.managed and ow.view_channel is True):
                explicit[target] = explicit.get(target, 0) + 1
    without = 0
    if legacy:
        for m in guild.members:
            if not m.bot and legacy not in m.roles and not m.guild_permissions.administrator:
                without += 1
    admin_roles = [r for r in guild.roles if r.permissions.administrator and not r.managed
                   and r != guild.default_role and r not in keep_roles]
    return {"legacy": legacy, "restricted": restricted, "missing_roles": missing_roles,
            "admin_roles": admin_roles, "open": open_items, "keep_roles": keep_roles,
            "explicit": explicit, "without": without, "channels": len(guild.channels)}

async def paywall_apply(guild, client_bot, strict: bool = True) -> dict:
    plan = await paywall_plan(guild, client_bot)
    if not plan["legacy"]:
        return {"ok": False, "error": "Роль **Legacy** не найдена на сервере."}
    missing = [it["label"] + f" ({it['id']})" for it in plan["open"] if it["target"] is None]
    if missing:
        return {"ok": False, "error": "Не найдены открытые каналы: " + ", ".join(missing) + ". Проверь ID и доступ бота."}
    open_map = _expand_open(plan["open"])
    restricted = plan["restricted"]
    me = guild.me
    everyone = guild.default_role
    all_backup, failed, done = [], [], 0

    for ch in guild.channels:
        ows = dict(ch.overwrites)
        snaps = []

        def snap(target):
            existed = target in ows
            if existed:
                allow, deny = (p.value for p in ows[target].pair())
            else:
                allow = deny = 0
            snaps.append({"channel_id": ch.id, "target_id": target.id,
                          "target_type": "role" if isinstance(target, discord.Role) else "member",
                          "allow_bits": allow, "deny_bits": deny, "existed": existed})

        item = open_map.get(ch.id)

        snap(everyone)
        ow = _copy_overwrite(ows, everyone)
        if item:
            ow.view_channel = True
            if item["readonly"]:
                ow.send_messages = False
                ow.send_messages_in_threads = False
                ow.create_public_threads = False
                ow.create_private_threads = False
                ow.add_reactions = False
        else:
            ow.view_channel = False
        ows[everyone] = ow

        # Роли без подписки (Trader, Girl trader, student by Castro, macro-trader): явно видят только открытые каналы.
        # Подписчики с Legacy всё равно видят всё: allow роли Legacy сильнее deny этих ролей.
        for r in restricted:
            snap(r)
            o = _copy_overwrite(ows, r)
            o.view_channel = True if item else False
            ows[r] = o

        for r in plan["keep_roles"]:
            snap(r)
            o = _copy_overwrite(ows, r)
            o.view_channel = True
            ows[r] = o

        if me is not None:
            snap(me)
            o = _copy_overwrite(ows, me)
            o.view_channel = True
            ows[me] = o

        if strict and not item:
            for t in list(ows.keys()):
                if t == everyone or t == me or t in restricted or t in plan["keep_roles"]:
                    continue
                if isinstance(t, discord.Role) and t.managed:
                    continue
                if ows[t].view_channel is True:
                    snap(t)
                    o = _copy_overwrite(ows, t)
                    o.view_channel = None
                    ows[t] = o

        try:
            await ch.edit(overwrites=ows, reason="Paywall: доступ только по подписке Legacy")
            all_backup.extend(snaps)
            done += 1
        except Exception as e:
            failed.append(f"{ch.name} ({type(e).__name__})")
            logger.error(f"[PAYWALL] не удалось изменить права канала {ch.name}: {e}")
        await asyncio.sleep(0.4)

    await db_replace_paywall_backup(all_backup)
    PAYWALL_STATE["enabled"] = True
    await db_set_setting("paywall_enabled", "1")
    return {"ok": True, "done": done, "failed": failed, "total": len(guild.channels)}

def build_closed_announcement_embed() -> discord.Embed:
    embed = discord.Embed(
        title="🔒 Бесплатный формат сервера закрыт",
        description=(
            "Discord-сервер **The Legacy** в бесплатном формате закрыт.\n\n"
            f"Чтобы продолжить пользоваться **всеми разделами и ветками** сервера, перейди в <#{PAYWALL_THREAD_ID}> "
            "и оформи или продли подписку **Legacy**."),
        color=discord.Color.from_rgb(214, 178, 116))
    embed.add_field(name="💎 Тарифы", value="\n".join(
        f"• **{t}** — **{p}$**" for t, p, _ in PAYWALL_PRICES), inline=False)
    embed.add_field(name="✅ Остаётся открытым", value=(
        f"• <#1534103088099496058> — результаты\n• <#1502292137889501235> — общий чат\n"
        f"• <#1551834834639261766> — фидбек\n• <#1493200920304160891> — Open Hall (общие стримы)\n• <#1447282181297672314> — медиа\n"
        f"• <#{ANNOUNCE_CHANNEL_ID}> — анонсы и важная информация"), inline=False)
    embed.add_field(name="Как оформить подписку", value=(
        f"Свяжись лично с админом сервера: **{PAYMENT_CONTACT}**. Ссылки на оплату он отправляет только в личных сообщениях — "
        "прямых оплат на сервере и в боте нет."), inline=False)
    embed.set_footer(text="С ролью Legacy открывается доступ ко всему Discord-серверу")
    return embed

async def send_closed_announcement(client_bot, ping_everyone: bool = True) -> bool:
    try:
        ch = await get_target_channel(ANNOUNCE_CHANNEL_ID, client_bot)
        if ch is None:
            return False
        if isinstance(ch, discord.Thread) and ch.archived:
            await ch.edit(archived=False)
        content = None
        if ping_everyone:
            content = "@everyone"
            trader = discord.utils.get(ch.guild.roles, name="Trader")
            if trader:
                content += f" {trader.mention}"
        await ch.send(
            content=content,
            embed=build_closed_announcement_embed(),
            allowed_mentions=discord.AllowedMentions(everyone=ping_everyone, roles=ping_everyone),
        )
        return True
    except Exception as e:
        logger.error(f"[PAYWALL ANNOUNCE] {type(e).__name__}: {e}")
        return False

async def paywall_restore(guild) -> dict:
    by_channel = {}
    for r in PAYWALL_BACKUP:
        by_channel.setdefault(r["channel_id"], []).append(r)
    done, failed = 0, []
    for ch_id, rows in by_channel.items():
        ch = guild.get_channel(ch_id)
        if ch is None:
            continue
        ows = dict(ch.overwrites)
        for r in rows:
            target = guild.get_role(r["target_id"]) if r["target_type"] == "role" else guild.get_member(r["target_id"])
            if target is None:
                continue
            if r["existed"]:
                ows[target] = discord.PermissionOverwrite.from_pair(
                    discord.Permissions(r["allow_bits"]), discord.Permissions(r["deny_bits"]))
            else:
                ows.pop(target, None)
        try:
            await ch.edit(overwrites=ows, reason="Paywall отключён: возврат прав")
            done += 1
        except Exception as e:
            failed.append(f"{ch.name} ({type(e).__name__})")
            logger.error(f"[PAYWALL RESTORE] {ch.name}: {e}")
        await asyncio.sleep(0.4)
    await db_replace_paywall_backup([])
    PAYWALL_STATE["enabled"] = False
    await db_set_setting("paywall_enabled", "0")
    return {"done": done, "failed": failed}

# ---------------------------------------------------------------------------
# ТОП АКТИВНОСТИ: кликабельные участники, страницы и выбор человека
# ---------------------------------------------------------------------------
XP_PER_MESSAGE = 5
LEADERBOARD_PAGE_SIZE = 10

def get_ranking(guild) -> list:
    """[(user_id, xp)] по убыванию XP. Вышедших с сервера скрываем (если список участников загружен)."""
    items = sorted(USER_XP.items(), key=lambda it: it[1], reverse=True)
    if guild.chunked:
        items = [(uid, xp) for uid, xp in items if guild.get_member(uid) is not None]
    return items

def build_user_rank_embed(guild, user_id: int) -> discord.Embed:
    ranking = get_ranking(guild)
    xp = USER_XP.get(user_id, 0)
    place = next((i for i, (uid, _) in enumerate(ranking, start=1) if uid == user_id), None)
    member = guild.get_member(user_id)
    embed = discord.Embed(title="📊 Активность участника", color=discord.Color.gold())
    embed.description = f"Участник: <@{user_id}>"
    embed.add_field(name="🏅 Место", value=(f"**{place}** из {len(ranking)}" if place else "—"), inline=True)
    embed.add_field(name="⚡️ Опыт", value=f"**{xp} XP**", inline=True)
    embed.add_field(name="💬 Сообщений", value=f"≈ **{xp // XP_PER_MESSAGE}**", inline=True)
    if place and place > 1:
        above_xp = ranking[place - 2][1]
        embed.add_field(name="⬆️ До места выше", value=f"{above_xp - xp + 1} XP (~{max(1, (above_xp - xp) // XP_PER_MESSAGE + 1)} сообщ.)", inline=False)
    elif place == 1:
        embed.add_field(name="👑", value="Лидер рейтинга!", inline=False)
    if member is not None:
        embed.set_thumbnail(url=member.display_avatar.url)
    return embed

class LeaderboardSelect(discord.ui.Select):
    def __init__(self, view_ref):
        super().__init__(placeholder="👤 Выбери участника — посмотреть его статистику", min_values=1, max_values=1,
                         options=[discord.SelectOption(label="…", value="0")])
        self.view_ref = view_ref

    async def callback(self, interaction: discord.Interaction):
        try:
            uid = int(self.values[0])
            await interaction.response.send_message(
                embed=build_user_rank_embed(self.view_ref.guild, uid), ephemeral=True)
        except Exception as e:
            logger.error(f"[LEADERBOARD SELECT] {e}")

class LeaderboardView(discord.ui.View):
    def __init__(self, guild, ranking, author_id):
        super().__init__(timeout=600)
        self.guild = guild
        self.ranking = ranking
        self.author_id = author_id
        self.page = 0
        self.pages = max(1, (len(ranking) + LEADERBOARD_PAGE_SIZE - 1) // LEADERBOARD_PAGE_SIZE)
        self.select = LeaderboardSelect(self)
        self.add_item(self.select)
        self._sync()

    def _slice(self):
        start = self.page * LEADERBOARD_PAGE_SIZE
        return start, self.ranking[start:start + LEADERBOARD_PAGE_SIZE]

    def _sync(self):
        start, chunk = self._slice()
        opts = []
        for offset, (uid, xp) in enumerate(chunk):
            m = self.guild.get_member(uid)
            name = (m.display_name if m else f"Участник {uid}")[:70]
            opts.append(discord.SelectOption(label=f"{start + offset + 1}. {name}", value=str(uid),
                                             description=f"{xp} XP · ≈{xp // XP_PER_MESSAGE} сообщ."))
        self.select.options = opts or [discord.SelectOption(label="Пусто", value="0")]
        self.prev_btn.disabled = self.page <= 0
        self.next_btn.disabled = self.page >= self.pages - 1
        self.page_btn.label = f"{self.page + 1}/{self.pages}"

    def make_embed(self) -> discord.Embed:
        start, chunk = self._slice()
        medals = ["🥇", "🥈", "🥉"]
        lines = []
        for offset, (uid, xp) in enumerate(chunk):
            idx = start + offset + 1
            prefix = medals[idx - 1] if idx <= 3 else f"**{idx}.**"
            lines.append(f"{prefix} <@{uid}> — **{xp} XP** · ≈{xp // XP_PER_MESSAGE} сообщ.")
        embed = discord.Embed(title="🏆 Рейтинг активности участников", description="\n".join(lines),
                              color=discord.Color.gold())
        embed.set_footer(text=f"Страница {self.page + 1}/{self.pages} · участников: {len(self.ranking)} · нажми на имя или выбери в списке")
        return embed

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary, row=1)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = max(0, self.page - 1)
        self._sync()
        await interaction.response.edit_message(embed=self.make_embed(), view=self)

    @discord.ui.button(label="1/1", style=discord.ButtonStyle.secondary, disabled=True, row=1)
    async def page_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary, row=1)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = min(self.pages - 1, self.page + 1)
        self._sync()
        await interaction.response.edit_message(embed=self.make_embed(), view=self)

# ---------------------------------------------------------------------------
# FACTORY FUNCTION TO CREATE BOT INSTANCE
# ---------------------------------------------------------------------------
#
# ВАЖНО (фикс нестабильного подключения):
# Раньше здесь был класс ProxyClient с методом __http_client_factory__,
# который ДОЛЖЕН был подменять aiohttp-сессию бота на сессию с прокси.
# Проблема: discord.py не вызывает такой метод — это не часть его API,
# метод никогда не вызывался, и бот реально всегда стучался в Discord
# напрямую, без прокси. Из-за этого Cloudflare видел IP дата-центра
# (Render) и отдавал challenge-страницу вместо ответа API — это и были
# бесконечные "Ошибка соединения... Переподключение через 15 секунд".
#
# Правильный и официально поддерживаемый discord.py способ — передать
# aiohttp-коннектор (в т.ч. SOCKS5 через aiohttp_socks) напрямую в
# конструктор commands.Bot(..., connector=connector). Он применяется
# и к обычным REST-запросам, и к Gateway WebSocket-соединению.
# ---------------------------------------------------------------------------
def create_bot(connector=None):
    intents = discord.Intents.default()
    intents.message_content = True
    intents.guild_scheduled_events = True
    intents.members = True

    new_bot = commands.Bot(
        command_prefix="!",
        intents=intents,
        connector=connector,
    )

    @new_bot.event
    async def on_disconnect():
        # Запоминаем момент потери связи — по нему сторож в main() решает,
        # пора ли переключаться на другой прокси.
        if CONN_STATE["down_since"] is None:
            CONN_STATE["down_since"] = time.time()

    @new_bot.event
    async def on_resumed():
        CONN_STATE["down_since"] = None

    @new_bot.event
    async def on_ready():
        CONN_STATE["down_since"] = None
        logger.info(f"✅ Успешный вход: {new_bot.user} (ID: {new_bot.user.id})")
        if not getattr(new_bot, "_paywall_view_added", False):
            new_bot.add_view(PaywallView())   # чтобы кнопка «Я оплатил(а)» работала и после перезапуска
            new_bot._paywall_view_added = True
        try:
            # Без force_refresh: если в БД свежий кэш новостей — берём его и
            # не дёргаем источники при каждом перезапуске бота.
            await fetch_economic_news()

            if not check_30min_news_alerts.is_running():
                check_30min_news_alerts.start()
            if not check_discord_events_alerts.is_running():
                check_discord_events_alerts.start()
            if not scheduled_news_digests.is_running():
                scheduled_news_digests.start()
            if not check_subscriptions_daily_loop.is_running():
                check_subscriptions_daily_loop.start()
            if YOUTUBE_CHANNEL_ID and not check_youtube_streams_loop.is_running():
                check_youtube_streams_loop.start()
        except Exception as e:
            logger.error(f"[ON_READY] {e}")

    @new_bot.event
    async def on_member_join(member: discord.Member):
        if member.bot:
            return
        # Платный режим: Trader выдаётся как обычно, плюс оповещение с тарифами в канал оплаты.
        if PAYWALL_STATE["enabled"]:
            # Если у человека ещё действует подписка (вышел и вернулся) — просто возвращаем роль Legacy.
            try:
                subs = await db_load_subs()
                data = subs.get(str(member.id))
                if data and datetime.fromisoformat(data["expires_at"]) > datetime.now(timezone.utc):
                    legacy_role = discord.utils.get(member.guild.roles, name="Legacy")
                    if legacy_role:
                        await member.add_roles(legacy_role, reason="Активная подписка при повторном входе")
                        logger.info(f"[PAYWALL] подписка активна, Legacy возвращена: {member} ({member.id})")
                        return
            except Exception as e:
                logger.error(f"[PAYWALL REJOIN] {type(e).__name__}: {e}")
            # Новичок получает Trader как обычно, а оповещение о подписке уходит в фоне.
            asyncio.create_task(send_paywall_notice(new_bot, member))
        role = discord.utils.get(member.guild.roles, name="Trader")
        if not role:
            logger.error(f"[AUTO-ROLE] Роль 'Trader' не найдена на сервере — {member} ({member.id}) остался без роли. Проверь точное название роли.")
            return

        # Через SOCKS5-прокси REST-запросы изредка рвутся (Connection reset /
        # timeout) — поэтому несколько попыток с нарастающей паузой.
        delays = [0, 3, 10, 30, 90]
        last_err = None
        for attempt, delay in enumerate(delays, start=1):
            if delay:
                await asyncio.sleep(delay)
            try:
                if role in member.roles:
                    return
                await member.add_roles(role, reason="Авто-роль при входе")
                logger.info(f"[AUTO-ROLE] ✅ Роль Trader выдана: {member} ({member.id}), попытка {attempt}")
                return
            except discord.Forbidden as e:
                logger.error(f"[AUTO-ROLE] Нет прав выдать роль Trader ({e}). Подними роль бота выше Trader и включи 'Управление ролями'.")
                return
            except Exception as e:
                last_err = e
                logger.warning(f"[AUTO-ROLE] попытка {attempt}/{len(delays)} для {member} не удалась: {type(e).__name__}: {e}")
        logger.error(f"[AUTO-ROLE] ❌ Не удалось выдать Trader {member} ({member.id}) после {len(delays)} попыток: {last_err}")

    @new_bot.event
    async def on_message(message: discord.Message):
        if message.author.bot or not message.guild:
            return
        try:
            await _handle_activity(message)
        except Exception as e:
            logger.error(f"[ON_MESSAGE] {e}")
        # Команды обрабатываем всегда, даже если подсчёт активности дал ошибку
        await new_bot.process_commands(message)

    async def _handle_activity(message: discord.Message):
        global LAST_TOP_THREE, LAST_OVERTAKE_TIME, LAST_OVERTAKE_PAIR, PAIR_OVERTAKE_TIME
        if True:

            author_id = message.author.id
            await add_user_xp(author_id, 5)

            sorted_users = sorted(USER_XP.items(), key=lambda item: item[1], reverse=True)
            current_top_three = [u_id for u_id, _ in sorted_users[:3]]

            if LAST_TOP_THREE and current_top_three != LAST_TOP_THREE:
                now = time.time()
                if now - LAST_OVERTAKE_TIME >= GLOBAL_OVERTAKE_COOLDOWN:
                    overtaker_id = author_id
                    if overtaker_id in current_top_three:
                        new_pos = current_top_three.index(overtaker_id) + 1

                        overtaken_id = None
                        if overtaker_id in LAST_TOP_THREE:
                            old_pos = LAST_TOP_THREE.index(overtaker_id)
                            if new_pos - 1 < old_pos:
                                overtaken_id = LAST_TOP_THREE[new_pos - 1]
                        else:
                            overtaken_id = LAST_TOP_THREE[-1]

                        if overtaken_id and overtaken_id != overtaker_id:
                            current_pair = (overtaker_id, overtaken_id)

                            if current_pair != LAST_OVERTAKE_PAIR or (now - PAIR_OVERTAKE_TIME >= PAIR_OVERTAKE_COOLDOWN):
                                overtaker_member = message.guild.get_member(overtaker_id)
                                overtaken_member = message.guild.get_member(overtaken_id)

                                overtaker_name = overtaker_member.mention if overtaker_member else f"<@{overtaker_id}>"
                                overtaken_name = overtaken_member.mention if overtaken_member else f"<@{overtaken_id}>"

                                medals = {1: "🥇 1-е место", 2: "🥈 2-е место", 3: "🥉 3-е место"}
                                pos_str = medals.get(new_pos, f"**{new_pos}-е место**")

                                embed = discord.Embed(
                                    title="⚡️ Смена лидера в Топе Активности!",
                                    description=(
                                        f"🔥 {overtaker_name} вырывается вперед и забирает {pos_str}, обгоняя {overtaken_name}!\n\n"
                                        f"👀 {overtaken_name}, самое время поднапрячься и вернуть свою позицию!"
                                    ),
                                    color=discord.Color.gold()
                                )
                                embed.set_footer(text="Рейтинг рассчитывается по сообщениям на сервере")

                                await message.channel.send(embed=embed)

                                LAST_OVERTAKE_TIME = now
                                LAST_OVERTAKE_PAIR = current_pair
                                PAIR_OVERTAKE_TIME = now

            LAST_TOP_THREE = current_top_three

    @new_bot.event
    async def on_command_error(ctx, error):
        if isinstance(error, commands.CommandNotFound):
            return
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("🚫 Эта команда доступна только администратору.")
        elif isinstance(error, (commands.MemberNotFound, commands.BadArgument)):
            await ctx.send("❌ Неверные данные. Укажи участника (упоминание или ID) и срок, например: `!sub @user 30`")
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(f"❌ Не хватает параметра: `{error.param.name}`. Смотри `!sub`, `!paywall` в списке команд.")
        else:
            logger.error(f"[COMMAND ERROR] {ctx.command}: {type(error).__name__}: {error}")

    @new_bot.command(name="sub", aliases=["подписка"])
    @commands.has_permissions(administrator=True)
    async def cmd_sub(ctx, member: discord.Member, days: int):
        try:
            if days not in [30, 90, 360]:
                await ctx.send("❌ Доступные сроки подписки: **30**, **90** или **360** дней.")
                return

            role = discord.utils.get(ctx.guild.roles, name="Legacy")
            if not role:
                await ctx.send("❌ Роль с именем **Legacy** не найдена на сервере!")
                return

            # ВАЖНО (фикс "выдаёт 60 дней вместо 30"):
            # Раньше, если у участника уже была активная подписка, бот ПРИБАВЛЯЛ
            # новые дни к оставшемуся сроку (30 старых + 30 новых = 60), из-за
            # чего команда "!sub 30" могла визуально "выдать 60 дней" и сбивать
            # с толку — выбранный срок (30/90/360) переставал совпадать с тем,
            # что реально записывалось и показывалось. Теперь "!sub <дней>"
            # всегда означает ровно столько дней от текущего момента —
            # без скрытого накопления поверх старой подписки.
            now = datetime.now(timezone.utc)
            new_end = now + timedelta(days=days)

            await db_save_sub(member.id, new_end.isoformat(), days, False)

            if role not in member.roles:
                await member.add_roles(role)

            expires_str = new_end.astimezone(MSK_TZ).strftime("%d.%m.%Y в %H:%M")

            embed = discord.Embed(
                title="✅ Подписка Legacy оформлена!",
                description=f"Участнику {member.mention} выдана подписка на **{days} дней**.\n⏳ Активна до: **{expires_str} (МСК)**",
                color=discord.Color.green()
            )
            await ctx.send(embed=embed)

            try:
                await member.send(f"🎉 Вам на сервере **{ctx.guild.name}** оформлена/продлена подписка **Legacy** на {days} дней! Она активна до {expires_str} (МСК).")
            except Exception:
                pass

        except Exception as e:
            logger.error(f"[SUB COMMAND ERROR] {e}")
            await ctx.send("❌ Произошла ошибка при выдаче подписки.")

    @new_bot.command(name="subreset", aliases=["сбросподписки", "сброс_подписки"])
    @commands.has_permissions(administrator=True)
    async def cmd_subreset(ctx, member: discord.Member, days: int):
        """
        Сброс срока подписки до ровно 30 / 90 / 360 дней от текущего момента.
        Нужна, если человеку по ошибке выдали больше (например, 60 вместо 30).
        Пример: !subreset @user 30
        """
        try:
            if days not in [30, 90, 360]:
                await ctx.send("❌ Доступные сроки подписки: **30**, **90** или **360** дней.")
                return

            role = discord.utils.get(ctx.guild.roles, name="Legacy")
            if not role:
                await ctx.send("❌ Роль с именем **Legacy** не найдена на сервере!")
                return

            subs = await db_load_subs()
            old_data = subs.get(str(member.id))
            if not old_data:
                await ctx.send(f"❌ У {member.mention} нет записи о подписке — сбрасывать нечего. Для выдачи используйте `!sub @участник {days}`.")
                return

            old_end = datetime.fromisoformat(old_data["expires_at"])
            old_str = old_end.astimezone(MSK_TZ).strftime("%d.%m.%Y в %H:%M")

            now = datetime.now(timezone.utc)
            new_end = now + timedelta(days=days)
            new_str = new_end.astimezone(MSK_TZ).strftime("%d.%m.%Y в %H:%M")

            # notified_1day сбрасываем: срок новый, напоминание за сутки должно прийти снова
            await db_save_sub(member.id, new_end.isoformat(), days, False)

            if role not in member.roles:
                await member.add_roles(role)

            embed = discord.Embed(
                title="🔄 Срок подписки Legacy сброшен",
                description=(
                    f"Участник: {member.mention}\n"
                    f"Было до: **{old_str} (МСК)**\n"
                    f"Теперь: **{days} дней** — активна до **{new_str} (МСК)**"
                ),
                color=discord.Color.orange()
            )
            await ctx.send(embed=embed)

            try:
                await member.send(
                    f"ℹ️ Срок вашей подписки **Legacy** на сервере **{ctx.guild.name}** скорректирован: "
                    f"{days} дней, активна до {new_str} (МСК)."
                )
            except Exception:
                pass

        except Exception as e:
            logger.error(f"[SUBRESET COMMAND ERROR] {e}")
            await ctx.send("❌ Произошла ошибка при сбросе срока подписки.")

    @new_bot.command(name="subremove", aliases=["снятьподписку", "снять_подписку"])
    @commands.has_permissions(administrator=True)
    async def cmd_subremove(ctx, member: discord.Member):
        """
        Полностью снимает подписку: удаляет запись из БД и роль Legacy.
        Пример: !subremove @user
        """
        try:
            subs = await db_load_subs()
            if str(member.id) not in subs:
                await ctx.send(f"❌ У {member.mention} нет записи о подписке.")
                return

            await db_delete_sub(member.id)

            role = discord.utils.get(ctx.guild.roles, name="Legacy")
            if role and role in member.roles:
                await member.remove_roles(role)

            embed = discord.Embed(
                title="🗑 Подписка Legacy снята",
                description=f"У участника {member.mention} удалена подписка и роль **Legacy**.",
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)
        except Exception as e:
            logger.error(f"[SUBREMOVE COMMAND ERROR] {e}")
            await ctx.send("❌ Произошла ошибка при снятии подписки.")

    @new_bot.command(name="subinfo", aliases=["mysub", "мояподписка"])
    async def cmd_subinfo(ctx):
        try:
            subs = await db_load_subs()
            user_id_str = str(ctx.author.id)

            if user_id_str not in subs:
                await ctx.send("📉 У вас нет активной подписки **Legacy**.")
                return

            data = subs[user_id_str]
            expires_at = datetime.fromisoformat(data["expires_at"])
            now = datetime.now(timezone.utc)

            if expires_at <= now:
                await ctx.send("⌛️ Ваша подписка **Legacy** истекла.")
                return

            remaining = expires_at - now
            days_left = remaining.days
            hours_left = remaining.seconds // 3600
            expires_str = expires_at.astimezone(MSK_TZ).strftime("%d.%m.%Y в %H:%M")

            embed = discord.Embed(
                title="💎 Статус вашей подписки Legacy",
                description=(
                    f"Статус: **Активна** ✅\n"
                    f"Осталось времени: **{days_left} дн. {hours_left} ч.**\n"
                    f"Истекает: **{expires_str} (МСК)**\n\n"
                    f"💬 Для продления подписки обратитесь к **Castro**."
                ),
                color=discord.Color.gold()
            )
            await ctx.send(embed=embed)
        except Exception as e:
            logger.error(f"[SUBINFO ERROR] {e}")

    @new_bot.command(name="subslist", aliases=["списокподписок", "список_подписок"])
    @commands.has_permissions(administrator=True)
    async def cmd_subslist(ctx):
        try:
            subs = await db_load_subs()
            if not subs:
                await ctx.send("📉 База данных подписок пуста.")
                return

            now = datetime.now(timezone.utc)
            lines = []

            for user_id_str, data in subs.items():
                expires_at = datetime.fromisoformat(data["expires_at"])
                if expires_at <= now:
                    continue

                user = ctx.guild.get_member(int(user_id_str))
                name = user.mention if user else f"ID: {user_id_str}"
                expires_str = expires_at.astimezone(MSK_TZ).strftime("%d.%m.%Y в %H:%M")

                remaining = expires_at - now
                days_left = remaining.days

                lines.append(f"👤 {name} — активна до **{expires_str} МСК** (осталось {days_left} дн.)")

            if not lines:
                await ctx.send("📉 Активных подписок на данный момент нет.")
                return

            embed = discord.Embed(
                title="💎 Список активных подписок Legacy",
                description="\n".join(lines),
                color=discord.Color.gold()
            )
            await ctx.send(embed=embed)
        except Exception as e:
            logger.error(f"[SUBSLIST ERROR] {e}")
            await ctx.send(f"❌ Ошибка при чтении подписок: {e}")

    @new_bot.command(name="paywall", aliases=["платный_доступ"])
    @commands.has_permissions(administrator=True)
    async def cmd_paywall(ctx, action: str = "", mode: str = ""):
        """!paywall — предпросмотр | !paywall confirm — включить | !paywall off — выключить и вернуть права | !paywall test — тест оповещения"""
        action = (action or "").lower()
        try:
            if action in ("", "preview", "status"):
                plan = await paywall_plan(ctx.guild, new_bot)
                state = "🟢 ВКЛЮЧЁН" if PAYWALL_STATE["enabled"] else "⚪ выключен"
                embed = discord.Embed(title="🔒 Платный доступ (paywall)", color=discord.Color.gold(),
                                      description=f"Сейчас: **{state}**")
                lines = []
                for it in plan["open"]:
                    if it["obj"] is None:
                        lines.append(f"❌ {it['label']} — не найден (ID {it['id']})")
                    else:
                        extra = " (ветка: права задаются на родительском канале)" if it["is_thread"] else ""
                        if isinstance(it["target"], discord.CategoryChannel):
                            extra = f" (категория, каналов внутри: {len(it['target'].channels)})"
                        mode_txt = "только чтение" if it["readonly"] else "можно писать"
                        lines.append(f"• {it['obj'].mention} — {it['label']}, {mode_txt}{extra}")
                embed.add_field(name="Останется открытым для всех", value="\n".join(lines), inline=False)
                keep = ", ".join(r.name for r in plan["keep_roles"]) or "—"
                embed.add_field(name="Что сделает `!paywall confirm`", value=(
                    f"• Скроет остальные каналы и категории (всего на сервере {plan['channels']}) от всех без Legacy\n"
                    f"• Роли без подписки видят только открытые каналы: **{', '.join(r.name for r in plan['restricted']) or '—'}** (и @everyone)\n"
                    f"• Все остальные роли, а также прямые доступы участников к закрытым каналам снимаются — полный доступ только у Legacy\n"
                    f"• **Trader** по-прежнему выдаётся при входе\n"
                    f"• Полный доступ у ролей: **{keep}**\n"
                    f"• Без Legacy сейчас **{plan['without']}** участников — они потеряют доступ до оплаты\n"
                    f"• Бот сам напишет анонс-embed в канал анонсов и оповещение в канал подписки"), inline=False)
                if plan["missing_roles"]:
                    embed.add_field(name="⚠️ Роли не найдены", value=", ".join(plan["missing_roles"]) + "\nПроверь точное название роли.", inline=False)
                if plan["explicit"]:
                    items = "\n".join(f"• **{r.name}** — прямой доступ в {n} каналах (будет снят)" for r, n in plan["explicit"].items())
                    embed.add_field(name="Роли с прямым доступом к каналам", value=items, inline=False)
                if plan["admin_roles"]:
                    embed.add_field(name="🚨 Роли с правом Администратор", value=(
                        ", ".join(r.name for r in plan["admin_roles"]) +
                        "\nАдминистратора нельзя ограничить каналами: эти роли будут видеть всё. Если это не должно так быть — убери у них право «Администратор»."), inline=False)
                embed.add_field(name="Команды", value=(
                    "`!paywall confirm` — включить\n"
                    "`!paywall off` — выключить и вернуть прежние права\n`!paywall test` — тестовое оповещение в канал подписки\n"
                    "`!paywall announce` — повторить анонс о закрытии сервера"),
                    inline=False)
                await ctx.send(embed=embed)

            elif action == "confirm":
                me = ctx.guild.me
                if not (me.guild_permissions.administrator or (me.guild_permissions.manage_roles and me.guild_permissions.manage_channels)):
                    await ctx.send("❌ Боту нужны права **Управление ролями** и **Управление каналами** (или Администратор).")
                    return
                strict = True
                await ctx.send("⏳ Применяю платный доступ — это займёт около минуты...")
                res = await paywall_apply(ctx.guild, new_bot, strict=strict)
                if not res["ok"]:
                    await ctx.send(f"❌ {res['error']}")
                    return
                msg = f"✅ Платный доступ включён. Каналов обновлено: **{res['done']}** из **{res['total']}**."
                if res["failed"]:
                    msg += "\n⚠️ Не удалось изменить: " + ", ".join(res["failed"][:15])
                await ctx.send(msg)
                ok_ann = await send_closed_announcement(new_bot)
                ok_pay = await send_paywall_notice(new_bot, None)
                await ctx.send(("✅ Анонс отправлен в канал анонсов. " if ok_ann else "⚠️ Анонс отправить не удалось (проверь права бота). ")
                               + ("✅ Сообщение с тарифами отправлено в канал подписки." if ok_pay else "⚠️ Сообщение в канал подписки не отправилось."))

            elif action == "off":
                if not PAYWALL_BACKUP and not PAYWALL_STATE["enabled"]:
                    await ctx.send("ℹ️ Платный доступ и так выключен.")
                    return
                await ctx.send("⏳ Возвращаю прежние права каналов...")
                res = await paywall_restore(ctx.guild)
                msg = f"✅ Платный доступ выключен, права возвращены в **{res['done']}** каналах."
                if res["failed"]:
                    msg += "\n⚠️ Не удалось вернуть: " + ", ".join(res["failed"][:15])
                await ctx.send(msg)

            elif action == "announce":
                ok = await send_closed_announcement(new_bot)
                await ctx.send("✅ Анонс отправлен." if ok else "❌ Не удалось отправить анонс — смотри логи.")

            elif action == "test":
                ok = await send_paywall_notice(new_bot, ctx.author)
                await ctx.send("✅ Тестовое оповещение отправлено в ветку." if ok else "❌ Не удалось отправить — смотри логи.")
            else:
                await ctx.send("Использование: `!paywall`, `!paywall confirm`, `!paywall off`, `!paywall test`, `!paywall announce`")
        except Exception as e:
            logger.error(f"[PAYWALL COMMAND] {type(e).__name__}: {e}\n{traceback.format_exc()}")
            await ctx.send(f"❌ Ошибка: {type(e).__name__}: {e}")

    @new_bot.command(name="rank", aliases=["level", "активность"])
    async def cmd_rank(ctx, member: discord.Member = None):
        try:
            target = member or ctx.author
            await ctx.send(embed=build_user_rank_embed(ctx.guild, target.id))
        except Exception as e:
            logger.error(f"[RANK] {e}")

    @new_bot.command(name="leaderboard", aliases=["top", "топ", "рейтинг"])
    async def cmd_leaderboard(ctx):
        try:
            ranking = get_ranking(ctx.guild)
            if not ranking:
                await ctx.send("📉 База активности пока пуста. Участники еще не отправляли сообщения.")
                return
            view = LeaderboardView(ctx.guild, ranking, ctx.author.id)
            await ctx.send(embed=view.make_embed(), view=view)
        except Exception as e:
            logger.error(f"[LEADERBOARD] {type(e).__name__}: {e}\n{traceback.format_exc()}")

    @new_bot.command(name="clear", aliases=["purge", "очистить"])
    @commands.has_permissions(manage_messages=True)
    async def cmd_clear(ctx, amount: int = 10):
        try:
            await ctx.channel.purge(limit=amount + 1)
            msg = await ctx.send(f"🧹 Успешно удалено сообщений: {amount}")
            await asyncio.sleep(3)
            await msg.delete()
        except Exception as e:
            logger.error(f"[CLEAR] {e}")

    @new_bot.command(name="news")
    async def cmd_news(ctx):
        try:
            thread = await get_target_channel(TARGET_NEWS_THREAD_ID, new_bot)
            if not thread:
                await ctx.send("❌ Целевая ветка для новостей не найдена.")
                return

            async with ctx.typing():
                news = await fetch_economic_news()
                now_msk = datetime.now(MSK_TZ)
                today_date = now_msk.date()

                today_events = []
                for ev in news:
                    if not ev.get("dt_utc"):
                        continue
                    ev_msk = ev["dt_utc"].astimezone(MSK_TZ)
                    if ev_msk.date() == today_date:
                        today_events.append((ev, ev_msk))

                embed = discord.Embed(
                    title="**Ежедневный экономический календарь Forex**",
                    color=discord.Color.gold()
                )

                day_name = DAYS_RU[now_msk.weekday()]
                lines = [f"📅 *Запланированные события: {day_name}, {now_msk.strftime('%d.%m')}*"]

                for ev, ev_msk in today_events:
                    flag = get_flag(ev["country"])
                    time_str = ev_msk.strftime("%H:%M")
                    impact_str = "🔴 **HIGH**" if ev["impact"] == "HIGH" else "🟠 **MEDIUM**"
                    lines.append(f"\n🕘 {time_str} | {flag} **{ev['country']}** — {ev['title']}\n{impact_str}")

                embed.description = "\n".join(lines) if len(lines) > 1 else "На сегодня важных новостей больше нет."
                await thread.send(embed=embed)

                if ctx.channel.id != TARGET_NEWS_THREAD_ID:
                    await ctx.send(f"✅ Новости опубликованы в ветку <#{TARGET_NEWS_THREAD_ID}>")
        except Exception as e:
            logger.error(f"[NEWS] {e}")

    return new_bot

# ---------------------------------------------------------------------------
# WEB SERVER FOR RENDER KEEP-ALIVE
# ---------------------------------------------------------------------------
async def handle_ping(request):
    return web.Response(text="Bot is running!", status=200)

async def start_web_server():
    try:
        app = web.Application()
        app.router.add_get("/", handle_ping)
        app.router.add_get("/ping", handle_ping)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", PORT)
        await site.start()
        logger.info(f"🌐 Веб-сервер запущен на порту {PORT}")
    except Exception as e:
        logger.error(f"❌ Ошибка веб-сервера: {e}")

# ---------------------------------------------------------------------------
# HELPER FUNCTIONS & NEWS PARSER
# ---------------------------------------------------------------------------
async def get_target_channel(channel_id: int, client_bot=None):
    b = client_bot if client_bot else bot
    if not b:
        return None
    try:
        channel = b.get_channel(channel_id)
        if not channel:
            channel = await b.fetch_channel(channel_id)
        return channel
    except Exception as e:
        logger.error(f"[CHANNEL ERROR] {e}")
        return None

def get_flag(currency: str) -> str:
    return COUNTRY_FLAGS.get(str(currency).upper(), "🌐")

async def http_get_with_fallback(url: str, timeout: int = 15, headers: dict = None):
    """
    GET-запрос для внешних источников (новости Forex Factory, YouTube).
    Сначала пробуем НАПРЯМУЮ (без прокси): прокси понадобился боту из-за
    блокировки Discord, а его провайдер может отказывать в соединении к
    другим доменам (curl: (97) cannot complete SOCKS5 connection).
    Если прямой запрос не удался — пробуем через прокси (если он задан).
    Возвращает response; если оба способа провалились — бросает исключение.
    """
    kwargs = {"impersonate": "chrome120", "timeout": timeout}
    if headers:
        kwargs["headers"] = headers

    direct_error = None
    try:
        r = await asyncio.to_thread(async_requests.get, url, **kwargs)
        if r.status_code == 200:
            return r
        direct_error = f"HTTP {r.status_code}"
    except Exception as e:
        direct_error = f"{type(e).__name__}: {e}"

    if not PROXY_URL:
        raise RuntimeError(f"Прямой запрос к {url} не удался ({direct_error}), прокси не задан.")

    logger.warning(f"[FETCH] Прямой запрос к {url} не удался ({direct_error}) — пробую через прокси.")
    proxies = {"http": PROXY_URL, "https": PROXY_URL}
    return await asyncio.to_thread(async_requests.get, url, proxies=proxies, **kwargs)

def parse_ff_xml_feed(xml_content: str) -> list:
    events = []
    try:
        root = ET.fromstring(xml_content)
        for item in root.findall("event"):
            title = item.findtext("title", "No Title")
            country = item.findtext("country", "USD")
            date_str = item.findtext("date", "")
            time_str = item.findtext("time", "")
            impact = item.findtext("impact", "Low")

            impact_level = None
            if impact in ["High", "High Impact Expected", "Red"]:
                impact_level = "HIGH"
            elif impact in ["Medium", "Medium Impact Expected", "Orange"]:
                impact_level = "MEDIUM"

            if not impact_level:
                continue

            try:
                dt_str = f"{date_str} {time_str}".strip()
                dt_utc = datetime.strptime(dt_str, "%m-%d-%Y %I:%M%p").replace(tzinfo=timezone.utc)
            except Exception:
                dt_utc = None

            events.append({
                "title": title,
                "country": country,
                "dt_utc": dt_utc,
                "impact": impact_level
            })
    except Exception as e:
        logger.error(f"[NEWS PARSER] {e}")
    return events

async def fetch_economic_news(force_refresh: bool = False) -> list:
    now = time.time()
    if not force_refresh and NEWS_CACHE["data"] and (now - NEWS_CACHE["last_fetch"] < CACHE_TTL_SECONDS):
        return NEWS_CACHE["data"]

    # Если прошлая попытка была неудачной (last_attempt новее last_fetch) —
    # ждём паузу, чтобы не получать 429/403 из-за слишком частых запросов.
    last_attempt_failed = NEWS_CACHE["last_attempt"] > NEWS_CACHE["last_fetch"]
    cooldown = 60 if force_refresh else NEWS_FAIL_COOLDOWN_SECONDS
    if last_attempt_failed and (now - NEWS_CACHE["last_attempt"] < cooldown):
        return NEWS_CACHE["data"]
    NEWS_CACHE["last_attempt"] = now

    # Запасной адрес той же ленты (XML на nfs.faireconomy.media) — по логам
    # он отдаёт данные, когда JSON отвечает 429.
    sources = [
        {"url": "https://nfs.faireconomy.media/ff_calendar_thisweek.json", "type": "json"},
        {"url": "https://nfs.faireconomy.media/ff_calendar_thisweek.xml", "type": "xml"},
        {"url": "https://www.forexfactory.com/ffcalendar.xml", "type": "xml"}
    ]

    for src in sources:
        url = src["url"]
        stype = src["type"]
        try:
            r = await http_get_with_fallback(
                url,
                timeout=15,
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.8",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                }
            )

            if r.status_code == 200:
                if "<html" in r.text.lower() or "<!doctype html" in r.text.lower():
                    continue

                parsed = []
                if stype == "json":
                    try:
                        data = r.json()
                        for item in data:
                            imp = str(item.get("impact", "")).upper()
                            impact_level = None
                            if "HIGH" in imp or "RED" in imp:
                                impact_level = "HIGH"
                            elif "MEDIUM" in imp or "ORANGE" in imp:
                                impact_level = "MEDIUM"

                            if not impact_level:
                                continue

                            date_raw = item.get("date", "")
                            try:
                                dt_utc = datetime.fromisoformat(date_raw.replace("Z", "+00:00"))
                            except Exception:
                                dt_utc = None

                            parsed.append({
                                "title": item.get("title", item.get("name", "N/A")),
                                "country": item.get("country", "USD"),
                                "dt_utc": dt_utc,
                                "impact": impact_level
                            })
                    except Exception:
                        continue

                elif stype == "xml":
                    parsed = parse_ff_xml_feed(r.text)

                if parsed:
                    NEWS_CACHE["data"] = parsed
                    NEWS_CACHE["last_fetch"] = now
                    await db_save_news_cache(parsed, now)
                    logger.info("✅ Новости Forex Factory успешно обновлены!")
                    return parsed
        except Exception as e:
            logger.error(f"[NEWS API ERROR] {e}")

    logger.warning(
        f"⚠️ Все источники новостей недоступны. Следующая попытка не раньше чем через "
        f"{NEWS_FAIL_COOLDOWN_SECONDS // 60} мин."
    )
    return NEWS_CACHE["data"]

# ---------------------------------------------------------------------------
# AUTOMATED TASKS
# ---------------------------------------------------------------------------

@tasks.loop(minutes=5)
async def check_youtube_streams_loop():
    try:
        if not bot or not YOUTUBE_CHANNEL_ID:
            return
        await bot.wait_until_ready()

        channel = await get_target_channel(TARGET_STREAMS_CHANNEL_ID)
        if not channel:
            return

        rss_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={YOUTUBE_CHANNEL_ID}"
        r = await http_get_with_fallback(rss_url, timeout=10)

        if r.status_code == 200:
            root = ET.fromstring(r.text)
            ns = {"atom": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}

            for entry in root.findall("atom:entry", ns):
                video_id = entry.findtext("yt:videoId", namespaces=ns)
                title = entry.findtext("atom:title", namespaces=ns)
                link = f"https://www.youtube.com/watch?v={video_id}"

                if video_id and video_id not in SENT_YOUTUBE_STREAMS:
                    video_page = await http_get_with_fallback(link, timeout=10)

                    if video_page.status_code == 200 and ('"isLive":true' in video_page.text or '"style":"LIVE"' in video_page.text):
                        SENT_YOUTUBE_STREAMS.add(video_id)

                        embed = discord.Embed(
                            title="🔴 На YouTube начался прямой эфир!",
                            description=f"**[{title}]({link})**\n\nПрисоединяйтесь к трансляции!",
                            color=discord.Color.red()
                        )
                        embed.set_thumbnail(url=f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg")

                        await channel.send(content="@everyone", embed=embed)
                        break
    except Exception as e:
        logger.error(f"[YOUTUBE LOOP ERROR] {e}")

@tasks.loop(hours=1)
async def check_subscriptions_daily_loop():
    if not bot:
        return
    await bot.wait_until_ready()
    try:
        subs = await db_load_subs()
        if not subs:
            return

        now = datetime.now(timezone.utc)
        to_remove = []

        for user_id_str, data in subs.items():
            expires_at = datetime.fromisoformat(data["expires_at"])
            time_left = expires_at - now
            hours_left = time_left.total_seconds() / 3600.0

            if expires_at <= now:
                to_remove.append(int(user_id_str))
            elif 20 <= hours_left <= 26 and not data.get("notified_1day", False):
                for guild in bot.guilds:
                    member = guild.get_member(int(user_id_str))
                    if member:
                        try:
                            expires_str = expires_at.astimezone(MSK_TZ).strftime("%d.%m.%Y в %H:%M")
                            await member.send(
                                f"⚠️ Напоминание: Срок действия вашей подписки **Legacy** на сервере **{guild.name}** истекает завтра (**{expires_str} МСК**).\n"
                                f"Для продления подписки напишите администратору **@alchemistcastro**."
                            )
                            await db_save_sub(int(user_id_str), data["expires_at"], data["days_total"], True)
                        except Exception as e:
                            logger.error(f"[SUB NOTIFY ERROR] {e}")

        if to_remove:
            for guild in bot.guilds:
                role = discord.utils.get(guild.roles, name="Legacy")
                if not role:
                    continue

                for user_id in to_remove:
                    member = guild.get_member(user_id)
                    if member and role in member.roles:
                        try:
                            await member.remove_roles(role)
                            try:
                                await member.send(f"⚠️ Срок действия вашей подписки **Legacy** на сервере **{guild.name}** истек. Для продления напишите **@alchemistcastro**.")
                            except Exception:
                                pass
                        except Exception as e:
                            logger.error(f"[SUB REMOVE ERROR] {e}")

                    await db_delete_sub(user_id)
    except Exception as e:
        logger.error(f"[SUBS LOOP ERROR] {e}")

@tasks.loop(minutes=1)
async def check_30min_news_alerts():
    try:
        if not bot:
            return
        await bot.wait_until_ready()
        thread = await get_target_channel(TARGET_NEWS_THREAD_ID)
        if not thread:
            return

        news = await fetch_economic_news()
        now_utc = datetime.now(timezone.utc)
        now_msk = now_utc.astimezone(MSK_TZ)

        is_night_time = (0 <= now_msk.hour < 8)
        upcoming_events = []

        for ev in news:
            if ev.get("impact") != "HIGH" or not ev.get("dt_utc"):
                continue

            dt_utc = ev["dt_utc"]
            time_diff = (dt_utc - now_utc).total_seconds() / 60.0
            event_id = f"{ev['title']}_{dt_utc.isoformat()}"

            if 28 <= time_diff <= 31 and event_id not in SENT_NEWS_ALERTS:
                upcoming_events.append((ev, event_id))

        if upcoming_events:
            lines = []
            for ev, event_id in upcoming_events:
                SENT_NEWS_ALERTS.add(event_id)
                dt_msk = ev["dt_utc"].astimezone(MSK_TZ)
                time_str = dt_msk.strftime("%H:%M")
                flag = get_flag(ev["country"])
                lines.append(f"{flag} **{ev['country']}** — {ev['title']} | 🕘 {time_str}")

            description_text = "\n".join(lines) + "\n\n-# ⌛️ Публикация через 30 минут"

            embed = discord.Embed(
                title="**Запланированное событие:**" if len(upcoming_events) == 1 else "**Запланированные события:**",
                description=description_text,
                color=discord.Color.red()
            )

            content = "" if is_night_time else "@everyone"
            await thread.send(content=content, embed=embed)
    except Exception as e:
        logger.error(f"[NEWS ALERT TASK] {e}")

@tasks.loop(minutes=2)
async def check_discord_events_alerts():
    try:
        if not bot:
            return
        await bot.wait_until_ready()
        thread = await get_target_channel(TARGET_EVENTS_THREAD_ID)
        if not thread:
            return

        now_utc = datetime.now(timezone.utc)

        for guild in bot.guilds:
            try:
                events = await guild.fetch_scheduled_events(with_counts=True)
                for event in events:
                    if not event.start_time:
                        continue

                    time_diff = (event.start_time - now_utc).total_seconds() / 60.0
                    event_id = f"{event.id}_{event.start_time.isoformat()}"

                    if 28 <= time_diff <= 32 and event_id not in SENT_DISCORD_EVENTS:
                        SENT_DISCORD_EVENTS.add(event_id)

                        embed = discord.Embed(
                            description=(
                                f"🕘 Через 30 минут начнётся **{event.name}**!\n\n"
                                f"Ссылка, чтоб присоединиться к нашему брифингу 👇\n"
                                f"{event.url}"
                            ),
                            color=discord.Color.gold()
                        )
                        await thread.send(content="@everyone", embed=embed)
            except Exception as e:
                logger.error(f"[GUILD EVENT] {e}")
    except Exception as e:
        logger.error(f"[EVENTS CHECK] {e}")

@tasks.loop(hours=1)
async def scheduled_news_digests():
    try:
        if not bot:
            return
        await bot.wait_until_ready()
        thread = await get_target_channel(TARGET_NEWS_THREAD_ID)
        if not thread:
            return

        now_msk = datetime.now(MSK_TZ)

        if now_msk.hour == 8:
            news = await fetch_economic_news(force_refresh=True)

            if now_msk.weekday() == 0:
                grouped = {}
                for ev in news:
                    if not ev.get("dt_utc"):
                        continue
                    ev_msk = ev["dt_utc"].astimezone(MSK_TZ)
                    day_key = ev_msk.date()
                    if day_key not in grouped:
                        grouped[day_key] = []
                    grouped[day_key].append((ev, ev_msk))

                lines = []
                for day_date in sorted(grouped.keys()):
                    first_ev_msk = grouped[day_date][0][1]
                    day_name = DAYS_RU[first_ev_msk.weekday()]
                    month_name = MONTHS_RU[first_ev_msk.month]

                    lines.append(f"\n**📅 {day_name}, {first_ev_msk.day}, {month_name}**")

                    for ev, ev_msk in grouped[day_date]:
                        flag = get_flag(ev["country"])
                        time_str = ev_msk.strftime("%H:%M")
                        impact_str = "🔴 **HIGH**" if ev["impact"] == "HIGH" else "🟠 **MEDIUM**"
                        lines.append(f"{flag} **{ev['country']}** — {ev['title']} | 🕘 {time_str} | {impact_str}")

                current_text = ""
                embeds_to_send = []

                for line in lines:
                    if len(current_text) + len(line) + 1 > 3900:
                        embeds_to_send.append(discord.Embed(description=current_text, color=discord.Color.blue()))
                        current_text = line
                    else:
                        current_text += "\n" + line if current_text else line

                if current_text:
                    embeds_to_send.append(discord.Embed(description=current_text, color=discord.Color.blue()))

                if embeds_to_send:
                    embeds_to_send[0].title = "**Экономический календарь Forex на неделю:**"
                    for emb in embeds_to_send:
                        await thread.send(embed=emb)

            today_date = now_msk.date()
            today_events = []
            for ev in news:
                if not ev.get("dt_utc"):
                    continue
                ev_msk = ev["dt_utc"].astimezone(MSK_TZ)
                if ev_msk.date() == today_date:
                    today_events.append((ev, ev_msk))

            day_name = DAYS_RU[now_msk.weekday()]
            date_str_formatted = f"{day_name}, {now_msk.strftime('%d.%m')}"

            embed = discord.Embed(
                title="**Ежедневный экономический календарь Forex**",
                color=discord.Color.gold()
            )

            lines = [f"📅 *Запланированные события: {date_str_formatted}*"]
            for ev, ev_msk in today_events:
                flag = get_flag(ev["country"])
                time_str = ev_msk.strftime("%H:%M")
                impact_str = "🔴 **HIGH**" if ev["impact"] == "HIGH" else "🟠 **MEDIUM**"
                lines.append(f"\n🕘 {time_str} | {flag} **{ev['country']}** — {ev['title']}\n{impact_str}")

            embed.description = "\n".join(lines) if len(lines) > 1 else "На сегодня важных новостей нет."
            await thread.send(embed=embed)
    except Exception as e:
        logger.error(f"[DIGEST TASK] {e}")

# ---------------------------------------------------------------------------
# MAIN EXECUTION
# ---------------------------------------------------------------------------
def mask_proxy(url) -> str:
    """Прячет логин/пароль из адреса прокси для логов."""
    if not url:
        return "прямое подключение"
    try:
        scheme, rest = url.split("://", 1)
        host = rest.split("@", 1)[-1]
        return f"{scheme}://{host}"
    except Exception:
        return "прокси"

async def probe_discord(proxy_url, timeout: int = 10) -> bool:
    """Проверяет, отвечает ли Discord API через данный прокси (или напрямую, если proxy_url=None)."""
    try:
        connector = ProxyConnector.from_url(proxy_url) if proxy_url else None
        to = aiohttp.ClientTimeout(total=timeout)
        async with aiohttp.ClientSession(connector=connector, timeout=to) as session:
            async with session.get("https://discord.com/api/v10/gateway") as resp:
                if resp.status != 200:
                    logger.warning(f"[PROBE] {mask_proxy(proxy_url)}: HTTP {resp.status}")
                    return False
                data = await resp.json(content_type=None)
                return isinstance(data, dict) and "url" in data
    except Exception as e:
        logger.warning(f"[PROBE] {mask_proxy(proxy_url)}: {type(e).__name__}: {str(e)[:120] or '(пусто)'}")
        return False

async def fetch_free_proxy_candidates(limit: int = 120) -> list:
    """Скачивает публичные списки бесплатных прокси (ip:port) и возвращает случайную выборку URL."""
    import random
    import re
    found = []
    timeout = aiohttp.ClientTimeout(total=15)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for scheme, src in FREE_PROXY_SOURCES:
                try:
                    async with session.get(src) as resp:
                        if resp.status != 200:
                            continue
                        text = await resp.text()
                        for ip, port in re.findall(r"(\d{1,3}(?:\.\d{1,3}){3}):(\d{2,5})", text):
                            found.append(f"{scheme}://{ip}:{port}")
                except Exception as e:
                    logger.warning(f"[FREE PROXY] источник недоступен: {src} ({type(e).__name__})")
    except Exception as e:
        logger.warning(f"[FREE PROXY] ошибка загрузки списков: {e}")
    random.shuffle(found)
    return found[:limit]

async def find_working_free_proxy(concurrency: int = 25):
    """Быстро проверяет бесплатные прокси параллельно и возвращает первый рабочий (или None)."""
    candidates = await fetch_free_proxy_candidates()
    if not candidates:
        logger.warning("[FREE PROXY] не удалось получить список бесплатных прокси.")
        return None
    logger.info(f"[FREE PROXY] проверяю {len(candidates)} бесплатных прокси...")
    sem = asyncio.Semaphore(concurrency)

    async def check(url):
        async with sem:
            ok = await probe_discord(url, timeout=8)
            return url if ok else None

    tasks_ = [asyncio.create_task(check(u)) for u in candidates]
    try:
        for fut in asyncio.as_completed(tasks_):
            res = await fut
            if res:
                return res
    finally:
        for t in tasks_:
            if not t.done():
                t.cancel()
    return None

async def choose_connection():
    """
    Выбирает, как подключаться к Discord. Возвращает (метка, proxy_url|None, это_основной_прокси)
    или None, если ничего не работает. Порядок: платные прокси -> напрямую -> бесплатные.
    """
    for i, p in enumerate(PAID_PROXIES):
        if await probe_discord(p):
            return (f"платный прокси #{i + 1} ({mask_proxy(p)})", p, i == 0)
        logger.warning(f"⚠️ Платный прокси #{i + 1} ({mask_proxy(p)}) не отвечает.")
    if USE_DIRECT_FALLBACK:
        if await probe_discord(None):
            return ("прямое подключение (без прокси)", None, False)
        logger.warning("⚠️ Прямое подключение к Discord не работает.")
    if USE_FREE_PROXIES:
        free = await find_working_free_proxy()
        if free:
            return (f"бесплатный прокси ({mask_proxy(free)}) — ВРЕМЕННО", free, False)
        logger.warning("⚠️ Рабочих бесплатных прокси не нашлось.")
    return None

async def run_bot_once(label: str, proxy_url, is_primary: bool):
    """
    Запускает бота через выбранное подключение и следит за ним. Возвращает причину
    переключения (строка), либо None, если бот завершился сам.
    """
    global bot
    connector = ProxyConnector.from_url(proxy_url) if proxy_url else None
    bot = create_bot(connector)
    CONN_STATE["down_since"] = time.time()
    logger.info(f"⚡️ Подключаюсь к Discord: {label}")
    start_task = asyncio.create_task(bot.start(DISCORD_TOKEN))
    reason = None
    last_primary_check = time.time()
    try:
        while not start_task.done():
            await asyncio.sleep(15)
            ds = CONN_STATE["down_since"]
            if ds and time.time() - ds > FAILOVER_AFTER_SECONDS:
                reason = f"нет связи с Discord уже {int(time.time() - ds)} с"
                break
            if (not is_primary) and PAID_PROXIES and time.time() - last_primary_check > PRIMARY_RECHECK_SECONDS:
                last_primary_check = time.time()
                if await probe_discord(PAID_PROXIES[0]):
                    reason = "основной платный прокси снова работает — возвращаюсь на него"
                    break
    finally:
        if reason:
            logger.warning(f"🔁 Переключение подключения: {reason}.")
        try:
            if bot and not bot.is_closed():
                await bot.close()
        except Exception:
            pass
        try:
            # после close() discord.py может ещё секунд до 60 досыпать паузу
            # реконнекта — не ждём дольше 30 с, потом снимаем задачу.
            await asyncio.wait_for(asyncio.shield(start_task), timeout=30)
        except asyncio.TimeoutError:
            start_task.cancel()
        except asyncio.CancelledError:
            pass
    return reason

async def main():
    await start_web_server()
    await init_db()

    if not PAID_PROXIES:
        logger.warning("⚠️ Платный прокси не задан (PROXY_URL) — будут пробоваться запасные способы.")

    while True:
        choice = await choose_connection()
        if not choice:
            logger.error("❌ Ни один способ подключения к Discord сейчас не работает. Повторная проверка через 60 секунд.")
            await asyncio.sleep(60)
            continue

        label, proxy_url, is_primary = choice
        try:
            reason = await run_bot_once(label, proxy_url, is_primary)
            if not reason:
                await asyncio.sleep(10)
        except discord.PrivilegedIntentsRequired as e:
            logger.critical(
                f"❌ Не включены привилегированные Intents в Discord Developer Portal: {e}\n"
                f"Зайди в https://discord.com/developers/applications -> твой бот -> Bot -> "
                f"включи 'SERVER MEMBERS INTENT' и 'MESSAGE CONTENT INTENT'."
            )
            await asyncio.sleep(30)
        except discord.LoginFailure as e:
            logger.critical(f"❌ Неверный DISCORD_TOKEN: {e}. Проверь токен в переменных окружения.")
            await asyncio.sleep(60)
        except Exception as e:
            logger.error(
                f"❌ Ошибка соединения [{type(e).__name__}]: {e or '(пустое сообщение)'}\n"
                f"{traceback.format_exc()}\nПереподключение через 15 секунд..."
            )
            await asyncio.sleep(15)

if __name__ == "__main__":
    # ВАЖНО (фикс "ConnectionResetError" при TLS-рукопожатии через прокси):
    # Render запускает бота на Python 3.11.0 — в ранних сборках 3.11 есть
    # известный баг в asyncio.start_tls() (спонтанный ConnectionResetError
    # при оборачивании TLS поверх уже открытого сокета — а именно так
    # python_socks поднимает HTTPS поверх уже установленного SOCKS5-туннеля).
    # uvloop использует собственный SSL-транспорт и этот баг не затрагивает.
    # Если пакет не установлен — просто продолжаем на стандартном event loop
    # (сработает обычный asyncio.run), но тогда баг с прокси, скорее всего,
    # вернётся — тогда нужно добавить "uvloop" в requirements.txt.
    try:
        import uvloop
        uvloop.install()
        logger.info("⚡ uvloop включен как event loop (обходит баг asyncio.start_tls() в Python 3.11 при TLS через прокси).")
    except ImportError:
        logger.warning(
            "⚠️ uvloop не установлен — добавь 'uvloop' в requirements.txt. "
            "Без него возможен ConnectionResetError при TLS-рукопожатии через прокси (баг Python 3.11.0)."
        )

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен вручную.")
