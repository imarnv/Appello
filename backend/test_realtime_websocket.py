"""
Quick test: Azure OpenAI Realtime WebSocket API
Verifies that the Realtime WebSocket connection can be successfully opened and configured.
"""
import asyncio
import os
import json
import aiohttp
from dotenv import load_dotenv

load_dotenv()

ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT", "").rstrip("/")
API_KEY  = os.getenv("AZURE_OPENAI_API_KEY", "")
DEPLOY   = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-realtime-mini")
VERSION  = os.getenv("AZURE_OPENAI_API_VERSION", "2025-04-01-preview")

async def test_realtime():
    # Convert HTTPS endpoint to WSS
    ws_endpoint = ENDPOINT.replace("https://", "wss://").replace("http://", "ws://")
    azure_url = f"{ws_endpoint}/openai/realtime?api-version={VERSION}&deployment={DEPLOY}"
    
    print(f"📡 Endpoint : {ENDPOINT}")
    print(f"🔗 WS URL   : {azure_url}")
    print(f"🔑 API Key  : {API_KEY[:12]}...{API_KEY[-6:]}")
    print(f"🧠 Deployment: {DEPLOY}")
    print(f"📅 Version   : {VERSION}")
    print()

    headers = {
        "api-key": API_KEY,
        "Content-Type": "application/json"
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(azure_url, headers=headers) as ws:
                print("✅ WebSocket connected successfully!")
                
                # Try sending session update
                session_config = {
                    "modalities": ["text"],
                    "instructions": "You are a helpful assistant.",
                    "voice": "alloy",
                    "input_audio_format": "pcm16",
                    "output_audio_format": "pcm16",
                }
                
                await ws.send_json({
                    "type": "session.update",
                    "session": session_config
                })
                print("✅ Sent session.update message.")

                # Wait for session.created / session.updated / error response
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        data = json.loads(msg.data)
                        print(f"📥 Received event: {data.get('type')}")
                        if data.get("type") == "session.updated":
                            print("🎉 SUCCESS — Session configured and ready!")
                            break
                        elif data.get("type") == "session.created":
                            print("🎉 SUCCESS — Session created!")
                        elif data.get("type") == "error":
                            print(f"❌ Error event received: {data}")
                            break
                    elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        print("❌ WebSocket closed or encountered error.")
                        break
    except Exception as e:
        print(f"❌ FAILED to connect: {e}")

if __name__ == "__main__":
    asyncio.run(test_realtime())
