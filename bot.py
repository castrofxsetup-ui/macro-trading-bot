import os
import re
import asyncio
import discord
from discord.ext import commands
from fastapi import FastAPI
import uvicorn
from groq import AsyncGroq
import yfinance as yf

# ==========================================
# 1. ИНИЦИАЛИЗАЦИЯ И НАСТРОЙКИ
# ==========================================

DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")

# ИИ отвечает ТОЛЬКО в этом канале — в любых других каналах и в личных
# сообщениях бот полностью игнорирует упоминания/ответы.
AI_CHAT_CHANNEL_ID = 1502292137889501235

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
    return {"status": "ok", "bot": "Macro Bot powered fully by Groq API"}

# ==========================================
# 1.1 ПАМЯТЬ ДИАЛОГОВ — ОТДЕЛЬНО НА КАЖДОГО ЮЗЕРА
# ==========================================
# Ключ — user_id (а не channel_id!): один пользователь = один непрерывный диалог,
# независимо от того, что пишут другие люди в том же канале.
# Формат: {user_id: [{"role": "user"/"assistant", "content": "..."}]}
user_ai_context = {}
MAX_HISTORY_MESSAGES = 10  # сколько последних сообщений (юзер+бот) хранить на юзера

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
# 3. СИСТЕМНЫЙ ПРОМПТ (SMC/ICT/MSNR)
# ==========================================

SYSTEM_PROMPT = """
Ты — профессиональный аналитик и ментор в закрытом Discord-сообществе для трейдеров.

ТВОЯ СПЕЦИАЛИЗАЦИЯ И МЕХАНИКА АНАЛИЗА:
1. Основа твоего анализа — концепции Smart Money Concepts (SMC), Inner Circle Trader (ICT) и методология Alchemist MSNR.
2. Категорически ИЗБЕГАЙ и НЕ ИСПОЛЬЗУЙ классический технический анализ (никаких индикаторов RSI, MACD, скользящих средних, линий тренда, фигур вроде "голова и плечи").
3. Ты объясняешь движения рынка исключительно через механику ликвидности (Liquidity Sweep, Buy-side / Sell-side Liquidity, PDH/PDL), работу алгоритма доставки цены (IPDA), дисбалансы (FVG / Fair Value Gap), имбалансы, блоки заказов (Order Block, Breaker Block) и структуры MSNR (Market Structure, Market Structure Shift / MSS, Change of Character / CHOCh).
4. Ты помнишь контекст текущего диалога с этим конкретным пользователем и отвечаешь с учётом того, что обсуждали раньше — не начинай каждый раз "с нуля".
"""

# ==========================================
# 4. ЗАПРОСЫ К GROQ API (ТЕКСТ, С ПАМЯТЬЮ ПО ЮЗЕРУ)
# ==========================================

async def get_groq_ai_response(user_id: int, user_message: str) -> str:
    """Текстовые ответы через Llama 3.3 70B Versatile, с историей диалога конкретного пользователя."""
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

        # Сохраняем в историю именно исходное сообщение юзера (без подмешанных котировок),
        # чтобы история не раздувалась служебными данными от yfinance.
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
# 5. СОБЫТИЯ И КОМАНДЫ DISCORD БОТА
# ==========================================

@bot.event
async def on_ready():
    print(f"✅ Бот {bot.user.name} успешно подключился к Discord!")
    print(f"💬 ИИ отвечает только в канале ID {AI_CHAT_CHANNEL_ID}. Память диалогов — по каждому юзеру отдельно.")

@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user or message.author.bot:
        return

    # Работаем только в одном заданном канале — никаких личных сообщений
    # и никаких других каналов сервера.
    if message.channel.id != AI_CHAT_CHANNEL_ID:
        await bot.process_commands(message)
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

            # Разбивка длинных сообщений для Discord (лимит 2000 символов)
            if len(ai_reply) > 2000:
                for i in range(0, len(ai_reply), 1900):
                    await message.reply(ai_reply[i:i + 1900])
            else:
                await message.reply(ai_reply)

    await bot.process_commands(message)

# ==========================================
# 6. ЗАПУСК ВЕБ-СЕРВЕРА И БОТА
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
