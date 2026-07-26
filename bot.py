import discord
from discord.ext import commands, tasks
from fastapi import FastAPI
import uvicorn
import asyncio
import threading
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
import os
from google import genai
from google.genai import types as genai_types

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
TASK_CHANNEL_ID = 1502292137889501235     # Ветка для утренних заданий дня И для общения с ИИ

# ИИ отвечает ТОЛЬКО в этом канале — в любых других ветках бот не реагирует
# на упоминания/ответы вообще, даже если его тегнуть.
AI_CHAT_CHANNEL_ID = TASK_CHANNEL_ID

# Реальный публичный XML-календарь ForexFactory
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
# КЕШ КАЛЕНДАРЯ FOREXFACTORY
# Раньше XML скачивался заново КАЖДУЮ минуту (МОДУЛЬ 2 крутится в цикле 60с) —
# это и вызывало "HTTP Error 429: Too Many Requests" от источника.
# Теперь данные обновляются не чаще, чем раз в CACHE_TTL секунд, и переиспользуются
# и в МОДУЛЕ 1, и в МОДУЛЕ 2.
# =========================================================================
CACHE_TTL = 180  # секунд между реальными обращениями к nfs.faireconomy.media
_ff_cache = {"root": None, "fetched_at": None}

def get_ff_calendar():
    now = datetime.now(timezone.utc)
    if _ff_cache["root"] is not None and _ff_cache["fetched_at"] is not None:
        if (now - _ff_cache["fetched_at"]).total_seconds() < CACHE_TTL:
            return _ff_cache["root"]

    req = urllib.request.Request(FF_CALENDAR_URL, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=15) as response:
        xml_data = response.read()
    root = ET.fromstring(xml_data)

    _ff_cache["root"] = root
    _ff_cache["fetched_at"] = now
    return root


def _ev_text(event, tag):
    """Безопасно достаёт .text у дочернего тега XML-события.
    Раньше event.find('impact').text падал с 'NoneType' object has no attribute 'text',
    если у конкретного события в фиде не было такого тега (бывает у holiday/all-day событий)."""
    node = event.find(tag)
    return node.text if node is not None else None

# =========================================================================
# ИИ-МОДУЛЬ ОБЩЕНИЯ — Google Gen AI SDK
# =========================================================================
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
# gemini-3.5-flash: стабильная GA-модель Google на июль 2026.
# Если Google снова закроет доступ новым ключам (как было с 1.5 и 2.5),
# следующий кандидат на замену — "gemini-3.6-flash" (вышла 21.07.2026).
GEMINI_MODEL = "gemini-3.5-flash"

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

gemini_client = None
if GEMINI_API_KEY:
    gemini_client = genai.Client(api_key=GEMINI_API_KEY)


def ask_free_ai(prompt, context_history=None):
    if not gemini_client:
        print("Ошибка ИИ: не задан GEMINI_API_KEY в переменных окружения.", flush=True)
        return "Секунду, настраиваю графики... (ИИ временно не настроен)"

    try:
        contents = []
        if context_history:
            for msg in context_history:
                role = "model" if msg["role"] == "assistant" else "user"
                contents.append(genai_types.Content(role=role, parts=[genai_types.Part(text=msg["content"])]))
        contents.append(genai_types.Content(role="user", parts=[genai_types.Part(text=prompt)]))

        response = gemini_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=contents,
            config=genai_types.GenerateContentConfig(system_instruction=SYSTEM_INSTRUCTION),
        )
        return response.text
    except Exception as e:
        print(f"Ошибка ИИ (Gemini): {e}", flush=True)
        return "Секунду, графики подвисли — попробуй написать ещё раз через пару секунд. 📈"


@bot.event
async def on_ready():
    print(f"Бот {bot.user.name} успешно подключился к серверам Discord!", flush=True)
    if not main_checking_loop.is_running():
        main_checking_loop.start()

@bot.event
async def on_message(message):
    if message.author.bot or not message.guild:
        return

    # ИИ реагирует ТОЛЬКО в канале AI_CHAT_CHANNEL_ID.
    # В любой другой ветке — полностью игнорируем упоминания/ответы боту.
    if message.channel.id == AI_CHAT_CHANNEL_ID:
        if bot.user.mentioned_in(message) or (message.reference and message.reference.cached_message and message.reference.cached_message.author == bot.user):
            print(f"[AI] Получено сообщение от {message.author}: {message.content!r}", flush=True)
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

    # МОДУЛЬ 0. ЗАДАНИЕ ДНЯ (09:30 МСК)
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

    # МОДУЛЬ 1. ЕЖЕДНЕВНЫЙ КАЛЕНДАРЬ (08:00 МСК)
    if news_channel and now_msk.hour == 8 and now_msk.minute == 0 and last_daily_report_date != current_date_str:
        try:
            root = get_ff_calendar()
            daily_events = []

            for event in root.findall('event'):
                impact = _ev_text(event, 'impact')
                if impact not in ["High", "Medium"]:
                    continue

                title = _ev_text(event, 'title')
                currency = _ev_text(event, 'currency')
                date_str = _ev_text(event, 'date')
                time_str = _ev_text(event, 'time')

                if not date_str or not time_str or not title or not currency:
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
        except urllib.error.HTTPError as e:
            print(f"Произошла ошибка в МОДУЛЕ 1 (календарь): HTTP {e.code} {e.reason}", flush=True)
        except Exception as e:
            print(f"Произошла ошибка в МОДУЛЕ 1 (календарь): {e}", flush=True)

    # МОДУЛЬ 2. МОНИТОРИНГ КРАСНЫХ НОВОСТЕЙ (ЗА 15 МИНУТ)
    if news_channel:
        try:
            root = get_ff_calendar()
            for event in root.findall('event'):
                impact = _ev_text(event, 'impact')
                if impact != "High":
                    continue

                title = _ev_text(event, 'title')
                currency = _ev_text(event, 'currency')
                date_str = _ev_text(event, 'date')
                time_str = _ev_text(event, 'time')

                if not date_str or not time_str or not title or not currency:
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

        except urllib.error.HTTPError as e:
            print(f"Ошибка при парсинге новостей ForexFactory: HTTP {e.code} {e.reason}", flush=True)
        except Exception as news_err:
            print(f"Ошибка при парсинге новостей ForexFactory: {news_err}", flush=True)

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
