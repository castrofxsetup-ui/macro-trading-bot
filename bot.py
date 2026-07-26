import os
import re
import base64
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

if not DISCORD_TOKEN:
    print("⚠️ WARNING: DISCORD_TOKEN не найден в переменных окружения Render!")
if not GROQ_API_KEY:
    print("⚠️ WARNING: GROQ_API_KEY не найден в переменных окружения Render!")

groq_client = AsyncGroq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

# Настройки Discord бота
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Веб-сервер FastAPI для Render Health Check (поддержка GET и HEAD)
app = FastAPI()

@app.api_route("/", methods=["GET", "HEAD"])
def read_root():
    return {"status": "ok", "bot": "Macro Bot powered fully by Groq API"}

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

    context_lines.append("ВАЖНО: Опирайся на эти данные при сопоставлении с графиком!")
    return "\n".join(context_lines)

# ==========================================
# 3. СИСТЕМНЫЕ ПРОМПТЫ (SMC/ICT/MSNR)
# ==========================================

SYSTEM_PROMPT = """
Ты — профессиональный аналитик и ментор в закрытом Discord-сообществе для трейдеров.

ТВОЯ СПЕЦИАЛИЗАЦИЯ И МЕХАНИКА АНАЛИЗА:
1. Основа твоего анализа — концепции Smart Money Concepts (SMC), Inner Circle Trader (ICT) и методология Alchemist MSNR.
2. Категорически ИЗБЕГАЙ и НЕ ИСПОЛЬЗУЙ классический технический анализ (никаких индикаторов RSI, MACD, скользящих средних, линий тренда, фигур вроде "голова и плечи").
3. Ты объясняешь движения рынка исключительно через механику ликвидности (Liquidity Sweep, Buy-side / Sell-side Liquidity, PDH/PDL), работу алгоритма доставки цены (IPDA), дисбалансы (FVG / Fair Value Gap), имбалансы, блоки заказов (Order Block, Breaker Block) и структуры MSNR (Market Structure, Market Structure Shift / MSS, Change of Character / CHOCh).
"""

VISION_PROMPT = SYSTEM_PROMPT + """
ИНСТРУКЦИЯ ПО АНАЛИЗУ СКРИНШОТОВ ГРАФИКА:
- Внимательно изучи прикрепленный скриншот с TradingView.
- Определи актив, таймфрейм и направление сетапа (Short / Long).
- Оцени локацию точки входа (Entry), уровня Stop Loss и Take Profit.
- Проверь валидность сетапа по концепциям ICT/SMC/MSNR:
  1. Был ли сдвиг структуры (MSS / BOS)?
  2. Захвачена ли ликвидность перед входом (Liquidity Sweep)?
  3. Находится ли вход в зоне FVG, Order Block или Premium/Discount?
  4. Безопасен ли Стоп-лосс (стоит ли за валидным фракталом/блоком)?
  5. Логичен ли Тейк-профит (направлен ли на снятие пула ликвидности)?
- Дай четкое заключение: Качество сетапа (High Probability / Low Probability), его плюсы и минусы, и что стоит подправить.
"""

# ==========================================
# 4. ЗАПРОСЫ К GROQ API (ТЕКСТ И VISION)
# ==========================================

async def get_groq_vision_response(user_message: str, image_bytes: bytes, mime_type: str) -> str:
    """Анализ скриншотов через модель Groq Vision (meta-llama/llama-3.2-11b-vision-instruct)"""
    if not groq_client:
        return "⚠️ Ошибка: Переменная `GROQ_API_KEY` не настроена на сервисе Render!"

    market_context = await asyncio.to_thread(fetch_live_market_context, user_message)
    prompt_text = user_message if user_message else "Проанализируй этот сетап по концепциям SMC, ICT и Alchemist MSNR."
    
    if market_context:
        prompt_text = f"{market_context}\n\nЗапрос пользователя по графику: {prompt_text}"

    # Кодирование картинки в base64
    base64_image = base64.b64encode(image_bytes).decode('utf-8')
    data_url = f"data:{mime_type};base64,{base64_image}"

    try:
        completion = await groq_client.chat.completions.create(
            model="meta-llama/llama-3.2-11b-vision-instruct",
            messages=[
                {"role": "system", "content": VISION_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt_text},
                        {
                            "type": "image_url",
                            "image_url": {"url": data_url}
                        }
                    ]
                }
            ],
            temperature=0.2,
            max_tokens=1200
        )
        return completion.choices[0].message.content
    except Exception as e:
        error_str = str(e)
        if "429" in error_str:
            return "⏳ Превышен временный лимит запросов Groq API. Пожалуйста, подождите 30-60 секунд."
        print(f"[GROQ VISION ERROR] Ошибка обработки изображения: {e}")
        return f"⚠️ Ошибка при анализе графика через Groq Vision: {e}"

async def get_groq_ai_response(user_message: str) -> str:
    """Текстовые ответы через Llama 3.3 70B Versatile"""
    if not groq_client:
        return "⚠️ Ошибка: Переменная `GROQ_API_KEY` не настроена на сервисе Render."

    market_context = await asyncio.to_thread(fetch_live_market_context, user_message)
    full_prompt = f"{market_context}\n\nЗапрос пользователя: {user_message}" if market_context else user_message

    try:
        completion = await groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": full_prompt}
            ],
            temperature=0.3,
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
    print(f"✅ Бот {bot.user.name} успешно подключился к Discord!")
    print(f"👁️ Анализ графиков переведен на Groq Vision (meta-llama/llama-3.2-11b-vision-instruct)")

@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user:
        return

    if bot.user in message.mentions or isinstance(message.channel, discord.DMChannel):
        async with message.channel.typing():
            clean_content = message.content.replace(f"<@{bot.user.id}>", "").strip()

            image_attachment = None
            if message.attachments:
                for att in message.attachments:
                    if att.content_type and att.content_type.startswith("image/"):
                        image_attachment = att
                        break

            # Если прикреплена картинка -> вызываем Groq Vision
            if image_attachment:
                try:
                    img_bytes = await image_attachment.read()
                    mime_type = image_attachment.content_type or "image/png"
                    ai_reply = await get_groq_vision_response(clean_content, img_bytes, mime_type)
                except Exception as e:
                    ai_reply = f"⚠️ Не удалось прочитать скриншот: {e}"
            
            # Если только текст -> вызываем Groq Llama 3.3
            else:
                if not clean_content:
                    clean_content = "Привет! Пришли скриншот графика или задай вопрос по концепциям SMC/ICT/MSNR."
                ai_reply = await get_groq_ai_response(clean_content)
            
            # Разбивка длинных сообщений для Discord (лимит 2000 символов)
            if len(ai_reply) > 2000:
                for i in range(0, len(ai_reply), 1900):
                    await message.reply(ai_reply[i:i+1900])
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
