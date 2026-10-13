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
CACHE_TTL_SECONDS = 1800

USER_XP = {}

LAST_TOP_THREE = []
LAST_OVERTAKE_TIME = 0
GLOBAL_OVERTAKE_COOLDOWN = 180  # 3 минуты

LAST_OVERTAKE_PAIR = (None, None)
PAIR_OVERTAKE_TIME = 0
PAIR_OVERTAKE_COOLDOWN = 300  # 5 минут

# ---------------------------------------------------------------------------
# BOT CLASS WITH FORCED PROXY CONNECTOR
# ---------------------------------------------------------------------------
class LegacyBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guild_scheduled_events = True
        intents.members = True
        
        # Настраиваем прокси прямо при создании бота, если PROXY_URL присутствует
        kwargs = {
            "command_prefix": "!",
            "intents": intents
        }
        if PROXY_URL:
            connector = ProxyConnector.from_url(PROXY_URL)
            kwargs["connector"] = connector
            logger.info("🔒 SOCKS5 прокси успешно привязан через коннектор бота.")

        super().__init__(**kwargs)

bot = LegacyBot()

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
async def get_target_channel(channel_id: int):
    try:
        channel = bot.get_channel(channel_id)
        if not channel:
            channel = await bot.fetch_channel(channel_id)
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
            logger.error(f"[NEWS API] {e}")

    return NEWS_CACHE["data"]

# ---------------------------------------------------------------------------
# AUTOMATED TASKS
# ---------------------------------------------------------------------------

@tasks.loop(minutes=1)
async def check_30min_news_alerts():
    try:
        await bot.wait_until_ready()
        thread = await get_target_channel(TARGET_NEWS_THREAD_ID)
        if not thread:
            return

        news = await fetch_economic_news()
        now_utc = datetime.now(timezone.utc)
        now_msk = now_utc.astimezone(MSK_TZ)

        is_night_time = (0 <= now_msk.hour < 8)
        upcoming_events = []
        
        for ev in news
