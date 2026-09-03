"""
GGS Knowledge Base Ingestion Script
====================================
Uses GPT-4.1-mini (Azure) vision to extract text from PDF pages,
then chunks, embeds, and stores in Qdrant under 'kb_ggs_support'.

Handles:
- Text overlaid on images
- Tables with dot/checkmark indicators → natural language
- Multi-page brochures and spec sheets

Usage:
    python ingest_briefs.py
"""

import asyncio
import base64
import json
import os
import sys
import time
import urllib.request
import uuid
from pathlib import Path

import pymupdf  # PyMuPDF for PDF → image conversion

# ─── Config ──────────────────────────────────────────────────────────────
GGS_SAMPLES_DIR = "/Users/arnavmehta/Desktop/GGS Samples"

# Azure GPT-4.1-mini (Vision)
AZURE_CHAT_RESOURCE = os.getenv("AZURE_OPENAI_RESOURCE", "").rstrip("/")
VISION_DEPLOYMENT = os.getenv("VISION_DEPLOYMENT", "gpt-4.1-mini")
VISION_ENDPOINT = (
    f"{AZURE_CHAT_RESOURCE}/openai/deployments/{VISION_DEPLOYMENT}"
    "/chat/completions?api-version=2025-01-01-preview"
)

# Azure Embeddings (reuse existing .env config)
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

AZURE_API_KEY = os.getenv("AZURE_OPENAI_CHAT_API_KEY", "")
AZURE_EMBEDDING_ENDPOINT = os.getenv("AZURE_OPENAI_EMBEDDING_ENDPOINT", "").rstrip("/")
AZURE_EMBEDDING_API_KEY = os.getenv("AZURE_OPENAI_EMBEDDING_API_KEY", "")
AZURE_EMBEDDING_DEPLOYMENT = os.getenv("AZURE_OPENAI_EMBEDDING_DEPLOYMENT", "text-embedding-3-small")
AZURE_EMBEDDING_API_VERSION = os.getenv("AZURE_OPENAI_EMBEDDING_API_VERSION", "2023-05-15")

# Qdrant
QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY", None)
COLLECTION_NAME = "kb_ggs_support"
VECTOR_DIM = 1536

# Chunking
CHUNK_SIZE = 1500
CHUNK_OVERLAP = 200

VISION_SYSTEM_PROMPT = """You are a document text extractor. Your job is to extract ALL text content from PDF page images with perfect accuracy.

Rules:
1. Extract every piece of text visible on the page — headings, body text, captions, footnotes, page numbers.
2. For tables with dots (■), squares, or checkmarks as indicators:
   - Convert each row into a clear natural language sentence.
   - Format: "Feature X: Available with Product A, Product B. NOT available with Product C, Product D."
3. For spec sheets with technical data, preserve exact numbers, units, and model names.
4. For text overlaid on images or colored backgrounds, extract it as normal text.
5. Preserve the logical reading order of the page.
6. Do NOT describe images — only extract text content.
7. Do NOT add any commentary or analysis — just the extracted text."""


def extract_page_image(doc, page_num: int) -> str:
    """Convert a PDF page to base64-encoded PNG image, auto-scaling for large pages."""
    page = doc[page_num]
    # Start at 150 DPI; if the resulting image is too large (>1.5MB), reduce further
    for dpi in [150, 100, 72]:
        pix = page.get_pixmap(dpi=dpi)
        img_bytes = pix.tobytes("png")
        if len(img_bytes) < 1_500_000:  # Under 1.5MB
            break
    b64 = base64.b64encode(img_bytes).decode()
    return b64


def call_vision_api(b64_image: str, retries: int = 3) -> str:
    """Send a page image to GPT-4.1-mini vision and get extracted text, with retry logic."""
    payload = {
        "messages": [
            {"role": "system", "content": VISION_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "Extract ALL text from this PDF page image following the rules in your system prompt.",
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/png;base64,{b64_image}",
                            "detail": "high",
                        },
                    },
                ],
            },
        ],
        "max_tokens": 4000,
        "temperature": 0.0,
    }
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                VISION_ENDPOINT,
                data=json.dumps(payload).encode(),
                headers={
                    "api-key": AZURE_API_KEY,
                    "Content-Type": "application/json",
                },
            )
            resp = urllib.request.urlopen(req, timeout=180)
            data = json.loads(resp.read())
            text = data["choices"][0]["message"]["content"]
            tokens = data["usage"]
            return text, tokens["prompt_tokens"], tokens["completion_tokens"]
        except Exception as e:
            last_err = e
            wait = 2 ** (attempt + 1)
            print(f"\n      ⚠️  Attempt {attempt+1} failed: {e}. Retrying in {wait}s...", flush=True)
            time.sleep(wait)
    raise last_err


def chunk_text(text: str) -> list[str]:
    """Split text into overlapping chunks for vector search."""
    if len(text) <= CHUNK_SIZE:
        return [text] if text.strip() else []

    chunks = []
    start = 0
    while start < len(text):
        end = start + CHUNK_SIZE
        chunk = text[start:end]

        # Try to break at sentence boundary
        if end < len(text):
            last_period = chunk.rfind(". ")
            last_newline = chunk.rfind("\n")
            break_at = max(last_period, last_newline)
            if break_at > CHUNK_SIZE // 2:
                chunk = text[start : start + break_at + 1]
                end = start + break_at + 1

        if chunk.strip():
            chunks.append(chunk.strip())

        start = end - CHUNK_OVERLAP

    return chunks


def get_embedding(text: str) -> list[float]:
    """Get embedding vector from Azure OpenAI text-embedding-3-small."""
    url = f"{AZURE_EMBEDDING_ENDPOINT}/openai/deployments/{AZURE_EMBEDDING_DEPLOYMENT}/embeddings?api-version={AZURE_EMBEDDING_API_VERSION}"
    payload = {"input": text}
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={
            "api-key": AZURE_EMBEDDING_API_KEY,
            "Content-Type": "application/json",
        },
    )
    resp = urllib.request.urlopen(req, timeout=30)
    data = json.loads(resp.read())
    return data["data"][0]["embedding"]


def get_embeddings_batch(texts: list[str]) -> list[list[float]]:
    """Get embeddings for a batch of texts (Azure supports up to 16 in one call)."""
    url = f"{AZURE_EMBEDDING_ENDPOINT}/openai/deployments/{AZURE_EMBEDDING_DEPLOYMENT}/embeddings?api-version={AZURE_EMBEDDING_API_VERSION}"
    # Azure supports batching — send up to 16 at a time
    all_embeddings = []
    batch_size = 16
    for i in range(0, len(texts), batch_size):
        batch = texts[i : i + batch_size]
        payload = {"input": batch}
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={
                "api-key": AZURE_EMBEDDING_API_KEY,
                "Content-Type": "application/json",
            },
        )
        resp = urllib.request.urlopen(req, timeout=60)
        data = json.loads(resp.read())
        # Sort by index to maintain order
        sorted_data = sorted(data["data"], key=lambda x: x["index"])
        all_embeddings.extend([d["embedding"] for d in sorted_data])
        if i + batch_size < len(texts):
            time.sleep(0.5)  # Rate limiting
    return all_embeddings


def setup_qdrant():
    """Create or recreate the Qdrant collection."""
    from qdrant_client import QdrantClient
    from qdrant_client.models import Distance, VectorParams

    client = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY, timeout=10.0)

    # Delete existing collection if it exists
    try:
        client.delete_collection(COLLECTION_NAME)
        print(f"  ♻️  Deleted existing collection '{COLLECTION_NAME}'")
    except Exception:
        pass

    client.create_collection(
        collection_name=COLLECTION_NAME,
        vectors_config=VectorParams(size=VECTOR_DIM, distance=Distance.COSINE),
    )
    print(f"  ✅ Created collection '{COLLECTION_NAME}' ({VECTOR_DIM}d, cosine)")
    return client


def store_chunks(client, chunks: list[str], embeddings: list[list[float]], filename: str, page_num: int):
    """Store chunks with embeddings in Qdrant."""
    from qdrant_client.models import PointStruct

    points = []
    for i, (chunk, embedding) in enumerate(zip(chunks, embeddings)):
        point_id = str(uuid.uuid4())
        points.append(
            PointStruct(
                id=point_id,
                vector=embedding,
                payload={
                    "text": chunk,
                    "filename": filename,
                    "page": page_num + 1,
                    "chunk_index": i,
                    "agent_type": "ggs_support",
                    "category": "pdf_knowledge_base",
                },
            )
        )

    if points:
        client.upsert(collection_name=COLLECTION_NAME, points=points)


def main():
    print("=" * 60)
    print("  GGS Knowledge Base Ingestion Pipeline")
    print("  Using: GPT-4.1-mini (Vision) + text-embedding-3-small")
    print("=" * 60)

    # 1. Find all PDFs
    pdf_dir = Path(GGS_SAMPLES_DIR)
    pdf_files = sorted(pdf_dir.glob("*.pdf"))
    print(f"\n📁 Found {len(pdf_files)} PDFs in {GGS_SAMPLES_DIR}:\n")
    for f in pdf_files:
        size_kb = f.stat().st_size / 1024
        print(f"   • {f.name} ({size_kb:.0f} KB)")

    # 2. Setup Qdrant
    print(f"\n🔧 Setting up Qdrant collection...")
    qdrant_client = setup_qdrant()

    # 3. Process each PDF
    total_pages = 0
    total_chunks = 0
    total_input_tokens = 0
    total_output_tokens = 0
    all_extracted = {}

    for pdf_file in pdf_files:
        print(f"\n{'─' * 50}")
        print(f"📄 Processing: {pdf_file.name}")
        print(f"{'─' * 50}")

        doc = pymupdf.open(str(pdf_file))
        print(f"   Pages: {doc.page_count}")

        pdf_text_pages = []

        for page_num in range(doc.page_count):
            print(f"   📖 Page {page_num + 1}/{doc.page_count}...", end=" ", flush=True)

            # Convert page to image
            b64_image = extract_page_image(doc, page_num)

            # Extract text via GPT-4.1-mini vision
            try:
                text, inp_tokens, out_tokens = call_vision_api(b64_image)
                total_input_tokens += inp_tokens
                total_output_tokens += out_tokens
                print(f"✅ ({len(text)} chars, {inp_tokens}+{out_tokens} tokens)")
            except Exception as e:
                print(f"❌ Error: {e}")
                continue

            if not text.strip():
                print(f"   ⚠️  Empty page, skipping.")
                continue

            pdf_text_pages.append(text)

            # Chunk the extracted text
            chunks = chunk_text(text)
            if not chunks:
                continue

            # Embed the chunks
            try:
                embeddings = get_embeddings_batch(chunks)
            except Exception as e:
                print(f"   ❌ Embedding error: {e}")
                continue

            # Store in Qdrant
            store_chunks(qdrant_client, chunks, embeddings, pdf_file.name, page_num)
            total_chunks += len(chunks)
            total_pages += 1

            # Be nice to API rate limits
            time.sleep(1.0)

        doc.close()
        all_extracted[pdf_file.name] = "\n\n---\n\n".join(pdf_text_pages)

    # 4. Save extracted text to a file for reference
    output_file = Path(__file__).parent / "ggs_extracted_text.txt"
    with open(output_file, "w") as f:
        for filename, text in all_extracted.items():
            f.write(f"{'=' * 60}\n")
            f.write(f"FILE: {filename}\n")
            f.write(f"{'=' * 60}\n\n")
            f.write(text)
            f.write("\n\n")
    print(f"\n💾 Saved extracted text to: {output_file}")

    # 5. Summary
    print(f"\n{'=' * 60}")
    print(f"  ✅ GGS Knowledge Base Ingestion Complete!")
    print(f"{'=' * 60}")
    print(f"  📄 PDFs processed:    {len(pdf_files)}")
    print(f"  📖 Pages extracted:   {total_pages}")
    print(f"  🧩 Chunks stored:     {total_chunks}")
    print(f"  🪙 Vision tokens:     {total_input_tokens:,} input + {total_output_tokens:,} output")
    print(f"  📦 Qdrant collection: {COLLECTION_NAME}")
    print(f"{'=' * 60}\n")

    # 6. Quick test search
    print("🔍 Running test search: 'What are the lease options?'")
    test_query = "What are the lease options available?"
    try:
        test_embedding = get_embedding(test_query)
        results = qdrant_client.query_points(
            collection_name=COLLECTION_NAME,
            query=test_embedding,
            limit=3,
        ).points
        for i, r in enumerate(results):
            print(f"\n   Result {i + 1} (score: {r.score:.3f}, source: {r.payload['filename']} p{r.payload['page']}):")
            print(f"   {r.payload['text'][:200]}...")
    except Exception as e:
        print(f"   ❌ Test search error: {e}")


if __name__ == "__main__":
    main()
