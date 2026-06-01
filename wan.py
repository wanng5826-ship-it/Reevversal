from telethon import TelegramClient, events
from telethon.sessions import StringSession
import os
import asyncio

api_id = int(os.environ.get("API_ID"))
api_hash = os.environ.get("API_HASH")
session_string = os.environ.get("SESSION_STRING")
BOT_USERNAME = "@JBAZ_bot"

client = TelegramClient(StringSession(session_string), api_id, api_hash)

@client.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
async def handler(event):
    await event.forward_to(BOT_USERNAME)

async def main():
    await client.start()
    print("Userbot jalan!")
    await client.run_until_disconnected()

asyncio.run(main())
