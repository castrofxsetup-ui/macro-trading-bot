import os
import sys
import asyncio
import logging
import time
import json
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

import aiohttp
from aiohttp import web
import discord
from discord.ext import commands, tasks
from curl_cffi import requests as async_requests

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
PROXY_URL = os.getenv("PROXY_URL")  # Прокси используется ТОЛЬКО для парсинга новостей
PORT = int(os.getenv("PORT", 10000))

TARGET_NEWS_THREAD_ID = 1528319066513604688
TARGET_EVENTS_THREAD_ID = 1528506824687485118
TARGET_STREAMS_CHANNEL_ID = 1528319066513604688

YOUTUBE_CHANNEL_ID = os.getenv("YOUTUBE_CHANNEL_ID", "")

MSK_TZ = timezone(timedelta(hours=3))
SUBS_FILE = "subs_db.json"
XP_FILE = "xp_db.json"

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

NEWS_CACHE = {"data": [], "last_fetch": 0}
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

# ---------------------------------------------------------------------------
# DATABASE HELPERS (SUBS & XP)
# ---------------------------------------------------------------------------
def load_subs():
    if os.path.exists(SUBS_FILE):
        try:
            with open(SUBS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_subs(data):
    try:
        with open(SUBS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
    except Exception as e:
        logger.error(f"[SUBS DB ERROR] Ошибка сохранения базы подписок: {e}")

def load_xp():
    if os.path.exists(XP_FILE):
        try:
            with open(XP_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return {int(k): v for k, v in data.items()}
        except Exception:
            return {}
    return {}

def save_xp(data):
    try:
        with open(XP_FILE, "w", encoding="utf-8") as f:
            json.dump({str(k): v for k, v in data.items()}, f, ensure_ascii=False, indent=4)
    except Exception as e:
        logger.error(f"[XP DB ERROR] Ошибка сохранения опыта: {e}")

USER_XP = load_xp()

# ---------------------------------------------------------------------------
# FACTORY FUNCTION TO CREATE BOT INSTANCE
# ---------------------------------------------------------------------------
def create_bot():
    intents = discord.Intents.default()
    intents.message_content = True
    intents.guild_scheduled_events = True
    intents.members = True
    
    new_bot = commands.Bot(command_prefix="!", intents=intents)

    @new_bot.event
    async def on_ready():
        logger.info(f"✅ Успешный вход: {new_bot.user} (ID: {new_bot.user.id})")
        try:
            await fetch_economic_news(force_refresh=True)

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
        try:
            role = discord.utils.get(member.guild.roles, name="Trader")
            if role:
                await member.add_roles(role)
        except Exception as e:
            logger.error(f"[AUTO-ROLE] {e}")

    @new_bot.event
    async def on_message(message: discord.Message):
        global LAST_TOP_THREE, LAST_OVERTAKE_TIME, LAST_OVERTAKE_PAIR, PAIR_OVERTAKE_TIME

        try:
            if message.author.bot or not message.guild:
                return

            author_id = message.author.id
            USER_XP[author_id] = USER_XP.get(author_id, 0) + 5
            save_xp(USER_XP)

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

            subs = load_subs()
            now = datetime.now(timezone.utc)
            user_id_str = str(member.id)

            if user_id_str in subs:
                current_end = datetime.fromisoformat(subs[user_id_str]["expires_at"])
                if current_end > now:
                    new_end = current_end + timedelta(days=days)
                else:
                    new_end = now + timedelta(days=days)
            else:
                new_end = now + timedelta(days=days)

            subs[user_id_str] = {
                "expires_at": new_end.isoformat(),
                "days_total": days,
                "notified_1day": False
            }
            save_subs(subs)

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

    @new_bot.command(name="subinfo", aliases=["mysub", "мояподписка"])
    async def cmd_subinfo(ctx):
        try:
            subs = load_subs()
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

    @new_bot.command(name="subslist", aliases=["списокподписок"])
    @commands.has_permissions(administrator=True)
    async def cmd_subslist(ctx):
        try:
            subs = load_subs()
            if not subs:
                await ctx.send("📉 База данных подписок (`subs_db.json`) пуста.")
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
            await ctx.send("❌ Ошибка при чтении базы подписок.")

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
# HELPER FUNCTIONS & NEWS PARSER (С ПОДДЕРЖКОЙ ПРОКСИ ДЛЯ HTTP ЗАПРОСОВ)
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

    sources = [
        {"url": "https://nfs.faireconomy.media/ff_calendar_thisweek.json", "type": "json"},
        {"url": "https://www.forexfactory.com/ffcalendar.xml", "type": "xml"}
    ]

    # Настройка прокси для HTTP-запросов к Forex Factory
    proxies = None
    if PROXY_URL:
        proxies = {"http": PROXY_URL, "https": PROXY_URL}

    for src in sources:
        url = src["url"]
        stype = src["type"]
        try:
            r = await asyncio.to_thread(
                async_requests.get, 
                url, 
                impersonate="chrome120", 
                timeout=15,
                proxies=proxies,
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
                    logger.info("✅ Новости Forex Factory успешно обновлены!")
                    return parsed
        except Exception as e:
            logger.error(f"[NEWS API ERROR] {e}")

    return NEWS_CACHE["data"]

# ---------------------------------------------------------------------------
# AUTOMATED TASKS (YOUTUBE, DISCORD EVENTS, NEWS & SUBSCRIPTIONS)
# ---------------------------------------------------------------------------

@tasks.loop(minutes=5)
async def check_youtube_streams_loop():
    """Проверка начала прямых трансляций на YouTube."""
    try:
        if not bot or not YOUTUBE_CHANNEL_ID:
            return
        await bot.wait_until_ready()

        channel = await get_target_channel(TARGET_STREAMS_CHANNEL_ID)
        if not channel:
            return

        rss_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={YOUTUBE_CHANNEL_ID}"
        proxies = {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None
        
        r = await asyncio.to_thread(
            async_requests.get,
            rss_url,
            impersonate="chrome120",
            timeout=10,
            proxies=proxies
        )

        if r.status_code == 200:
            root = ET.fromstring(r.text)
            ns = {"atom": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
            
            for entry in root.findall("atom:entry", ns):
                video_id = entry.findtext("yt:videoId", namespaces=ns)
                title = entry.findtext("atom:title", namespaces=ns)
                link = f"
