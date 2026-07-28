import asyncio
import logging
import os
import time
from datetime import datetime, timedelta, timezone
import aiohttp
import discord
from discord.ext import commands, tasks
from aiohttp import web

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("LegacyBot")

TOKEN = os.getenv("DISCORD_TOKEN", "YOUR_DISCORD_BOT_TOKEN")

# ID каналов Discord
NEWS_CHANNEL_ID = 1528319066513604688      # Ветка для экономических новостей
EVENTS_CHANNEL_ID = 1528506824687485118    # Ветка для мероприятий / стримов

# Московское время (UTC+3)
MSK_TZ = timezone(timedelta(hours=3))

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.guild_scheduled_events = True  # Чтение мероприятий Discord

bot = commands.Bot(command_prefix="!", intents=intents)

sent_30m_alerts = set()
sent_30m_events = set()

# Глобальный кэш для защиты от Rate Limit (429)
NEWS_CACHE = {
    "data": [],
    "last_fetch": 0
}
CACHE_TTL_SECONDS = 1800  # Обновлять новости из API не чаще чем раз в 30 минут

# Список рабочих источников новостей
NEWS_API_URLS = [
    "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
    "https://raw.githubusercontent.com/man-c/forex_factory_calendar/main/calendar.json"
]

CURRENCY_MAP = {
    "USD": {"flag": "🇺🇸", "assets": "**EUR/USD**, **GBP/USD**, **USD/JPY**, **XAU/USD**, **DXY**, **NAS100**"},
    "EUR": {"flag": "🇪🇺", "assets": "**EUR/USD**, **EUR/GBP**, **EUR/JPY**, **DAX40**"},
    "GBP": {"flag": "🇬🇧", "assets": "**GBP/USD**, **EUR/GBP**, **GBP/JPY**"},
    "JPY": {"flag": "🇯🇵", "assets": "**USD/JPY**, **EUR/JPY**, **GBP/JPY**"},
    "CAD": {"flag": "🇨🇦", "assets": "**USD/CAD**, **CAD/JPY**, **WTI Oil**"},
    "AUD": {"flag": "🇦🇺", "assets": "**AUD/USD**, **AUD/JPY**, **Gold**"},
    "NZD": {"flag": "🇳🇿", "assets": "**NZD/USD**, **NZD/JPY**"},
    "CHF": {"flag": "🇨🇭", "assets": "**USD/CHF**, **EUR/CHF**"},
    "CNY": {"flag": "🇨🇳", "assets": "**USD/CNH**, **Commodities**"},
}

DAYS_RU = {
    0: "Понедельник", 1: "Вторник", 2: "Среда",
    3: "Четверг", 4: "Пятница", 5: "Суббота", 6: "Воскресенье"
}

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

def get_currency_info(currency_code: str) -> dict:
    code = str(currency_code).strip().upper()
    return CURRENCY_MAP.get(code, {"flag": "🌐", "assets": f"**{code}**"})

def is_high_impact(impact_value) -> bool:
    if not impact_value:
        return False
    val = str(impact_value).strip().lower()
    return val in ["high", "red", "3", "high impact", "красный", "высокая", "3.0"]

def parse_event_date(event: dict) -> datetime | None:
    raw_date = event.get("date") or event.get("time") or event.get("datetime") or event.get("timestamp")
    if not raw_date:
        return None

    if isinstance(raw_date, (int, float)):
        return datetime.fromtimestamp(raw_date, tz=timezone.utc)

    if isinstance(raw_date, str):
        raw_date = raw_date.strip()
        if raw_date.endswith("Z"):
            raw_date = raw_date[:-1] + "+00:00"

        formats = [
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%dT%H:%M:%S.%f%z",
            "%Y-%m-%d %H:%M:%S%z",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d",
        ]
        
        for fmt in formats:
            try:
                dt = datetime.strptime(raw_date, fmt)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            except ValueError:
                continue
                
        try:
            dt = datetime.fromisoformat(raw_date)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            pass

    return None

async def fetch_economic_news(force_refresh: bool = False) -> list:
    """Получает новости с использованием кэша, чтобы избежать HTTP 429."""
    now = time.time()
    
    # Если кэш ещё свежий и не запрошен принудительный сброс
    if not force_refresh and NEWS_CACHE["data"] and (now - NEWS_CACHE["last_fetch"] < CACHE_TTL_SECONDS):
        logger.info("[NEWS API] Использование данных из кэша")
        return NEWS_CACHE["data"]

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*"
    }
    
    async with aiohttp.ClientSession() as session:
        for url in NEWS_API_URLS:
            try:
                async with session.get(url, headers=headers, timeout=12) as response:
                    if response.status == 200:
                        data = await response.json(content_type=None)
                        if isinstance(data, list) and len(data) > 0:
                            logger.info(f"[NEWS API] Успешно получено {len(data)} событий из {url}")
                            NEWS_CACHE["data"] = data
                            NEWS_CACHE["last_fetch"] = now
                            return data
                        else:
                            logger.warning(f"[NEWS API] Источник {url} вернул пустой список.")
                    else:
                        logger.warning(f"[NEWS API] Источник {url} вернул статус {response.status}")
            except Exception as e:
                logger.error(f"[NEWS API] Ошибка запроса к {url}: {e}")
                
    # Если все API упали, но в кэше хоть что-то есть — отдаём старый кэш
    if NEWS_CACHE["data"]:
        logger.warning("[NEWS API] Не удалось обновить данные, возвращаем устаревший кэш.")
        return NEWS_CACHE["data"]

    return []

def process_and_filter_news(raw_events: list, start_dt: datetime, end_dt: datetime) -> list:
    filtered_events = []
    logger.info(f"[DEBUG] Всего получено сырых событий от API: {len(raw_events)}")

    for event in raw_events:
        impact = str(event.get("impact") or event.get("importance") or event.get("level") or "").strip().lower()
        currency = str(event.get("country") or event.get("currency") or event.get("symbol") or "GLOBAL").strip().upper()
        title = event.get("title") or event.get("name") or event.get("event") or "Экономическое событие"

        is_high = is_high_impact(impact)
        event_date = parse_event_date(event)
        
        if is_high and event_date:
            if start_dt <= event_date <= end_dt:
                c_info = get_currency_info(currency)
                filtered_events.append({
                    "id": f"{currency}_{title}_{event_date.timestamp()}",
                    "title": title,
                    "currency": currency,
                    "flag": c_info["flag"],
                    "assets": c_info["assets"],
                    "impact": "🔴 HIGH",
                    "date": event_date,
                    "forecast": event.get("forecast", "-"),
                    "previous": event.get("previous", "-")
                })

    logger.info(f"[DEBUG] Успешно отфильтровано High-событий: {len(filtered_events)}")
    filtered_events.sort(key=lambda x: x["date"])
    return filtered_events

async def get_channel_by_id(channel_id: int):
    try:
        return await bot.fetch_channel(channel_id)
    except Exception as e:
        logger.warning(f"Ошибка получения канала {channel_id}: {e}")
        return bot.get_channel(channel_id)

# ==========================================
# ПОСТРОЕНИЕ EMBED-СООБЩЕНИЙ
# ==========================================

def build_weekly_embed(events: list, start_msk: datetime, end_msk: datetime) -> discord.Embed:
    embed = discord.Embed(
        title="📊 ЕЖЕНЕДЕЛЬНЫЙ ЭКОНОМИЧЕСКИЙ КАЛЕНДАРЬ FOREX",
        description=f"📅 **Неделя с {start_msk.strftime('%d.%m.%Y')} по {end_msk.strftime('%d.%m.%Y')}**",
        color=discord.Color.blue(),
        timestamp=start_msk
    )

    if not events:
        embed.add_field(name="Информация", value="На этой неделе важных новостей (🔴 HIGH) не ожидается.", inline=False)
        embed.set_footer(text="Legacy Community | Weekly Analytics", icon_url=bot.user.display_avatar.url)
        return embed

    current_day = None
    day_blocks = []

    for ev in events:
        ev_msk = ev["date"].astimezone(MSK_TZ)
        day_key = ev_msk.weekday()

        if day_key != current_day:
            if day_blocks:
                embed.add_field(
                    name=f"📅 {DAYS_RU[current_day]} ({day_blocks[0]['date_str']})",
                    value="\n".join([b["text"] for b in day_blocks]),
                    inline=False
                )
                day_blocks = []
            current_day = day_key

        time_str = ev_msk.strftime("%H:%M")
        block_text = (
            f"{ev['flag']} **{ev['currency']}** | 🕘 **{time_str} МСК** — {ev['title']} ({ev['impact']})\n"
            f"└ 🎯 Активы: {ev['assets']}\n"
            f"└ 📊 Прогноз: `{ev['forecast']}` | Пред: `{ev['previous']}`"
        )
        day_blocks.append({"date_str": ev_msk.strftime("%d.%m"), "text": block_text})

    if day_blocks:
        embed.add_field(
            name=f"📅 {DAYS_RU[current_day]} ({day_blocks[0]['date_str']})",
            value="\n".join([b["text"] for b in day_blocks]),
            inline=False
        )

    embed.set_footer(text="Legacy Community | Weekly Analytics", icon_url=bot.user.display_avatar.url)
    return embed

def build_daily_embed(events: list) -> discord.Embed:
    now_msk = datetime.now(MSK_TZ)
    day_name = DAYS_RU[now_msk.weekday()]
    date_str = now_msk.strftime("%d.%m.%Y")

    embed = discord.Embed(
        title="📊 ЕЖЕДНЕВНЫЙ ЭКОНОМИЧЕСКИЙ КАЛЕНДАРЬ FOREX",
        description=f"📅 **{day_name} ({date_str})**",
        color=discord.Color.gold(),
        timestamp=now_msk
    )

    if not events:
        embed.add_field(name="Информация", value="На сегодня важных новостей (🔴 HIGH) не найдено.", inline=False)
        embed.set_footer(text="Legacy Community | Daily Analytics", icon_url=bot.user.display_avatar.url)
        return embed

    for ev in events:
        ev_msk = ev["date"].astimezone(MSK_TZ)
        time_str = ev_msk.strftime("%H:%M")
        field_name = f"{ev['flag']} **{ev['currency']}** | 🕘 {time_str} МСК — {ev['title']}"
        field_value = (
            f"🎯 Активы: {ev['assets']}\n"
            f"📊 Прогноз: `{ev['forecast']}` | Пред: `{ev['previous']}`"
        )
        embed.add_field(name=field_name, value=field_value, inline=False)

    embed.set_footer(text="Legacy Community | Daily Analytics", icon_url=bot.user.display_avatar.url)
    return embed

def build_30m_news_embed(ev: dict) -> discord.Embed:
    now_msk = datetime.now(MSK_TZ)
    ev_msk = ev["date"].astimezone(MSK_TZ)
    time_str = ev_msk.strftime("%H:%M")

    embed = discord.Embed(
        title="🚨 ВНИМАНИЕ: ВАЖНАЯ НОВОСТЬ ЧЕРЕЗ 30 МИНУТ!",
        description=f"⚠️ High Impact Event. Ожидается высокая волатильность!",
        color=discord.Color.red(),
        timestamp=now_msk
    )

    field_name = f"{ev['flag']} **{ev['currency']}** | 🕘 {time_str} МСК — {ev['title']}"
    field_value = (
        f"🎯 Активы: {ev['assets']}\n"
        f"📊 Прогноз: `{ev['forecast']}` | Пред: `{ev['previous']}`"
    )
    embed.add_field(name=field_name, value=field_value, inline=False)
    embed.set_footer(text="Legacy Community | Macro Alerts", icon_url=bot.user.display_avatar.url)
    return embed

def build_event_30m_embed(event_name: str, event_time_msk: datetime, description: str = None, location: str = "OPEN HALL!", event_url: str = None) -> discord.Embed:
    day_name = DAYS_RU[event_time_msk.weekday()]
    date_str = event_time_msk.strftime("%d.%m")
    time_str = event_time_msk.strftime("%H:%M")

    embed = discord.Embed(
        title=f"🎙️ Напоминание о **{event_name.upper()}** | До старта 30 минут!",
        color=discord.Color.gold(),
        timestamp=event_time_msk
    )

    info_text = f"📅 {day_name} ({date_str}) | 🕘 {time_str} МСК"
    if description and description.strip():
        info_text += f"\n\n📌 Тема: {description.strip()}"

    embed.description = info_text
    loc_text = location.strip() if location and location.strip() else "OPEN HALL!"
    
    embed_content = f"🎧 Локация — {loc_text}"
    if event_url:
        embed_content += f"\n\nСсылка на брифинг 👇\n{event_url}"

    embed.add_field(name="", value=embed_content, inline=False)
    embed.set_footer(text=f"Legacy Community | Stream Alert {event_time_msk.strftime('%d.%m.%Y')}", icon_url=bot.user.display_avatar.url)
    return embed

# ==========================================
# ФОНОВЫЙ ТАЙМЕР (SCHEDULED TASKS)
# ==========================================

@tasks.loop(minutes=1)
async def schedule_checker():
    now_utc = datetime.now(timezone.utc)
    now_msk = now_utc.astimezone(MSK_TZ)

    # 1. Понедельник 08:00 МСК -> Еженедельный календарь
    if now_msk.weekday() == 0 and now_msk.hour == 8 and now_msk.minute == 0:
        news_channel = await get_channel_by_id(NEWS_CHANNEL_ID)
        if news_channel:
            start_week_msk = now_msk.replace(hour=0, minute=0, second=0, microsecond=0)
            end_week_msk = start_week_msk + timedelta(days=6, hours=23, minutes=59)
            raw = await fetch_economic_news()
            events = process_and_filter_news(raw, start_week_msk.astimezone(timezone.utc), end_week_msk.astimezone(timezone.utc))
            embed = build_weekly_embed(events, start_week_msk, end_week_msk)
            await news_channel.send(embed=embed)

    # 2. Каждый день 09:00 МСК -> Ежедневный календарь
    if now_msk.hour == 9 and now_msk.minute == 0:
        news_channel = await get_channel_by_id(NEWS_CHANNEL_ID)
        if news_channel:
            start_day = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
            end_day = now_utc.replace(hour=23, minute=59, second=59)
            raw = await fetch_economic_news()
            events = process_and_filter_news(raw, start_day, end_day)
            embed = build_daily_embed(events)
            await news_channel.send(embed=embed)

    # 3. За 30 минут до HIGH новостей -> С @everyone
    news_channel = await get_channel_by_id(NEWS_CHANNEL_ID)
    if news_channel:
        look_start = now_utc + timedelta(minutes=28)
        look_end = now_utc + timedelta(minutes=32)
        raw = await fetch_economic_news()
        upcoming = process_and_filter_news(raw, look_start, look_end)

        for ev in upcoming:
            if ev["id"] not in sent_30m_alerts:
                sent_30m_alerts.add(ev["id"])
                embed = build_30m_news_embed(ev)
                await news_channel.send(content="@everyone", embed=embed)

    # 4. За 30 минут до мероприятий Discord -> С @everyone и ссылкой внутри Embed
    for guild in bot.guilds:
        try:
            scheduled_events = await guild.fetch_scheduled_events()
            for ev in scheduled_events:
                if ev.start_time:
                    time_diff = (ev.start_time - now_utc).total_seconds()
                    if 1680 <= time_diff <= 1920 and ev.id not in sent_30m_events:
                        sent_30m_events.add(ev.id)
                        events_channel = await get_channel_by_id(EVENTS_CHANNEL_ID)
                        if events_channel:
                            ev_msk = ev.start_time.astimezone(MSK_TZ)
                            loc_name = ev.location if ev.location else "OPEN HALL!"
                            event_url = f"https://discord.com/events/{guild.id}/{ev.id}"
                            
                            embed = build_event_30m_embed(
                                event_name=ev.name,
                                event_time_msk=ev_msk,
                                description=ev.description,
                                location=loc_name,
                                event_url=event_url
                            )
                            
                            await events_channel.send(content="@everyone", embed=embed)
        except Exception as e:
            logger.error(f"Ошибка проверки мероприятий сервера: {e}")

# ==========================================
# КОМАНДЫ ДЛЯ ТЕСТИРОВАНИЯ
# ==========================================

@bot.event
async def on_ready():
    logger.info(f"✅ Бот {bot.user} запущен!")
    if not schedule_checker.is_running():
        schedule_checker.start()

@bot.command(name="test_weekly")
async def test_weekly(ctx):
    """Тест еженедельного календаря"""
    news_channel = await get_channel_by_id(NEWS_CHANNEL_ID)
    target_channel = news_channel if news_channel else ctx.channel

    now_msk = datetime.now(MSK_TZ)
    start_week_msk = now_msk.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=now_msk.weekday())
    end_week_msk = start_week_msk + timedelta(days=6, hours=23, minutes=59, seconds=59)

    raw = await fetch_economic_news(force_refresh=True)
    events = process_and_filter_news(
        raw, 
        start_week_msk.astimezone(timezone.utc), 
        end_week_msk.astimezone(timezone.utc)
    )
    
    embed = build_weekly_embed(events, start_week_msk, end_week_msk)
    await target_channel.send(embed=embed)

@bot.command(name="test_daily")
async def test_daily(ctx):
    """Тест ежедневного календаря"""
    news_channel = await get_channel_by_id(NEWS_CHANNEL_ID)
    target_channel = news_channel if news_channel else ctx.channel

    now_utc = datetime.now(timezone.utc)
    start_day = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    end_day = now_utc.replace(hour=23, minute=59, second=59)
    raw = await fetch_economic_news()
    events = process_and_filter_news(raw, start_day, end_day)
    embed = build_daily_embed(events)
    await target_channel.send(embed=embed)

@bot.command(name="test_event30m")
async def test_event30m(ctx, event_name: str = "Morning Briefing by Castro", *, description: str = None):
    """Тест анонса мероприятия со ссылкой внутри Embed."""
    target_channel = await get_channel_by_id(EVENTS_CHANNEL_ID)
    if not target_channel:
        target_channel = ctx.channel

    now_msk = datetime.now(MSK_TZ)
    event_time_msk = now_msk + timedelta(minutes=30)
    fake_event_url = f"https://discord.com/events/{ctx.guild.id}/123456789012345678"

    embed = build_event_30m_embed(
        event_name=event_name,
        event_time_msk=event_time_msk,
        description=description,
        location="OPEN HALL!",
        event_url=fake_event_url
    )

    await target_channel.send(content="@everyone", embed=embed)

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
