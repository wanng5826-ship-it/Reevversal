from telethon import TelegramClient, events

api_id = 123456        # ganti dengan punyamu
api_hash = "xxxx"      # ganti dengan punyamu
BOT_USERNAME = "username_tradbot"  # ganti username Tradbot

client = TelegramClient('session', api_id, api_hash)

@client.on(events.NewMessage(incoming=True, from_users=BOT_USERNAME))
async def handler(event):
    await event.reply(event.text)

client.start()
client.run_until_disconnected()
