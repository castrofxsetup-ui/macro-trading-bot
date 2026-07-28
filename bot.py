import os
import sys
import asyncio
import logging
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import aiohttp
from aiohttp import web
import discord
from discord.ext import commands
from curl_cffi import requests as async_requests
from groq import Groq

# ---------------------------------------------------------------------------
# LOGGING SETUP
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("LegacyBot")

# ---------------------------------------------------------------------------
# CONFIGURATION & ENVIRONMENT
# ---------------------------------------------------------------------------
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
PORT = int(os.getenv("PORT", 10000))

if not DISCORD_TOKEN:
    logger.critical("❌ Ошибка: Переменная окружения DISCORD_TOKEN не найдена!")
    sys.exit(1)

if not GROQ_API_KEY:
    logger.warning("⚠️ Внимание: GROQ_API_KEY не установлен. Функции ИИ будут недоступны.")

# Инициализация клиента Groq
groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

# Кэш новостей
NEWS_CACHE = {
    "data": [],
    "last_fetch": 0
}
CACHE_TTL_SECONDS = 1800  # 30 минут

# ---------------------------------------------------------------------------
# DISCORD BOT SETUP
# ---------------------------------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ---------------------------------------------------------------------------
# WEB SERVER FOR RENDER KEEP-ALIVE
# ---------------------------------------------------------------------------
async def handle_ping(request):
    return web.Response(text="Bot is online and running!", status=200)

async def start_web_server():
    app = web.Application()
    # add_get в актуальных версиях aiohttp автоматически обрабатывает и HEAD запросы
    app.router.add_get("/", handle_ping)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"🌐 Веб-сервер запущен на порту {PORT}")

# ---------------------------------------------------------------------------
# NEWS PARSER & FETCHING (WITH CLOUDFLARE BYPASS)
# ---------------------------------------------------------------------------
def parse_ff_xml_feed(xml_content: str) -> list:
    """Парсинг XML-ленты Forex Factory"""
    events = []
    try:
        root = ET.fromstring(xml_content)
        for item in root.findall("event"):
            title = item.findtext("title", "No Title")
            country = item.findtext("country", "Global")
            date_str = item.findtext("date", "")
            time_str = item.findtext("time", "")
            impact = item.findtext("impact", "Low")
            forecast = item.findtext("forecast", "")
            previous = item.findtext("previous", "")

            impact_level = "LOW"
            if impact in ["High", "High Impact Expected", "Red"]:
                impact_level = "HIGH"
            elif impact in ["Medium", "Medium Impact Expected", "Orange"]:
                impact_level = "MEDIUM"

            events.append({
                "title": title,
                "country": country,
                "date": f"{date_str} {time_str}".strip(),
                "impact": impact_level,
                "forecast": forecast,
                "previous": previous
            })
    except Exception as e:
        logger.error(f"[NEWS PARSER] Ошибка парсинга XML: {e}")
    return events

async def fetch_economic_news(force_refresh: bool = False) -> list:
    """Получение новостей с обходом Cloudflare через curl_cffi"""
    now = time.time()
    
    if not force_refresh and NEWS_CACHE["data"] and (now - NEWS_CACHE["last_fetch"] < CACHE_TTL_SECONDS):
        logger.info("[NEWS API] Данные взяты из кэша")
        return NEWS_CACHE["data"]

    sources = [
        {"url": "https://nfs.faireconomy.media/ff_calendar_thisweek.json", "type": "json_impersonate"},
        {"url": "https://www.forexfactory.com/ffcalendar.xml", "type": "xml_impersonate"},
        {"url": "https://raw.githubusercontent.com/martinventer/forexfactory-calendar/main/calendar.json", "type": "json_direct"}
    ]

    for src in sources:
        url = src["url"]
        stype = src["type"]
        try:
            if "impersonate" in stype:
                r = await asyncio.to_thread(
                    async_requests.get, 
                    url, 
                    impersonate="chrome120", 
                    timeout=12,
                    headers={"Accept": "application/json, text/xml"}
                )
                if r.status_code == 200:
                    if "json" in stype:
                        data = r.json()
                        if isinstance(data, list) and len(data) > 0:
                            parsed = []
                            for item in data:
                                parsed.append({
                                    "title": item.get("title", item.get("name", "N/A")),
                                    "country": item.get("country", "Global"),
                                    "date": item.get("date", "Today"),
                                    "impact": str(item.get("impact", "Low")).upper(),
                                    "forecast": item.get("forecast", ""),
                                    "previous": item.get("previous", "")
                                })
                            logger.info(f"[NEWS API] ✅ Получено {len(parsed)} событий ({url})")
                            NEWS_CACHE["data"] = parsed
                            NEWS_CACHE["last_fetch"] = now
                            return parsed
                    elif "xml" in stype:
                        data = parse_ff_xml_feed(r.text)
                        if data:
                            logger.info(f"[NEWS API] ✅ Спарен XML ({len(data)} событий)")
                            NEWS_CACHE["data"] = data
                            NEWS_CACHE["last_fetch"] = now
                            return data
                else:
                    logger.warning(f"[NEWS API] {url} вернул статус {r.status_code}")
            
            else:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, timeout=10) as response:
                        if response.status == 200:
                            data = await response.json(content_type=None)
                            if isinstance(data, list) and len(data) > 0:
                                logger.info(f"[NEWS API] ✅ Получено {len(data)} событий из открытого зеркала")
                                NEWS_CACHE["data"] = data
                                NEWS_CACHE["last_fetch"] = now
                                return data
                        else:
                            logger.warning(f"[NEWS API] {url} вернул статус {response.status}")

        except Exception as e:
            logger.error(f"[NEWS API] Ошибка запроса к {url}: {e}")

    if NEWS_CACHE["data"]:
        logger.warning("[NEWS API] Ошибка всех источников. Возврат сохранённого кэша.")
        return NEWS_CACHE["data"]

    return []

# ---------------------------------------------------------------------------
# GROQ AI INTEGRATION
# ---------------------------------------------------------------------------
async def ask_groq(prompt: str, system_prompt: str = "") -> str:
    """Запрос к модели Llama 3 через Groq API"""
    if not groq_client:
        return "❌ Ошибка: GROQ_API_KEY не передан в настройках сервера."

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    try:
        completion = await asyncio.to_thread(
            groq_client.chat.completions.create,
            model="llama-3.3-70b-versatile",
            messages=messages,
            temperature=0.5,
            max_tokens=1000
        )
        return completion.choices[0].message.content
    except Exception as e:
        logger.error(f"[GROQ ERROR] {e}")
        return f"❌ Ошибка генерации ИИ: {e}"

# ---------------------------------------------------------------------------
# BOT COMMANDS & EVENTS
# ---------------------------------------------------------------------------
@bot.event
async def on_ready():
    logger.info(f"✅ Бот {bot.user} успешно запущен!")
    await fetch_economic_news(force_refresh=True)

@bot.command(name="news")
async def cmd_news(ctx):
    """Вывести важные макроэкономические новости (🔴 High Impact)"""
    async with ctx.typing():
        news = await fetch_economic_news()
        
        high_impact = [n for n in news if "HIGH" in str(n.get("impact", "")).upper() or "RED" in str(n.get("impact", "")).upper()]

        if not high_impact and news:
            high_impact = news[:5]

        if not high_impact:
            await ctx.send("📅 На ближайшее время важных макроэкономических новостей не найдено.")
            return

        embed = discord.Embed(
            title="📊 Экономический календарь (Forex Factory)",
            color=discord.Color.red(),
            timestamp=datetime.now(timezone.utc)
        )

        for event in high_impact[:10]:
            country = event.get("country", "USD")
            title = event.get("title", "Без названия")
            date_val = event.get("date", "Сегодня")
            forecast = event.get("forecast", "-")
            previous = event.get("previous", "-")

            embed.add_field(
                name=f"🔴 [{country}] {title}",
                value=f"⏰ **Время:** {date_val}\n📈 **Прогноз:** {forecast} | **Пред:** {previous}",
                inline=False
            )

        embed.set_footer(text="Macro & SMC Bot • Data via FF")
        await ctx.send(embed=embed)

@bot.command(name="ai")
async def cmd_ai(ctx, *, query: str):
    """Задать вопрос ИИ с акцентом на trading / Smart Money Concepts"""
    async with ctx.typing():
        system_instructions = (
            "Ты — профессиональный аналитик и трейдер по методологиям Smart Money Concepts (SMC), ICT и MSNR. "
            "Отвечай структурированно, профессионально и по делу."
        )
        response = await ask_groq(query, system_prompt=system_instructions)
        
        if len(response) > 1900:
            for chunk in [response[i:i+1900] for i in range(0, len(response), 1900)]:
                await ctx.send(chunk)
        else:
            await ctx.send(response)

# ---------------------------------------------------------------------------
# MAIN EXECUTION
# ---------------------------------------------------------------------------
async def main():
    await start_web_server()
    await bot.start(DISCORD_TOKEN)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен вручную.")
