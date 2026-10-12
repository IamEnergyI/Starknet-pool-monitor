import asyncio
import os
from datetime import datetime, timedelta, timezone
from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.types import Message
from aiogram.filters import Command
from starknet_py.net.full_node_client import FullNodeClient
from starknet_py.hash.selector import get_selector_from_name
from supabase import create_client, Client

bot_state = {"last_checked_block": 0}
dp = Dispatcher()

# Connect to Supabase
supabase_url = os.environ.get("SUPABASE_URL")
supabase_key = os.environ.get("SUPABASE_KEY")
db: Client = create_client(supabase_url, supabase_key)

@dp.message(Command("report"))
async def send_report(message: Message):
    report_text = (
        "<b>Status Report</b>\n"
        "<i>Monitoring:</i> <code>Active</code>\n"
        "<i>Last Checked Block:</i> <code>" + str(bot_state["last_checked_block"]) + "</code>"
    )
    await message.answer(text=report_text, parse_mode="HTML")

@dp.message(Command("recent"))
async def get_recent_tokens(message: Message):
    command_args = message.text.split()
    
    if len(command_args) < 2:
        error_text = (
            "<b>Command Error</b>\n"
            "<i>Reason:</i> <code>Missing timeframe</code>\n"
            "<i>Usage:</i> <code>/recent 2h</code> or <code>/recent 7d</code>"
        )
        await message.answer(text=error_text, parse_mode="HTML")
        return
        
    time_limit = command_args[1]
    hours_to_subtract = 0
    
    if time_limit.endswith("h"):
        hours_to_subtract = int(time_limit.replace("h", ""))
    elif time_limit.endswith("d"):
        hours_to_subtract = int(time_limit.replace("d", "")) * 24
    else:
        await message.answer(text="<b>Error:</b> Use 'h' for hours or 'd' for days.", parse_mode="HTML")
        return
        
    target_time = datetime.now(timezone.utc) - timedelta(hours=hours_to_subtract)
    target_time_iso = target_time.isoformat()
    
    try:
        response = db.table("pools").select("*").gte("created_at", target_time_iso).execute()
        pools = response.data
        
        result_text = f"<b>Recent Pools ({time_limit})</b>\n"
        result_text += f"<i>Total found:</i> <code>{len(pools)}</code>\n\n"
        
        # Only show the last 5 results to prevent Telegram message length limits
        for pool in pools[-5:]: 
            result_text += f"<i>Block:</i> <code>{pool['block_number']}</code>\n"
            result_text += f"<i>Transaction:</i> <code>{pool['transaction_hash']}</code>\n\n"
            
        await message.answer(text=result_text, parse_mode="HTML")
    except Exception as error:
        await message.answer(text=f"<b>Database Error:</b> {error}", parse_mode="HTML")

async def monitor_factory_events(bot: Bot, chat_id: str, rpc_url: str):
    client = FullNodeClient(node_url=rpc_url)
    factory_address_hex = "0x00add5529cf10e8d0e2e9c1598f8eeeb1203eb658fbff80e92cae6a4b3f81e3a"
    event_key = get_selector_from_name("PoolCreated")

    bot_state["last_checked_block"] = await client.get_block_number()
    print(f"Monitoring started at block: {bot_state['last_checked_block']}")

    startup_text = (
        "<b>Starknet Monitor Active</b>\n"
        "<i>Current Block:</i> <code>" + str(bot_state['last_checked_block']) + "</code>\n"
        "<i>Status:</i> <code>Connected to RPC & Database</code>"
    )
    await bot.send_message(chat_id=chat_id, text=startup_text, parse_mode="HTML")

    while True:
        try:
            current_block = await client.get_block_number()

            if current_block > bot_state["last_checked_block"]:
                events_response = await client.get_events(
                    address=int(factory_address_hex, 16),
                    keys=[[event_key]],
                    from_block_number=bot_state["last_checked_block"] + 1,
                    to_block_number=current_block,
                    chunk_size=100
                )

                for event in events_response.events:
                    tx_hash = hex(event.transaction_hash)
                    
                    # Store the new pool in Supabase
                    db.table("pools").insert({
                        "block_number": event.block_number,
                        "transaction_hash": tx_hash
                    }).execute()
                    
                    pool_text = (
                        "<b>New Starknet Pool Created</b>\n"
                        "<i>Block:</i> <code>" + str(event.block_number) + "</code>\n"
                        "<i>Transaction:</i> <code>" + tx_hash + "</code>"
                    )
                    await bot.send_message(chat_id=chat_id, text=pool_text, parse_mode="HTML")

                bot_state["last_checked_block"] = current_block

            await asyncio.sleep(15)

        except Exception as error:
            print(f"Error during polling: {error}")
            await asyncio.sleep(15)

async def handle_health_check(request):
    return web.Response(text="Bot is running.")

async def start_web_server():
    app = web.Application()
    app.add_routes([web.get('/', handle_health_check)])
    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"Web server started on port {port}")

async def main():
    bot_token = os.getenv("BOT_TOKEN")
    chat_id = os.getenv("CHAT_ID")
    rpc_url = os.getenv("STARKNET_RPC_URL")

    if not bot_token or not chat_id or not rpc_url:
        raise ValueError("Environment variables are missing.")

    bot = Bot(token=bot_token)

    await asyncio.gather(
        start_web_server(),
        monitor_factory_events(bot, chat_id, rpc_url),
        dp.start_polling(bot)
    )

if __name__ == "__main__":
    asyncio.run(main())
