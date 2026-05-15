import os
import discord

TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.message_content = True

client = discord.Client(intents=intents)

@client.event
async def on_ready():
    print(f'Logged in as {client.user}')

def ask_ai(question):
    return "You said: " + question + " cefepim"

@client.event
async def on_message(message):

    if message.author == client.user:
        return

    answer = ask_ai(message.content)

    await message.channel.send(answer)

client.run(TOKEN)