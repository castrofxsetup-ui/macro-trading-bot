import os
import asyncio
import logging
from datetime import datetime
import aiohttp
import discord
from discord.ext import commands, tasks
from groq import AsyncGroq
from aiohttp import web

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
PROXY_URL = os.getenv("PROXY_URL")  # Необязательно (Решение 2)
PORT = int(os.getenv("PORT", 10000))

# ID каналов Discord (укажите свои значения в переменной окружения или поставьте ID)
CALENDAR_CHANNEL_ID = int(os.getenv("CALENDAR_CHANNEL_ID", 0))
TASKS_CHANNEL_ID = int(os.getenv("TASKS_CHANNEL_ID", 0))
STREAM_CHANNEL_ID = int(os.getenv("STREAM_CHANNEL_ID", 0))

# ---------------------------------------------------------------------------
# INITIALIZE CLIENTS & BOT
# ---------------------------------------------------------------------------
groq_client = AsyncGroq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

intents = discord.Intents.default()
intents.message_content = True
intents.guild_scheduled_events = True

# Инициализация бота с поддержкой прокси (Решение 2)
bot = commands.Bot(
    command_prefix="!",
    intents=intents,
    proxy=PROXY_URL if PROXY_URL else None
)

# ---------------------------------------------------------------------------
# DUMMY WEB SERVER (FOR RENDER HEALTH CHECKS)
# ---------------------------------------------------------------------------
async def handle_ping(request):
    return web.Response(text="Bot is alive!", status=200)

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
# MODULE 1: FOREXFACTORY CALENDAR
# ---------------------------------------------------------------------------
async def fetch_forexfactory_calendar():
    url = "https://nfp.ourforecast.com/api/v1/events"  # Или актуальный API источник
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=10) as response:
                if response.status == 200:
                    return await response.json()
    except Exception as e:
        logger.error(f"Ошибка получения календаря ForexFactory: {e}")
    return []

@tasks.loop(hours=24)
async def daily_calendar_task():
    if not CALENDAR_CHANNEL_ID:
        return
    channel = bot.get_channel(CALENDAR_CHANNEL_ID)
    if channel:
        events = await fetch_forexfactory_calendar()
        if events:
            embed = discord.Embed(
                title="📅 Экономический календарь на сегодня",
                color=discord.Color.blue(),
                timestamp=datetime.utcnow()
            )
            for ev in events[:10]:  # Показываем первые 10 событий
                embed.add_field(
                    name=f"{ev.get('time', 'N/A')} - {ev.get('currency', '')}",
                    value=f"**{ev.get('title', 'Event')}** (Impact: {ev.get('impact', 'Low')})",
                    inline=False
                )
            await channel.send(embed=embed)

# ---------------------------------------------------------------------------
# MODULE 2: DAILY TASKS
# ---------------------------------------------------------------------------
@tasks.loop(hours=24)
async def daily_tasks_module():
    if not TASKS_CHANNEL_ID:
        return
    channel = bot.get_channel(TASKS_CHANNEL_ID)
    if channel:
        embed = discord.Embed(
            title="📋 Ежедневный чек-лист задач",
            description="1. Проверить торговые сессии\n2. Разместить аналитику\n3. Проверить экономический календарь",
            color=discord.Color.green()
        )
        await channel.send(embed=embed)

# ---------------------------------------------------------------------------
# MODULE 3: STREAM NOTIFICATIONS (EVERY 30 MIN)
# ---------------------------------------------------------------------------
@tasks.loop(minutes=30)
async def stream_notifications_task():
    if not STREAM_CHANNEL_ID:
        return
    channel = bot.get_channel(STREAM_CHANNEL_ID)
    if channel:
        # Здесь логика проверки активности стрима
        logger.info("📡 Проверка уведомлений о стримах (каждые 30 мин)...")

# ---------------------------------------------------------------------------
# MODULE 4: GROQ AI CHAT & EVENTS
# ---------------------------------------------------------------------------
@bot.event
async def on_ready():
    logger.info(f"✅ Успешный вход под именем {bot.user} (ID: {bot.user.id})")
    
    # Запуск фоновых задач
    if not daily_calendar_task.is_running():
        daily_calendar_task.start()
    if not daily_tasks_module.is_running():
        daily_tasks_module.start()
    if not stream_notifications_task.is_running():
        stream_notifications_task.start()

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    # Ответ на упоминание или личные сообщения с помощью Groq AI
    if bot.user.mentioned_in(message) or isinstance(message.channel, discord.DMChannel):
        if not groq_client:
            await message.channel.send("⚠️ Groq API ключ не настроен.")
            return

        user_prompt = message.content.replace(f"<@{bot.user.id}>", "").strip()
        if not user_prompt:
            user_prompt = "Привет!"

        async with message.channel.typing():
            try:
                chat_completion = await groq_client.chat.completions.create(
                    messages=[
                        {
                            "role": "system",
                            "content": "Ты полезный ассистент и эксперт по трейдингу. Отвечай кратко и по делу."
                        },
                        {"role": "user", "content": user_prompt}
                    ],
                    model="llama-3.3-70b-versatile",
                )
                response_text = chat_completion.choices[0].message.content
                await message.reply(response_text)
            except Exception as e:
                logger.error(f"Ошибка Groq API: {e}")
                await message.reply("❌ Произошла ошибка при обработке запроса к ИИ.")

    await bot.process_commands(message)

# ---------------------------------------------------------------------------
# MAIN EXECUTION WITH AUTO-RECONNECT & RATE LIMIT HANDLING (РЕШЕНИЕ 3)
# ---------------------------------------------------------------------------
async def main():
    # Запускаем локальный веб-сервер для порт-чеков Render
    await start_web_server()
    
    # Бесконечный цикл переподключения при любых банах/сбоях сети
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
