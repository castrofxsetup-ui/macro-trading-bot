import os
import re
import asyncio
from datetime import datetime, timedelta
import pytz
import discord
from discord.ext import commands, tasks
from fastapi import FastAPI
import uvicorn
from groq import AsyncGroq
import yfinance as yf
import httpx
from bs4 import BeautifulSoup

# ==========================================
# 1. ИНИЦИАЛИЗАЦИЯ И НАСТРОЙКИ
# ==========================================

DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")

# --- ID КАНАЛОВ И ВЕТОК DISCORD ---
AI_CHAT_CHANNEL_ID = 1502292137889501235         # Чат с ИИ
STREAMS_CHANNEL_ID = 1528506824687485118        # Ветка анонсов стримов / мероприятий
NEWS_CHANNEL_ID = 1528319066513604688           # Ветка экономических новостей

if not DISCORD_TOKEN:
    print("⚠️ WARNING: DISCORD_TOKEN не найден в переменных окружения Render!")
if not GROQ_API_KEY:
    print("⚠️ WARNING: GROQ_API_KEY не найден в переменных окружения Render!")

groq_client = AsyncGroq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

# Настройки Discord бота
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Веб-сервер FastAPI для Render Health Check
app = FastAPI()

@app.api_route("/", methods=["GET", "HEAD"])
def read_root():
    return {"status": "ok", "bot": "Macro Bot fully loaded with AI, News, Streams, Tasks"}

# ==========================================
# 1.1 ПАМЯТЬ И ХРАНИЛИЩА ДАННЫХ
# ==========================================

user_ai_context = {}
MAX_HISTORY_MESSAGES = 10

# Хранилище запланированных стримов: список словарей [{"title": ..., "time": datetime, "notified": bool}]
upcoming_streams = []

# База заданий дня по SMC / ICT / MSNR
daily_tasks_db = [
    "📌 **Задание дня (SMC/ICT):** Найдите на графике EUR/USD (15m) сегодняшний Liquidity Sweep азиатского максимума (BSL) и сформировавшийся после него Order Block.",
    "📌 **Задание дня (MSNR):** Определите ключевой Market Structure Shift (MSS) на 4H таймфрейме по BTC и отметьте ближайшую зону Fair Value Gap (FVG).",
    "📌 **Задание дня (IPDA):** Разберите дневной диапазон GOLD (XAUUSD): укажите Premium и Discount зоны относительно вчерашнего Low и High (PDL/PDH)."
]

# ==========================================
# 2. МАРШРУТИЗАЦИЯ И ТИКЕРЫ (yfinance)
# ==========================================

TICKER_DICTIONARY = {
    # Криптовалюта
    "BTC": "BTC-USD", "BITCOIN": "BTC-USD", "БИТКОИН": "BTC-USD", "БИТКОЙН": "BTC-USD", "BTCUSD": "BTC-USD",
    "ETH": "ETH-USD", "ETHEREUM": "ETH-USD", "ЭФИР": "ETH-USD", "ЭФИРИУМ": "ETH-USD", "ETHUSD": "ETH-USD",
    "SOL": "SOL-USD", "SOLANA": "SOL-USD", "СОЛАНА": "SOL-USD",
    "XRP": "XRP-USD", "RIPPLE": "XRP-USD", "РИППЛ": "XRP-USD",

    # Металлы
    "GOLD": "GC=F", "XAUUSD": "GC=F", "ЗОЛОТО": "GC=F", "XAU": "GC=F",
    "SILVER": "SI=F", "XAGUSD": "SI=F", "СЕРЕБРО": "SI=F", "XAG": "SI=F",

    # Форекс пары
    "EURUSD": "EURUSD=X", "EUR/USD": "EURUSD=X", "ЕВРО": "EURUSD=X",
    "GBPUSD": "GBPUSD=X", "GBP/USD": "GBPUSD=X", "ФУНТ": "GBPUSD=X",
    "USDJPY": "JPY=X", "USD/JPY": "JPY=X", "ИЕНА": "JPY=X", "ЙЕНА": "JPY=X",
    "AUDUSD": "AUDUSD=X", "AUD/USD": "AUDUSD=X",
    "USDCAD": "CAD=X", "USD/CAD": "CAD=X",
    "USDCHF": "CHF=X", "USD/CHF": "CHF=X",
    "DXY": "DX-Y.NYB", "ДОЛЛАР": "DX-Y.NYB", "ИНДЕКС ДОЛЛАРА": "DX-Y.NYB",

    # Индексы
    "SPX": "^GSPC", "SP500": "^GSPC", "S&P500": "^GSPC", "S&P 500": "^GSPC",
    "NDX": "^IXIC", "NASDAQ": "^IXIC", "НАСДАК": "^IXIC", "NAS100": "NQ=F",
    "DOW": "^DJI", "DJI": "^DJI", "ДЖОНС": "^DJI"
}

def extract_tickers_from_query(text: str):
    clean_text = re.sub(r'[^\w\s/]', '', text.upper())
    words = clean_text.split()
    detected_tickers = {}

    for word in words:
        if word in TICKER_DICTIONARY:
            detected_tickers[TICKER_DICTIONARY[word]] = word

    text_upper = text.upper()
    for key, ticker in TICKER_DICTIONARY.items():
        if key in text_upper and ticker not in detected_tickers:
            detected_tickers[ticker] = key

    return detected_tickers

def fetch_live_market_context(user_query: str) -> str:
    target_tickers = extract_tickers_from_query(user_query)
    if not target_tickers:
        return ""

    context_lines = ["=== АКТУАЛЬНЫЕ КОТИРОВКИ И ДАННЫЕ РЫНКА (РЕАЛЬНОЕ ВРЕМЯ) ==="]

    for symbol, user_name in target_tickers.items():
        try:
            ticker = yf.Ticker(symbol)
            hist = ticker.history(period="5d")
            if hist.empty:
                continue

            latest_row = hist.iloc[-1]
            prev_row = hist.iloc[-2] if len(hist) > 1 else latest_row

            current_price = latest_row["Close"]
            day_high = latest_row["High"]
            day_low = latest_row["Low"]
            prev_close = prev_row["Close"]
            change_pct = ((current_price - prev_close) / prev_close) * 100

            context_lines.append(
                f"• АКТИВ: {user_name} ({symbol})\n"
                f"  - Текущая цена: {current_price:.2f}\n"
                f"  - Дневной максимум (High): {day_high:.2f}\n"
                f"  - Дневной минимум (Low): {day_low:.2f}\n"
                f"  - Вчерашний High (PDH): {prev_row['High']:.2f}\n"
                f"  - Вчерашний Low (PDL): {prev_row['Low']:.2f}\n"
                f"  - Изменение за день: {change_pct:+.2f}%\n"
            )
        except Exception as e:
            print(f"[Market Data Error] Ошибка получения данных для {symbol}: {e}")

    context_lines.append("ВАЖНО: Опирайся на эти данные при сопоставлении с вопросом пользователя!")
    return "\n".join(context_lines)

# ==========================================
# 3. МОДУЛЬ ПАРСИНГА NЕWS (FOREXFACTORY)
# ==========================================

async def fetch_forexfactory_news() -> str:
    """Получает ключевые новости с высоким импактом (High Impact) с ForexFactory"""
    url = "https://www.forexfactory.com/calendar"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
    }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, headers=headers, timeout=10.0)
            if response.status_code != 200:
                return "⚠️ Не удалось подключиться к ForexFactory."

            soup = BeautifulSoup(response.text, "html.parser")
            rows = soup.find_all("tr", class_="calendar__row")
            
            news_events = []
            for row in rows:
                impact_td = row.find("td", class_="calendar__impact")
                if not impact_td:
                    continue
                
                # Фильтруем события по красному флажку (High Impact)
                impact_span = impact_td.find("span", class_="red") or impact_td.find("span", class_="icon--ff-impact-red")
                if impact_span:
                    time_td = row.find("td", class_="calendar__time")
                    currency_td = row.find("td", class_="calendar__currency")
                    event_td = row.find("td", class_="calendar__event")

                    time_str = time_td.text.strip() if time_td else "All Day"
                    currency_str = currency_td.text.strip() if currency_td else "USD"
                    event_str = event_td.text.strip() if event_td else "High Impact Event"

                    news_events.append(f"🔴 **[{time_str}] {currency_str}**: {event_str}")

            if not news_events:
                return "📅 На сегодня не запланировано важных новостей (High Impact)."

            return "📊 **ВАЖНЫЕ ЭКОНОМИЧЕСКИЕ НОВОСТИ НА СЕГОДНЯ (HIGH IMPACT):**\n\n" + "\n".join(news_events)
    except Exception as e:
        print(f"[News Error] {e}")
        return "⚠️ Ошибка при получении данных с календаря ForexFactory."

# ==========================================
# 4. СИСТЕМНЫЙ ПРОМПТ И ИИ-ЧАТ
# ==========================================

SYSTEM_PROMPT = """
Ты — профессиональный аналитик и ментор в закрытом Discord-сообществе для трейдеров.

ТВОЯ СПЕЦИАЛИЗАЦИЯ И МЕХАНИКА АНАЛИЗА:
1. Основа твоего анализа — концепции Smart Money Concepts (SMC), Inner Circle Trader (ICT) и методология Alchemist MSNR.
2. Категорически ИЗБЕГАЙ и НЕ ИСПОЛЬЗУЙ классический технический анализ (никаких индикаторов RSI, MACD, скользящих средних, линий тренда, фигур вроде "голова и плечи").
3. Ты объясняешь движения рынка исключительно через механику ликвидности (Liquidity Sweep, Buy-side / Sell-side Liquidity, PDH/PDL), работу алгоритма доставки цены (IPDA), дисбалансы (FVG / Fair Value Gap), имбалансы, блоки заказов (Order Block, Breaker Block) и структуры MSNR (Market Structure, Market Structure Shift / MSS, Change of Character / CHOCh).
4. Ты помнишь контекст текущего диалога с этим конкретным пользователем и отвечаешь с учётом того, что обсуждали раньше — не начинай каждый раз "с нуля".
"""

async def get_groq_ai_response(user_id: int, user_message: str) -> str:
    if not groq_client:
        return "⚠️ Ошибка: Переменная `GROQ_API_KEY` не настроена на сервисе Render."

    market_context = await asyncio.to_thread(fetch_live_market_context, user_message)
    full_prompt = f"{market_context}\n\nЗапрос пользователя: {user_message}" if market_context else user_message

    history = user_ai_context.get(user_id, [])

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(history)
    messages.append({"role": "user", "content": full_prompt})

    try:
        completion = await groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=messages,
            temperature=0.3,
            max_tokens=1024,
        )
        reply = completion.choices[0].message.content

        history.append({"role": "user", "content": user_message})
        history.append({"role": "assistant", "content": reply})
        if len(history) > MAX_HISTORY_MESSAGES:
            history = history[-MAX_HISTORY_MESSAGES:]
        user_ai_context[user_id] = history

        return reply
    except Exception as e:
        error_str = str(e)
        if "429" in error_str:
            return "⏳ Достигнут временный лимит запросов Groq API. Пожалуйста, подождите минуту."
        print(f"[ERROR] Ошибка Groq API: {e}")
        return "⚠️ Секунду, что-то с ИИ пошло не так — попробуй написать ещё раз через пару секунд."

# ==========================================
# 5. ФОНОВЫЕ ЗАДАЧИ (STREAM NOTIFIER & AUTO NEWS)
# ==========================================

@tasks.loop(seconds=60)
async def check_stream_schedule():
    """Проверяет запланированные стримы и отправляет анонс за 30 минут до старта."""
    now = datetime.now(pytz.utc)
    channel = bot.get_channel(STREAMS_CHANNEL_ID)

    if not channel:
        return

    for stream in upcoming_streams:
        if not stream["notified"]:
            # Проверяем, осталось ли до стрима 30 минут или меньше
            time_diff = (stream["time"] - now).total_seconds()
            if 0 <= time_diff <= 1800:
                await channel.send(
                    f"⏰ **ВНИМАНИЕ! ДО НАЧАЛА СТРИМА ОСТАЛОСЬ 30 МИНУТ!**\n"
                    f"🎙️ **Тема:** {stream['title']}\n"
                    f"🔔 **Начало:** <t:{int(stream['time'].timestamp())}:t> (через 30 минут!)\n"
                    f"@everyone Приготовьтесь к подключению!"
                )
                stream["notified"] = True

@tasks.loop(hours=24)
async def auto_daily_news():
    """Автоматическая ежедневная рассылка новостей в ветку новостей."""
    channel = bot.get_channel(NEWS_CHANNEL_ID)
    if channel:
        news_report = await fetch_forexfactory_news()
        await channel.send(news_report)

# ==========================================
# 6. СОБЫТИЯ И КОМАНДЫ DISCORD
# ==========================================

@bot.event
async def on_ready():
    print(f"✅ Бот {bot.user.name} успешно подключился к Discord!")
    print(f"💬 AI Chat Channel: {AI_CHAT_CHANNEL_ID}")
    print(f"📺 Streams Channel/Thread: {STREAMS_CHANNEL_ID}")
    print(f"📰 News Channel/Thread: {NEWS_CHANNEL_ID}")
    
    # Запуск фоновых задач
    if not check_stream_schedule.is_running():
        check_stream_schedule.start()
    if not auto_daily_news.is_running():
        auto_daily_news.start()

# --- КОМАНДА: ЗАДАНИЕ ДНЯ ---
@bot.command(name="task")
async def get_daily_task(ctx):
    import random
    task = random.choice(daily_tasks_db)
    await ctx.send(task)

@bot.command(name="add_task")
@commands.has_permissions(administrator=True)
async def add_daily_task(ctx, *, new_task: str):
    daily_tasks_db.append(f"📌 **Задание дня:** {new_task}")
    await ctx.send("✅ Новое задание успешно добавлено в базу!")

# --- КОМАНДА: НОВОСТИ (РУЧНОЙ ВЫЗОВ В ВЕТКУ НОВОСТЕЙ) ---
@bot.command(name="news")
async def get_news_cmd(ctx):
    async with ctx.typing():
        news_data = await fetch_forexfactory_news()
        news_channel = bot.get_channel(NEWS_CHANNEL_ID) or ctx.channel
        await news_channel.send(news_data)

# --- КОМАНДЫ УПРАВЛЕНИЯ СТРИМАМИ ---
@bot.command(name="add_stream")
@commands.has_permissions(administrator=True)
async def add_stream_cmd(ctx, time_str: str, *, title: str):
    """
    Добавить стрим. Формат времени: YYYY-MM-DD_HH:MM (в UTC)
    Пример: !add_stream 2026-07-28_19:00 Разбор дневной сессии
    """
    try:
        dt = datetime.strptime(time_str, "%Y-%m-%d_%H:%M").replace(tzinfo=pytz.utc)
        upcoming_streams.append({"title": title, "time": dt, "notified": False})
        
        streams_channel = bot.get_channel(STREAMS_CHANNEL_ID)
        await ctx.send(f"✅ Стрим **'{title}'** успешно запланирован на {time_str} UTC.")
        
        if streams_channel:
            await streams_channel.send(
                f"📅 **ЗАПЛАНИРОВАН НОВЫЙ СТРИМ/МЕРОПРИЯТИЕ!**\n"
                f"🎙️ **Тема:** {title}\n"
                f"🕒 **Время:** <t:{int(dt.timestamp())}:F>\n"
                f"Уведомление придёт за 30 минут до начала!"
            )
    except ValueError:
        await ctx.send("❌ Неверный формат даты! Используйте: `YYYY-MM-DD_HH:MM` (например: `2026-07-28_19:00`)")

@bot.command(name="streams")
async def list_streams_cmd(ctx):
    if not upcoming_streams:
        await ctx.send("📺 Запланированных стримов пока нет.")
        return

    msg = ["📺 **СПИСОК БЛИЖАЙШИХ СТРИМОВ:**"]
    for i, s in enumerate(upcoming_streams, 1):
        status = "🔔 (Оповещён)" if s["notified"] else "⏳ (Ожидает)"
        msg.append(f"{i}. **{s['title']}** — <t:{int(s['time'].timestamp())}:F> {status}")
    
    await ctx.send("\n".join(msg))

# --- ОБРАБОТЧИК AI-ЧАТА ---
@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user or message.author.bot:
        return

    # Обработка команд бота (!task, !news, !add_stream и т.д.)
    await bot.process_commands(message)

    # ИИ отвечает ТОЛЬКО в заданном канале ИИ-чата
    if message.channel.id != AI_CHAT_CHANNEL_ID:
        return

    is_mention = bot.user in message.mentions
    is_reply_to_bot = bool(
        message.reference
        and message.reference.cached_message
        and message.reference.cached_message.author == bot.user
    )

    if is_mention or is_reply_to_bot:
        async with message.channel.typing():
            clean_content = message.content.replace(f"<@{bot.user.id}>", "").strip()

            if not clean_content:
                clean_content = "Привет! Задай вопрос по концепциям SMC/ICT/MSNR или спроси актуальные уровни/котировки по активу."

            ai_reply = await get_groq_ai_response(message.author.id, clean_content)

            if len(ai_reply) > 2000:
                for i in range(0, len(ai_reply), 1900):
                    await message.reply(ai_reply[i:i + 1900])
            else:
                await message.reply(ai_reply)

# ==========================================
# 7. ЗАПУСК ВЕБ-СЕРВЕРА И БОТА
# ==========================================

async def run_fastapi():
    port = int(os.environ.get("PORT", 10000))
    config = uvicorn.Config(app=app, host="0.0.0.0", port=port, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()

async def main():
    if not DISCORD_TOKEN:
        print("CRITICAL ERROR: DISCORD_TOKEN не задан.")
        return

    await asyncio.gather(
        run_fastapi(),
        bot.start(DISCORD_TOKEN)
    )

if __name__ == "__main__":
    asyncio.run(main())
