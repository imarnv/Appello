"""
Quick test: Azure OpenAI text-embedding-3-small API
Verifies that the embedding endpoint is reachable and returns vectors.
"""
import asyncio
import os
import aiohttp
from dotenv import load_dotenv

load_dotenv()

ENDPOINT = os.getenv("AZURE_OPENAI_EMBEDDING_ENDPOINT", "").rstrip("/")
API_KEY  = os.getenv("AZURE_OPENAI_EMBEDDING_API_KEY", "")
DEPLOY   = os.getenv("AZURE_OPENAI_EMBEDDING_DEPLOYMENT", "text-embedding-3-small")

async def test_embedding():
    url = f"{ENDPOINT}/openai/deployments/{DEPLOY}/embeddings?api-version=2023-05-15"
    print(f"📡 Endpoint : {ENDPOINT}")
    print(f"🔑 API Key  : {API_KEY[:12]}...{API_KEY[-6:]}")
    print(f"🧠 Model    : {DEPLOY}")
    print(f"🔗 Full URL : {url}")
    print()

    test_text = "What is the price of butter chicken?"

    async with aiohttp.ClientSession() as session:
        async with session.post(
            url,
            headers={
                "api-key": API_KEY,
                "Content-Type": "application/json",
            },
            json={"input": test_text},
        ) as resp:
            status = resp.status
            print(f"📬 HTTP Status: {status}")

            if status == 200:
                data = await resp.json()
                embedding = data["data"][0]["embedding"]
                dim = len(embedding)
                print(f"✅ SUCCESS — Got embedding vector!")
                print(f"   Dimensions : {dim}")
                print(f"   First 5    : {embedding[:5]}")
                print(f"   Model used : {data.get('model', 'unknown')}")
                print(f"   Usage      : {data.get('usage', {})}")
            else:
                error = await resp.text()
                print(f"❌ FAILED — {error[:500]}")

if __name__ == "__main__":
    asyncio.run(test_embedding())
