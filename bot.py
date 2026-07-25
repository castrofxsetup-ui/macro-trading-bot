import discord
from discord.ext import commands, tasks
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
import os

# --- ВЕБ-СЕРВЕР ДЛЯ ОБХОДА ПЛАТНОГО ТАРИФА RENDER ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/html")
        self.end_headers()
        self.wfile.write(b"Macro Bot is perfectly running!")

def run_web_server():
    server = HTTPServer(('0.0.0.0', 10000), HealthCheckHandler)
    server.serve_forever()

threading.Thread(target=run_web_server, daemon=True).start()
# ----------------------------------------------------

# ТОЧНЫЕ НАСТРОЙКИ ВЕТОК И КАНАЛОВ ИЗ ВАШЕГО ЗАПРОСА
NEWS_CHANNEL_ID = 1528319066513604688     # Ветка для красных новостей Forex Factory
STREAMS_CHANNEL_ID = 1528506824687485118  # Ветка для уведомлений о стримах и мероприятиях

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.guild_scheduled_events = True  # Разрешение на чтение стримов сервера

bot = commands.Bot(command_prefix="!", intents=intents)

# Базы данных для предотвращения дубликатов сообщений
notified_news = set()
notified_events_60m = set()
notified_events_15m = set()

@bot.event
async def on_ready():
    print(f"Бот {bot.user.name} успешно запущен!")
    main_checking_loop.start()  # Запуск цикла ежеминутной проверки

@tasks.loop(seconds=60)
async def main_checking_loop():
    now_utc = datetime.now(timezone.utc)

    # 1. МОДУЛЬ ЭКОНОМИЧЕСКИХ НОВОСТЕЙ (FOREX FACTORY)
    news_channel = bot.get_channel(NEWS_CHANNEL_ID)
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
                date_str = event.find('date').text
                time_str = event.find('time').text
                
                try:
                    # Время в XML-фиде идет по Нью-Йорку (EST/EDT, UTC-5)
                    event_datetime = datetime.strptime(f"{date_str} {time_str}", "%m-%d-%Y %I:%M%p").replace(tzinfo=timezone(timedelta(hours=-5)))
                except Exception:
                    continue

                time_diff = event_datetime - now_utc
                event_id = f"{title}_{date_str}_{time_str}"

                # Строго за 30 минут (интервал от 29 до 31 минуты для надежности парсинга)
                if timedelta(minutes=29) <= time_diff <= timedelta(minutes=31) and event_id not in notified_news:
                    embed = discord.Embed(title="🚨 ВНИМАНИЕ! КРАСНЫЕ НОВОСТИ", color=0xff0000)
                    embed.add_field(name="Ожидаемые события:", value=title, inline=False)
                    embed.add_field(name="Актив:", value=f"**{currency}**", inline=True)
                    embed.add_field(name="Время выхода:", value=f"⏰ {time_str} (Нью-Йорк)", inline=True)
                    embed.add_field(name="Важность:", value="🔴 HIGH IMPACT", inline=False)
                    embed.set_footer(text="Публикация через 30 минут")

                    await news_channel.send(content="@everyone Срочное предупреждение о волатильности!", embed=embed)
                    notified_news.add(event_id)
        except Exception as e:
            print(f"Ошибка календаря: {e}")

    # 2. МОДУЛЬ МОНИТОРИНГА МЕРОПРИЯТИЙ И СТРИМОВ СЕРВЕРА
    streams_channel = bot.get_channel(STREAMS_CHANNEL_ID)
    if streams_channel:
        for guild in bot.guilds:
            try:
                events = await guild.fetch_scheduled_events()
                for event in events:
                    if event.status != discord.EventStatus.scheduled:
                        continue
                    
                    time_to_start = event.start_time - now_utc
                    event_url = f"https://discord.com{guild.id}/{event.id}"

                    # Оповещение за 60 минут
                    if timedelta(minutes=58) <= time_to_start <= timedelta(minutes=62) and event.id not in notified_events_60m:
                        msg_text = (
                            f"@everyone Напоминаем: через 60 минут начинается {event.name}\n"
                            f"Присоединяйтесь!\n"
                            f"{event_url}"
                        )
                        await streams_channel.send(msg_text)
                        notified_events_60m.add(event.id)

                    # Оповещение за 15 минут
                    if timedelta(minutes=13) <= time_to_start <= timedelta(minutes=17) and event.id not in notified_events_15m:
                        msg_text = (
                            f"@everyone Напоминаем: через 15 минут начинается {event.name}\n"
                            f"Присоединяйтесь!\n"
                            f"{event_url}"
                        )
                        await streams_channel.send(msg_text)
                        notified_events_15m.add(event.id)
            except Exception as e:
                print(f"Ошибка проверки мероприятий: {e}")

# Безопасный вызов токена из скрытых настроек хостинга
bot.run(os.getenv("DISCORD_TOKEN"))
