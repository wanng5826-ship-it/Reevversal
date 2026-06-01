from telethon import TelegramClient, events
from telethon.sessions import StringSession
import os
import asyncio

api_id = 36038616
api_hash = "7a0977625d58d2e0d81c4178f49bff31"
session_string = os.environ.get("SESSION_STRING")
BOT_USERNAME = "@JBAZ_bot"

client = TelegramClient(StringSession(session_string), api_id, api_hash)

@client.on(events.NewMessage(incoming=True, from_users=BOT_USERNAME))
async def handler(event):
    await event.reply(event.text)

async def main():
    await client.start()
    print("Userbot jalan!")
    await client.run_until_disconnected()

asyncio.run(main())
