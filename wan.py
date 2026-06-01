from telethon import TelegramClient, events
from telethon.sessions import StringSession
import os

api_id = 36038616
api_hash = "7a0977625d58d2e0d81c4178f49bff31"
session_string = os.environ.get("SESSION_STRING")

client = TelegramClient(StringSession(session_string), api_id, api_hash)

@client.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
async def handler(event):
    await event.reply(event.text)

client.start()
client.run_until_disconnected()
