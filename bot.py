import os
import glob
import discord
import numpy as np
from openai import OpenAI

# Environment variables from Railway
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# OpenAI client
openai_client = OpenAI(api_key=OPENAI_API_KEY)

# Discord setup
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

# Models
EMBEDDING_MODEL = "text-embedding-3-small"
CHAT_MODEL = "gpt-5.4-mini"

# Global storage
PROTOCOL_CHUNKS = []
SYSTEM_RULES = ""


def load_system_rules():
    rule_files = glob.glob("protocols/system/*.txt")
    rules = []

    for file_path in rule_files:
        with open(file_path, "r", encoding="utf-8") as f:
            rules.append(f"Source: {file_path}\n{f.read()}")

    return "\n\n---\n\n".join(rules)


def chunk_text(text, source, max_chars=900):
    sections = text.split("\n\n")
    chunks = []
    current = ""

    for section in sections:
        if len(current) + len(section) < max_chars:
            current += section + "\n\n"
        else:
            if current.strip():
                chunks.append({"source": source, "text": current.strip()})
            current = section + "\n\n"

    if current.strip():
        chunks.append({"source": source, "text": current.strip()})

    return chunks


def get_embedding(text):
    response = openai_client.embeddings.create(
        model=EMBEDDING_MODEL,
        input=text
    )
    return np.array(response.data[0].embedding)


def load_protocols():
    global PROTOCOL_CHUNKS

    files = glob.glob("protocols/medical/*.txt")

    for file_path in files:
        with open(file_path, "r", encoding="utf-8") as f:
            text = f.read()

        chunks = chunk_text(text, source=file_path)

        for chunk in chunks:
            chunk["embedding"] = get_embedding(chunk["text"])
            PROTOCOL_CHUNKS.append(chunk)

    print(f"Loaded {len(PROTOCOL_CHUNKS)} medical protocol chunks")


def search_protocols(question, top_k=4):
    question_embedding = get_embedding(question)

    results = []

    for chunk in PROTOCOL_CHUNKS:
        similarity = float(np.dot(question_embedding, chunk["embedding"]))

        results.append({
            "source": chunk["source"],
            "text": chunk["text"],
            "similarity": similarity
        })

    results.sort(key=lambda x: x["similarity"], reverse=True)

    return results[:top_k]


@client.event
async def on_ready():
    global SYSTEM_RULES

    print(f"Logged in as {client.user}")

    if not SYSTEM_RULES:
        SYSTEM_RULES = load_system_rules()
        print("Loaded system rules")

    if not PROTOCOL_CHUNKS:
        load_protocols()


def ask_ai(question):
    retrieved_chunks = search_protocols(question, top_k=4)

    context = "\n\n---\n\n".join(
        [
            f"Source: {chunk['source']}\n"
            f"Similarity: {chunk['similarity']:.3f}\n"
            f"{chunk['text']}"
            for chunk in retrieved_chunks
        ]
    )

    response = openai_client.responses.create(
        model=CHAT_MODEL,
        input=[
            {
                "role": "system",
                "content": f"""
{SYSTEM_RULES}

You are answering using retrieved hospital protocol excerpts.

Use ONLY the retrieved medical protocol excerpts below.
Do not use outside medical knowledge.
Do not invent recommendations.
Do not treat system or safety rules as medical treatment content.

If the answer is not clearly contained in the protocol excerpts, say exactly:
"This is not specified in the uploaded protocol."

When answering:
- Be concise.
- Mention the relevant protocol source file.
- Include important notes from the same retrieved section.
- If the question is broad, summarize the relevant protocol pathways.
- If the question lacks necessary pathway information, ask for the missing information.

RETRIEVED MEDICAL PROTOCOL EXCERPTS:
{context}
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