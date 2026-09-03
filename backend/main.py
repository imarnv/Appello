"""
Appello Voice Bridge — FastAPI WebSocket Server
Connects: Browser ↔ Azure OpenAI Realtime (STT+Brain) ↔ Sarvam TTS ↔ Browser
Optimized for sub-800ms end-to-end latency with Redis pre-hydration & Postgres logging.
"""

import asyncio
import base64
import json
import logging
import os
import re
import time
import uuid
from typing import Optional, Dict, Any, List
from datetime import date

import aiohttp
from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.websockets import WebSocketState

# Database & Caching helpers
from redis_session import RedisSessionManager
from postgres_store import PostgresStore
import call_analytics
import api_routes
import tenant_context
import tenant_routes
from tenant_store import TenantStore

# Modular component imports
from scenarios import SCENARIOS
from tools import execute_tool, SCENARIO_TOOLS
from language_detect import detect_language
from audio_utils import (
    resample_pcm16,
    upsample_8k_to_24k,
    downsample_24k_to_8k,
    pcm_to_wav,
    synthesize_tts,
    synthesize_tts_streaming,
    extract_sentences,
    SAMPLE_RATE,
    CHUNK_SIZE_BYTES,
    CHUNK_DURATION_MS
)
from latency import LatencyTracker
from api_routes import (
    publish_transcript,
    cleanup_transcript_pubsub,
    _greeting_cache,
    _customer_greeting_cache,
    _active_call_sid_map,
    _active_call_start_times,
    _outbound_customer_map,
    _outbound_product_map,
    _outbound_language_map,
    _call_sid_to_session,
    _call_sid_to_contact_id,
    _reminder_contacts,
    DEFAULT_PRODUCT_NAME,
    REAL_ESTATE_PRODUCT_NAME,
    generate_call_summary,
    extract_lead_qualification_post_call
)

def clean_tool_hallucinations(text: str) -> str:
    """Strip out any raw simulated function/tool calls and JSON blocks hallucinated in the text channel."""
    idx = text.find("{functions.")
    if idx != -1:
        text = text[:idx]
    
    idx = text.find("functions.")
    if idx != -1:
        text = text[:idx]

    for tool in ["record_feedback", "check_table_availability", "reserve_table", "get_my_bookings"]:
        idx = text.find(f"{{{tool}")
        if idx != -1:
            text = text[:idx]
        idx = text.find(tool)
        if idx != -1:
            if idx > 0 and (text[idx-1] in "{(" or text[idx+len(tool)] in ")}"):
                text = text[:idx-1] if text[idx-1] in "{(" else text[:idx]
    return text

REAL_ESTATE_LANG_PROMPTS = { 
    "tamil": """You are Maya, a friendly relationship manager at Urban Rise, on a LIVE PHONE CALL with {customer_name} about Estancia Apartments.
PRONOUNCE ALL ENGLISH WORDS IN NATURAL INDIAN ACCENT NOT AMERICAN
# YOUR OBJECTIVE
Get the customer's interest, site visit availability, and budget. This is not a strict script; follow the conversation flow dynamically based on the customer's responses.

# CRITICAL RESPONSE RULES
- Keep replies natural and conversational — about 2-3 short sentences per reply.
- Ask only ONE question per reply. NEVER bundle multiple questions together.
- NEVER say thinking sentences, filler narration, or meta-commentary.
- NEVER narrate your own actions. Do NOT say things like "I'll record this", "summary record panren", "notes eduthukkren", "note eduthuvittu mudichidren", "உங்களுக்கு ஒரு note எடுக்கிறேன்" etc. The customer should ONLY hear normal human conversation directed at them.
- NEVER ask for something the customer has already told you.
- If the customer expresses frustration or call quality issues, acknowledge it in one sentence before proceeding.

# LANGUAGE RULES
- Speak in natural Tamil mixed with English words — casual, like a real Tamil phone conversation.
- Tamil words in Tamil script, English words in English letters.
- STRICTLY pronounce ALL English words with a NATURAL INDIAN ACCENT.
- STRICT ACKNOWLEDGMENT CONSTRAINT: NEVER start any response with acknowledgment words like "sari", "sari sir", "saringa sir", "saringa", "சரி", "சரிங்க", "சரிங்க சார்", "சரி சார்" unless you are confirming their budget. For all other responses, you MUST start directly by saying the message (especially when answering questions). Use variations like "sari {customer_name} sir" or "saringa {customer_name} sir" extremely rarely and naturally, but do not prefix your messages with these acknowledgment words.
- YES/NO DIRECT ANSWER RULE: Never use acknowledgment words like "sari", "sari sir", or "saringa" to answer a question (e.g. if the user asks "is the swimming pool free?"). Instead, answer directly with "Yes" / "No" or "Aamaa" (ஆமா) / "Illai" (இல்லை) without prefixing it with acknowledgment words.
- Use the customer's name ONLY ONCE in the entire call (in the opening greeting). After that, use "சார்" alone.

# SPEECH PACE
Speak at a brisk pace like a real Indian sales call. Short and crisp sentences.

# PROJECT KNOWLEDGE

## Estancia Apartments
- Developer: Urban Rise
- Land Area & Space: Project is built on 15 acres of land with 60% open area.
- Configurations: We have both 2BHK and 3BHK configurations.
- Location: Guduvancheri, GST Road, Chennai
- Nearby: Guduvancheri railway station is at a walking distance of 5 minutes, SRM University (close)
- Amenities: Pickleball court, rooftop garden, gym, half-olympic swimming pool (free), 24/7 security, garden, play area, community hall
- Brochure: floor plans + price sheets — can WhatsApp

## Raunaq Avinya Villa (Alternate project)
- Location: Chrompet, near Chennai Airport
- Type: Premium villa, starts from 1.2 Crores
- Features: Gated community, gym, clubhouse, private terrace, 24/7 security

# CALL FLOW

## Step 1: Initial Greeting Response (Identity Confirmed)
Right after the user confirms their identity (e.g. "Aamaa", "Yes, I am Arnav"), you MUST respond with this exact line:
"Hey {customer_name}, நான் Urban Rise-ல இருந்து Maya பேசறேன். Guduvancheri-ல இருக்கற நம்ம Estancia Apartments Project பத்தி enquiry பண்ணிருந்தீங்க. பேசறதுக்கு இது நல்ல time-ஆ? ஒரு couple of minutes தான் ஆகும், if you're still interested."

Follow their response:
- Willing to talk / interested -> Move to Step 2.
- Says busy -> Apologize politely, say you'll call later, end the call immediately.
- Says not interested in Estancia -> Move to Step 5 (Offer Raunaq Avinya Villa).

## Step 2: Estancia Location & Amenities Pitch
If the customer shows interest in Estancia, you MUST respond with this exact line only and nothing else:
"Estancia ஒரு greenery-based project, 15 acres land-ல, 60% open area. Gym, pickle ball court, rooftop garden மாதிரி amenities இருக்கு, 2 BHK-யும் 3 BHK-யும் இருக்கு. நீங்க site visit-க்கு interested-ஆ சார்?"

If they ask anything else, answer that accordingly.

Follow their response:
- Says NO to visit / not interested in scheduling -> Say "okay sir" (சரிங்க சார்) and move directly to the Closing/Brochure line.
- Says YES to site visit -> Move to Step 3.

## Step 3: Site Visit Schedule
Ask for their convenient day and time.
- Tamil: "கண்டிப்பா சார். site visit-க்கு எப்போ வரலாம்? நாளைக்கா இல்ல weekend-ஆ? என்ன time convenient?"
- Once day and time are confirmed, proceed to Step 4.

## Step 4: Budget Qualification
Ask for their rough budget range.
- Tamil: "சரிங்க சார், site visit schedule பண்ணிடலாம். உங்க rough budget range என்னனு தெரிஞ்சுக்கலாமா?"
- Evaluate their budget range:
  - If budget fits Estancia (e.g., 40-70 Lakhs+): suggest that a 2BHK or 3BHK will perfectly suit their budget. (Tamil: "பகிருவதற்கு நன்றி சார், இந்த budget-க்கு 2BHK அல்லது 3BHK உங்களுக்கு நல்லா suit ஆகும்.")
  - If budget is low: suggest home loan assistance. (Tamil: "சரிங்க சார், உங்க budget-க்கு ஏத்த மாதிரி நம்மகிட்ட home loan assist support-உம் இருக்கு, அதை check பண்ணலாம்.")
- Once confirmed, ask if they have any other queries. If none, move to Closing/Brochure line.

## Step 5: Offer Raunaq Avinya Villa (Alternate Project)
If the customer says they are not interested in Estancia (either at Step 1 or Step 2), suggest the alternate project Raunaq Avinya Villa in Chrompet:
- Tamil: "சரிங்க சார், Estancia Apartments உங்களுக்கு செட் ஆகலனா, நம்மகிட்ட Chrompet-ல, near Chennai airport, Raunaq Avinya-னு ஒரு independent premium villa project இருக்கு சார். Gated community-ல gym, clubhouse, private terrace-ஓட 1.2 Crores-ல இருந்து start ஆகுது. இதை பத்தி தெரிஞ்சுக்க interest இருக்கா சார்?"
- Follow their response:
  - If INTERESTED in Raunaq Avinya Villa -> Follow the exact same flow questions as Estancia: Site Visit Scheduling (Step 3) -> Budget Qualification (Step 4) -> Closing/Brochure Line.
  - If NOT INTERESTED in Raunaq Avinya Villa either -> Say thank you and wish them a great day ahead:
    "ஓகே சார், ரொம்ப நன்றி. Have a great day ahead!"
    (CRITICAL: Do NOT mention any brochure, WhatsApp, or sending documents if they decline both projects.)

## Closing/Brochure Line
If they completed the flow or declined site visits (but remained interested in details), deliver this brochure line and end the call:
"சரிங்க சார், உங்க number-க்கு WhatsApp-ல brochure, floor plans அப்புறம் price sheets அனுப்பி வைக்கிறேன். Have a good day sir!"
""",
    "hindi": """You are Maya, a friendly and natural-sounding relationship manager at Urban Rise, on a LIVE PHONE CALL with {customer_name} about Estancia Apartments.

# YOUR PERSONALITY
You sound like a real Indian sales executive making a warm, conversational call — NOT like someone reading from a script. You are polite, cheerful, and speak naturally. You use casual Hindi mixed with English words, like how a real Hindi-speaking salesperson would talk on the phone. You are NOT a robot.

# LANGUAGE RULES
- Speak in natural Hindi mixed with English words — the way real Hindi-speaking people talk in everyday phone conversations.
- Hindi words in Hindi script, English words in English letters.
- STRICTLY pronounce ALL English words (Hello, sir, Urban Rise, Estancia, budget, site visit, WhatsApp, brochure, floor plans, price sheets, amenities, gym, pool, security) with a NATURAL INDIAN ACCENT — NOT American accent. This is EXTREMELY important.
- NEVER speak a full sentence in English. Every sentence must have Hindi in it.
- NEVER say your internal thoughts, reasoning, or instructions aloud. The customer should only hear your conversation.
- Use "ठीक है" / "अच्छा" naturally as acknowledgment throughout the conversation.
- Use "{customer_name}-जी" very rarely — at most twice in the whole call (once when asking budget, once when closing).
- NEVER repeat the exact same sentence if the customer asks the same question again. Rephrase naturally.

# SPEECH PACE
Speak at a brisk, natural conversational pace — like a real Indian sales call. Do NOT speak slowly or deliberately. Keep sentences short and crisp.

# PROJECT KNOWLEDGE (use this to answer questions naturally)

## Estancia Apartments (Primary Project)
- Developer: Urban Rise
- Location: Guduvancheri, GST Road, Chennai
- Nearby: Guduvancheri railway station (5-min walk), SRM University (very close)
- Type: Apartment complex
- Amenities: Gym, half-olympic size swimming pool (free), 24/7 security, garden, children's play area, community hall
- Brochure: Digital brochure with floor plans and price sheets — can be WhatsApp-ed

## Raunaq Avinya Villa (Alternate — offer ONLY if customer rejects Estancia)
- Location: Chrompet, near Chennai Airport
- Type: Premium independent villa
- Pricing: Starts from 1.2 Crores
- Features: Gated community, gym, clubhouse, private terrace, 24/7 security

# CALL FLOW (follow these steps, but speak NATURALLY — formulate your own Hindi+English sentences)

## Step 1: Greeting (already spoken by system)
System already said the greeting. Wait for customer to confirm identity.
- Confirms → Step 2
- Busy/meeting/can't talk → apologize politely, say you'll call later, end call

## Step 2: Introduction
Introduce yourself as Maya from Urban Rise. Mention they enquired about Estancia Apartments. Ask if interested in details.
- Interested → Step 3
- Not interested → offer Raunaq Avinya Villa (Chrompet). If interested, share details then go to Step 5. If not, thank them and end.
- Busy → apologize, end call

## Step 3: Location & Project Info
Share location naturally — Guduvancheri, GST Road, near station, near SRM. Ask if they want to know amenities.
- Yes → Step 4
- No → skip to Step 5

## Step 4: Amenities
Share amenities conversationally — gym, pool, security, garden. Ask if any other questions.
- Done → Step 5

## Step 5: Budget
Ask budget range. Use "{customer_name}-जी" here.
- Shared → Step 6

## Step 6: Site Visit
Ask when they can visit. First the day, then time (morning/afternoon/evening).
- Confirmed → Step 7

## Step 7: Closing
Thank them. Say you'll WhatsApp the brochure with floor plans and price sheets. Use "{customer_name}-जी" here. Wish great day.

# IMPORTANT BEHAVIORS
- If customer asks ANY question outside the flow — answer it naturally and helpfully, then steer back.
- If customer asks for brochure anytime — say you'll WhatsApp it, continue flow.
- If customer says busy at ANY point — apologize, end call immediately.
""",

    "telugu": """You are Maya, a relationship manager at Urban Rise, calling {customer_name} about {product_name}.

CRITICAL LANGUAGE LOCK:
- You MUST speak ONLY in Telugu mixed with 40-60% English words.
- You MUST NEVER speak a single sentence entirely in English.
- NEVER generate filler/transitional phrases in English.
- Keep replies short (1-2 sentences max) to prevent speech stuttering.

CALL FLOW:
1. Greeting: Already spoken. Wait for customer reply.
2. Step 2: Directly reply with:
   "Nenu Maya matladutunnanu, Urban Rise nundi. Meeru recently Estancia Apartments gurinchi enquiry chesaru, adhi follow-up call. Details kosam interest unda sir?"
3. Step 3: Briefly list location/amenities and ask for budget: "Mee budget roughly entha sir?"
4. Step 4 (Factual checks): Call `query_knowledge_base`. Say "okka sec, check chestanu..." before executing.
5. Step 5: Ask site visit date/time and confirm. Do NOT call any tool at the end.
""",
    "kannada": """You are Maya, a relationship manager at Urban Rise, calling {customer_name} about {product_name}.

CRITICAL LANGUAGE LOCK:
- You MUST speak ONLY in Kannada mixed with 40-60% English words.
- You MUST NEVER speak a single sentence entirely in English.
- NEVER generate filler/transitional phrases in English.
- Keep replies short (1-2 sentences max) to prevent speech stuttering.

CALL FLOW:
1. Greeting: Already spoken. Wait for customer reply.
2. Step 2: Directly reply with:
   "Naanu Maya matadta ideene, Urban Rise inda. Neevu recently Estancia Apartments gurinchi enquiry maadidri, adhu follow-up call. Details kelalu interest idya sir?"
3. Step 3: Briefly list location/amenities and ask for budget: "Nimma budget roughly eshtu sir?"
4. Step 4 (Factual checks): Call `query_knowledge_base`. Say "ondhu sec, check maadteene..." before executing.
5. Step 5: Ask site visit date/time and confirm. Do NOT call any tool at the end.
"""
}

# Logger configuration
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("appello")

# ─── Configuration ───────────────────────────────────────────────────────
AZURE_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT", "").rstrip("/")
AZURE_API_KEY = os.getenv("AZURE_OPENAI_API_KEY", "")
AZURE_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-realtime-2.1")
AZURE_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2025-04-01-preview")

PORT = int(os.getenv("PORT", "8000"))
DEFAULT_SCENARIO = os.getenv("DEFAULT_SCENARIO", "real_estate_lead")

# CORS allowed origins
raw_origins = os.getenv("ALLOWED_ORIGINS", "")
ALLOWED_ORIGINS = [o.strip() for o in raw_origins.split(",") if o.strip()] if raw_origins else []
for dev_origin in [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:3001",
    "http://127.0.0.1:3001",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "https://appello-ai.vercel.app",
    "https://appello-kappa.vercel.app",
]:
    if dev_origin not in ALLOWED_ORIGINS:
        ALLOWED_ORIGINS.append(dev_origin)

# ─── Shared Database & Cache Init ────────────────────────────────────────
redis_cache = RedisSessionManager()
db_store = PostgresStore()

app = FastAPI(title="Appello Voice Bridge", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS if ALLOWED_ORIGINS else ["*"],
    allow_origin_regex=r"https://.*\.vercel\.app",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Tenancy: one store for tenants, their agents and their usage, sharing the same
# Postgres pool as everything else. The pool is built lazily during
# db_store.connect(), so the store takes a getter rather than the pool itself.
tenant_store = TenantStore(lambda: db_store.pool)
tenant_context.init(tenant_store)

# Initialize and mount modular routers
api_routes.init(db_store, redis_cache)
api_routes.init_tenancy(tenant_store)
call_analytics.init(db_store)
tenant_routes.init(tenant_store, db_store)
app.include_router(api_routes.router)
app.include_router(call_analytics.router)
app.include_router(tenant_routes.router)


@app.on_event("startup")
async def startup():
    # Connect cache & database
    await redis_cache.connect()
    await db_store.connect()

    # Initialize KBEngine (Qdrant RAG)
    try:
        from kb_engine import KBEngine
        from tools import set_kb_engine, set_redis
        kb_engine = KBEngine(redis_cache)
        await kb_engine.initialize()
        set_kb_engine(kb_engine)
        set_redis(redis_cache)
        api_routes._kb_engine = kb_engine
        logger.info("[startup] KBEngine initialized and registered successfully.")
    except Exception as e:
        logger.error(f"[startup] Failed to initialize KBEngine: {e}")

    # Pre-seed dynamic lead profile in cache and DB
    await db_store.save_lead(
        name="Mr. Gautham",
        phone="+919999999999",
        loan_id="PL-2024-78432",
        emi=8500.0,
        overdue=5
    )
    await redis_cache.prehydrate_customer("+919999999999", {
        "name": "Mr. Gautham",
        "loan_id": "PL-2024-78432",
        "emi_amount": 8500.0,
        "overdue_days": 5
    })

    # Dispatch database seeding and greeting caching in the background
    logger.info("[startup] Dispatching background initialization tasks (TTS warming & DB seeding)...")
    asyncio.create_task(_initialize_background())


async def _initialize_background():
    # 1. Seed restaurant availability slots
    logger.info("[startup-bg] Seeding restaurant availability slots in background...")
    try:
        await db_store.seed_demo_availability()
        logger.info("[startup-bg] Database seeding complete.")
    except Exception as e:
        logger.error(f"[startup-bg] Database seeding failed: {e}")
    
    # Silence all loggers after startup initialization completes
    logging.getLogger("appello").setLevel(logging.ERROR)
    logging.getLogger("uvicorn").setLevel(logging.ERROR)
    logging.getLogger("uvicorn.access").setLevel(logging.ERROR)
    logging.getLogger().setLevel(logging.ERROR)


@app.on_event("shutdown")
async def shutdown():
    await redis_cache.close()
    await db_store.close()


# ─── WebSocket Voice Pipeline (Browser Path) ───────────────────────────
@app.websocket("/ws/voice-gemini")
async def ws_voice_gemini(ws: WebSocket):
    """
    WebSocket voice pipeline endpoint using Gemini Live for multimodal realtime path.
    """
    from test_realtime_gemini import voice_pipeline as gemini_voice_pipeline
    await gemini_voice_pipeline(ws)

# ─── Payment webhook ─────────────────────────────────────────────────────
@app.post("/webhooks/phonepe")
async def phonepe_webhook(request: Request):
    """Server-to-server payment notification.

    This is the only signal trusted to move a payment to confirmed. It is
    verified twice over: the X-VERIFY signature proves the message came from the
    provider, and `order_status` re-asks the provider what the order's state
    actually is — because a signed message still only tells us what the provider
    *sent*, and re-querying is what closes the gap on a replayed or stale one.

    Always answers 200 once the signature checks out. A webhook is a delivery
    mechanism with retries: returning 5xx because our own session lookup failed
    would have the provider redeliver a message we already understood.
    """
    import payments

    raw = await request.body()
    try:
        body = json.loads(raw.decode("utf-8") or "{}")
    except Exception:
        return {"ok": False, "error": "malformed body"}

    encoded = body.get("response")
    if not encoded:
        return {"ok": False, "error": "missing response payload"}

    if not payments.verify_webhook(request.headers.get("x-verify", ""), encoded):
        logger.warning("[payments] webhook rejected: bad signature")
        # 401 rather than 200: this one really is worth retrying, and worth
        # showing up in the provider's dashboard as a failure.
        from fastapi.responses import JSONResponse
        return JSONResponse({"ok": False, "error": "invalid signature"}, status_code=401)

    try:
        decoded = payments.decode_webhook(encoded)
    except Exception as e:
        logger.error(f"[payments] webhook payload could not be decoded: {e}")
        return {"ok": False, "error": "undecodable payload"}

    data = decoded.get("data") or decoded
    order_id = (
        data.get("merchantOrderId")
        or data.get("merchantTransactionId")
        or data.get("orderId")
    )
    if not order_id:
        return {"ok": False, "error": "no order id"}

    # Which call asked for this? Written when the link was created.
    mapping = None
    try:
        cached = await redis_cache.get_raw(f"pay:order:{order_id}")
        if cached:
            mapping = json.loads(cached)
    except Exception as e:
        logger.error(f"[payments] could not read the checkout mapping: {e}")

    # Idempotency: the provider retries, and a caller must not be thanked twice.
    try:
        already = await redis_cache.get_raw(f"pay:done:{order_id}")
        if already:
            logger.info(f"[payments] webhook for {order_id} already handled")
            return {"ok": True, "duplicate": True}
    except Exception:
        pass

    # The signature proves provenance; this proves state.
    try:
        status = await payments.order_status(order_id)
        paid = status["paid"]
        state = status["state"]
    except payments.PaymentUnavailable as e:
        logger.error(f"[payments] status re-check failed for {order_id}: {e}")
        return {"ok": False, "error": "status check failed"}

    try:
        await redis_cache.set_raw(f"pay:done:{order_id}", state, ttl=86400)
    except Exception:
        pass

    if mapping and mapping.get("session_id"):
        delivered = await redis_cache.publish_event(
            mapping["session_id"],
            {
                "type": "payment",
                "state": state,
                "paid": paid,
                "order_id": order_id,
                "amount_rupees": mapping.get("amount_rupees"),
            },
        )
        logger.info(
            f"[payments] {order_id} -> {state}; delivered to {delivered} live session(s)"
        )
    else:
        logger.info(f"[payments] {order_id} -> {state}; no live session to notify")

    return {"ok": True, "state": state}


@app.websocket("/ws/voice")
async def voice_pipeline(ws: WebSocket):
    """
    Main voice pipeline WebSocket endpoint.
    Optimized streaming connection including Redis session cache and Postgres transcript persistence.
    """
    await ws.accept()
    
    # Generate unique call session ID
    session_id = f"call_{uuid.uuid4().hex[:12]}"
    logger.info(f"[ws] Client connected (Session: {session_id})")

    scenario_key = "restaurant_booking"
    scenario = SCENARIOS.get("restaurant_booking", list(SCENARIOS.values())[0] if SCENARIOS else {})
    speaker = scenario.get("speaker", "kabir")
    phone_number = "+919999999999" # Default dummy phone number

    # ── Latency Profiler ─────────────────────────────────────────────
    tracker = LatencyTracker(session_id)

    # ── Token Usage Tracker ──────────────────────────────────────────
    call_start_time = time.monotonic()
    token_usage_per_turn = []  # List of usage dicts from each response.done
    token_totals = {
        "total_tokens": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "input_text_tokens": 0,
        "input_audio_tokens": 0,
        "input_cached_tokens": 0,
        "output_text_tokens": 0,
        "output_audio_tokens": 0,
    }

    # State
    close_after_response = False
    azure_ws = None
    azure_session = None
    text_buffer = ""
    full_call_transcript = []
    tts_queue: asyncio.Queue = asyncio.Queue()
    is_speaking = False
    playback_cancel = asyncio.Event()
    greeting_lock = asyncio.Event()  # Set when greeting is done playing
    is_first_text_token = False  # Track first token per response
    is_first_sentence = False    # Track first sentence per response
    response_done_flag = False   # Flag: LLM finished, waiting for TTS to send report
    native_audio_started = False # Track if native GPT Realtime audio started for current response
    report_sent_for_turn = 0     # Tracks which turn the report was already sent for
    use_native_audio = False     # Clean scoped initialization
    greeting_end_time = 0.0      # Timestamp until when VAD is ignored to protect greeting

    # State variables for background Sarvam STT
    user_turn_count = 0
    current_user_turn = None
    user_audio_buffers = {}
    audio_packet_count = 0
    text_history = []

    # Persistent HTTP session for Sarvam TTS (connection keep-alive = lower latency)
    http_session = aiohttp.ClientSession()

    async def send_status(state: str):
        try:
            if ws.client_state == WebSocketState.CONNECTED:
                await ws.send_json({"type": "status", "state": state})
        except Exception:
            pass

    async def send_transcript(role: str, text: str):
        try:
            if ws.client_state == WebSocketState.CONNECTED:
                await ws.send_json({"type": "transcript", "role": role, "text": text})
            full_call_transcript.append(f"{role}: {text}")
            asyncio.create_task(db_store.save_transcript_turn(session_id, role, text))
        except Exception:
            pass

    async def process_sarvam_stt_voice(whisper_text: str, turn_index: int):
        audio_bytes = user_audio_buffers.get(turn_index)
        sarvam_text = None
        if audio_bytes:
            sarvam_text = await transcribe_audio_sarvam(bytes(audio_bytes), http_session, sample_rate=SAMPLE_RATE)
        
        # Determine if transcription succeeded (even if empty) or failed/skipped
        stt_succeeded = (sarvam_text is not None) or (whisper_text != "")
        final_text = (sarvam_text if sarvam_text is not None else whisper_text or "").strip()
        
        logger.info(f"[sarvam-stt] [voice] Turn #{turn_index} final transcript: {final_text} (succeeded={stt_succeeded})")
        if final_text:
            await send_transcript("user", final_text)

            # Log empty/filler transcripts
            _stripped = re.sub(r"[\s\.\,\?\!\u0964]+", "", final_text).lower()
            if not _stripped or _stripped in {"um", "umm", "uh", "uhh", "hmm", "hm", "ah", "aa", "er", "err", "haan", "haa"}:
                logger.info(f"[language] Sarvam STT detected filler: \"{final_text}\"")

    async def emit_latency_report():
        """Emit the latency report (log + send to client). Only once per turn."""
        nonlocal report_sent_for_turn
        if report_sent_for_turn >= tracker.turn_count:
            return  # Already sent for this turn
        report_sent_for_turn = tracker.turn_count
        tracker.log_report()
        try:
            await ws.send_json({
                "type": "latency_report",
                "data": tracker.report(),
            })
        except Exception:
            pass

    def enqueue_tts(text: str, is_greeting: bool = False):
        tts_queue.put_nowait((text, is_greeting))

    # ── TTS Playback Worker (Streaming) ──────────────────────────────
    async def tts_playback_worker():
        nonlocal is_speaking, response_done_flag

        while True:
            item = await tts_queue.get()
            if item is None:
                break

            text, is_greeting = item
            _tts_pickup_t = time.monotonic()
            logger.info(f"[EVENT] tts_worker picked up sentence: \"{text[:60]}\" | greeting={is_greeting}")

            is_speaking = True
            playback_cancel.clear()
            await send_status("speaking")

            first_chunk_sent = False
            t = tracker if not is_greeting else None

            # Check greeting cache first
            cached_audio = _greeting_cache.get(scenario_key) if is_greeting else None

            try:
                if cached_audio:
                    logger.info(f"[tts] Using greeting audio ({len(cached_audio)} bytes)")
                    offset = 0
                    while offset < len(cached_audio) and not playback_cancel.is_set():
                        if ws.client_state != WebSocketState.CONNECTED:
                            break
                        end = min(offset + CHUNK_SIZE_BYTES, len(cached_audio))
                        chunk = cached_audio[offset:end]
                        await ws.send_json({
                            "type": "audio",
                            "data": base64.b64encode(chunk).decode("ascii"),
                        })
                        offset = end
                        await asyncio.sleep(CHUNK_DURATION_MS / 1000)
                    if not playback_cancel.is_set():
                        await send_transcript("assistant", text)
                else:
                    curr_pace = 1.20 if scenario_key == "feedback_agent" else 1.15
                    async for chunk in synthesize_tts_streaming(text, speaker, http_session, tracker=t, pace=curr_pace):
                        if playback_cancel.is_set():
                            break
                        if ws.client_state != WebSocketState.CONNECTED:
                            break

                        if not first_chunk_sent and t:
                            t.mark("first_audio_to_client")
                            first_chunk_sent = True

                        await ws.send_json({
                            "type": "audio",
                            "data": base64.b64encode(chunk).decode("ascii"),
                        })

                    # If not cancelled, record assistant turn in history & DB
                    if not playback_cancel.is_set():
                        await send_transcript("assistant", text)

            except Exception as e:
                logger.error(f"[tts] Streaming synthesis error: {e}")
            finally:
                tts_queue.task_done()
                is_speaking = False

                if is_greeting:
                    greeting_lock.set()

                # If the queue is now empty and LLM finished generating (response_done_flag),
                # our response is fully spoken, so emit latency report and return to listening.
                if tts_queue.empty() and response_done_flag:
                    response_done_flag = False
                    await emit_latency_report()
                    await send_status("listening")

    # Start TTS worker in background
    playback_task = asyncio.create_task(tts_playback_worker())

    # ── Main Connection Loop ─────────────────────────────────────────
    try:
        # 1. Wait for config message from browser
        config_msg = await asyncio.wait_for(ws.receive_json(), timeout=10.0)
        if config_msg.get("type") == "config":
            scenario_key = config_msg.get("scenario", "restaurant_booking")
            scenario = SCENARIOS.get(scenario_key, list(SCENARIOS.values())[0] if SCENARIOS else {})
            speaker = scenario.get("speaker", "kabir")
            phone_number = config_msg.get("phone_number", "+919999999999")
            logger.info(f"[ws] Scenario: {scenario_key} | Phone: {phone_number}")

        # 2. Log Call start in database and session cache
        await db_store.log_call_start(session_id, phone_number, scenario_key)

        # Dashboard: initialize log rows for restaurant_booking or feedback_agent
        if scenario_key == "restaurant_booking":
            asyncio.create_task(db_store.init_restaurant_booking_log(session_id))
        elif scenario_key in ("feedback_agent", "real_estate_lead"):
            feedback_customer = config_msg.get("customer_name") or ("Mr. Arnav" if scenario_key == "real_estate_lead" else "Mr. Gautham")
            asyncio.create_task(db_store.init_feedback_agent_log(
                session_id=session_id,
                customer_name=feedback_customer,
            ))

        await redis_cache.set_session(session_id, {
            "phone_number": phone_number,
            "scenario": scenario_key,
            "status": "active"
        })

        # 3. Retrieve pre-hydrated customer profile from cache
        customer_profile = await redis_cache.get_customer(phone_number)
        today_str = date.today().strftime("%Y-%m-%d (%A)")
        if scenario_key == "real_estate_lead":
            selected_lang = (config_msg.get("language") or "hindi").lower()
            dynamic_instructions = REAL_ESTATE_LANG_PROMPTS.get(selected_lang, REAL_ESTATE_LANG_PROMPTS["hindi"]) + f"\n\nTODAY'S DATE: {today_str}"
        else:
            dynamic_instructions = scenario.get("instructions", "") + f"\n\nTODAY'S DATE: {today_str}"

        if customer_profile:
            dynamic_instructions += f"\nDYNAMIC CUSTOMER PROFILE: {json.dumps(customer_profile)}"

        # Dynamic customer name + product injection for feedback_agent and real_estate_lead
        if scenario_key in ("feedback_agent", "real_estate_lead"):
            feedback_customer = config_msg.get("customer_name") or ("Mr. Arnav" if scenario_key == "real_estate_lead" else "Mr. Gautham")
            dynamic_instructions = dynamic_instructions.replace("{customer_name}", feedback_customer)
            product_name = REAL_ESTATE_PRODUCT_NAME if scenario_key == "real_estate_lead" else DEFAULT_PRODUCT_NAME
            dynamic_instructions = dynamic_instructions.replace("{product_name}", product_name)

        # Use native GPT Realtime audio for ALL agents (STT+LLM+TTS served by gpt-realtime model)
        use_native_audio = True

        # 4. Speak welcome greeting
        welcome_message = scenario.get("welcome", "")
        if scenario_key == "real_estate_lead":
            selected_lang = (config_msg.get("language") or "hindi").lower()
            lang_greetings = {
                "hindi": "नमस्ते, क्या मैं {customer_name}-जी से बात कर रहा हूँ?",
                "tamil": "ஹலோ சார், வணக்கம், நான் {customer_name}-கிட்ட பேசறனா?",
                "telugu": "Namaste, nenu {customer_name}-garitho matladutunnana?",
                "kannada": "Namaste, naanu {customer_name}-avara jothe matadta iddeena?"
            }
            welcome_message = lang_greetings.get(selected_lang, lang_greetings["hindi"])
            welcome_message = welcome_message.replace("{customer_name}", feedback_customer)
        elif welcome_message:
            if scenario_key == "feedback_agent":
                welcome_message = welcome_message.replace("{customer_name}", config_msg.get("customer_name", "Mr. Gautham"))
                welcome_message = welcome_message.replace("{product_name}", DEFAULT_PRODUCT_NAME)

        if welcome_message:
            # Calculate greeting VAD ignore duration to prevent echoes/interruption
            greeting_duration = 3.5
            greeting_end_time = time.time() + greeting_duration
            logger.info(f"[ws] Welcome greeting set. Ignoring VAD for {greeting_duration:.2f}s (until {greeting_end_time})")

            if use_native_audio:
                # Azure will speak greeting natively — greeting_lock set after response completes
                pass
            else:
                enqueue_tts(welcome_message, is_greeting=True)
        else:
            greeting_lock.set()

        # 5. Open connection to Azure OpenAI Realtime API
        azure_url = f"{AZURE_ENDPOINT}/openai/realtime?api-version={AZURE_API_VERSION}&deployment={AZURE_DEPLOYMENT}"
        azure_headers = {
            "api-key": AZURE_API_KEY,
            "Content-Type": "application/json"
        }

        # Determine VAD settings initially (disable if greeting is playing to protect against echo/noise)
        initial_turn_detection = None if welcome_message else {
            "type": "server_vad",
            "threshold": 0.5,
            "prefix_padding_ms": 300,
            "silence_duration_ms": 150,
        }

        # Select voice: real_estate_lead uses female voice 'shimmer', other agents use male voice 'ash'
        if scenario_key == "real_estate_lead":
            selected_voice = "shimmer" if use_native_audio else "alloy"
        else:
            selected_voice = "ash" if use_native_audio else "alloy"

        # Session configuration body
        session_config = {
            "modalities": ["text", "audio"],
            "instructions": dynamic_instructions,
            "voice": selected_voice,
            "input_audio_format": "g711_alaw" if scenario_key == "telephony" else "pcm16",
            "output_audio_format": "pcm16",
            "speed": 1.15,
            "turn_detection": initial_turn_detection
        }
        if not use_native_audio:
            session_config["input_audio_transcription"] = {
                "model": "whisper-1"
            }

        # Inject tool schemas if defined for this scenario
        if scenario_key in SCENARIO_TOOLS:
            session_config["tools"] = SCENARIO_TOOLS[scenario_key]
            session_config["tool_choice"] = "auto"
            logger.info(f"[azure] Registered {len(SCENARIO_TOOLS[scenario_key])} tools for {scenario_key}")

        # Maximum history of turns to send back to the model context.
        # This keeps the model focused and prevents context inflation.
        MAX_HISTORY_TURNS = 6
        last_user_lang = "ta-IN" if scenario_key == "feedback_agent" else None
        assistant_item_ids = []
        ignore_deltas_until_new_response = False

        async with aiohttp.ClientSession() as azure_session:
            async with azure_session.ws_connect(azure_url, headers=azure_headers) as azure_ws:
                logger.info("[azure] Connected to Azure OpenAI Realtime WebSocket")

                # Receive loop from Azure OpenAI
                async def azure_recv():
                    nonlocal text_buffer, is_speaking, is_first_text_token, is_first_sentence
                    nonlocal response_done_flag, last_user_lang, assistant_item_ids
                    nonlocal user_turn_count, current_user_turn, ignore_deltas_until_new_response
                    nonlocal native_audio_started, greeting_end_time, close_after_response

                    async for msg in azure_ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            data = json.loads(msg.data)
                            t = data.get("type")

                            # Log event arrival times for debugging latency bottlenecks
                            if t not in ("response.audio.delta", "response.audio_transcript.delta"):
                                logger.info(f"[TIMING] [WS RECEIVE] {t} at {time.time():.4f}")
                            elif t == "response.audio.delta" and not native_audio_started:
                                logger.info(f"[TIMING] [WS RECEIVE] first response.audio.delta at {time.time():.4f}")

                            if t == "error":
                                err_code = data.get("error", {}).get("code")
                                if err_code == "response_cancel_not_active":
                                    logger.debug(f"[azure] Harmless cancel race: {data}")
                                else:
                                    logger.error(f"[azure] Error received: {data}")
                                continue

                            # VAD Event: User started speaking
                            if t == "input_audio_buffer.speech_started":
                                if time.time() < greeting_end_time:
                                    logger.info(f"[VAD] User speech detected during welcome greeting lockout (remaining: {greeting_end_time - time.time():.2f}s) — IGNORED to prevent premature interruption")
                                    continue

                                tracker.new_turn()
                                tracker.mark("speech_start")
                                logger.info("[VAD] User started speaking — stopping assistant speech immediately (barge-in)")
                                
                                # Send clear event to browser to stop local playback
                                try:
                                    await ws.send_json({"type": "clear"})
                                except Exception:
                                    pass

                                # Stop playback worker and clear queues
                                playback_cancel.set()
                                while not tts_queue.empty():
                                    try:
                                        tts_queue.get_nowait()
                                        tts_queue.task_done()
                                    except asyncio.QueueEmpty:
                                        break

                                # Setup user turn track
                                user_turn_count += 1
                                user_audio_buffers[user_turn_count] = bytearray()
                                current_user_turn = user_turn_count
                                is_speaking = False

                                # Tell Azure to cancel generation
                                try:
                                    await azure_ws.send_json({"type": "response.cancel"})
                                except Exception:
                                    pass
                                ignore_deltas_until_new_response = True
                                text_buffer = ""

                            elif t == "input_audio_buffer.speech_stopped":
                                if time.time() < greeting_end_time:
                                    continue
                                tracker.mark("speech_end")
                                if use_native_audio:
                                    asyncio.create_task(process_sarvam_stt_voice("", user_turn_count))
                                current_user_turn = None
                                await send_status("thinking")

                            elif t == "conversation.item.input_audio_transcription.completed":
                                tracker.mark("stt_complete")
                                user_text = data.get("transcript", "").strip()

                                # Process Sarvam STT asynchronously for the UI display
                                asyncio.create_task(process_sarvam_stt_voice(user_text, user_turn_count))

                                if use_native_audio:
                                    # For native audio (monolingual), let the server's automatic response continue.
                                    # DO NOT call response.cancel or response.create here!
                                    continue

                                # Skip empty/filler transcripts
                                _stripped = re.sub(r"[\s\.\,\?\!\u0964]+", "", user_text).lower()
                                if not _stripped or _stripped in {"um", "umm", "uh", "uhh", "hmm", "hm", "ah", "aa", "er", "err"}:
                                    logger.info(f"[language] Skipping response — empty/filler: \"{user_text}\"")
                                    continue

                                user_lang = detect_language(user_text) if user_text else "en-IN"
                                logger.info(f"[language] Detected user_lang={user_lang} (last={last_user_lang})")

                                # Drop prior assistant turns on language switch to keep context consistent
                                if (
                                    user_lang in ("hi-IN", "ta-IN")
                                    and last_user_lang is not None
                                    and user_lang != last_user_lang
                                ):
                                    removed = sum(1 for h in text_history if h["role"] == "assistant" and h["lang"] != user_lang)
                                    if removed:
                                        logger.info(f"[language] Language switch {last_user_lang} -> {user_lang}. Dropping {removed} prior assistant turns.")
                                        text_history[:] = [h for h in text_history if not (h["role"] == "assistant" and h["lang"] != user_lang)]

                                if (
                                    user_lang in ("hi-IN", "ta-IN")
                                    and last_user_lang is not None
                                    and user_lang != last_user_lang
                                    and assistant_item_ids
                                ):
                                    logger.info(f"[language] Purging {len(assistant_item_ids)} server-side assistant items.")
                                    for item_id in assistant_item_ids:
                                        await azure_ws.send_json({
                                            "type": "conversation.item.delete",
                                            "item_id": item_id,
                                        })
                                    assistant_item_ids = []

                                if user_lang in ("hi-IN", "ta-IN"):
                                    last_user_lang = user_lang

                                if user_text:
                                    text_history.append({"role": "user", "text": user_text, "lang": user_lang})
                                    del text_history[: max(0, len(text_history) - MAX_HISTORY_TURNS)]

                                # Set absolute language directives based on detected language
                                if scenario_key == "feedback_agent":
                                    lang_directive = (
                                        "ABSOLUTE LANGUAGE RULE: Reply ONLY in Tamil script (Tanglish — Tamil mixed with English loanwords written in Tamil). "
                                        "Do NOT use English or Devanagari script under any circumstances."
                                    )
                                elif scenario_key == "real_estate_lead":
                                    if selected_lang == "tamil":
                                        lang_directive = (
                                            "ABSOLUTE LANGUAGE RULE: Reply ONLY in Tamil script (Tanglish — Tamil mixed with English loanwords written in Tamil). "
                                            "Do NOT use English or Devanagari script under any circumstances."
                                        )
                                    elif selected_lang == "hindi":
                                        lang_directive = (
                                            "ABSOLUTE LANGUAGE RULE: Reply ONLY in Devanagari script (Hinglish — Hindi mixed with English loanwords written in Devanagari). "
                                            "Do NOT use Tamil script under any circumstances."
                                        )
                                    else:
                                        lang_directive = (
                                            f"ABSOLUTE LANGUAGE RULE: Reply ONLY in {selected_lang.capitalize()} mixed with English. "
                                            "Do NOT switch to other Indic scripts."
                                        )
                                elif user_lang == "hi-IN":
                                    lang_directive = (
                                        "ABSOLUTE LANGUAGE RULE: The user is speaking in Hindi. "
                                        "Reply ONLY in Devanagari script (Hinglish — Hindi mixed with English loanwords written in Devanagari). "
                                        "Do NOT use Tamil, Punjabi, or any other Indic script."
                                    )
                                elif user_lang == "ta-IN":
                                    lang_directive = (
                                        "ABSOLUTE LANGUAGE RULE: The user is speaking in Tamil. "
                                        "Reply ONLY in Tamil script (Tanglish — Tamil mixed with English loanwords written in Tamil). "
                                        "Do NOT use Devanagari or any other Indic script."
                                    )
                                else:
                                    lang_directive = (
                                        "ABSOLUTE LANGUAGE RULE: The user is speaking in English. "
                                        "Reply ONLY in English. Do NOT use any Indic script."
                                    )

                                response_input = []
                                for h in text_history:
                                    response_input.append({
                                        "type": "message",
                                        "role": h["role"],
                                        "content": [{
                                            "type": "input_text" if h["role"] == "user" else "text",
                                            "text": h["text"],
                                        }],
                                    })

                                await azure_ws.send_json({
                                    "type": "response.create",
                                    "response": {
                                        "instructions": lang_directive + "\n\n" + dynamic_instructions,
                                        "input": response_input,
                                    },
                                })

                            elif t == "response.created":
                                tracker.mark("response_created")
                                is_first_text_token = True
                                is_first_sentence = True
                                ignore_deltas_until_new_response = False
                                native_audio_started = False

                            elif t == "response.output_item.added":
                                item_id = data.get("item", {}).get("id")
                                role = data.get("item", {}).get("role")
                                if role == "assistant" and item_id:
                                    assistant_item_ids.append(item_id)
                                    del assistant_item_ids[: max(0, len(assistant_item_ids) - MAX_HISTORY_TURNS)]

                            # Token delta received
                            elif t == "response.content_part.added":
                                await send_status("thinking")

                            elif t == "response.text.delta":
                                if ignore_deltas_until_new_response:
                                    continue
                                if use_native_audio:
                                    continue  # Audio streamed natively via response.audio.delta
                                if is_first_text_token:
                                    tracker.mark("first_text_token")
                                    is_first_text_token = False

                                delta = data.get("delta", "")
                                text_buffer += delta
                                text_buffer = clean_tool_hallucinations(text_buffer)

                                # Split text into sentences for low-latency TTS
                                sentences, text_buffer = extract_sentences(
                                    text_buffer,
                                    aggressive_first_flush=is_first_sentence
                                )
                                for s in sentences:
                                    if is_first_sentence:
                                        tracker.mark("first_sentence_ready")
                                        is_first_sentence = False
                                    enqueue_tts(s)

                            elif t == "response.text.done":
                                if ignore_deltas_until_new_response:
                                    continue
                                if use_native_audio:
                                    continue  # Transcript handled via response.audio_transcript
                                logger.info(f"[azure] Text done: \"{data.get('text', '')}\"")
                                full_text = data.get("text", "").strip()
                                if last_user_lang in ("hi-IN", "ta-IN") and full_text:
                                    text_history.append({"role": "assistant", "text": full_text, "lang": last_user_lang})
                                    del text_history[: max(0, len(text_history) - MAX_HISTORY_TURNS)]

                            # ── Native GPT Realtime Audio (restaurant_booking) ──
                            elif t == "response.audio.delta":
                                if ignore_deltas_until_new_response or not use_native_audio:
                                    continue
                                audio_data = data.get("delta", "")
                                if audio_data:
                                    if not native_audio_started:
                                        native_audio_started = True
                                        is_speaking = True
                                        tracker.mark("first_audio_to_client")
                                        await send_status("speaking")
                                    try:
                                        if ws.client_state == WebSocketState.CONNECTED:
                                            await ws.send_json({
                                                "type": "audio",
                                                "data": audio_data,
                                            })
                                    except Exception:
                                        pass

                            elif t == "response.audio.done":
                                if ignore_deltas_until_new_response or not use_native_audio:
                                    continue
                                is_speaking = False

                            elif t == "response.audio_transcript.delta":
                                if ignore_deltas_until_new_response or not use_native_audio:
                                    continue
                                text_buffer += data.get("delta", "")

                            elif t == "response.audio_transcript.done":
                                if ignore_deltas_until_new_response or not use_native_audio:
                                    continue
                                full_text = data.get("transcript", "").strip()
                                if full_text:
                                    await send_transcript("assistant", full_text)
                                text_buffer = ""

                            # Tool Call requested by LLM
                            elif t == "response.function_call_arguments.done":
                                if ignore_deltas_until_new_response:
                                    continue
                                tool_name = data.get("name")
                                call_id = data.get("call_id")
                                arguments_str = data.get("arguments", "{}")
                                logger.info(f"[tool] Azure requested tool {tool_name} with args {arguments_str}")

                                try:
                                    args = json.loads(arguments_str)
                                    # Latency Firewall: Bypass Qdrant during intro turns to guarantee <400ms speed
                                    if tool_name == "query_knowledge_base" and (tracker.turn_count <= 2 or "aama" in arguments_str.lower() or "yes" in arguments_str.lower()):
                                        logger.info("[tool] Latency Firewall triggered: returning cached response instantly")
                                        result_str = json.dumps({"found": True, "context": [{"text": "Estancia Apartments is located in Guduvancheri, GST Road. Amenities include swimming pool, gym, security.", "source": "cache"}], "message": "Quick cache lookup."})
                                    else:
                                        result_str = await execute_tool(tool_name, args, session_id, phone_number, db_store, scenario_key=scenario_key)
                                        logger.info(f"[tool] Result: {result_str[:200]}")
                                        if tool_name == "query_knowledge_base":
                                            await asyncio.sleep(0.5)

                                    # Send tool result back to Azure Realtime
                                    await azure_ws.send_json({
                                        "type": "conversation.item.create",
                                        "item": {
                                            "type": "function_call_output",
                                            "call_id": call_id,
                                            "output": result_str,
                                        }
                                    })
                                    if tool_name == "record_feedback":
                                        logger.info("[tool] Feedback recorded, setting close_after_response flag")
                                        close_after_response = True
                                    await azure_ws.send_json({"type": "response.create"})
                                except Exception as ex:
                                    logger.error(f"[tool] Error sending output back: {ex}")

                            elif t == "response.done":
                                if ignore_deltas_until_new_response:
                                    continue
                                tracker.mark("response_done")
                                logger.info("[azure] response.done received")
                                if close_after_response:
                                    logger.info("[ws] Close after feedback response pending. Scheduling delayed close...")
                                    async def delayed_close():
                                        await asyncio.sleep(8.0)
                                        logger.info("[ws] Gracefully closing WebSocket after feedback response finished speaking")
                                        try:
                                            await ws.close()
                                        except Exception:
                                            pass
                                    asyncio.create_task(delayed_close())

                                # ── Capture token usage from response.done ──
                                response_obj = data.get("response", {})
                                usage = response_obj.get("usage", {})
                                if usage:
                                    turn_usage = {
                                        "turn": len(token_usage_per_turn) + 1,
                                        "total_tokens": usage.get("total_tokens", 0),
                                        "input_tokens": usage.get("input_tokens", 0),
                                        "output_tokens": usage.get("output_tokens", 0),
                                        "input_text_tokens": usage.get("input_token_details", {}).get("text_tokens", 0),
                                        "input_audio_tokens": usage.get("input_token_details", {}).get("audio_tokens", 0),
                                        "input_cached_tokens": usage.get("input_token_details", {}).get("cached_tokens", 0),
                                        "output_text_tokens": usage.get("output_token_details", {}).get("text_tokens", 0),
                                        "output_audio_tokens": usage.get("output_token_details", {}).get("audio_tokens", 0),
                                    }
                                    token_usage_per_turn.append(turn_usage)
                                    for k in token_totals:
                                        token_totals[k] += turn_usage.get(k, 0)
                                    # logger.info(
                                    #     f"[TOKEN] Turn #{turn_usage['turn']} usage: "
                                    #     f"total={turn_usage['total_tokens']}, "
                                    #     f"input(text={turn_usage['input_text_tokens']}, audio={turn_usage['input_audio_tokens']}, cached={turn_usage['input_cached_tokens']}), "
                                    #     f"output(text={turn_usage['output_text_tokens']}, audio={turn_usage['output_audio_tokens']}) | "
                                    #     f"Running total: {token_totals['total_tokens']} tokens"
                                    # )

                                # Set greeting_lock if not already set (native audio greeting complete)
                                if not greeting_lock.is_set():
                                    greeting_lock.set()

                                if use_native_audio:
                                    # Audio was streamed directly from Azure Realtime
                                    is_speaking = False
                                    native_audio_started = False
                                    text_buffer = ""
                                    await emit_latency_report()
                                    await send_status("listening")
                                else:
                                    # Sarvam TTS path: flush remaining text
                                    final_flush = text_buffer.strip()
                                    if final_flush:
                                        if is_first_sentence:
                                            tracker.mark("first_sentence_ready")
                                            is_first_sentence = False
                                        enqueue_tts(final_flush)
                                    text_buffer = ""

                                    # If nothing is playing, we can immediately emit report and return to listening.
                                    # Otherwise set flag, and the play worker will emit it once finished.
                                    if tts_queue.empty() and not is_speaking:
                                        response_done_flag = False
                                        await emit_latency_report()
                                        await send_status("listening")
                                    else:
                                        response_done_flag = True

                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                             break

                # Start receive loop immediately so we don't miss greeting packets
                asyncio.create_task(azure_recv())

                # Configure the session
                await azure_ws.send_json({
                    "type": "session.update",
                    "session": session_config
                })

                # Enable VAD and clear audio buffer after the welcome greeting finishes playing
                if welcome_message:
                    async def enable_vad_after_greeting(delay: float):
                        await asyncio.sleep(delay)
                        try:
                            if ws.client_state == WebSocketState.CONNECTED:
                                logger.info(f"[azure] Welcome greeting done playing. Enabling VAD & clearing buffer.")
                                await azure_ws.send_json({"type": "input_audio_buffer.clear"})
                                await azure_ws.send_json({
                                    "type": "session.update",
                                    "session": {
                                        **session_config,
                                        "turn_detection": {
                                            "type": "server_vad",
                                            "threshold": 0.5,
                                            "prefix_padding_ms": 300,
                                            "silence_duration_ms": 150,
                                        }
                                    }
                                })
                        except Exception as ex:
                            logger.error(f"[azure] Error enabling VAD after greeting: {ex}")

                    asyncio.create_task(enable_vad_after_greeting(greeting_duration))

                # Handle greeting in Azure conversation
                if welcome_message:
                    if use_native_audio:
                        # Trigger Azure to generate and speak the greeting natively, overriding instructions
                        await azure_ws.send_json({
                            "type": "response.create",
                            "response": {
                                "instructions": f"You are starting the call. Speak the welcome greeting exactly: \"{welcome_message}\""
                            }
                        })
                    else:
                        # Register the already-spoken Sarvam TTS greeting in conversation history
                        await azure_ws.send_json({
                            "type": "conversation.item.create",
                            "item": {
                                "type": "message",
                                "role": "assistant",
                                "content": [
                                    {
                                        "type": "text",
                                        "text": welcome_message
                                    }
                                ]
                            }
                        })

                # Browser Receive Loop (for microphone audio chunks)
                async for msg in ws.iter_json():
                    if msg.get("type") == "audio" and azure_ws and not azure_ws.closed and greeting_lock.is_set():
                        if current_user_turn is not None:
                            try:
                                user_audio_buffers[current_user_turn].extend(base64.b64decode(msg["data"]))
                            except Exception:
                                pass
                        await azure_ws.send_json({
                            "type": "input_audio_buffer.append",
                            "audio": msg["data"],
                        })

    except WebSocketDisconnect:
        logger.info(f"[ws] Session disconnected: {session_id}")
    except Exception as e:
        logger.error(f"[ws] Session {session_id} error: {e}")
    finally:
        # Cleanup connection resources
        await tts_queue.put(None)
        if azure_ws and not azure_ws.closed:
            await azure_ws.close()
        if azure_session:
            await azure_session.close()
        await http_session.close()

        # ── TOKEN COST REPORT ────────────────────────────────────────
        # Token cost report logging commented out to keep console clean
        # call_duration_s = time.monotonic() - call_start_time
        # call_duration_min = call_duration_s / 60.0
        # pipeline_mode = "WebRTC (native audio)" if use_native_audio else "Hybrid (Sarvam TTS)"
        # 
        # # Azure pricing per 1M tokens (gpt-realtime-mini)
        # PRICE_TEXT_INPUT = 0.60 / 1_000_000
        # PRICE_TEXT_OUTPUT = 2.40 / 1_000_000
        # PRICE_AUDIO_INPUT = 10.00 / 1_000_000
        # PRICE_AUDIO_OUTPUT = 20.00 / 1_000_000
        # PRICE_CACHED_INPUT = 0.30 / 1_000_000  # cached audio input rate
        # 
        # cost_text_in = token_totals["input_text_tokens"] * PRICE_TEXT_INPUT
        # cost_text_out = token_totals["output_text_tokens"] * PRICE_TEXT_OUTPUT
        # cost_audio_in = token_totals["input_audio_tokens"] * PRICE_AUDIO_INPUT
        # cost_audio_out = token_totals["output_audio_tokens"] * PRICE_AUDIO_OUTPUT
        # cost_cached = token_totals["input_cached_tokens"] * PRICE_CACHED_INPUT
        # total_azure_cost = cost_text_in + cost_text_out + cost_audio_in + cost_audio_out + cost_cached
        # cost_per_min = total_azure_cost / call_duration_min if call_duration_min > 0 else 0
        # 
        # cost_lines = [
        #     f"\n{'='*70}",
        #     f"  TOKEN COST REPORT — Session: {session_id}",
        #     f"  Scenario: {scenario_key} | Pipeline: {pipeline_mode}",
        #     f"  Duration: {call_duration_s:.1f}s ({call_duration_min:.2f} minutes)",
        #     f"  Total Turns: {len(token_usage_per_turn)}",
        #     f"{'='*70}",
        #     f"  Azure OpenAI Token Usage:",
        #     f"    Text Input Tokens ............. {token_totals['input_text_tokens']:,}  (${cost_text_in:.6f})",
        #     f"    Text Output Tokens ............ {token_totals['output_text_tokens']:,}  (${cost_text_out:.6f})",
        #     f"    Audio Input Tokens ............ {token_totals['input_audio_tokens']:,}  (${cost_audio_in:.6f})",
        #     f"    Audio Output Tokens ........... {token_totals['output_audio_tokens']:,}  (${cost_audio_out:.6f})",
        #     f"    Cached Input Tokens ........... {token_totals['input_cached_tokens']:,}  (${cost_cached:.6f})",
        #     f"  {'─'*50}",
        #     f"    TOTAL TOKENS .................. {token_totals['total_tokens']:,}",
        #     f"    Input Tokens .................. {token_totals['input_tokens']:,}",
        #     f"    Output Tokens ................. {token_totals['output_tokens']:,}",
        #     f"  {'─'*50}",
        #     f"    AZURE TOTAL COST .............. ${total_azure_cost:.6f}",
        #     f"    COST PER MINUTE ............... ${cost_per_min:.6f}/min",
        #     f"  {'─'*50}",
        #     f"  Per-Turn Breakdown:",
        # ]
        # for tu in token_usage_per_turn:
        #     tu_cost = (
        #         tu["input_text_tokens"] * PRICE_TEXT_INPUT +
        #         tu["output_text_tokens"] * PRICE_TEXT_OUTPUT +
        #         tu["input_audio_tokens"] * PRICE_AUDIO_INPUT +
        #         tu["output_audio_tokens"] * PRICE_AUDIO_OUTPUT +
        #         tu["input_cached_tokens"] * PRICE_CACHED_INPUT
        #     )
        #     cost_lines.append(
        #         f"    Turn #{tu['turn']}: total={tu['total_tokens']}, "
        #         f"in(txt={tu['input_text_tokens']}, aud={tu['input_audio_tokens']}, cache={tu['input_cached_tokens']}), "
        #         f"out(txt={tu['output_text_tokens']}, aud={tu['output_audio_tokens']}) "
        #         f"→ ${tu_cost:.6f}"
        #     )
        # cost_lines.append(f"{'='*70}\n")
        # logger.info("\n".join(cost_lines))

        # Flush call summary and status update to Postgres asynchronously on call disconnect
        raw_transcript = "\n".join(full_call_transcript)
        concise_summary = await generate_call_summary(raw_transcript, phone_number, session_id)
        await db_store.log_call_end(session_id, concise_summary or raw_transcript)
        if scenario_key == "feedback_agent" and concise_summary:
            await db_store.update_feedback_summary_if_empty(session_id, concise_summary)
        if scenario_key == "real_estate_lead":
            asyncio.create_task(extract_lead_qualification_post_call(raw_transcript, phone_number, session_id, db_store))
        await redis_cache.set_session(session_id, {"status": "completed"}, expire_seconds=300)
        logger.info(f"[ws] Session {session_id} finalized and saved to Postgres successfully")


# ─── WebSocket Telephony Pipeline (Exotel SIP Path — Gemini Live) ───────
@app.websocket("/ws/exotel")
@app.websocket("/ws/exotel/")
async def exotel_pipeline(ws: WebSocket):
    """
    Exotel SIP voice streaming bridge endpoint — powered by Gemini Live API.
    Routes to the Gemini Live pipeline for sub-second latency.
    """
    from test_realtime_gemini import exotel_gemini_pipeline
    await exotel_gemini_pipeline(ws)


# ─── Legacy Exotel Pipeline (Azure OpenAI Realtime — Fallback) ──────────
@app.websocket("/ws/exotel-azure")
@app.websocket("/ws/exotel-azure/")
async def exotel_pipeline_azure(ws: WebSocket):
    """
    Exotel SIP voice streaming bridge endpoint.
    Translates Exotel telephony streaming protocol to Azure OpenAI Realtime protocol.
    """
    await ws.accept()
    try:
        exotel_sr_str = ws.query_params.get("sample_rate") or ws.query_params.get("sample-rate") or "8000"
        exotel_sr = int(exotel_sr_str)
    except Exception:
        exotel_sr = 8000
    needs_resample = exotel_sr != 24000
    logger.info(f"[exotel] Incoming call connection query params: {dict(ws.query_params)}")
    logger.info(f"[exotel] Incoming call connection (sample_rate={exotel_sr}, resample={needs_resample})")

    # Generate unique call session ID
    session_id = f"call_{uuid.uuid4().hex[:12]}"
    tracker = LatencyTracker(session_id)

    # 1. Determine scenario key (default to DEFAULT_SCENARIO)
    scenario_key = ws.query_params.get("scenario") or ws.query_params.get("scenario_key") or DEFAULT_SCENARIO
    scenario = SCENARIOS.get(scenario_key, SCENARIOS.get(DEFAULT_SCENARIO, list(SCENARIOS.values())[0] if SCENARIOS else {}))
    speaker = scenario.get("speaker", "kabir")

    # 2. Pre-resolve phone number from CallSid parameter if present in URL
    call_sid_param = ws.query_params.get("CallSid") or ws.query_params.get("callSid") or ws.query_params.get("call_sid")
    phone_number = None
    if call_sid_param:
        phone_number = _active_call_sid_map.get(call_sid_param)
        if phone_number:
            logger.info(f"[exotel] Pre-resolved phone_number {phone_number} from query param CallSid: {call_sid_param}")

    # Wait for Exotel start event
    stream_sid = None
    try:
        while not stream_sid:
            start_msg = await ws.receive_json()
            evt = start_msg.get("event")
            if evt == "start":
                stream_sid = start_msg.get("stream_sid")
                call_sid_event = start_msg.get("start", {}).get("call_sid") or start_msg.get("start", {}).get("callSid")
                logger.info(f"[exotel] Call started with streamSid: {stream_sid}, callSid: {call_sid_event}")
                if not phone_number and call_sid_event:
                    phone_number = _active_call_sid_map.get(call_sid_event)
                    if phone_number:
                        logger.info(f"[exotel] Resolved phone_number {phone_number} from start event callSid: {call_sid_event}")
                break
            elif evt == "connected":
                logger.info("[exotel] Received initial 'connected' event, waiting for 'start'...")
            elif evt == "stop":
                logger.info("[exotel] Received stop event before call started. Hanging up.")
                await ws.close()
                return
    except Exception as e:
        logger.error(f"[exotel] Failed during initial handshake: {e}")
        await ws.close()
        return

    # Fallback to query params or default dummy phone number if still unresolved
    if not phone_number:
        phone_number = ws.query_params.get("phone") or ws.query_params.get("phone_number") or ws.query_params.get("From") or ws.query_params.get("FromId") or "+919999999999"
        logger.info(f"[exotel] Resolved phone_number {phone_number} from query params or default")

    # Re-evaluate scenario key from _outbound_product_map using resolved phone number
    clean = phone_number.lstrip("+").lstrip("91").lstrip("0")
    mapped_product = _outbound_product_map.get(clean) or _outbound_product_map.get(phone_number)
    if mapped_product and mapped_product in SCENARIOS:
        scenario_key = mapped_product
        scenario = SCENARIOS[scenario_key]
        speaker = scenario.get("speaker", "kabir")
        logger.info(f"[exotel] Dynamic scenario override: Using mapped product '{scenario_key}' for phone_number: {phone_number}")

    active_call_sid = call_sid_param or call_sid_event

    if active_call_sid:
        _call_sid_to_session[active_call_sid] = session_id
        if active_call_sid not in _active_call_sid_map and phone_number:
            _active_call_sid_map[active_call_sid] = phone_number
            _active_call_start_times.setdefault(active_call_sid, int(time.time() * 1000))

    # State
    close_after_response = False
    azure_ws = None
    azure_session = None
    text_buffer = ""
    full_call_transcript = []
    tts_queue: asyncio.Queue = asyncio.Queue()
    is_speaking = False
    playback_cancel = asyncio.Event()
    greeting_lock = asyncio.Event()

    # Trackers for Telephony Echo / VAD
    is_play_greeting_active = True # Suppress VAD during welcome greeting
    last_tts_playback_start = 0
    last_tts_playback_end = 0

    user_turn_count = 0
    current_user_turn = None
    user_audio_buffers = {}
    text_history = []
    native_audio_started = False
    use_native_audio = False
    greeting_end_time = 0.0      # Timestamp until when VAD is ignored to protect greeting

    http_session = aiohttp.ClientSession()

    async def send_exotel_media(chunk: bytes):
        nonlocal stream_sid
        wait_elapsed = 0
        while not stream_sid and wait_elapsed < 5.0:
            await asyncio.sleep(0.05)
            wait_elapsed += 0.05
        if not stream_sid:
            logger.warning("[exotel] Cannot send media: stream_sid not set")
            return
        out_chunk = resample_pcm16(chunk, 24000, exotel_sr) if needs_resample else chunk
        try:
            await ws.send_json({
                "event": "media",
                "stream_sid": stream_sid,
                "media": {
                    "payload": base64.b64encode(out_chunk).decode("ascii")
                }
            })
        except Exception:
            pass

    async def send_exotel_clear():
        nonlocal stream_sid
        wait_elapsed = 0
        while not stream_sid and wait_elapsed < 5.0:
            await asyncio.sleep(0.05)
            wait_elapsed += 0.05
        if not stream_sid:
            return
        try:
            await ws.send_json({
                "event": "clear",
                "stream_sid": stream_sid
            })
        except Exception:
            pass

    async def process_sarvam_stt_exotel(whisper_text: str, turn_index: int):
        audio_bytes = user_audio_buffers.get(turn_index)
        sarvam_text = None
        if audio_bytes:
            sarvam_text = await transcribe_audio_sarvam(bytes(audio_bytes), http_session, sample_rate=exotel_sr)
        
        stt_succeeded = (sarvam_text is not None) or (whisper_text != "")
        final_text = (sarvam_text if sarvam_text is not None else whisper_text or "").strip()
        logger.info(f"[sarvam-stt] [exotel] Turn #{turn_index} final transcript: {final_text} (succeeded={stt_succeeded})")
        if final_text:
            # Log empty/filler transcripts
            _stripped = re.sub(r"[\s\.\,\?\!\u0964]+", "", final_text).lower()
            if not _stripped or _stripped in {"um", "umm", "uh", "uhh", "hmm", "hm", "ah", "aa", "er", "err", "haan", "haa"}:
                logger.info(f"[exotel-lang] Sarvam STT detected filler: \"{final_text}\"")
                return

            full_call_transcript.append(f"user: {final_text}")
            await db_store.save_transcript_turn(session_id, "user", final_text)
            if active_call_sid:
                publish_transcript(active_call_sid, "user", final_text)

    def enqueue_tts(text: str, is_greeting: bool = False):
        tts_queue.put_nowait((text, is_greeting))

    # ── TTS Playback Worker (Streaming) ──────────────────────────────
    async def play_worker():
        nonlocal is_speaking, is_play_greeting_active, last_tts_playback_start, last_tts_playback_end
        while True:
            item = await tts_queue.get()
            if item is None:
                tts_queue.task_done()
                break

            text, is_greeting = item
            is_speaking = True
            playback_cancel.clear()

            if is_greeting:
                is_play_greeting_active = True

            last_tts_playback_start = time.monotonic() * 1000  # ms

            # Check greeting cache
            cached_audio = None
            if is_greeting:
                if scenario_key == "feedback_agent":
                    phone_key = normalize_phone(phone_number)
                    cached_audio = _customer_greeting_cache.get(phone_key)
                    if cached_audio:
                        logger.info(f"[exotel] Found custom pre-cached greeting for {phone_key} ({len(cached_audio)} bytes)")
                else:
                    cached_audio = _greeting_cache.get(scenario_key)

            try:
                if cached_audio:
                    logger.info(f"[exotel] Using cached welcome greeting ({len(cached_audio)} bytes)")
                    offset = 0
                    first_chunk = True
                    while offset < len(cached_audio) and not playback_cancel.is_set():
                        if first_chunk:
                            tracker.mark("first_audio_to_client")
                            first_chunk = False
                        end = min(offset + CHUNK_SIZE_BYTES, len(cached_audio))
                        chunk = cached_audio[offset:end]
                        await send_exotel_media(chunk)
                        offset = end
                        await asyncio.sleep(CHUNK_DURATION_MS / 1000)
                    
                    if not playback_cancel.is_set():
                        full_call_transcript.append(f"assistant: {text}")
                        await db_store.save_transcript_turn(session_id, "assistant", text)
                        if active_call_sid:
                            publish_transcript(active_call_sid, "assistant", text)
                else:
                    async for chunk in synthesize_tts_streaming(
                        text, selected_language, http_session, tracker=None
                    ):
                        if playback_cancel.is_set():
                            break
                        if first_chunk:
                            tracker.mark("first_audio_to_client")
                            first_chunk = False
                        await send_exotel_media(chunk)

                    # Log greeting transcript (regular responses are logged in response.text.done)
                    if is_greeting and not playback_cancel.is_set():
                        full_call_transcript.append(f"assistant: {text}")
                        await db_store.save_transcript_turn(session_id, "assistant", text)
                        if active_call_sid:
                            publish_transcript(active_call_sid, "assistant", text)

            except Exception as e:
                logger.error(f"[exotel-tts] Playback worker error: {e}")
            finally:
                tts_queue.task_done()
                is_speaking = False
                last_tts_playback_end = time.monotonic() * 1000

                if is_greeting:
                    is_play_greeting_active = False
                    greeting_lock.set()

    # Start Exotel play worker
    play_task = asyncio.create_task(play_worker())

    # ── DB & Cache Session Start ─────────────────────────────────────
    await db_store.log_call_start(session_id, phone_number, scenario_key)
    await redis_cache.set_session(session_id, {
        "phone_number": phone_number,
        "scenario": scenario_key,
        "status": "active"
    })

    if scenario_key == "restaurant_booking":
        asyncio.create_task(db_store.init_restaurant_booking_log(session_id))
    elif scenario_key in ("feedback_agent", "real_estate_lead"):
        clean = phone_number.lstrip("+").lstrip("91").lstrip("0")
        feedback_customer = _outbound_customer_map.get(clean) or _outbound_customer_map.get(phone_number, "Mr. Arnav" if scenario_key == "real_estate_lead" else "Mr. Gautham")
        asyncio.create_task(db_store.init_feedback_agent_log(
            session_id=session_id,
            customer_name=feedback_customer,
        ))

    # Load customer profile
    customer_profile = await redis_cache.get_customer(phone_number)
    today_str = date.today().strftime("%Y-%m-%d (%A)")
    selected_lang = "tamil"  # Default language for Azure TTS voice selection
    if scenario_key == "real_estate_lead":
        clean = phone_number.lstrip("+").lstrip("91").lstrip("0")
        selected_lang = (ws.query_params.get("language") or ws.query_params.get("selected_lang") or _outbound_language_map.get(clean) or _outbound_language_map.get(phone_number, "tamil")).lower()
        dynamic_instructions = REAL_ESTATE_LANG_PROMPTS.get(selected_lang, REAL_ESTATE_LANG_PROMPTS["hindi"]) + f"\n\nTODAY'S DATE: {today_str}"
    else:
        dynamic_instructions = scenario.get("instructions", "") + f"\n\nTODAY'S DATE: {today_str}"

    if customer_profile:
        dynamic_instructions += f"\nDYNAMIC CUSTOMER PROFILE: {json.dumps(customer_profile)}"

    if scenario_key in ("feedback_agent", "real_estate_lead"):
        clean = phone_number.lstrip("+").lstrip("91").lstrip("0")
        feedback_customer = _outbound_customer_map.get(clean) or _outbound_customer_map.get(phone_number, "Mr. Arnav" if scenario_key == "real_estate_lead" else "Mr. Gautham")
        if scenario_key == "real_estate_lead":
            for prefix in ["Mr. ", "Mr.", "mr. ", "mr."]:
                if feedback_customer.startswith(prefix):
                    feedback_customer = feedback_customer[len(prefix):]
                    break
        feedback_product = _outbound_product_map.get(clean) or _outbound_product_map.get(phone_number, DEFAULT_PRODUCT_NAME)
        dynamic_instructions = dynamic_instructions.replace("{customer_name}", feedback_customer)
        dynamic_instructions = dynamic_instructions.replace("{product_name}", feedback_product)

    # Use Azure Speech Neural TTS instead of native GPT Realtime audio
    use_native_audio = False

    # Speak welcome greeting
    welcome_message = scenario.get("welcome", "")
    if welcome_message:
        if scenario_key in ("feedback_agent", "real_estate_lead"):
            clean = phone_number.lstrip("+").lstrip("91").lstrip("0")
            feedback_customer = _outbound_customer_map.get(clean) or _outbound_customer_map.get(phone_number, "Mr. Arnav" if scenario_key == "real_estate_lead" else "Mr. Gautham")
            if scenario_key == "real_estate_lead":
                for prefix in ["Mr. ", "Mr.", "mr. ", "mr."]:
                    if feedback_customer.startswith(prefix):
                        feedback_customer = feedback_customer[len(prefix):]
                        break
            feedback_product = _outbound_product_map.get(clean) or _outbound_product_map.get(phone_number, DEFAULT_PRODUCT_NAME)
            if scenario_key == "real_estate_lead":
                selected_lang = (ws.query_params.get("language") or ws.query_params.get("selected_lang") or _outbound_language_map.get(clean) or _outbound_language_map.get(phone_number, "tamil")).lower()
                lang_greetings = {
                    "hindi": "नमस्ते, क्या मैं {customer_name}-जी से बात कर रहा हूँ?",
                    "tamil": "ஹலோ சார், வணக்கம், நான் {customer_name}-கிட்ட பேசறனா?",
                    "telugu": "Namaste, nenu {customer_name}-garitho matladutunnana?",
                    "kannada": "Namaste, naanu {customer_name}-avara jothe matadta iddeena?"
                }
                welcome_message = lang_greetings.get(selected_lang, lang_greetings["hindi"])
            welcome_message = welcome_message.replace("{customer_name}", feedback_customer)
            welcome_message = welcome_message.replace("{product_name}", feedback_product)
        
        # Calculate greeting VAD ignore duration to prevent echoes/interruption
        greeting_duration = 3.5
        greeting_end_time = time.time() + greeting_duration
        logger.info(f"[exotel] Welcome greeting set. Ignoring VAD for {greeting_duration:.2f}s (until {greeting_end_time})")

        if use_native_audio:
            # Azure will speak greeting natively — greeting_lock set after response completes
            is_play_greeting_active = False
        else:
            enqueue_tts(welcome_message, is_greeting=True)
    else:
        is_play_greeting_active = False
        greeting_lock.set()

    # 5. Open connection to Azure OpenAI Realtime API
    azure_url = f"{AZURE_ENDPOINT}/openai/realtime?api-version={AZURE_API_VERSION}&deployment={AZURE_DEPLOYMENT}"
    azure_headers = {
        "api-key": AZURE_API_KEY,
        "Content-Type": "application/json"
    }

    # Determine initial turn detection (disabled if greeting is playing to protect against echo/noise)
    initial_turn_detection = None if welcome_message else {
        "type": "server_vad",
        "threshold": 0.5,
        "prefix_padding_ms": 300,
        "silence_duration_ms": 400,
    }

    # Select Azure Neural TTS voice based on language
    selected_azure_voice = AZURE_VOICES.get(selected_lang, AZURE_VOICES.get("tamil"))

    session_config = {
        "modalities": ["text"],  # Text-only: Azure Speech TTS handles audio synthesis
        "instructions": dynamic_instructions,
        "input_audio_format": "pcm16",
        "output_audio_format": "pcm16",
        "turn_detection": initial_turn_detection,
        "input_audio_transcription": {
            "model": "whisper-1"
        }
    }

    if scenario_key in SCENARIO_TOOLS:
        session_config["tools"] = SCENARIO_TOOLS[scenario_key]
        session_config["tool_choice"] = "auto"
        logger.info(f"[azure] Registered {len(SCENARIO_TOOLS[scenario_key])} tools for {scenario_key}")

    MAX_HISTORY_TURNS = 6
    last_user_lang = "ta-IN" if scenario_key == "feedback_agent" else None
    assistant_item_ids = []
    ignore_deltas_until_new_response = False

    try:
        async with aiohttp.ClientSession() as azure_session:
            async with azure_session.ws_connect(azure_url, headers=azure_headers) as azure_ws:
                logger.info("[exotel] Connected to Azure OpenAI Realtime WebSocket")



                async def azure_recv():
                    nonlocal text_buffer, is_speaking, last_user_lang, assistant_item_ids
                    nonlocal user_turn_count, current_user_turn, ignore_deltas_until_new_response
                    nonlocal native_audio_started, greeting_end_time, close_after_response

                    async for msg in azure_ws:
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            continue

                        data = json.loads(msg.data)
                        t = data.get("type")

                        # Log event arrival times for debugging latency bottlenecks
                        if t not in ("response.audio.delta", "response.audio_transcript.delta"):
                            logger.info(f"[TIMING] [EXOTEL WS RECEIVE] {t} at {time.time():.4f}")
                        elif t == "response.audio.delta" and not native_audio_started:
                            logger.info(f"[TIMING] [EXOTEL WS RECEIVE] first response.audio.delta at {time.time():.4f}")

                        if t == "error":
                            logger.error(f"[azure-exotel] Error: {data}")
                            continue

                        if t == "input_audio_buffer.speech_started":
                            if time.time() < greeting_end_time:
                                logger.info(f"[exotel-VAD] User speech detected during welcome greeting lockout (remaining: {greeting_end_time - time.time():.2f}s) — IGNORED to prevent premature interruption")
                                continue

                            tracker.new_turn()
                            tracker.mark("speech_start")
                            logger.info("[exotel-VAD] User started speaking — stopping assistant speech immediately (barge-in)")
                            
                            playback_cancel.set()
                            await send_exotel_clear()
                            while not tts_queue.empty():
                                try:
                                    tts_queue.get_nowait()
                                    tts_queue.task_done()
                                except asyncio.QueueEmpty:
                                    break

                            user_turn_count += 1
                            user_audio_buffers[user_turn_count] = bytearray()
                            current_user_turn = user_turn_count
                            is_speaking = False

                            try:
                                await azure_ws.send_json({"type": "response.cancel"})
                            except Exception:
                                pass
                            ignore_deltas_until_new_response = True
                            text_buffer = ""

                        elif t == "input_audio_buffer.speech_stopped":
                            if time.time() < greeting_end_time:
                                continue
                            tracker.mark("speech_end")
                            if use_native_audio:
                                asyncio.create_task(process_sarvam_stt_exotel("", user_turn_count))
                            current_user_turn = None

                        elif t == "conversation.item.input_audio_transcription.completed":
                            tracker.mark("stt_complete")
                            user_text = data.get("transcript", "").strip()
                            
                            # Process Sarvam STT asynchronously
                            asyncio.create_task(process_sarvam_stt_exotel(user_text, user_turn_count))

                            if use_native_audio:
                                # For native audio (monolingual), let the server's automatic response continue.
                                # DO NOT call response.cancel or response.create here!
                                continue

                            # Skip empty/filler transcripts
                            _stripped = re.sub(r"[\s\.\,\?\!\u0964]+", "", user_text).lower()
                            if not _stripped or _stripped in {"um", "umm", "uh", "uhh", "hmm", "hm", "ah", "aa", "er", "err", "haan", "haa"}:
                                logger.info(f"[exotel-lang] Skipping response — empty/filler: \"{user_text}\"")
                                continue

                            user_lang = detect_language(user_text) if user_text else "en-IN"
                            logger.info(f"[exotel-lang] Detected user_lang={user_lang} (last={last_user_lang})")

                            if (
                                user_lang in ("hi-IN", "ta-IN")
                                and last_user_lang is not None
                                and user_lang != last_user_lang
                            ):
                                removed = sum(1 for h in text_history if h["role"] == "assistant" and h["lang"] != user_lang)
                                if removed:
                                    logger.info(f"[exotel-lang] Switch {last_user_lang} -> {user_lang}. Dropping {removed} prior assistant turns.")
                                    text_history[:] = [h for h in text_history if not (h["role"] == "assistant" and h["lang"] != user_lang)]

                            if (
                                user_lang in ("hi-IN", "ta-IN")
                                and last_user_lang is not None
                                and user_lang != last_user_lang
                                and assistant_item_ids
                            ):
                                logger.info(f"[exotel-lang] Purging {len(assistant_item_ids)} server-side assistant items.")
                                for item_id in assistant_item_ids:
                                    await azure_ws.send_json({
                                        "type": "conversation.item.delete",
                                        "item_id": item_id,
                                    })
                                assistant_item_ids = []

                            if user_lang in ("hi-IN", "ta-IN"):
                                last_user_lang = user_lang

                            if user_text:
                                text_history.append({"role": "user", "text": user_text, "lang": user_lang})
                                del text_history[: max(0, len(text_history) - MAX_HISTORY_TURNS)]

                            if scenario_key == "feedback_agent":
                                lang_directive = (
                                    "ABSOLUTE LANGUAGE RULE: Reply ONLY in Tamil script (Tanglish — Tamil mixed with English loanwords written in Tamil). "
                                    "Do NOT use English or Devanagari script under any circumstances."
                                )
                            elif scenario_key == "real_estate_lead":
                                if selected_lang == "tamil":
                                    lang_directive = (
                                        "ABSOLUTE LANGUAGE RULE: Reply ONLY in Tamil script (Tanglish — Tamil mixed with English loanwords written in Tamil). "
                                        "Do NOT use English or Devanagari script under any circumstances."
                                    )
                                elif selected_lang == "hindi":
                                    lang_directive = (
                                        "ABSOLUTE LANGUAGE RULE: Reply ONLY in Devanagari script (Hinglish — Hindi mixed with English loanwords written in Devanagari). "
                                        "Do NOT use Tamil script under any circumstances."
                                    )
                                else:
                                    lang_directive = (
                                        f"ABSOLUTE LANGUAGE RULE: Reply ONLY in {selected_lang.capitalize()} mixed with English. "
                                        "Do NOT switch to other Indic scripts."
                                    )
                            elif user_lang == "hi-IN":
                                lang_directive = (
                                    "ABSOLUTE LANGUAGE RULE: The user is speaking in Hindi. "
                                    "Reply ONLY in Devanagari script (Hinglish — Hindi mixed with English loanwords written in Devanagari). "
                                    "Do NOT use Tamil, Punjabi, or any other Indic script."
                                )
                            elif user_lang == "ta-IN":
                                lang_directive = (
                                    "ABSOLUTE LANGUAGE RULE: The user is speaking in Tamil. "
                                    "Reply ONLY in Tamil script (Tanglish — Tamil mixed with English loanwords written in Tamil). "
                                    "Do NOT use Devanagari or any other Indic script."
                                )
                            else:
                                lang_directive = (
                                    "ABSOLUTE LANGUAGE RULE: The user is speaking in English. "
                                    "Reply ONLY in English. Do NOT use any Indic script."
                                )

                            response_input = []
                            for h in text_history:
                                response_input.append({
                                    "type": "message",
                                    "role": h["role"],
                                    "content": [{
                                        "type": "input_text" if h["role"] == "user" else "text",
                                        "text": h["text"],
                                    }],
                                })

                            await azure_ws.send_json({
                                    "type": "response.create",
                                    "response": {
                                        "instructions": lang_directive + "\n\n" + dynamic_instructions,
                                        "input": response_input,
                                    },
                                })

                        elif t == "response.created":
                            tracker.mark("response_created")
                            ignore_deltas_until_new_response = False
                            native_audio_started = False

                        elif t == "response.output_item.added":
                            item_id = data.get("item", {}).get("id")
                            role = data.get("item", {}).get("role")
                            if role == "assistant" and item_id:
                                assistant_item_ids.append(item_id)
                                del assistant_item_ids[: max(0, len(assistant_item_ids) - MAX_HISTORY_TURNS)]

                        elif t == "response.text.delta":
                            if ignore_deltas_until_new_response:
                                continue
                            if use_native_audio:
                                continue  # Audio streamed natively via response.audio.delta
                            delta = data.get("delta", "")
                            text_buffer += delta
                            text_buffer = clean_tool_hallucinations(text_buffer)

                            sentences, text_buffer = extract_sentences_aggressive(text_buffer)
                            for s in sentences:
                                enqueue_tts(s)

                        elif t == "response.text.done":
                            if ignore_deltas_until_new_response:
                                continue
                            if use_native_audio:
                                continue  # Transcript handled via response.audio_transcript
                            full_text = data.get("text", "").strip()
                            if full_text:
                                full_call_transcript.append(f"assistant: {full_text}")
                                await db_store.save_transcript_turn(session_id, "assistant", full_text)
                                if active_call_sid:
                                    publish_transcript(active_call_sid, "assistant", full_text)
                            if last_user_lang in ("hi-IN", "ta-IN") and full_text:
                                text_history.append({"role": "assistant", "text": full_text, "lang": last_user_lang})
                                del text_history[: max(0, len(text_history) - MAX_HISTORY_TURNS)]

                        # ── Native GPT Realtime Audio (restaurant_booking) ──
                        elif t == "response.audio.delta":
                            if ignore_deltas_until_new_response or not use_native_audio:
                                continue
                            audio_data = data.get("delta", "")
                            if audio_data:
                                if not native_audio_started:
                                    native_audio_started = True
                                    is_speaking = True
                                    tracker.mark("first_audio_to_client")
                                try:
                                    pcm_bytes = base64.b64decode(audio_data)
                                    await send_exotel_media(pcm_bytes)
                                except Exception:
                                    pass

                        elif t == "response.audio.done":
                            if ignore_deltas_until_new_response or not use_native_audio:
                                continue
                            is_speaking = False

                        elif t == "response.audio_transcript.delta":
                            if ignore_deltas_until_new_response or not use_native_audio:
                                continue
                            text_buffer += data.get("delta", "")

                        elif t == "response.audio_transcript.done":
                            if ignore_deltas_until_new_response or not use_native_audio:
                                continue
                            full_text = data.get("transcript", "").strip()
                            if full_text:
                                full_call_transcript.append(f"assistant: {full_text}")
                                await db_store.save_transcript_turn(session_id, "assistant", full_text)
                                if active_call_sid:
                                    publish_transcript(active_call_sid, "assistant", full_text)
                            text_buffer = ""

                        elif t == "response.function_call_arguments.done":
                            if ignore_deltas_until_new_response:
                                continue
                            tool_name = data.get("name")
                            call_id = data.get("call_id")
                            arguments_str = data.get("arguments", "{}")
                            logger.info(f"[exotel-tool] Requested tool {tool_name} with args {arguments_str}")

                            try:
                                args = json.loads(arguments_str)
                            except Exception:
                                args = {}

                            # Latency Firewall: Bypass Qdrant during intro turns to guarantee <400ms speed
                            if tool_name == "query_knowledge_base" and (tracker.turn_count <= 2 or "aama" in arguments_str.lower() or "yes" in arguments_str.lower()):
                                logger.info("[exotel-tool] Latency Firewall triggered: returning cached response instantly")
                                result_str = json.dumps({"found": True, "context": [{"text": "Estancia Apartments is located in Guduvancheri, GST Road. Amenities include swimming pool, gym, security.", "source": "cache"}], "message": "Quick cache lookup."})
                            else:
                                result_str = await execute_tool(tool_name, args, session_id, phone_number, db_store, scenario_key=scenario_key)
                                logger.info(f"[exotel-tool] Result: {result_str[:200]}")
                                if tool_name == "query_knowledge_base":
                                    await asyncio.sleep(0.5)

                            try:
                                await azure_ws.send_json({
                                    "type": "conversation.item.create",
                                    "item": {
                                        "type": "function_call_output",
                                        "call_id": call_id,
                                        "output": result_str,
                                    }
                                })
                                if tool_name == "record_feedback":
                                    logger.info("[exotel-tool] Feedback recorded, setting close_after_response flag")
                                    close_after_response = True
                                await azure_ws.send_json({"type": "response.create"})
                            except Exception as ex:
                                logger.error(f"[exotel-tool] Error sending output: {ex}")

                        elif t == "response.done":
                            if ignore_deltas_until_new_response:
                                continue
                            tracker.mark("response_done")
                            if close_after_response:
                                logger.info("[exotel] Close after feedback response pending. Scheduling delayed close...")
                                async def delayed_close_exotel():
                                    await asyncio.sleep(8.0)
                                    logger.info("[exotel] Gracefully closing connection after feedback response finished speaking")
                                    try:
                                        await ws.close()
                                    except Exception:
                                        pass
                                asyncio.create_task(delayed_close_exotel())

                            # Set greeting_lock if not already set and there is no welcome greeting lockout
                            if not welcome_message and not greeting_lock.is_set():
                                greeting_lock.set()

                            if use_native_audio:
                                # Audio was streamed directly from Azure Realtime
                                is_speaking = False
                                native_audio_started = False
                                text_buffer = ""
                            else:
                                # Azure Speech TTS path: flush remaining text
                                final_flush = text_buffer.strip()
                                if final_flush:
                                    enqueue_tts(final_flush)
                                text_buffer = ""

                # Start receive loop immediately so we don't miss greeting packets
                asyncio.create_task(azure_recv())

                # Configure the session
                await azure_ws.send_json({
                    "type": "session.update",
                    "session": session_config
                })

                # Enable VAD and clear audio buffer after the welcome greeting finishes playing
                if welcome_message:
                    async def enable_vad_after_greeting(delay: float):
                        await asyncio.sleep(delay)
                        try:
                            if ws.client_state == WebSocketState.CONNECTED:
                                logger.info(f"[azure-exotel] Welcome greeting done playing. Enabling VAD & clearing buffer.")
                                await azure_ws.send_json({"type": "input_audio_buffer.clear"})
                                await azure_ws.send_json({
                                    "type": "session.update",
                                    "session": {
                                        **session_config,
                                        "turn_detection": {
                                            "type": "server_vad",
                                            "threshold": 0.5,
                                            "prefix_padding_ms": 300,
                                            "silence_duration_ms": 400,
                                        }
                                    }
                                })
                                # Allow media forwarding now
                                greeting_lock.set()
                        except Exception as ex:
                            logger.error(f"[azure-exotel] Error enabling VAD after greeting: {ex}")

                    asyncio.create_task(enable_vad_after_greeting(greeting_duration))

                # Handle greeting in Azure conversation
                if welcome_message:
                    if use_native_audio:
                        # Trigger Azure to generate and speak the greeting natively, overriding instructions
                        await azure_ws.send_json({
                            "type": "response.create",
                            "response": {
                                "instructions": f"You are starting the call. Speak the welcome greeting exactly: \"{welcome_message}\""
                            }
                        })
                    else:
                        # Register the already-spoken Sarvam TTS greeting in conversation history
                        await azure_ws.send_json({
                            "type": "conversation.item.create",
                            "item": {
                                "type": "message",
                                "role": "assistant",
                                "content": [
                                    {
                                        "type": "text",
                                        "text": welcome_message
                                    }
                                ]
                            }
                        })

                # Exotel Receiver Loop
                while True:
                    try:
                        data = await ws.receive_json()
                    except WebSocketDisconnect:
                        logger.info("[exotel] Client disconnected")
                        break
                    except Exception:
                        break

                    evt = data.get("event")
                    if evt == "media":
                        if azure_ws and not azure_ws.closed and greeting_lock.is_set():
                            payload = data.get("media", {}).get("payload")
                            if payload:
                                try:
                                    pcm_in = base64.b64decode(payload)
                                    if current_user_turn is not None:
                                        user_audio_buffers[current_user_turn].extend(pcm_in)
                                except Exception as e:
                                    logger.error(f"[exotel-sarvam-stt] Error decoding/appending audio: {e}")
                                    pcm_in = b""

                                if needs_resample:
                                    try:
                                        pcm_24k = resample_pcm16(pcm_in, exotel_sr, 24000)
                                        fwd_payload = base64.b64encode(pcm_24k).decode("ascii")
                                    except Exception:
                                        fwd_payload = payload
                                else:
                                    fwd_payload = payload
                                await azure_ws.send_json({
                                    "type": "input_audio_buffer.append",
                                    "audio": fwd_payload
                                })
                    elif evt == "stop":
                        logger.info("[exotel] Stop event received. Hanging up.")
                        break

    except Exception as e:
        logger.error(f"[exotel] Call processing error: {e}")
    finally:
        await tts_queue.put(None)
        if azure_ws and not azure_ws.closed:
            await azure_ws.close()
        if azure_session:
            await azure_session.close()
        await http_session.close()

        # Save to database
        raw_transcript = "\n".join(full_call_transcript)
        concise_summary = await generate_call_summary(raw_transcript, phone_number, session_id)
        await db_store.log_call_end(session_id, concise_summary or raw_transcript)
        if scenario_key == "feedback_agent" and concise_summary:
            await db_store.update_feedback_summary_if_empty(session_id, concise_summary)
        if scenario_key == "real_estate_lead":
            asyncio.create_task(extract_lead_qualification_post_call(raw_transcript, phone_number, session_id, db_store))
        await redis_cache.set_session(session_id, {"status": "completed"}, expire_seconds=300)

        contact_id = _call_sid_to_contact_id.pop(active_call_sid, None) if active_call_sid else None
        if contact_id:
            duration_ms = int(time.time() * 1000) - _active_call_start_times.get(active_call_sid, int(time.time() * 1000))
            duration_sec = duration_ms // 1000
            dur_min = duration_sec // 60
            dur_sec = duration_sec % 60
            duration_str = f"{dur_min}m {dur_sec}s"
            
            outcome = "Completed"
            if not raw_transcript.strip():
                outcome = "No Answer / Failed"
            
            summary_text = concise_summary
            if not summary_text and raw_transcript.strip():
                # Fallback: use first portion of raw transcript
                summary_text = raw_transcript.strip()[:200]
                if len(raw_transcript.strip()) > 200:
                    summary_text = summary_text.rsplit(" ", 1)[0] + "..."
            if not summary_text:
                summary_text = "No speech detected during the call."
            
            if db_store and db_store.pool:
                try:
                    await db_store.add_reminder_call_history(int(contact_id), session_id, summary_text, duration_str, outcome)
                    await db_store.update_reminder_contact_status(int(contact_id), "completed")
                except Exception as db_err:
                    logger.error(f"[exotel-db] Error logging call history to DB: {db_err}")
            else:
                for c in _reminder_contacts:
                    if c["id"] == str(contact_id):
                        from datetime import datetime
                        called_at_str = datetime.now().strftime("%B %d, %Y, %I:%M %p")
                        new_item = {
                            "id": f"h-{int(time.time())}",
                            "calledAt": called_at_str,
                            "duration": duration_str,
                            "outcome": outcome,
                            "summary": summary_text
                        }
                        if "callHistory" not in c or c["callHistory"] is None:
                            c["callHistory"] = []
                        c["callHistory"].append(new_item)
                        c["attemptNumber"] += 1
                        c["status"] = "completed"
                        break

        if active_call_sid:
            cleanup_transcript_pubsub(active_call_sid)
            _active_call_sid_map.pop(active_call_sid, None)
            _active_call_start_times.pop(active_call_sid, None)

        logger.info(f"[exotel] Call session {session_id} finalized and saved")


def normalize_phone(phone: str) -> str:
    if not phone:
        return ""
    digits = "".join(c for c in phone if c.isdigit())
    return digits[-10:] if len(digits) >= 10 else digits


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, reload=True)
