import os
import asyncio
import httpx
import discord
from discord.ext import commands
from fastapi import FastAPI
import uvicorn
from groq import AsyncGroq

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

# Настройка интентов Discord
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# FastAPI веб-сервер для поддержки Render (Keep-Alive & Port Binding)
app = FastAPI()

@app.get("/")
def read_root():
    return {"status": "ok", "bot": "Legacy Bot with Groq AI (Smart Money / ICT / Alchemist MSNR)"}

@app.get("/health")
def health_check():
    return {"status": "healthy"}

# ==========================================
# 2. СИСТЕМНЫЙ ПРОМПТ ДЛЯ ТРЕЙДИНГ-КОМЬЮНИТИ
# ==========================================

SYSTEM_PROMPT = """
Ты — эрудированный ИИ-ассистент и ментор в закрытом Discord-сообществе для трейдеров.

ТВОЯ СПЕЦИАЛИЗАЦИЯ И МЕХАНИКА АНАЛИЗА:
1. Основа твоего анализа — концепции Smart Money Concepts (SMC), Inner Circle Trader (ICT) и методология Alchemist MSNR.
2. Категорически ИЗБЕГАЙ и НЕ ИСПОЛЬЗУЙ классический технический анализ (никаких индикаторов RSI, MACD, скользящих средних, линий тренда, фигурного анализа вроде "голова и плечи", "двойное дно" и т.д.).
3. Ты объясняешь движения рынка исключительно через механику ликвидности (Liquidity Sweep, Buy-side / Sell-side Liquidity), работу алгоритма доставки цены (IPDA), дисбалансы (FVG / Fair Value Gap), имбалансы, блоки заказов (Order Block, Breaker Block, Mitigation Block) и структуры MSNR (Market Structure, Market Structure Shift / MSS, Change of Character / CHOCh).
4. Учитывай понятия сессий (Asian Range, London Open, New York Judas Swing), Kill Zones и времени как ключевого фактора (Time & Price).

СТИЛЬ ОБЩЕНИЯ:
- Профессиональный, уверенный, лаконичный и точно подстроенный под трейдинг-комьюнити.
- Используй терминологию ICT/SMC/MSNR на английском или с общепринятой транслитерацией (например, FVG, OB, Liquidity Grab, BOS, MSS, MSNR, BPR, Liquidity Pool).
- Давай четкие структурированные ответы, помогая участникам понимать контекст рынка, а не слепо следовать сигналам.
- Помни: ты ментор и наставник, ориентированный на институциональное понимание механики цены.
"""

# ==========================================
# 3. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ==========================================

async def fetch_forexfactory_news():
    """Безопасный запрос новостей ForexFactory с обработкой HTTP 429"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get("https://www.forexfactory.com/calendar", headers=headers)
            if response.status_code == 429:
                return "⚠️ Превышен лимит запросов к ForexFactory (HTTP 429). Повторите попытку позже."
            elif response.status_code == 200:
                return "📅 Календарь ForexFactory успешно получен."
            else:
                return f"⚠️ ForexFactory вернул статус {response.status_code}."
    except Exception as e:
        return f"⚠️ Ошибка при запросе к ForexFactory: {e}"

async def get_groq_ai_response(user_message: str) -> str:
    """Запрос к Groq API с системным промптом ICT / Smart Money / Alchemist MSNR"""
    if not groq_client:
        return "⚠️ Ошибка: Переменная `GROQ_API_KEY` не настроена на сервисе Render."

    try:
        completion = await groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",  # Флагманская модель с высокими бесплатными лимитами
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_message}
            ],
            temperature=0.6,
            max_tokens=1024,
        )
        return completion.choices[0].message.content
    except Exception as e:
        error_str = str(e)
        if "429" in error_str:
            return "⏳ Достигнут временный лимит запросов Groq API. Пожалуйста, подождите минуту перед следующим вопросом."
        print(f"[ERROR] Ошибка Groq API: {e}")
        return f"⚠️ Ошибка при запросе к ИИ: {e}"

# ==========================================
# 4. СОБЫТИЯ И КОМАНДЫ DISCORD БОТА
# ==========================================

@bot.event
async def on_ready():
    print(f"✅ Бот {bot.user.name} успешно подключился к серверам Discord!")
    print(f"📊 Интегрированы модули: Smart Money Concepts, ICT, Alchemist MSNR")

@bot.event
async def on_message(message: discord.Message):
    # Игнорируем сообщения от самого бота
    if message.author == bot.user:
        return

    # Ответ при упоминании бота (@Legacy Bot) или в личных сообщениях
    if bot.user in message.mentions or isinstance(message.channel, discord.DMChannel):
        async with message.channel.typing():
            # Очищаем текст сообщения от упоминания бота
            clean_content = message.content.replace(f"<@{bot.user.id}>", "").strip()
            if not clean_content:
                clean_content = "Привет! Чем могу помочь по концепциям ICT, SMC и Alchemist MSNR?"
            
            print(f"[AI Request] Получено сообщение от {message.author}: '{clean_content}'")
            ai_reply = await get_groq_ai_response(clean_content)
            
            # Разбивка ответа, если он превышает лимит Discord (2000 символов)
            if len(ai_reply) > 2000:
                for i in range(0, len(ai_reply), 1900):
                    await message.reply(ai_reply[i:i+1900])
            else:
                await message.reply(ai_reply)

    # Обработка стандартных команд (префикс !)
    await bot.process_commands(message)

@bot.command(name="news")
async def news_command(ctx: commands.Context):
    """Команда для получения статуса новостей ForexFactory"""
    async with ctx.typing():
        result = await fetch_forexfactory_news()
        await ctx.reply(result)

@bot.command(name="ai")
async def ai_command(ctx: commands.Context, *, query: str):
    """Прямой запрос к ИИ по концепциям ICT / SMC / MSNR"""
    async with ctx.typing():
        ai_reply = await get_groq_ai_response(query)
        if len(ai_reply) > 2000:
            for i in range(0, len(ai_reply), 1900):
                await ctx.reply(ai_reply[i:i+1900])
        else:
            await ctx.reply(ai_reply)

# ==========================================
# 5. ЗАПУСК ВЕБ-СЕРВЕРА И БОТА
# ==========================================

async def run_fastapi():
    port = int(os.environ.get("PORT", 10000))
    config = uvicorn.Config(app=app, host="0.0.0.0", port=port, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()

async def main():
    if not DISCORD_TOKEN:
        print("CRITICAL ERROR: DISCORD_TOKEN не задан. Бот не может зайти в Discord.")
        return
    
    # Запускаем одновременно веб-сервер FastAPI и Discord бота
    await asyncio.gather(
        run_fastapi(),
        bot.start(DISCORD_TOKEN)
    )

if __name__ == "__main__":
    asyncio.run(main())
