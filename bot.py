import discord
from discord.ext import commands, tasks
from fastapi import FastAPI
import uvicorn
import asyncio
import threading
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
import os
import google.generativeai as genai

# --- ВЕБ-СЕРВЕР ДЛЯ ОБХОДА ПЛАТНОГО ТАРИФА RENDER ---
app = FastAPI()

@app.get("/")
def read_root():
    return {"status": "Macro Bot AI is perfectly running 24/7!"}

def start_web_server():
    port = int(os.getenv("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")

threading.Thread(target=start_web_server, daemon=True).start()
# -------------------------------------------------------------

# НАСТРОЙКИ КАНАЛОВ БОТА
NEWS_CHANNEL_ID = 1528319066513604688     # Ветка для новостей Forex Factory
STREAMS_CHANNEL_ID = 1528506824687485118  # Ветка для уведомлений о стримах
TASK_CHANNEL_ID = 1502292137889501235     # Ветка для утренних заданий дня

# Реальный публичный XML-календарь ForexFactory
# (forexfactory.com отдаёт HTML, а не XML — парсинг главной страницы падал с ошибкой)
FF_CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.guild_scheduled_events = True

bot = commands.Bot(command_prefix="!", intents=intents)

notified_news = set()
notified_events_30m = set()
last_daily_report_date = ""
last_task_date = ""

FLAGS = {
    "USD": "🇺🇸", "EUR": "🇪🇺", "GBP": "🇬🇧",
    "JPY": "🇯🇵", "AUD": "🇦🇺", "CAD": "🇨🇦",
    "CHF": "🇨🇭", "NZD": "🇳🇿", "CNY": "🇨🇳"
}

DAYS_RU = ["ПН", "ВТ", "СР", "ЧТ", "ПТ", "СБ", "ВС"]
MONTHS_RU = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря"
]

# Хранилище контекста диалогов для ИИ {channel_id: [messages]}
ai_context = {}

# =========================================================================
# ИИ-МОДУЛЬ ОБЩЕНИЯ — GOOGLE GEMINI (бесплатный тариф Google AI Studio)
# =========================================================================
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

SYSTEM_INSTRUCTION = (
    "Ты — Macro Expert Bot, опытный трейдер и наставник в закрытом трейдинг-комьюнити. "
    "Твой стиль: профессиональный, в меру ироничный, хладнокровный, но по-настоящему заботливый. "
    "Ты против торговли без стопов, завышенных рисков и тильта. Давай чёткие ответы по макроэкономике, "
    "структуре рынка, риск-менеджменту и психологии трейдинга. Отвечай кратко, без воды, используй "
    "трейдерский сленг (сетап, стоп, тейк, ликвидность, забор, лонг, шорт). "
    "Ты умеешь поддерживать обычный разговор, помнишь контекст переписки и продолжаешь диалог естественно. "
    "Если человек делится эмоциональными трудностями (тильт, страх, выгорание, неудачи в трейдинге или в жизни) — "
    "переходи в поддерживающий, тёплый тон, без сарказма, выслушай и помоги словом, как хороший старший друг. "
    "Отвечай строго на русском языке."
)

gemini_model = None
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    gemini_model = genai.GenerativeModel(
        model_name="gemini-1.5-flash",
        system_instruction=SYSTEM_INSTRUCTION,
    )


def ask_free_ai(prompt, context_history=None):
    if not gemini_model:
        print("Ошибка ИИ: не задан GEMINI_API_KEY в переменных окружения.")
        return "Секунду, настраиваю графики... (ИИ временно не настроен)"

    try:
        gemini_history = []
        if context_history:
            for msg in context_history:
                role = "model" if msg["role"] == "assistant" else "user"
                gemini_history.append({"role": role, "parts": [msg["content"]]})

        chat = gemini_model.start_chat(history=gemini_history)
        response = chat.send_message(prompt)
        return response.text
    except Exception as e:
        print(f"Ошибка ИИ (Gemini): {e}")
        return "Секунду, графики подвисли — попробуй написать ещё раз через пару секунд. 📈"


@bot.event
async def on_ready():
    print(f"Бот {bot.user.name} успешно подключился к серверам Discord!")
    if not main_checking_loop.is_running():
        main_checking_loop.start()

@bot.event
async def on_message(message):
    if message.author.bot or not message.guild:
        return

    if bot.user.mentioned_in(message) or (message.reference and message.reference.cached_message and message.reference.cached_message.author == bot.user):
        async with message.channel.typing():
            user_text = message.content.replace(f'<@{bot.user.id}>', '').strip()
            if not user_text and message.reference:
                user_text = message.content.strip()

            channel_id = message.channel.id
            if channel_id not in ai_context:
                ai_context[channel_id] = []

            loop = asyncio.get_event_loop()
            ai_response = await loop.run_in_executor(None, ask_free_ai, user_text, ai_context[channel_id])

            ai_context[channel_id].append({"role": "user", "content": user_text})
            ai_context[channel_id].append({"role": "assistant", "content": ai_response})
            if len(ai_context[channel_id]) > 6:
                ai_context[channel_id] = ai_context[channel_id][-6:]

            await message.reply(ai_response)

    await bot.process_commands(message)

# =========================================================================
# АВТОНОМНЫЙ ЦИКЛ ПРОВЕРКИ (ТАЙМЕРЫ И МАКРО)
# =========================================================================
@tasks.loop(seconds=60)
async def main_checking_loop():
    global last_daily_report_date, last_task_date

    now_utc = datetime.now(timezone.utc)
    now_msk = now_utc + timedelta(hours=3)

    news_channel = bot.get_channel(NEWS_CHANNEL_ID)
    task_channel = bot.get_channel(TASK_CHANNEL_ID)
    current_date_str = now_msk.strftime("%Y-%m-%d")

    if task_channel and now_msk.weekday() < 5:
        if now_msk.hour == 9 and now_msk.minute == 30 and last_task_date != current_date_str:
            embed_text = (
                "Найдите сегодня на графиках один качественный сетап по тренду "
                "с риск-ревардом от 1:3 и скиньте в соответствующую ветку на сервере. "
                "Автор лучшего разбора получит бонусную печеньку в карму!"
            )
            embed = discord.Embed(title="🎯 Задание дня:", description=embed_text, color=0x3498db)
            await task_channel.send(embed=embed)
            last_task_date = current_date_str

    if news_channel and now_msk.hour == 8 and now_msk.minute == 0 and last_daily_report_date != current_date_str:
        try:
            req = urllib.request.Request(FF_CALENDAR_URL, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=15) as response:
                xml_data = response.read()

            root = ET.fromstring(xml_data)
            daily_events = []

            for event in root.findall('event'):
                impact = event.find('impact').text
                if impact not in ["High", "Medium"]:
                    continue

                title = event.find('title').text
                currency = event.find('currency').text
                date_node = event.find('date')
                date_str = date_node.text if date_node is not None else None
                time_str = event.find('time').text

                if not date_str:
                    continue

                try:
                    event_date_obj = datetime.strptime(date_str, "%m-%d-%Y")
                    if event_date_obj.day == now_msk.day and event_date_obj.month == now_msk.month:
                        flag = FLAGS.get(currency.upper(), "🌐")
                        impact_tag = "🔴 HIGH" if impact == "High" else "🟠 MEDIUM"
                        daily_events.append(f"⏰ {time_str} | {flag} **{currency}** — {title}\n{impact_tag}")
                except Exception:
                    continue

            if daily_events:
                day_name = DAYS_RU[now_msk.weekday()]
                month_name = MONTHS_RU[now_msk.month - 1]
                date_header = f"{day_name}, {now_msk.day} {month_name}"
                events_text = "\n\n".join(daily_events)
                embed_description = f"**Запланированные мероприятия:**\n{date_header}\n\n{events_text}"
                embed = discord.Embed(title="Ежедневный экономический календарь Forex", description=embed_description, color=0x2f3136)
                await news_channel.send(embed=embed)
            last_daily_report_date = current_date_str
        except Exception as e:
            print(f"Произошла ошибка в МОДУЛЕ 1 (календарь): {e}")

    if news_channel:
        try:
            req = urllib.request.Request(FF_CALENDAR_URL, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=15) as response:
                xml_data = response.read()

            root = ET.fromstring(xml_data)
            for event in root.findall('event'):
                if event.find('impact').text != "High":
                    continue

                title = event.find('title').text
                currency = event.find('currency').text
                date_node = event.find('date')
                date_str = date_node.text if date_node is not None else None
                time_str = event.find('time').text

                if not date_str:
                    continue

                try:
                    event_datetime = datetime.strptime(f"{date_str} {time_str}", "%m-%d-%Y %I:%M%p").replace(tzinfo=timezone(timedelta(hours=-5)))
                except Exception:
                    continue

                time_diff = event_datetime - now_utc
                event_id = f"{title}_{date_str}_{time_str}"

                if timedelta(minutes=14) <= time_diff <= timedelta(minutes=16) and event_id not in notified_news:
                    flag = FLAGS.get(currency.upper(), "🌐")
                    embed_description = f"**Ожидаемые события:**\n{flag} **{currency}** — {title}\n⏰ {time_str} (Нью-Йорк)\n🔴 HIGH\n\n<sub>⌛️Публикация через 15 минут</sub>"

                    embed = discord.Embed(description=embed_description, color=0xff0000)
                    await news_channel.send(embed=embed)

                    notified_news.add(event_id)

        except Exception as news_err:
            print(f"Ошибка при парсинге новостей ForexFactory: {news_err}")

# =========================================================================
# ЗАПУСК БОТА
# =========================================================================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

if not DISCORD_TOKEN:
    raise RuntimeError(
        "Не найден DISCORD_TOKEN. Добавь переменную окружения DISCORD_TOKEN "
        "в настройках сервиса на Render (Environment -> Environment Variables)."
    )

bot.run(DISCORD_TOKEN)
