import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
import aiohttp
import discord
from discord.ext import commands
from aiohttp import web

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("LegacyBot")

TOKEN = os.getenv("DISCORD_TOKEN", "YOUR_DISCORD_BOT_TOKEN")
NEWS_CHANNEL_ID = 1528319066513604688  # ID вашей ветки / канала новостей

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ==========================================
# ВЕБ-СЕРВЕР ДЛЯ RENDER (PORT BINDING)
# ==========================================

async def handle_ping(request):
    return web.Response(text="Bot is running and healthy!")

async def start_dummy_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    app.router.add_get("/health", handle_ping)

    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"🌐 Веб-сервер запущен на порту {port}")

# ==========================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ==========================================

def is_high_impact(impact_value) -> bool:
    if not impact_value:
        return False
    val = str(impact_value).strip().lower()
    high_keywords = ["high", "red", "3", "high impact", "красный", "высокая"]
    return any(k in val for k in high_keywords)

def normalize_currency(currency_value) -> str:
    if not currency_value:
        return "GLOBAL"
    return str(currency_value).strip().upper()

def parse_event_date(event: dict) -> datetime | None:
    raw_date = event.get("date") or event.get("time") or event.get("datetime") or event.get("timestamp")
    if not raw_date:
        return None

    if isinstance(raw_date, (int, float)):
        return datetime.fromtimestamp(raw_date, tz=timezone.utc)

    if isinstance(raw_date, str):
        raw_date = raw_date.replace("Z", "+00:00")
        formats = [
            "%Y-%m-%d%z",
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%dT%H:%M:%S.%f%z",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d",
            "%m/%d/%Y %H:%M:%S",
        ]
        for fmt in formats:
            try:
                dt = datetime.strptime(raw_date, fmt)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            except ValueError:
                continue
    return None

async def fetch_economic_news() -> list:
    """Запрашивает экономический календарь."""
    url = "https://nss.forexfactory.com/calendar/monthly.json"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=15) as response:
                if response.status == 200:
                    data = await response.json()
                    logger.info(f"[NEWS API] Успешно получено элементов: {len(data) if isinstance(data, list) else 0}")
                    return data if isinstance(data, list) else []
                else:
                    logger.error(f"[NEWS API] Ошибка сервера: HTTP {response.status}")
                    return []
    except Exception as e:
        logger.error(f"[NEWS API] Исключение при запросе: {e}")
        return []

def process_and_filter_news(raw_events: list, start_dt: datetime, end_dt: datetime) -> list:
    filtered_events = []

    for event in raw_events:
        impact = event.get("impact") or event.get("importance") or event.get("severity") or ""
        currency = event.get("currency") or event.get("country") or event.get("symbol") or ""
        title = event.get("title") or event.get("name") or event.get("event") or "Экономическое событие"

        if is_high_impact(impact):
            event_date = parse_event_date(event)
            if event_date and (start_dt <= event_date <= end_dt):
                filtered_events.append({
                    "title": title,
                    "currency": normalize_currency(currency),
                    "impact": "High",
                    "date": event_date,
                    "forecast": event.get("forecast", "-"),
                    "previous": event.get("previous", "-")
                })

    filtered_events.sort(key=lambda x: x["date"])
    return filtered_events

async def get_target_channel(ctx):
    """Гарантированно находит целевой канал или ветку по NEWS_CHANNEL_ID."""
    try:
        # Сначала пробуем получить из API напрямую (надежно для веток)
        channel = await bot.fetch_channel(NEWS_CHANNEL_ID)
        return channel
    except Exception as e:
        logger.warning(f"Не удалось получить канал {NEWS_CHANNEL_ID} через fetch_channel: {e}")
        channel = bot.get_channel(NEWS_CHANNEL_ID)
        return channel if channel else ctx.channel

# ==========================================
# КОМАНДЫ БОТА
# ==========================================

@bot.event
async def on_ready():
    logger.info(f"✅ Бот {bot.user} успешно запущен!")
    logger.info(f"📢 Канал новостей ID: {NEWS_CHANNEL_ID}")

@bot.command(name="test_weekly")
async def test_weekly(ctx):
    target_channel = await get_target_channel(ctx)
    if ctx.channel.id != target_channel.id:
        await ctx.send(f"🔄 Отправляю недельный отчёт в целевой канал <#{target_channel.id}>...")

    now = datetime.now(timezone.utc)
    start_of_week = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    end_of_week = start_of_week + timedelta(days=6, hours=23, minutes=59, seconds=59)

    raw_events = await fetch_economic_news()
    events = process_and_filter_news(raw_events, start_of_week, end_of_week)

    if not events:
        await target_channel.send("📊 На эту неделю важных (красных) новостей не найдено.")
        return

    embed = discord.Embed(
        title="📅 Важные (High Impact) новости на эту неделю",
        color=discord.Color.red(),
        timestamp=now
    )

    for ev in events:
        time_str = ev["date"].strftime("%d.%m (%a) в %H:%M UTC")
        embed.add_field(
            name=f"🔴 [{ev['currency']}] {ev['title']}",
            value=f"⏰ **Время:** {time_str}\n📊 **Прогноз:** {ev['forecast']} | **Пред:** {ev['previous']}",
            inline=False
        )

    await target_channel.send(embed=embed)

@bot.command(name="test_daily")
async def test_daily(ctx):
    target_channel = await get_target_channel(ctx)
    if ctx.channel.id != target_channel.id:
        await ctx.send(f"🔄 Отправляю дневной отчёт в целевой канал <#{target_channel.id}>...")

    now = datetime.now(timezone.utc)
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end_of_day = now.replace(hour=23, minute=59, second=59, microsecond=0)

    raw_events = await fetch_economic_news()
    events = process_and_filter_news(raw_events, start_of_day, end_of_day)

    if not events:
        await target_channel.send("📊 На сегодня важных (красных) новостей не найдено.")
        return

    embed = discord.Embed(
        title=f"🔴 Важные новости на {start_of_day.strftime('%d.%m.%Y')}",
        color=discord.Color.red(),
        timestamp=now
    )

    for ev in events:
        time_str = ev["date"].strftime("%H:%M UTC")
        embed.add_field(
            name=f"🔴 [{ev['currency']}] {ev['title']}",
            value=f"⏰ **Время:** {time_str}\n📊 **Прогноз:** {ev['forecast']} | **Пред:** {ev['previous']}",
            inline=False
        )

    await target_channel.send(embed=embed)

@bot.command(name="test_30min")
async def test_30min(ctx):
    target_channel = await get_target_channel(ctx)
    if ctx.channel.id != target_channel.id:
        await ctx.send(f"🔄 Отправляю анонс в целевой канал <#{target_channel.id}>...")

    now = datetime.now(timezone.utc)
    future_window = now + timedelta(minutes=40)

    raw_events = await fetch_economic_news()
    events = process_and_filter_news(raw_events, now, future_window)

    if not events:
        await target_channel.send("⏳ В ближайшие 30–40 минут важных новостей не ожидается.")
        return

    for ev in events:
        embed = discord.Embed(
            title="🚨 ВНИМАНИЕ: Новость через 30 минут!",
            description=f"**[{ev['currency']}] {ev['title']}**",
            color=discord.Color.gold(),
            timestamp=now
        )
        embed.add_field(name="Время выхода", value=f"⏰ {ev['date'].strftime('%H:%M UTC')}", inline=True)
        embed.add_field(name="Прогноз / Пред", value=f"{ev['forecast']} / {ev['previous']}", inline=True)
        await target_channel.send(embed=embed)

# ==========================================
# ТОЧКА ВХОДА
# ==========================================

async def main():
    await start_dummy_server()
    if TOKEN == "YOUR_DISCORD_BOT_TOKEN" or not TOKEN:
        logger.error("❌ ОШИБКА: Укажите токен бота в переменной DISCORD_TOKEN!")
        return
    await bot.start(TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
