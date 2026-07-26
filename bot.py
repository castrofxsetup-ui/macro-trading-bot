import os
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
# 2. МОДУЛЬ РЕАЛЬНЫХ КАТИРОВОК РЫНКА (Trading/Market Data)
# ==========================================

# Сварка популярных тикеров для yfinance
TICKER_MAP = {
    "GOLD": "GC=F", "XAUUSD": "GC=F", "ЗОЛОТО": "GC=F",
    "SILVER": "SI=F", "XAGUSD": "SI=F",
    "EURUSD": "EURUSD=X", "EUR/USD": "EURUSD=X",
    "GBPUSD": "GBPUSD=X", "GBP/USD": "GBPUSD=X",
    "USDJPY": "JPY=X", "USD/JPY": "JPY=X",
    "BTC": "BTC-USD", "BTCUSD": "BTC-USD", "БИТКОИН": "BTC-USD",
    "ETH": "ETH-USD", "ETHUSD": "ETH-USD",
    "SPX": "^GSPC", "SP500": "^GSPC", "S&P500": "^GSPC",
    "NDX": "^IXIC", "NASDAQ": "^IXIC", "NAS100": "NQ=F",
    "DXY": "DX-Y.NYB", "USDT": "USDT-USD"
}

def get_market_data_summary(text: str) -> str:
    """Автоматически находит упоминания активов в тексте и запрашивает живые цены"""
    found_data = []
    words = text.upper().replace("/", "").split()
    
    # Проверяем, есть ли упоминания известного тикера
    tickers_to_check = set()
    for word in words:
        if word in TICKER_MAP:
            tickers_to_check.add(TICKER_MAP[word])
            
    # Если явных совпадений нет, но спросили про основные рынки — добавим ключевые
    if not tickers_to_check and any(k in text.lower() for k in ["рынок", "цена", "курс", "какая цена"]):
        tickers_to_check = {"GC=F", "EURUSD=X", "BTC-USD"}

    for symbol in tickers_to_check:
        try:
            ticker = yf.Ticker(symbol)
            fast_info = ticker.fast_info
            price = fast_info.last_price
            prev_close = fast_info.previous_close
            change_pct = ((price - prev_close) / prev_close) * 100 if prev_close else 0.0
            
            found_data.append(
                f"• {symbol}: Текущая цена = {price:.4f} (Изменение за день: {change_pct:+.2f}%)"
            )
        except Exception as e:
            print(f"[Market Data Error] Не удалось получить данные по {symbol}: {e}")

    if found_data:
        return "\n--- АКТУАЛЬНЫЕ ДАННЫЕ РЫНКА (REAL-TIME DATA) ---\n" + "\n".join(found_data) + "\n-------------------------------------------\n"
    return ""

# ==========================================
# 3. СИСТЕМНЫЙ ПРОМПТ С УЧЕТОМ СВЕЖИХ ДАННЫХ
# ==========================================

SYSTEM_PROMPT = """
Ты — эрудированный ИИ-ассистент и ментор в закрытом Discord-сообществе для трейдеров.

ТВОЯ СПЕЦИАЛИЗАЦИЯ И МЕХАНИКА АНАЛИЗА:
1. Основа твоего анализа — концепции Smart Money Concepts (SMC), Inner Circle Trader (ICT) и методология Alchemist MSNR.
2. Категорически ИЗБЕГАЙ и НЕ ИСПОЛЬЗУЙ классический технический анализ (никаких индикаторов RSI, MACD, скользящих средних, линий тренда, фигурного анализа вроде "голова и плечи", "двойное дно" и т.д.).
3. Ты объясняешь движения рынка исключительно через механику ликвидности (Liquidity Sweep, Buy-side / Sell-side Liquidity), работу алгоритма доставки цены (IPDA), дисбалансы (FVG / Fair Value Gap), имбалансы, блоки заказов (Order Block, Breaker Block, Mitigation Block) и структуры MSNR (Market Structure, Market Structure Shift / MSS, Change of Character / CHOCh).
4. Тебе могут передаваться актуальные рыночные данные в реальном времени. Опирайся на эти точные цифры при ответе о текущей цене активов!

СТИЛЬ ОБЩЕНИЯ:
- Профессиональный, уверенный, лаконичный и подстроенный под трейдинг-комьюнити.
- Используй терминологию ICT/SMC/MSNR на английском или с общепринятой транслитерацией (FVG, OB, Liquidity Grab, BOS, MSS, MSNR, BPR).
- Помни: ты ментор и наставник, ориентированный на институциональное понимание механики цены.
"""

# ==========================================
# 4. ЗАПРОС К GROQ API С ОБОГАЩЕНИЕМ ДАННЫМИ
# ==========================================

async def get_groq_ai_response(user_message: str) -> str:
    if not groq_client:
        return "⚠️ Ошибка: Переменная `GROQ_API_KEY` не настроена на сервисе Render."

    # 1. Получаем живые цены по тикерам из запроса
    market_context = await asyncio.to_thread(get_market_data_summary, user_message)
    
    # 2. Формируем итоговый промпт для ИИ
    full_prompt = user_message
    if market_context:
        full_prompt = f"{market_context}\nЗапрос пользователя: {user_message}"

    try:
        completion = await groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": full_prompt}
            ],
            temperature=0.5,
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
    print(f"📊 Подключен модуль реальных котировок (yfinance/Trading Data)")

@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user:
        return

    if bot.user in message.mentions or isinstance(message.channel, discord.DMChannel):
        async with message.channel.typing():
            clean_content = message.content.replace(f"<@{bot.user.id}>", "").strip()
            if not clean_content:
                clean_content = "Привет! Какие котировки или концепции SMC/ICT/MSNR тебя интересуют?"
            
            ai_reply = await get_groq_ai_response(clean_content)
            
            if len(ai_reply) > 2000:
                for i in range(0, len(ai_reply), 1900):
                    await message.reply(ai_reply[i:i+1900])
            else:
                await message.reply(ai_reply)

    await bot.process_commands(message)

@bot.command(name="price")
async def price_command(ctx: commands.Context, *, symbol: str):
    """Команда !price EURUSD / !price GOLD для быстрого получения текущей цены"""
    async with ctx.typing():
        data = await asyncio.to_thread(get_market_data_summary, symbol)
        if data:
            await ctx.reply(data)
        else:
            await ctx.reply(f"Не удалось найти котировки по активу: {symbol}")

# ==========================================
# 6. ЗАПУСК
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
