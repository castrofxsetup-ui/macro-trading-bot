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
            """)

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
        global LAST_TOP_THREE, LAST_OVERTAKE_TIME, LAST_OVERTAKE_PAIR, PAIR_OVERTAKE_TIME

        try:
            if message.author.bot or not message.guild:
                return

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
            await new_bot.process_commands(message)
        except Exception as e:
            logger.error(f"[ON_MESSAGE] {e}")

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

    @new_bot.command(name="rank", aliases=["level", "активность"])
    async def cmd_rank(ctx):
        try:
            xp = USER_XP.get(ctx.author.id, 0)
            embed = discord.Embed(
                title="📊 Статистика активности",
                description=f"Участник: {ctx.author.mention}\n⚡️ Накоплено опыта (XP): **{xp}**",
                color=discord.Color.blue()
            )
            await ctx.send(embed=embed)
        except Exception as e:
            logger.error(f"[RANK] {e}")

    @new_bot.command(name="leaderboard", aliases=["top", "топ", "рейтинг"])
    async def cmd_leaderboard(ctx):
        try:
            if not USER_XP:
                await ctx.send("📉 База активности пока пуста. Участники еще не отправляли сообщения.")
                return

            sorted_users = sorted(USER_XP.items(), key=lambda item: item[1], reverse=True)
            lines = []
            medals = ["🥇", "🥈", "🥉"]

            for idx, (user_id, xp) in enumerate(sorted_users, start=1):
                user = ctx.guild.get_member(user_id)
                name = user.mention if user else f"Пользователь (ID: {user_id})"
                prefix = medals[idx - 1] if idx <= 3 else f"**{idx}.**"
                lines.append(f"{prefix} {name} — **{xp} XP**")

            current_chunk = ""
            embeds_to_send = []

            for line in lines:
                if len(current_chunk) + len(line) + 1 > 3800:
                    embeds_to_send.append(discord.Embed(
                        title="🏆 Рейтинг активности участников",
                        description=current_chunk,
                        color=discord.Color.gold()
                    ))
                    current_chunk = line
                else:
                    current_chunk += "\n" + line if current_chunk else line

            if current_chunk:
                embeds_to_send.append(discord.Embed(
                    title="🏆 Рейтинг активности участников",
                    description=current_chunk,
                    color=discord.Color.gold()
                ))

            for idx, emb in enumerate(embeds_to_send):
                if idx == len(embeds_to_send) - 1:
                    emb.set_footer(text=f"Всего участников в списке: {len(sorted_users)} | Активность за сообщения")
                await ctx.send(embed=emb)

        except Exception as e:
            logger.error(f"[LEADERBOARD] {e}")

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

@tasks.loop(hours=24)
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
                                f"Для продления подписки напишите администратору **Castro**."
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
                                await member.send(f"⚠️ Срок действия вашей подписки **Legacy** на сервере **{guild.name}** истек. Для продления напишите **Castro**.")
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
