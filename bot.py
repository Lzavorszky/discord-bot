import os
import discord
from openai import OpenAI

# Load secrets from Railway environment variables
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# OpenAI client
openai_client = OpenAI(api_key=OPENAI_API_KEY)

# Discord setup
intents = discord.Intents.default()
intents.message_content = True

client = discord.Client(intents=intents)

@client.event
async def on_ready():
    print(f"Logged in as {client.user}")

# AI function
def ask_ai(question):

    response = openai_client.responses.create(
        model="gpt-5.4-mini",
        input=[
            {
                "role": "system",
                "content": """
You are a clinical protocol assistant.

Rules:
- Keep answers concise.
- Ask clarifying questions if important information is missing.
- Use bullet points when appropriate.
- State uncertainty clearly.
- Do not invent hospital protocol recommendations.
- Do not claim to replace a clinician.
"""
            },
            {
                "role": "user",
                "content": question
            }
        ],
    )

    return response.output_text

# Discord message handler
@client.event
async def on_message(message):

    # Prevent bot replying to itself
    if message.author == client.user:
        return

    # Show typing indicator while AI is thinking
    async with message.channel.typing():

        answer = ask_ai(message.content)

    # Send response back to Discord
    await message.channel.send(answer)

# Start bot
client.run(DISCORD_TOKEN)