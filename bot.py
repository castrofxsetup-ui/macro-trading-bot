import discord
from discord.ext import commands, tasks
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
import os

# --- АВТОМАТИЧЕСКИЙ ВЕБ-СЕРВЕР ДЛЯ ОБХОДА ПЛАТНОГО ТАРИФА RENDER ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/html")
        self.end_headers()
        self.wfile.write(b"Macro Bot is perfectly running!")

def run_web_server():
    # Render автоматически передает свободный порт в переменную окружения PORT.
    # Если её нет, используем стандартный 10000.
    port = int(os.getenv("PORT", 10000))
    server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
    server.serve_forever()

# Запускаем сервер строго в фоновом потоке
threading.Thread(target=run_web_server, daemon=True).start()
# ------------------------------------------------------------------

# НАСТРОЙКИ КАНАЛОВ БОТА
NEWS_CHANNEL_ID = 1528319066513604688     # Ветка для новостей Forex Factory
STREAMS_CHANNEL_ID = 1528506824687485118  # Ветка для уведомлений о стримах
TASK_CHANNEL_ID = 1502292137889501235     # Ветка для утренних заданий дня

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

@bot.event
async def on_ready():
    print(f"Бот {bot.user.name} успешно подключился к серверам Discord!")
    if not main_checking_loop.is_running():
        main_checking_loop.start()

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
            url = "https://forexfactory.com"
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req) as response:
                xml_data = response.read()
            
            root = ET.fromstring(xml_data)
            daily_events = []
            
            for event in root.findall('event'):
                impact = event.find('impact').text
                if impact not in ["High", "Medium"]:
                    continue
                    
                title = event.find('title').text
                currency = event.find('currency').text
                date_str = event.find('event_date' if event.find('event_date') is not None else 'date').text
                time_str = event.find('time').text
                
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
            print(f"Ошибка ежедневной сводки: {e}")

    # МОДУЛЬ 2. МОНИТОРИНГ КРАСНЫХ НОВОСТЕЙ (ЗА 15 МИНУТ)
    if news_channel:
        try:
            url = "https://forexfactory.com"
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req) as response:
                xml_data = response.read()
            
            root = ET.fromstring(xml_data)
            for event in root.findall('event'):
                if event.find('impact').text != "High":
                    continue
                    
                title = event.find('title').text
                currency = event.find('currency').text
                date_str = event.find('event_date' if event.find('event_date') is not None else 'date').text
                time_str = event.find('time').text
                
                try:
                    event_datetime = datetime.strptime(f"{date_str} {time_str}", "%m-%d-%Y %I:%M%p").replace(tzinfo=timezone(timedelta(hours=-5)))
                except Exception:
                    continue

                time_diff = event_datetime - now_utc
                event_id = f"{title}_{date_str}_{time_str}"

                if timedelta(minutes=14) <= time_diff <= timedelta(minutes=16) and event_id not in notified_news:
                    flag = FLAGS.get(currency.upper(), "🌐")
                    embed_description = (
                        f"**Ожидаемые события:**\n"
                        f"{flag} **{currency}** — {title}\n"
                        f"⏰ {time_str} (Нью-Йорк)\n"
                        f"🔴 HIGH\n\n"
                        f"<sub>⌛️Публикация через 15 минут</sub>"
                    )
                    embed = discord.Embed(description=embed_description, color=0xff0000)
                    await news_channel.send(content="@everyone", embed=embed)
                    notified_news.add(event_id)
        except Exception as e:
            print(f"Ошибка календаря: {e}")

    # МОДУЛЬ 3. МОНИТОРИНГ МЕРОПРИЯТИЙ И СТРИМОВ (ЗА 30 МИНУТ)
    streams_channel = bot.get_channel(STREAMS_CHANNEL_ID)
    if streams_channel:
        for guild in bot.guilds:
            try:
                events = await guild.fetch_scheduled_events()
                for event in events:
                    if event.status != discord.EventStatus.scheduled:
                        continue
                    time_to_start = event.start_time - now_utc
                    if timedelta(minutes=28) <= time_to_start <= timedelta(minutes=32) and event.id not in notified_events_30m:
                        msg_text = f"@everyone ⏰ Напоминаем: через полчаса начинается **{event.name}**. Присоединяйтесь 👇\n\n\n{event.url}"
                        await streams_channel.send(msg_text)
                        notified_events_30m.add(event.id)
            except Exception as e:
                print(f"Ошибка проверки мероприятий: {e}")

# Запуск
bot.run(os.getenv("DISCORD_TOKEN"))
