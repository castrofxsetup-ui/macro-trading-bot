import os
import sys
import asyncio
import logging
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

import aiohttp
from aiohttp import web
import discord
from discord.ext import commands, tasks
from curl_cffi import requests as async_requests
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
PORT = int(os.getenv("PORT", 10000))

# ID целевых веток Discord
TARGET_NEWS_THREAD_ID = 1528319066513604688
TARGET_EVENTS_THREAD_ID = 1528506824687485118

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

NEWS_CACHE = {"data": [], "last_fetch": 0}
SENT_NEWS_ALERTS = set()
SENT_DISCORD_EVENTS = set()
CACHE_TTL_SECONDS = 1800  # 30 минут

# База данных активности в памяти (User ID -> XP)
USER_XP = {}

# Отслеживание топ-3 лидеров и параметров антиспама обгонов
LAST_TOP_THREE = []
LAST_OVERTAKE_TIME = 0
GLOBAL_OVERTAKE_COOLDOWN = 180  # Общий кулдаун между любыми анонсами обгона (3 минуты)

LAST_OVERTAKE_PAIR = (None, None)  # (Кто обогнал, Кого обогнали)
PAIR_OVERTAKE_TIME = 0
PAIR_OVERTAKE_COOLDOWN = 300  # Кулдаун для конкретной пары участников (5 минут)

# ---------------------------------------------------------------------------
# INITIALIZE DISCORD BOT
# ---------------------------------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True
intents.guild_scheduled_events = True
intents.members = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)

# ---------------------------------------------------------------------------
# WEB SERVER FOR RENDER KEEP-ALIVE
# ---------------------------------------------------------------------------
async def handle_ping(request):
    return web.Response(text="Bot is running!", status=200)

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    app.router.add_get("/ping", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"🌐 Веб-сервер запущен на порту {PORT}")

# ---------------------------------------------------------------------------
# HELPER FUNCTIONS & NEWS PARSER
# ---------------------------------------------------------------------------
async def get_target_channel(channel_id: int):
    channel = bot.get_channel(channel_id)
    if not channel:
        try:
            channel = await bot.fetch_channel(channel_id)
        except Exception as e:
            logger.error(f"[CHANNEL ERROR] Не удалось найти канал {channel_id}: {e}")
            return None
    return channel

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
        logger.error(f"[NEWS PARSER] Ошибка парсинга XML: {e}")
    return events

async def fetch_economic_news(force_refresh: bool = False) -> list:
    now = time.time()
    if not force_refresh and NEWS_CACHE["data"] and (now - NEWS_CACHE["last_fetch"] < CACHE_TTL_SECONDS):
        return NEWS_CACHE["data"]

    sources = [
        {"url": "https://nfs.faireconomy.media/ff_calendar_thisweek.json", "type": "json"},
        {"url": "https://www.forexfactory.com/ffcalendar.xml", "type": "xml"}
    ]

    for src in sources:
        url = src["url"]
        stype = src["type"]
        try:
            r = await asyncio.to_thread(
                async_requests.get, 
                url, 
                impersonate="chrome120", 
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
                    return parsed
        except Exception as e:
            logger.error(f"[NEWS API] Ошибка запроса к {url}: {e}")

    return NEWS_CACHE["data"]

# ---------------------------------------------------------------------------
# AUTOMATED TASKS (NEWS & EVENTS)
# ---------------------------------------------------------------------------

@tasks.loop(minutes=1)
async def check_30min_news_alerts():
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

@tasks.loop(minutes=2)
async def check_discord_events_alerts():
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
            logger.error(f"[EVENTS CHECK ERROR] {e}")

@tasks.loop(hours=1)
async def scheduled_news_digests():
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
            lines.append(f"\n🕘 {time_str} | {flag} **{ev['country']}** — {ev['title']}")
            lines.append(f"{impact_str}")

        embed.description = "\n".join(lines) if len(lines) > 1 else "На сегодня важных новостей нет."
        await thread.send(embed=embed)

# ---------------------------------------------------------------------------
# AUTO-ROLE & ACTIVITY & LEADERBOARD SYSTEM (WITH SMART OVERTAKE ANTISPAM)
# ---------------------------------------------------------------------------

@bot.event
async def on_member_join(member: discord.Member):
    """Выдача роли @Trader при входе"""
    role = discord.utils.get(member.guild.roles, name="Trader")
    if role:
        try:
            await member.add_roles(role)
            logger.info(f"[AUTO-ROLE] Выдана роль Trader пользователю {member.name}")
        except Exception as e:
            logger.error(f"[AUTO-ROLE ERROR] {e}")

@bot.event
async def on_message(message: discord.Message):
    global LAST_TOP_THREE, LAST_OVERTAKE_TIME, LAST_OVERTAKE_PAIR, PAIR_OVERTAKE_TIME

    if message.author.bot or not message.guild:
        return

    # Начисление XP за сообщения
    author_id = message.author.id
    USER_XP[author_id] = USER_XP.get(author_id, 0) + 5

    # Проверка изменений в топ-3
    sorted_users = sorted(USER_XP.items(), key=lambda item: item[1], reverse=True)
    current_top_three = [u_id for u_id, _ in sorted_users[:3]]

    if LAST_TOP_THREE and current_top_three != LAST_TOP_THREE:
        now = time.time()
        
        # 1. Общий кулдаун уведомлений (не чаще чем раз в 3 минуты)
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

                    # 2. Проверка антиспама для той же пары (не чаще чем раз в 5 минут для одних и тех же людей)
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
                        
                        # Обновляем временные метки и текущую пару
                        LAST_OVERTAKE_TIME = now
                        LAST_OVERTAKE_PAIR = current_pair
                        PAIR_OVERTAKE_TIME = now

    LAST_TOP_THREE = current_top_three

    await bot.process_commands(message)

# Команда проверки личного ранга
@bot.command(name="rank", aliases=["level", "активность"])
async def cmd_rank(ctx):
    xp = USER_XP.get(ctx.author.id, 0)
    embed = discord.Embed(
        title="📊 Статистика активности",
        description=f"Участник: {ctx.author.mention}\n⚡️ Накоплено опыта (XP): **{xp}**",
        color=discord.Color.blue()
    )
    await ctx.send(embed=embed)

# Команда вывода таблицы лидеров
@bot.command(name="leaderboard", aliases=["top", "топ", "рейтинг"])
async def cmd_leaderboard(ctx):
    if not USER_XP:
        await ctx.send("📉 База активности пока пуста. Участники еще не отправляли сообщения.")
        return

    sorted_users = sorted(USER_XP.items(), key=lambda item: item[1], reverse=True)

    lines = []
    medals = ["🥇", "🥈", "🥉"]

    for idx, (user_id, xp) in enumerate(sorted_users[:10], start=1):
        user = ctx.guild.get_member(user_id)
        name = user.mention if user else f"Пользователь (ID: {user_id})"
        prefix = medals[idx - 1] if idx <= 3 else f"**{idx}.**"
        lines.append(f"{prefix} {name} — **{xp} XP**")

    embed = discord.Embed(
        title="🏆 Рейтинг активности участников",
        description="\n".join(lines),
        color=discord.Color.gold()
    )
    embed.set_footer(text="Активность начисляется за сообщения в чатах сервера.")
    await ctx.send(embed=embed)

# --- Модерационные команды ---

@bot.command(name="clear", aliases=["purge", "очистить"])
@commands.has_permissions(manage_messages=True)
async def cmd_clear(ctx, amount: int = 10):
    await ctx.channel.purge(limit=amount + 1)
    msg = await ctx.send(f"🧹 Успешно удалено сообщений: {amount}")
    await asyncio.sleep(3)
    await msg.delete()

@bot.command(name="kick", aliases=["кик"])
@commands.has_permissions(kick_members=True)
async def cmd_kick(ctx, member: discord.Member, *, reason: str = "Не указана"):
    await member.kick(reason=reason)
    await ctx.send(f"👢 Участник {member.mention} исключен. Причина: {reason}")

@bot.command(name="ban", aliases=["бан"])
@commands.has_permissions(ban_members=True)
async def cmd_ban(ctx, member: discord.Member, *, reason: str = "Не указана"):
    await member.ban(reason=reason)
    await ctx.send(f"🔨 Участник {member.mention} заблокирован. Причина: {reason}")

@cmd_clear.error
@cmd_kick.error
@cmd_ban.error
async def mod_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ У вас недостаточно прав для выполнения этой команды.")
    else:
        await ctx.send("❌ Ошибка при выполнении модерационной команды.")

# ---------------------------------------------------------------------------
# BOT EVENTS & COMMANDS (TRADING / NEWS)
# ---------------------------------------------------------------------------
@bot.event
async def on_ready():
    logger.info(f"✅ Успешный вход: {bot.user} (ID: {bot.user.id})")
    await fetch_economic_news(force_refresh=True)

    if not check_30min_news_alerts.is_running():
        check_30min_news_alerts.start()
    if not check_discord_events_alerts.is_running():
        check_discord_events_alerts.start()
    if not scheduled_news_digests.is_running():
        scheduled_news_digests.start()

@bot.command(name="news")
async def cmd_news(ctx):
    thread = await get_target_channel(TARGET_NEWS_THREAD_ID)
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

            lines.append(f"\n🕘 {time_str} | {flag} **{ev['country']}** — {ev['title']}")
            lines.append(f"{impact_str}")

        embed.description = "\n".join(lines) if len(lines) > 1 else "На сегодня важных новостей больше нет."
        await thread.send(embed=embed)
        
        if ctx.channel.id != TARGET_NEWS_THREAD_ID:
            await ctx.send(f"✅ Новости опубликованы в ветку <#{TARGET_NEWS_THREAD_ID}>")

# ---------------------------------------------------------------------------
# MAIN EXECUTION WITH AUTO-RECONNECT
# ---------------------------------------------------------------------------
async def main():
    await start_web_server()
    
    if PROXY_URL:
        connector = ProxyConnector.from_url(PROXY_URL)
        bot._connection.connector = connector
        logger.info("🔒 SOCKS5 прокси успешно подключен к коннектору бота.")

    while True:
        try:
            logger.info("⚡️ Попытка подключения к Discord...")
            await bot.start(DISCORD_TOKEN)
        except discord.errors.HTTPException as e:
            if e.status == 429 or "1015" in str(e):
                logger.warning("⚠️ Cloudflare/Discord заблокировал IP. Ожидание 90 секунд...")
                await asyncio.sleep(90)
            else:
                logger.error(f"❌ Ошибка Discord HTTP ({e.status}): {e}")
                await asyncio.sleep(30)
        except Exception as e:
            logger.error(f"❌ Непредвиденный сбой сети/подключения: {e}")
            await asyncio.sleep(20)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен вручную.")
