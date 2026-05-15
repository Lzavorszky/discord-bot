import os
import glob
import numpy as np
from openai import OpenAI
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, ContextTypes, filters

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

openai_client = OpenAI(api_key=OPENAI_API_KEY)

EMBEDDING_MODEL = "text-embedding-3-small"
CHAT_MODEL = "gpt-5.4-mini"

PROTOCOL_CHUNKS = []


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

    files = glob.glob("protocols/**/*.txt", recursive=True)

    for file_path in files:
        with open(file_path, "r", encoding="utf-8") as f:
            text = f.read()

        chunks = chunk_text(text, source=file_path)

        for chunk in chunks:
            chunk["embedding"] = get_embedding(chunk["text"])
            PROTOCOL_CHUNKS.append(chunk)

    print(f"Loaded {len(PROTOCOL_CHUNKS)} protocol chunks")


def search_protocols(question, top_k=3):
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


def format_debug_output(retrieved_chunks):
    debug_text = "DEBUG — retrieved protocol chunks:\n\n"

    for i, chunk in enumerate(retrieved_chunks, start=1):
        preview = chunk["text"][:600].replace("\n", " ")

        debug_text += (
            f"{i}. Source: {chunk['source']}\n"
            f"   Similarity: {chunk['similarity']:.4f}\n"
            f"   Preview: {preview}...\n\n"
        )

    return debug_text


def ask_ai(question):
    retrieved_chunks = search_protocols(question, top_k=3)

    context = "\n\n---\n\n".join(
        [f"Source: {c['source']}\n{c['text']}" for c in retrieved_chunks]
    )

    response = openai_client.responses.create(
        model=CHAT_MODEL,
        input=[
            {
                "role": "system",
                "content": f"""
You are a hospital protocol assistant.

Answer ONLY using the protocol excerpts below.

If the answer is not clearly contained in the protocol excerpts, say:
"This is not specified in the uploaded protocol."

Do not use outside medical knowledge.
Do not invent recommendations.
Do not ask for patient identifiers.
Keep answers concise.
Use bullet points where useful.
Mention the source file.

PROTOCOL EXCERPTS:
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


def split_message(text, max_length=3500):
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


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    question = update.message.text.strip()

    if not question:
        return

    await update.message.chat.send_action(action="typing")

    if question.lower().startswith("/debug"):
        debug_question = question.replace("/debug", "", 1).strip()

        if not debug_question:
            answer = "Please provide a question after /debug."
        else:
            retrieved_chunks = search_protocols(debug_question, top_k=5)
            answer = format_debug_output(retrieved_chunks)

    else:
        answer = ask_ai(question)

    for chunk in split_message(answer):
        await update.message.reply_text(chunk)


def main():
    load_protocols()

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )

    print("Telegram RAG bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()