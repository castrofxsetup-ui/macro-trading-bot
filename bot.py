import os
import discord
from discord.ext import commands, tasks
import httpx
from datetime import datetime, timedelta, time
import pytz

# ==========================================
# НАСТРОЙКИ БОТА И КОНСТАНТЫ
# ==========================================

# Токен Discord (рекомендуется передавать через переменную окружения RENDER/ENV)
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "YOUR_BOT_TOKEN_HERE")

# ID канала для публикации новостей
NEWS_CHANNEL_ID = 1528319066513604688

# Часовой пояс МСК
MSK_TZ = pytz.timezone("Europe/Moscow")

# Словари для форматирования дат на русском
DAYS_RU = {
    0: "понедельник",
    1: "вторник",
    2: "среда",
    3: "четверг",
    4: "пятница",
    5: "суббота",
    6: "воскресенье"
}

MONTHS_RU = [
    "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"
]

COUNTRY_FLAGS = {
    "USD": "🇺🇸", "EUR": "🇪🇺", "GBP": "🇬🇧", "JPY": "🇯🇵",
    "AUD": "🇦🇺", "CAD": "🇨🇦", "CHF": "🇨🇭", "NZD": "🇳🇿", "CNY": "🇨🇳"
}

# Инициализация бота
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ==========================================
# ВСПАМОГАТЕЛЬНЫЕ ФУНКЦИИ ПАРСИНГА
# ==========================================

async def get_raw_calendar_data():
    """Получает данные экономического календаря из стабильного JSON API"""
    url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json"
    }
    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            resp = await client.get(url, headers=headers, timeout=10.0)
            if resp.status_code == 200:
                return resp.json()
    except Exception as e:
        print(f"[News API Error] {e}")
    return []

def convert_to_msk(utc_date_str: str) -> tuple[datetime, str]:
    """Конвертирует UTC строку в объект datetime МСК и форматированное время HH:MM"""
    if not utc_date_str:
        now = datetime.now(MSK_TZ)
        return now, now.strftime("%H:%M")
    try:
        dt_utc = datetime.fromisoformat(utc_date_str.replace("Z", "+00:00"))
        dt_msk = dt_utc.astimezone(MSK_TZ)
        return dt_msk, dt_msk.strftime("%H:%M")
    except Exception:
        now = datetime.now(MSK_TZ)
        return now, now.strftime("%H:%M")

# ==========================================
# ГЕНЕРАЦИЯ EMBED-СООБЩЕНИЙ
# ==========================================

async def generate_daily_news_embed() -> discord.Embed:
    """Генерация дневного календаря (ПН - ПТ)"""
    data = await get_raw_calendar_data()
    now_msk = datetime.now(MSK_TZ)
    today_str = now_msk.strftime("%Y-%m-%d")

    day_name_ru = DAYS_RU[now_msk.weekday()].capitalize()
    month_name_ru = MONTHS_RU[now_msk.month - 1]

    embed = discord.Embed(
        title="📊 Ежедневный экономический календарь Forex",
        description=f"*Запланированные мероприятия — {day_name_ru}, {now_msk.day}, {month_name_ru}*\n",
        color=discord.Color.blue()
    )

    has_events = False
    for item in data:
        utc_date = item.get("date", "")
        dt_msk, time_msk = convert_to_msk(utc_date)

        if dt_msk.strftime("%Y-%m-%d") == today_str:
            impact = item.get("impact", "")
            if impact in ["High", "Medium"]:
                country = item.get("country", "USD").upper()
                title = item.get("title", "Event")
                flag = COUNTRY_FLAGS.get(country, "🌐")
                badge = "🔴 HIGH" if impact == "High" else "🟠 MEDIUM"

                embed.add_field(
                    name=f"{flag} {country}",
                    value=f"🕘 {time_msk} МСК — {title}\n{badge}",
                    inline=False
                )
                has_events = True

    if not has_events:
        embed.description += "\n📅 *На сегодня важных событий не запланировано.*"

    return embed

async def generate_weekly_news_embed() -> discord.Embed:
    """Генерация недельного календаря (ПОНЕДЕЛЬНИК)"""
    data = await get_raw_calendar_data()
    now_msk = datetime.now(MSK_TZ)

    monday = now_msk - timedelta(days=now_msk.weekday())
    friday = monday + timedelta(days=4)
    month_name_ru = MONTHS_RU[monday.month - 1]

    embed = discord.Embed(
        title="🗓️ Экономический календарь на неделю",
        description=f"*📅 ПН, {month_name_ru}, {monday.strftime('%d.%m')}-{friday.strftime('%d.%m')}*\n",
        color=discord.Color.gold()
    )

    has_events = False
    for item in data:
        utc_date = item.get("date", "")
        dt_msk, time_msk = convert_to_msk(utc_date)

        if monday.date() <= dt_msk.date() <= friday.date():
            impact = item.get("impact", "")
            if impact in ["High", "Medium"]:
                country = item.get("country", "USD").upper()
                title = item.get("title", "Event")
                flag = COUNTRY_FLAGS.get(country, "🌐")
                badge = "🔴 HIGH" if impact == "High" else "🟠 MEDIUM"

                event_day_ru = DAYS_RU[dt_msk.weekday()][:2].capitalize()
                event_month_ru = MONTHS_RU[dt_msk.month - 1]

                embed.add_field(
                    name=f"{flag} {country}",
                    value=f"🕘 {event_day_ru}, {time_msk}, {event_month_ru} — {title}\n{badge}",
                    inline=False
                )
                has_events = True

    if not has_events:
        embed.description += "\n📅 *На эту неделю важных новостей не запланировано.*"

    return embed

async def generate_breaking_news_embed(country: str, title: str, dt_msk: datetime) -> discord.Embed:
    """Генерация внепланового анонса"""
    day_en = dt_msk.strftime("%A")
    time_msk = dt_msk.strftime("%H:%M")
    flag = COUNTRY_FLAGS.get(country.upper(), "🌐")

    embed = discord.Embed(
        title="⚠️ Внимание! Внеплановый анонс!",
        description=f"🕘 {day_en} {time_msk} МСК\n",
        color=discord.Color.red()
    )

    embed.add_field(
        name=f"{flag} {country.upper()} — {title}",
        value="🔴 HIGH",
        inline=False
    )
    
    embed.set_footer(text="⚠️ Будьте аккуратны!")
    return embed

# ==========================================
# ФОНОВЫЕ ЗАДАЧИ АВТООТПРАВКИ (CRON/TASKS)
# ==========================================

# 1. Еженедельный календарь (Каждый Понедельник в 08:00 МСК)
@tasks.loop(time=time(hour=8, minute=0, tzinfo=MSK_TZ))
async def weekly_news_task():
    now_msk = datetime.now(MSK_TZ)
    if now_msk.weekday() == 0:  # 0 = Понедельник
        channel = bot.get_channel(NEWS_CHANNEL_ID)
        if channel:
            embed = await generate_weekly_news_embed()
            await channel.send(embed=embed)

# 2. Ежедневный календарь (Понедельник - Пятница в 09:00 МСК)
@tasks.loop(time=time(hour=9, minute=0, tzinfo=MSK_TZ))
async def daily_news_task():
    now_msk = datetime.now(MSK_TZ)
    if now_msk.weekday() < 5:  # 0..4 = Понедельник..Пятница
        channel = bot.get_channel(NEWS_CHANNEL_ID)
        if channel:
            embed = await generate_daily_news_embed()
            await channel.send(embed=embed)

# ==========================================
# КОМАНДЫ БОТА И ИНИЦИАЛИЗАЦИЯ
# ==========================================

@bot.command(name="test_daily")
@commands.has_permissions(administrator=True)
async def test_daily(ctx):
    """Команда для ручной проверки дневного календаря"""
    embed = await generate_daily_news_embed()
    await ctx.send(embed=embed)

@bot.command(name="test_weekly")
@commands.has_permissions(administrator=True)
async def test_weekly(ctx):
    """Команда для ручной проверки недельного календаря"""
    embed = await generate_weekly_news_embed()
    await ctx.send(embed=embed)

@bot.command(name="breaking_news")
@commands.has_permissions(administrator=True)
async def breaking_news(ctx, country: str, *, title: str):
    """Команда экстренной новости: !breaking_news USD Неплановое заседание ФРС"""
    now_msk = datetime.now(MSK_TZ)
    embed = await generate_breaking_news_embed(country, title, now_msk)
    channel = bot.get_channel(NEWS_CHANNEL_ID)
    if channel:
        await channel.send(embed=embed)
        await ctx.message.add_reaction("✅")

@bot.event
async def on_ready():
    print(f"✅ Бот {bot.user} успешно запущен!")
    print(f"📢 Канал новостей ID: {NEWS_CHANNEL_ID}")
    
    # Запуск фоновых задач
    if not weekly_news_task.is_running():
        weekly_news_task.start()
    if not daily_news_task.is_running():
        daily_news_task.start()

if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
