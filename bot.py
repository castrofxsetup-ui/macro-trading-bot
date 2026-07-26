import os
import re
import asyncio
import httpx
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

if not DISCORD_TOKEN:
    print("⚠️ WARNING: DISCORD_TOKEN не найден в переменных окружения!")
if not GROQ_API_KEY:
    print("⚠️ WARNING: GROQ_API_KEY не найден в переменных окружения!")

groq_client = AsyncGroq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

app = FastAPI()

@app.get("/")
def read_root():
    return {"status": "ok", "bot": "Legacy Bot with Real-time Market Data & Groq AI"}

# ==========================================
# 2. МАРШРУТИЗАЦИЯ И ТИКТЕРЫ (ТОЧНОЕ ОПРЕДЕЛЕНИЕ АКТИВА)
# ==========================================

# Точная карта соответствия запросов пользователя с символами Yahoo Finance
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
    """Ищет только те активы, которые пользователь явно попросил проанализировать"""
    clean_text = re.sub(r'[^\w\s/]', '', text.upper())
    words = clean_text.split()
    
    detected_tickers = {}
    
    for word in words:
        if word in TICKER_DICTIONARY:
            name = word
            ticker = TICKER_DICTIONARY[word]
            detected_tickers[ticker] = name

    # Проверка словосочетаний (например "S&P 500", "Индекс доллара")
    text_upper = text.upper()
    for key, ticker in TICKER_DICTIONARY.items():
        if key in text_upper and ticker not in detected_tickers:
            detected_tickers[ticker] = key

    return detected_tickers

def fetch_live_market_context(user_query: str) -> str:
    """Получает ДНЕВНЫЕ свежие котировки (OHLCV) строго по запрошенному активу"""
    target_tickers = extract_tickers_from_query(user_query)
    
    if not target_tickers:
        return ""

    context_lines = ["=== АКТУАЛЬНЫЕ КОТИРОВКИ И ДАННЫЕ РЫНКА (РЕАЛЬНОЕ ВРЕМЯ) ==="]

    for symbol, user_name in target_tickers.items():
        try:
            ticker = yf.Ticker(symbol)
            # Запрашиваем данные свечи за последние 5 дней для точного High/Low
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

            # Дополнительный High/Low за вчера (для ликвидности PDH/PDL - Previous Day High/Low)
            pdh = prev_row["High"]
            pdl = prev_row["Low"]

            context_lines.append(
                f"• АКТИВ: {user_name} ({symbol})\n"
                f"  - Текущая цена: {current_price:.2f}\n"
                f"  - Дневной максимум (High): {day_high:.2f}\n"
                f"  - Дневной минимум (Low): {day_low:.2f}\n"
                f"  - Вчерашний High (PDH): {pdh:.2f}\n"
                f"  - Вчерашний Low (PDL): {pdl:.2f}\n"
                f"  - Изменение за день: {change_pct:+.2f}%\n"
            )
        except Exception as e:
            print(f"[Market Data Error] Ошибка получения данных для {symbol}: {e}")

    context_lines.append("ВАЖНО: При ответе опирайся СТРОГО на эти цены и уровни High/Low! Не придумывай другие значения цен.")
    return "\n".join(context_lines)

# ==========================================
# 3. СИСТЕМНЫЙ ПРОМПТ
# ==========================================

SYSTEM_PROMPT = """
Ты — профессиональный аналитик и ментор в закрытом Discord-сообществе для трейдеров.

ТВОЯ СПЕЦИАЛИЗАЦИЯ И МЕХАНИКА АНАЛИЗА:
1. Основа твоего анализа — концепции Smart Money Concepts (SMC), Inner Circle Trader (ICT) и методология Alchemist MSNR.
2. Категорически ИЗБЕГАЙ и НЕ ИСПОЛЬЗУЙ классический технический анализ (никаких индикаторов RSI, MACD, скользящих средних, линий тренда, фигурного анализа вроде "голова и плечи", "двойное дно" и т.д.).
3. Ты объясняешь движения рынка исключительно через механику ликвидности (Liquidity Sweep, Buy-side / Sell-side Liquidity, PDH/PDL), работу алгоритма доставки цены (IPDA), дисбалансы (FVG / Fair Value Gap), имбалансы, блоки заказов (Order Block, Breaker Block) и структуры MSNR (Market Structure, Market Structure Shift / MSS, Change of Character / CHOCh).

СТРОГИЕ ПРАВИЛА ПО ЦЕНАМ И АКТИВАМ:
1. Внимательно смотри на переданный контекст рыночных данных! Использовать нужно СТРОГО те текущие цены и значения High/Low, которые переданы в блоке "АКТУАЛЬНЫЕ КОТИРОВКИ".
2. Не путай активы. Если спрашивают про Биткоин (BTC), говори ТОЛЬКО про Биткоин и его текущую цену. Если спрашивают про Золото — говори ТОЛЬКО про Золото.
3. Уровни поддержки/сопротивления (FVG, Order Block, Liquidity Pool) строй ВОКРУГ реальной текущей цены и диапазонов High/Low, переданных тебе.

СТИЛЬ ОБЩЕНИЯ:
- Профессиональный, уверенный, лаконичный.
- Используй терминологию ICT/SMC/MSNR на английском или с общепринятой транслитерацией (FVG, OB, Liquidity Grab, BOS, MSS, MSNR, BPR, PDH, PDL).
"""

# ==========================================
# 4. ЗАПРОС К GROQ API С ОБОГАЩЕНИЕМ ДАННЫМИ
# ==========================================

async def get_groq_ai_response(user_message: str) -> str:
    if not groq_client:
        return "⚠️ Ошибка: Переменная `GROQ_API_KEY` не настроена на сервисе Render."

    # 1. Вытягиваем актуальные котировки строго под запрошенный актив
    market_context = await asyncio.to_thread(fetch_live_market_context, user_message)
    
    # 2. Формируем итоговый запрос к модели
    if market_context:
        full_prompt = f"{market_context}\n\nЗапрос пользователя: {user_message}"
    else:
        full_prompt = user_message

    try:
        completion = await groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": full_prompt}
            ],
            temperature=0.3, # Пониженный шум для более точных ответов по цифрам
            max_tokens=1024,
        )
        return completion.choices[0].message.content
    except Exception as e:
        error_str = str(e)
        if "429" in error_str:
            return "⏳ Достигнут временный лимит запросов Groq API. Пожалуйста, подождите минуту."
        print(f"[ERROR] Ошибка Groq API: {e}")
        return f"⚠️ Ошибка при запросе к ИИ: {e}"

# ==========================================
# 5. СОБЫТИЯ И КОМАНДЫ DISCORD БОТА
# ==========================================

@bot.event
async def on_ready():
    print(f"✅ Бот {bot.user.name} успешно подключился к серверам Discord!")
    print(f"📊 Подключен модуль точных рыночных данных (OHLCV / Market Data)")

@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user:
        return

    if bot.user in message.mentions or isinstance(message.channel, discord.DMChannel):
        async with message.channel.typing():
            clean_content = message.content.replace(f"<@{bot.user.id}>", "").strip()
            if not clean_content:
                clean_content = "Привет! Напиши актив (например: BTC, Gold, EURUSD, SPX), и я дам актуальный разбор по SMC/ICT/MSNR."
            
            ai_reply = await get_groq_ai_response(clean_content)
            
            if len(ai_reply) > 2000:
                for i in range(0, len(ai_reply), 1900):
                    await message.reply(ai_reply[i:i+1900])
            else:
                await message.reply(ai_reply)

    await bot.process_commands(message)

@bot.command(name="price")
async def price_command(ctx: commands.Context, *, symbol: str):
    """Команда !price BTC / !price GOLD для получения текущих точных котировок"""
    async with ctx.typing():
        data = await asyncio.to_thread(fetch_live_market_context, symbol)
        if data:
            await ctx.reply(data)
        else:
            await ctx.reply(f"Не удалось распознать актив или получить котировки по: `{symbol}`")

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
