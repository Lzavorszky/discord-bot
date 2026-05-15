import os
import discord
from openai import OpenAI

# Load secrets
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# OpenAI client
openai_client = OpenAI(api_key=OPENAI_API_KEY)

# Discord setup
intents = discord.Intents.default()
intents.message_content = True

client = discord.Client(intents=intents)

# Load CAP protocol
with open("protocols/cap.txt", "r", encoding="utf-8") as f:
    CAP_PROTOCOL = f.read()


@client.event
async def on_ready():
    print(f"Logged in as {client.user}")


def is_cap_question(question):

    q = question.lower()

    cap_keywords = [
        "cap",
        "pneumonia",
        "community acquired pneumonia",
        "chest infection"
    ]

    return any(word in q for word in cap_keywords)


def ask_ai(question):

    # If CAP-related → use protocol
    if is_cap_question(question):

        system_prompt = f"""
You are a clinical protocol assistant.

You MUST answer ONLY using the following CAP protocol.

If the answer is not contained in the protocol, say:
'This is not specified in the CAP protocol.'

CAP PROTOCOL:
{CAP_PROTOCOL}

Rules:
- Keep answers concise
- Use bullet points
- Mention uncertainty clearly
- Ask for missing information if needed
- Do not invent recommendations
"""

    else:

        system_prompt = """
You are a helpful assistant.
Keep answers concise.
"""

    response = openai_client.responses.create(
        model="gpt-5.4-mini",
        input=[
            {
                "role": "system",
                "content": system_prompt
            },
            {
                "role": "user",
                "content": question
            }
        ],
    )

    return response.output_text


def split_message(text, max_length=1900):

    chunks = []

    while len(text) > max_length:

        split_at = text.rfind("\n", 0, max_length)

        if split_at == -1:
            split_at = max_length

        chunks.append(text[:split_at])
        text = text[split_at:].strip()

    if text:
        chunks.append(text)

    return chunks


@client.event
async def on_message(message):

    if message.author == client.user:
        return

    question = message.content.strip()

    if not question:
        return

    async with message.channel.typing():

        answer = ask_ai(question)

    for chunk in split_message(answer):
        await message.channel.send(chunk)


client.run(DISCORD_TOKEN)