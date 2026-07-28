import os
import discord
from discord.ext import commands, tasks
import httpx
from datetime import datetime, timedelta, time
import pytz

# ==========================================
# НАСТРОЙКИ БОТА И КОНСТАНТЫ
# ==========================================

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "YOUR_BOT_TOKEN_HERE")
NEWS_CHANNEL_ID = 1528319066513604688
MSK_TZ = pytz.timezone("Europe/Moscow")

notified_events = set()

DAYS_RU = {
    0: "понедельник", 1: "вторник", 2: "среда",
    3: "четверг", 4: "пятница", 5: "суббота", 6: "воскресенье"
}

MONTHS_RU = [
    "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"
]

COUNTRY_FLAGS = {
    "USD": "🇺🇸", "EUR": "🇪🇺", "GBP": "🇬🇧", "JPY": "🇯🇵",
    "AUD": "🇦🇺", "CAD": "🇨🇦", "CHF": "🇨🇭", "NZD": "🇳🇿", "CNY": "🇨🇳"
}

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ==========================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ==========================================

async def get_raw_calendar_data():
    url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
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
    data = await get_raw_calendar_data()
    now_msk = datetime.now(MSK_TZ)
    today_str = now_msk.strftime("%Y-%m-%d")

    day_name_ru = DAYS_RU[now_msk.weekday()].capitalize()
    month_name_ru = MONTHS_RU[now_msk.month - 1]

    embed = discord.Embed(
        title="📊 Ежедневный экономический календарь Forex",
        description=f"*Запланированные мероприятия — {day_name_ru}, {now_msk.day}, {month_name_ru}*\n\n",
        color=discord.Color.blue()
    )

    events_list = []
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

                events_list.append(f"{flag} **{country}** | 🕘 `{time_msk} МСК` — **{title}** ({badge})")

    if events_list:
        chunk_size = 10
        for i in range(0, len(events_list), chunk_size):
            chunk = events_list[i:i + chunk_size]
            embed.add_field(
                name="События" if i == 0 else "Продолжение",
                value="\n\n".join(chunk),
                inline=False
            )
    else:
        embed.description += "📅 *На сегодня важных событий не запланировано.*"

    return embed

async def generate_weekly_news_embed() -> discord.Embed:
    data = await get_raw_calendar_data()
    now_msk = datetime.now(MSK_TZ)

    monday = now_msk - timedelta(days=now_msk.weekday())
    sunday = monday + timedelta(days=6)
    month_name_ru = MONTHS_RU[monday.month - 1]

    embed = discord.Embed(
        title="🗓️ Экономический календарь на неделю",
        description=f"*📅 {month_name_ru}, {monday.strftime('%d.%m')} — {sunday.strftime('%d.%m')}*\n",
        color=discord.Color.gold()
    )

    grouped_events = {i: [] for i in range(7)}

    for item in data:
        impact = item.get("impact", "")
        if impact in ["High", "Medium"]:
            utc_date = item.get("date", "")
            dt_msk, time_msk = convert_to_msk(utc_date)

            day_idx = dt_msk.weekday()
            country = item.get("country", "USD").upper()
            title = item.get("title", "Event")
            flag = COUNTRY_FLAGS.get(country, "🌐")
            badge = "🔴 HIGH" if impact == "High" else "🟠 MEDIUM"

            grouped_events[day_idx].append(
                f"{flag} **{country}** | 🕘 `{time_msk}` — {title} ({badge})"
            )

    has_events = False
    for day_idx in range(7):
        events = grouped_events[day_idx]
        if events:
            has_events = True
            current_day_date = monday + timedelta(days=day_idx)
            day_title = f"📅 {DAYS_RU[day_idx].capitalize()} ({current_day_date.strftime('%d.%m')})"
            
            embed.add_field(
                name=day_title,
                value="\n".join(events[:10]),
                inline=False
            )

    if not has_events:
        embed.description += "\n📅 *На эту неделю важных новостей не запланировано.*"

    return embed

def generate_30min_warning_embed(country: str, title: str, time_msk: str) -> discord.Embed:
    flag = COUNTRY_FLAGS.get(country.upper(), "🌐")

    embed = discord.Embed(
        title="⚠️ Будьте осторожны!",
        color=discord.Color.red()
    )

    embed.add_field(
        name=f"{flag} {country.upper()} — {title}",
        value=f"🕘 **{time_msk} МСК**\n🔴 **HIGH**",
        inline=False
    )

    embed.set_footer(text="⌛ Публикация через 30 минут!")
    return embed

# ==========================================
# ФОНОВЫЕ ЗАДАЧИ (TASKS)
# ==========================================

@tasks.loop(time=time(hour=8, minute=0, tzinfo=MSK_TZ))
async def weekly_news_task():
    now_msk = datetime.now(MSK_TZ)
    if now_msk.weekday() == 0:
        channel = bot.get_channel(NEWS_CHANNEL_ID)
        if channel:
            embed = await generate_weekly_news_embed()
            await channel.send(embed=embed)

@tasks.loop(time=time(hour=9, minute=0, tzinfo=MSK_TZ))
async def daily_news_task():
    now_msk = datetime.now(MSK_TZ)
    if now_msk.weekday() < 5:
        channel = bot.get_channel(NEWS_CHANNEL_ID)
        if channel:
            embed = await generate_daily_news_embed()
            await channel.send(embed=embed)

@tasks.loop(minutes=1)
async def news_30min_notifier_task():
    data = await get_raw_calendar_data()
    now_msk = datetime.now(MSK_TZ)

    for item in data:
        if item.get("impact") == "High":
            utc_date = item.get("date", "")
            dt_msk, time_msk = convert_to_msk(utc_date)

            time_diff = (dt_msk - now_msk).total_seconds() / 60.0
            event_id = f"{dt_msk.strftime('%Y%m%d_%H%M')}_{item.get('country')}_{item.get('title')}"

            if 29.0 <= time_diff <= 30.5 and event_id not in notified_events:
                channel = bot.get_channel(NEWS_CHANNEL_ID)
                if channel:
                    country = item.get("country", "USD").upper()
                    title = item.get("title", "Event")
                    
                    embed = generate_30min_warning_embed(country, title, time_msk)
                    await channel.send(content="@everyone", embed=embed)
                    notified_events.add(event_id)

# ==========================================
# КОМАНДЫ БОТА
# ==========================================

@bot.command(name="test_daily")
@commands.has_permissions(administrator=True)
async def test_daily(ctx):
    channel = bot.get_channel(NEWS_CHANNEL_ID)
    if channel:
        embed = await generate_daily_news_embed()
        await channel.send(embed=embed)
        await ctx.message.add_reaction("✅")

@bot.command(name="test_weekly")
@commands.has_permissions(administrator=True)
async def test_weekly(ctx):
    channel = bot.get_channel(NEWS_CHANNEL_ID)
    if channel:
        embed = await generate_weekly_news_embed()
        await channel.send(embed=embed)
        await ctx.message.add_reaction("✅")

@bot.command(name="test_30min")
@commands.has_permissions(administrator=True)
async def test_30min(ctx):
    channel = bot.get_channel(NEWS_CHANNEL_ID)
    if channel:
        now_msk = datetime.now(MSK_TZ)
        time_str = now_msk.strftime("%H:%M")
        embed = generate_30min_warning_embed("USD", "Базовый индекс потребительских цен (ИПЦ)", time_str)
        await channel.send(content="@everyone", embed=embed)
        await ctx.message.add_reaction("✅")

@bot.command(name="breaking_news")
@commands.has_permissions(administrator=True)
async def breaking_news(ctx, country: str, *, title: str):
    channel = bot.get_channel(NEWS_CHANNEL_ID)
    if channel:
        now_msk = datetime.now(MSK_TZ)
        time_str = now_msk.strftime("%H:%M")
        flag = COUNTRY_FLAGS.get(country.upper(), "🌐")

        embed = discord.Embed(
            title="⚠️ Внимание! Внеплановый анонс!",
            description=f"🕘 **{now_msk.strftime('%A')} {time_str} МСК**",
            color=discord.Color.red()
        )
        embed.add_field(
            name=f"{flag} **{country.upper()}** — {title}",
            value="🔴 **HIGH**",
            inline=False
        )
        embed.set_footer(text="⚠️ Будьте аккуратны!")
        await channel.send(content="@everyone", embed=embed)
        await ctx.message.add_reaction("✅")

@bot.event
async def on_ready():
    print(f"✅ Бот {bot.user} успешно запущен!")
    print(f"📢 Канал новостей ID: {NEWS_CHANNEL_ID}")
    
    if not weekly_news_task.is_running():
        weekly_news_task.start()
    if not daily_news_task.is_running():
        daily_news_task.start()
    if not news_30min_notifier_task.is_running():
        news_30min_notifier_task.start()

if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
