import asyncio
import os
from aiohttp import web
from starknet_py.net.full_node_client import FullNodeClient
from starknet_py.hash.selector import get_selector_from_name
from aiogram import Bot

# Keep your existing Telegram HTML bot and Starknet logic here
async def monitor_factory_events():
    bot_token = os.getenv("BOT_TOKEN")
    chat_id = os.getenv("CHAT_ID")
    # ... your existing code ...
    while True:
        await asyncio.sleep(15)

async def handle_health_check(request):
    return web.Response(text="Bot is active.")

async def start_web_server():
    app = web.Application()
    app.add_routes([web.get('/', handle_health_check)])
    runner = web.AppRunner(app)
    await runner.setup()
    
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()

async def main():
    await asyncio.gather(
        start_web_server(),
        monitor_factory_events()
    )

if __name__ == "__main__":
    asyncio.run(main())
