import os
import discord
from openai import OpenAI

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

openai_client = OpenAI(api_key=OPENAI_API_KEY)

intents = discord.Intents.default()
intents.message_content = True

client = discord.Client(intents=intents)

@client.event
async def on_ready():
    print(f"Logged in as {client.user}")

def ask_ai(question):
    response = openai_client.responses.create(
        model="gpt-5.4-mini",
        input=[
            {
                "role": "system",
                "content": "You are a helpful assistant. Keep answers short and clear."
            },
            {
                "role": "user",
                "content": question
            }
        ],
    )

    return response.output_text

@client.event
async def on_message(message):
    if message.author == client.user:
        return

    answer = ask_ai(message.content)
    await message.channel.send(answer)

client.run(DISCORD_TOKEN)