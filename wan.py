from telethon import TelegramClient, events

api_id = 36038616
api_hash = "7a0977625d58d2e0d81c4178f49bff31"
BOT_USERNAME = "@JBAZ_bot"  # ganti dengan username Tradbot

client = TelegramClient('session', api_id, api_hash)

@client.on(events.NewMessage(incoming=True, from_users=BOT_USERNAME))
async def handler(event):
    await event.reply(event.text)

client.start()
client.run_until_disconnected()
