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


def clinical_safety_check(question):
    """
    Simple rule-based safety layer.
    This runs BEFORE the AI call.
    """

    q = question.lower()

    clinical_keywords = [
        "antibiotic",
        "antibiotics",
        "infection",
        "sepsis",
        "cellulitis",
        "pneumonia",
        "uti",
        "septic arthritis",
        "joint infection",
        "wound infection",
        "abscess",
        "fever",
    ]

    if any(word in q for word in clinical_keywords):
        return """
Before giving a clinical suggestion, please provide:

- Infection source/site
- Patient age
- Relevant allergies
- Renal function/eGFR or creatinine
- Severity: stable, septic, or shock
- Culture/microbiology results if available
- Pregnancy/immunosuppression if relevant

No patient identifiers please.
"""

    return None


def ask_ai(question):
    """
    Sends the user's question to OpenAI and returns the answer.
    """

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
- Do not accept or request patient identifiers.
- For clinical topics, remind the user that this is decision support only.
"""
            },
            {
                "role": "user",
                "content": question
            }
        ],
    )

    return response.output_text


def split_message(text, max_length=1900):
    """
    Discord has a 2000-character message limit.
    This splits long answers into smaller chunks.
    """

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

    # Prevent bot replying to itself
    if message.author == client.user:
        return

    question = message.content.strip()

    if not question:
        return

    async with message.channel.typing():

        # Step 4: simple clinical safety trigger
        safety_response = clinical_safety_check(question)

        if safety_response:
            answer = safety_response
        else:
            answer = ask_ai(question)

    for chunk in split_message(answer):
        await message.channel.send(chunk)


# Start bot
client.run(DISCORD_TOKEN)