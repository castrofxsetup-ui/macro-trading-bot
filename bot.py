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
from groq import AsyncGroq

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
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
PROXY_URL = os.getenv("PROXY_URL")
PORT = int(os.getenv("PORT", 10000))

# ID целевых веток Discord
TARGET_NEWS_THREAD_ID = 1528319066513604688
TARGET_EVENTS_THREAD_ID = 1528506824687485118
AI_ALLOWED_THREAD_ID = 1502292137889501235

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

groq_client = AsyncGroq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

NEWS_CACHE = {"data": [], "last_fetch": 0}
SENT_NEWS_ALERTS = set()
SENT_DISCORD_EVENTS = set()
CACHE_TTL_SECONDS = 1800  # 30 минут

# ---------------------------------------------------------------------------
# SYSTEM PROMPT FOR AI (ALCHEMIST MSNR & SMC)
# ---------------------------------------------------------------------------
SYSTEM_INSTRUCTIONS = (
    "Ты — профессиональный аналитик и эксперт в области Smart Money Concepts (SMC), ICT, "
    "а также концепций Malaysian Support & Resistance (MSNR / Alchemist MSNR).\n\n"
    "КЛЮЧЕВАЯ ИНФОРМАЦИЯ О MSNR / ALCHEMIST:\n"
    "- MSNR (Malaysian Support and Resistance / Малазийский SnR) — это графическая торговая концепция, "
    "основанная на поиске ключевых уровней поддержки и сопротивления на младших и старших таймфреймах "
    "с использованием специфических свечных формаций (Fresh levels, Overlap, Gap, Breakout, Major/Minor SnR).\n"
    "- Alchemist MSNR — это авторская интерпретация и систематизированная торговая система на основе "
    "малазийского SnR, сфокусированная на высокой точности входа (Sniper entries) и детальной "
    "оценке реакций цены на свечные уровни.\n"
    "- СТРОГОЕ ПРАВИЛО: MSNR в контексте трейдинга НЕ ИМЕЕТ НИКАКОГО ОТНОШЕНИЯ к Microsoft, Nasdaq или другим "
    "корпорациям! Не пиши про акции или сторонние финансовые инструменты.\n\n"
    "ПРАВИЛА ОТВЕТА:\n"
    "1. Распознавай запросы про MSNR, Alchemist, Malaysian SnR в любом регистре и формате.\n"
    "2. Отвечай кратко, структурировано, профессионально и по делу."
)

# ---------------------------------------------------------------------------
# INITIALIZE DISCORD BOT (WITH PROXY SUPPORT)
# ---------------------------------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True
intents.guild_scheduled_events = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents,
    proxy=PROXY_URL if PROXY_URL else None
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
                    logger.warning(f"[NEWS API] Cloudflare перехватил запрос к {url}. Пропускаем источник.")
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
                    except Exception as json_err:
                        logger.error(f"[NEWS API] Ошибка JSON {url}: {json_err}")
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
# AUTOMATED TASKS
# ---------------------------------------------------------------------------

@tasks.loop(minutes=1)
async def check_30min_news_alerts():
    """Алерт за 30 минут до важной новости"""
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
    """Алерт за 30 минут до начала Discord-брифингов"""
    await bot.wait_until_ready()
    thread = await get_target_channel(TARGET_EVENTS_THREAD_ID)
    if not thread:
        return

    now_utc = datetime.now(timezone.utc)

    for guild in bot.guilds:
        try:
            events = await guild.fetch_scheduled_events()
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
    """Ежедневные и еженедельные дайджесты в 8:00 МСК"""
    await bot.wait_until_ready()
    thread = await get_target_channel(TARGET_NEWS_THREAD_ID)
    if not thread:
        return

    now_msk = datetime.now(MSK_TZ)

    if now_msk.hour == 8:
        news = await fetch_economic_news(force_refresh=True)

        # 1. ЕЖЕНЕДЕЛЬНЫЙ ОТЧЕТ (Понедельник)
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

        # 2. ЕЖЕДНЕВНЫЙ ОТЧЕТ
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
# BOT EVENTS & COMMANDS
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

@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user:
        return

    # Вопросы к ИИ в специальном канале
    if bot.user in message.mentions and message.channel.id == AI_ALLOWED_THREAD_ID:
        if not groq_client:
            await message.reply("⚠️ GROQ API ключ не настроен.")
            return

        clean_prompt = message.content.replace(f"<@{bot.user.id}>", "").replace(f"<@!{bot.user.id}>", "").strip()
        if clean_prompt:
            async with message.channel.typing():
                try:
                    completion = await groq_client.chat.completions.create(
                        model="llama-3.1-8b-instant",
                        messages=[
                            {"role": "system", "content": SYSTEM_INSTRUCTIONS},
                            {"role": "user", "content": clean_prompt}
                        ],
                        temperature=0.5,
                        max_tokens=1000
                    )
                    await message.reply(completion.choices[0].message.content[:1900], mention_author=True)
                except Exception as e:
                    logger.error(f"Groq error: {e}")
                    await message.reply("❌ Ошибка генерации ответа ИИ.")
        return

    await bot.process_commands(message)

@bot.command(name="news")
async def cmd_news(ctx):
    """Ручной вызов новостей на сегодня"""
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

@bot.command(name="ai")
async def cmd_ai(ctx, *, query: str):
    """Задать вопрос ИИ по торговле, SMC и MSNR"""
    if not groq_client:
        await ctx.send("⚠️ GROQ API ключ не настроен.")
        return
    async with ctx.typing():
        try:
            completion = await groq_client.chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=[
                    {"role": "system", "content": SYSTEM_INSTRUCTIONS},
                    {"role": "user", "content": query}
                ],
                temperature=0.5,
                max_tokens=1000
            )
            await ctx.send(completion.choices[0].message.content[:1900])
        except Exception as e:
            logger.error(f"Groq error: {e}")
            await ctx.send("❌ Ошибка генерации ответа ИИ.")

# ---------------------------------------------------------------------------
# MAIN EXECUTION WITH AUTO-RECONNECT
# ---------------------------------------------------------------------------
async def main():
    await start_web_server()
    
    while True:
        try:
            logger.info("⚡️ Попытка подключения к Discord...")
            await bot.start(DISCORD_TOKEN)
        except discord.errors.HTTPException as e:
            if e.status == 429 or "1015" in str(e):
                logger.warning("⚠️ Cloudflare/Discord временно заблокировал IP (HTTP 429 / Error 1015). Ожидание 90 секунд...")
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
