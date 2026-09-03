"""
Appello — REST API Routes
All non-WebSocket HTTP endpoints: Exotel outbound, transcript SSE,
Appello UI endpoints, analytics, KB, settings, and dashboard.
"""

import asyncio
import base64 as b64
import csv
import io
import json

import tenant_context
import logging
import os
import re
import time
import uuid
from typing import Dict, List, Optional, Any

import aiohttp
from fastapi import APIRouter, Request, UploadFile, File as FastAPIFile, Form
from starlette.responses import StreamingResponse

from scenarios import SCENARIOS
from audio_utils import synthesize_tts

logger = logging.getLogger("appello")

# ─── Qdrant Vector Store ────────────────────────────────────────────────
# Uses server mode by default for production deployment.
# Configure QDRANT_URL in .env (defaults to http://localhost:6333).
# For Docker: docker run -p 6333:6333 qdrant/qdrant

_qdrant_client = None
QDRANT_COLLECTION = "knowledge_base"
QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")

# Azure Embeddings config
AZURE_EMBEDDING_ENDPOINT = os.getenv("AZURE_OPENAI_EMBEDDING_ENDPOINT", "").rstrip("/")
AZURE_EMBEDDING_API_KEY = os.getenv("AZURE_OPENAI_EMBEDDING_API_KEY", "")
AZURE_EMBEDDING_DEPLOYMENT = os.getenv("AZURE_OPENAI_EMBEDDING_DEPLOYMENT", "text-embedding-3-small")
AZURE_EMBEDDING_API_VERSION = os.getenv("AZURE_OPENAI_EMBEDDING_API_VERSION", "2023-05-15")



def _get_qdrant_client():
    """Lazy-initialize Qdrant client in server mode."""
    global _qdrant_client
    if _qdrant_client is None:
        try:
            from qdrant_client import QdrantClient
            _qdrant_client = QdrantClient(url=QDRANT_URL, timeout=1.0)
            logger.info(f"[qdrant] Connected to Qdrant server at {QDRANT_URL}")
        except Exception as e:
            logger.warning(f"[qdrant] Failed to connect to Qdrant: {e}. KB features will be limited.")
    return _qdrant_client


def _ensure_qdrant_collection(vector_size: int = 1536):
    """Create the knowledge_base collection if it doesn't exist."""
    client = _get_qdrant_client()
    if not client:
        return
    try:
        from qdrant_client.models import Distance, VectorParams
        collections = [c.name for c in client.get_collections().collections]
        if QDRANT_COLLECTION not in collections:
            client.create_collection(
                collection_name=QDRANT_COLLECTION,
                vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE),
            )
            logger.info(f"[qdrant] Created collection '{QDRANT_COLLECTION}' (dim={vector_size})")
    except Exception as e:
        logger.error(f"[qdrant] Error ensuring collection: {e}")


async def _compute_embedding(text: str) -> Optional[List[float]]:
    """Compute text embedding using Azure OpenAI Embeddings API."""
    if not AZURE_EMBEDDING_ENDPOINT or not AZURE_EMBEDDING_API_KEY:
        logger.warning("[qdrant] Azure embedding credentials not configured")
        return None
    url = f"{AZURE_EMBEDDING_ENDPOINT}/openai/deployments/{AZURE_EMBEDDING_DEPLOYMENT}/embeddings?api-version={AZURE_EMBEDDING_API_VERSION}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                headers={"api-key": AZURE_EMBEDDING_API_KEY, "Content-Type": "application/json"},
                json={"input": text[:8000]},
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data["data"][0]["embedding"]
                else:
                    error = await resp.text()
                    logger.error(f"[qdrant] Embedding API error ({resp.status}): {error[:200]}")
    except Exception as e:
        logger.error(f"[qdrant] Embedding computation failed: {e}")
    return None


def _chunk_text(text: str, chunk_size: int = 800, overlap: int = 100) -> List[str]:
    """Split text into overlapping chunks for vector embedding."""
    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        start = end - overlap
    return chunks


def _extract_menu_items_from_text(text: str) -> List[dict]:
    """Extract food items and prices from PDF text using regex heuristics."""
    
    def _is_clean_price(line: str) -> Optional[float]:
        # Strip common currency symbols, bullets, spaces, and punctuation
        cleaned = re.sub(r'[₹Rs\.\s,■\$–—\-]', '', line)
        if cleaned.isdigit():
            try:
                val = float(cleaned)
                if 5 <= val <= 50000:
                    return val
            except ValueError:
                pass
        return None

    items = []
    # Common patterns: "Item Name ... ₹123" or "Item Name ... Rs. 123" or "Item Name ... 123"
    patterns = [
        r'([A-Za-z][A-Za-z\s&/,\(\)\-\']+?)\s*[–—\-\.]+\s*[₹Rs\.]*\s*(\d+(?:\.\d{1,2})?)',
        r'([A-Za-z][A-Za-z\s&/,\(\)\-\']+?)\s+₹\s*(\d+(?:\.\d{1,2})?)',
        r'([A-Za-z][A-Za-z\s&/,\(\)\-\']+?)\s+Rs\.?\s*(\d+(?:\.\d{1,2})?)',
    ]
    
    # Category detection keywords
    category_keywords = {
        "Breakfast": ["breakfast", "morning", "brunch", "idli", "dosa", "upma", "poha", "paratha"],
        "Starters": ["starter", "appetizer", "snack", "chaat", "tikka", "kebab", "pakora", "samosa"],
        "Main Course": ["main course", "entree", "curry", "biryani", "dal", "paneer", "chicken", "mutton", "fish", "rice", "roti", "naan"],
        "Beverages": ["beverage", "drink", "juice", "lassi", "chai", "tea", "coffee", "shake", "mocktail"],
        "Desserts": ["dessert", "sweet", "gulab", "halwa", "kheer", "ice cream", "cake"],
        "Specials": ["special", "chef", "thali", "combo", "platter"],
    }
    
    clean_lines = [l.strip() for l in text.split("\n") if l.strip()]
    current_category = "Main Course"
    seen_names = set()
    
    i = 0
    while i < len(clean_lines):
        line1 = clean_lines[i]
        
        # Check if line1 is a category header
        lower_line1 = line1.lower()
        is_category = False
        for cat, keywords in category_keywords.items():
            if any(kw in lower_line1 for kw in keywords) and len(line1) < 40 and not any(c.isdigit() for c in line1[-5:]):
                current_category = cat
                is_category = True
                break
        if is_category:
            i += 1
            continue
            
        # Plausible item name checks
        is_name_plausible = len(line1) >= 3 and len(line1) <= 60 and re.match(r'^[A-Za-z]', line1) and not line1.isdigit()
        is_header_row = lower_line1 in ["item", "price", "price (₹)", "price(₹)", "menu", "description"]
        
        if is_name_plausible and not is_header_row:
            # Check 1: Alternating line direct (line1 = Name, line2 = Price)
            if i < len(clean_lines) - 1:
                line2 = clean_lines[i+1]
                price_val = _is_clean_price(line2)
                if price_val is not None:
                    if line1.lower() not in seen_names:
                        seen_names.add(line1.lower())
                        items.append({
                            "name": line1,
                            "price": f"₹{int(price_val)}",
                            "category": current_category,
                            "description": "",
                            "available": True
                        })
                    i += 2
                    continue
            
            # Check 2: Alternating line with description (line1 = Name, line2 = Desc, line3 = Price)
            if i < len(clean_lines) - 2:
                line2 = clean_lines[i+1]
                line3 = clean_lines[i+2]
                price_val = _is_clean_price(line3)
                if price_val is not None and _is_clean_price(line2) is None:
                    if line1.lower() not in seen_names:
                        seen_names.add(line1.lower())
                        items.append({
                            "name": line1,
                            "price": f"₹{int(price_val)}",
                            "category": current_category,
                            "description": line2,
                            "available": True
                        })
                    i += 3
                    continue
                    
        # Fallback to single line regex matches on line1
        for pattern in patterns:
            matches = re.findall(pattern, line1)
            for match in matches:
                name = match[0].strip()
                price = match[1].strip()
                if 3 <= len(name) <= 60 and name.lower() not in seen_names:
                    try:
                        price_val = float(price)
                        if 5 <= price_val <= 50000:
                            seen_names.add(name.lower())
                            items.append({
                                "name": name,
                                "price": f"₹{int(price_val)}",
                                "category": current_category,
                                "description": "",
                                "available": True
                            })
                    except ValueError:
                        pass
        i += 1
    
    return items


# ─── JWT Helper ─────────────────────────────────────────────────────────

def get_user_email_from_request(request: Request) -> str:
    """Extract user email from Firebase JWT Bearer token in the request.
    Falls back to 'anonymous@local' for unauthenticated requests."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return "anonymous@local"
    token = auth_header[7:]
    try:
        # Decode JWT payload without verification (Firebase tokens are already
        # validated by the frontend; the backend trusts the Bearer token).
        # Add padding if needed
        payload_b64 = token.split(".")[1]
        padding = 4 - len(payload_b64) % 4
        if padding != 4:
            payload_b64 += "=" * padding
        payload = json.loads(b64.b64decode(payload_b64))
        return payload.get("email", payload.get("sub", "anonymous@local"))
    except Exception:
        return "anonymous@local"

# ─── Module-level state (injected from main.py at init) ─────────────────
_db_store = None
_redis_cache = None
_tenant_store = None

# Module-level maps used for outbound calls and transcript streaming
_outbound_customer_map: Dict[str, str] = {}
_active_call_sid_map: Dict[str, str] = {}
_active_call_start_times: Dict[str, int] = {}
_outbound_product_map: Dict[str, str] = {}
_outbound_language_map: Dict[str, str] = {}
DEFAULT_PRODUCT_NAME = "copper wire gauge"
REAL_ESTATE_PRODUCT_NAME = "Estancia Apartments, Guduvancheri"

# Exotel telephony credentials
EXOTEL_ACCOUNT_SID = os.getenv("EXOTEL_ACCOUNT_SID", "")
EXOTEL_API_KEY = os.getenv("EXOTEL_API_KEY", "")
EXOTEL_API_TOKEN = os.getenv("EXOTEL_API_TOKEN", "")
EXOTEL_CALLER_ID = os.getenv("EXOTEL_CALLER_ID", "")
EXOTEL_APP_FLOW_ID = os.getenv("EXOTEL_APP_FLOW_ID", "")

# Azure config (for post-call summary)
AZURE_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT", "").rstrip("/")
AZURE_API_KEY = os.getenv("AZURE_OPENAI_API_KEY", "")
AZURE_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-realtime-2.1")
AZURE_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2025-04-01-preview")

# ─── Live Transcript Pub/Sub (SSE) ──────────────────────────────────────
_transcript_subscribers: Dict[str, List[asyncio.Queue]] = {}
_call_transcripts: Dict[str, List[dict]] = {}
_call_sid_to_session: Dict[str, str] = {}
_call_sid_to_contact_id: Dict[str, str] = {}

# Global greeting caches (shared with main.py pipeline)
_greeting_cache: dict[str, bytes] = {}
_customer_greeting_cache: dict[str, bytes] = {}


def normalize_phone(phone: str) -> str:
    if not phone:
        return ""
    digits = "".join(c for c in phone if c.isdigit())
    return digits[-10:] if len(digits) >= 10 else digits


def publish_transcript(call_sid: str, role: str, text: str):
    """Publish a transcript turn to all SSE subscribers for a given call_sid."""
    turn = {"role": role, "text": text, "timestamp": time.time()}
    if call_sid not in _call_transcripts:
        _call_transcripts[call_sid] = []
    _call_transcripts[call_sid].append(turn)
    subs = _transcript_subscribers.get(call_sid, [])
    logger.info(f"[transcript-pub] Publishing to call_sid={call_sid}, role={role}, text={text[:60]}, subscribers={len(subs)}")
    for q in subs:
        try:
            q.put_nowait(turn)
        except asyncio.QueueFull:
            logger.warning(f"[transcript-pub] Queue full for call_sid={call_sid}")


def cleanup_transcript_pubsub(call_sid: str):
    """Signal end-of-stream to all subscribers and clean up."""
    for q in _transcript_subscribers.get(call_sid, []):
        try:
            q.put_nowait(None)
        except asyncio.QueueFull:
            pass
    _transcript_subscribers.pop(call_sid, None)


# Module-level KBEngine reference (injected from main.py)
_kb_engine = None

def init(db_store, redis_cache, kb_engine=None):
    """Inject shared stores from main.py."""
    global _db_store, _redis_cache, _kb_engine
    _db_store = db_store
    _redis_cache = redis_cache
    _kb_engine = kb_engine
    logger.info("[api_routes] Initialized with shared stores")


def init_tenancy(tenant_store):
    """Inject the tenant store so routes here can attribute work to a tenant."""
    global _tenant_store
    _tenant_store = tenant_store
    logger.info("[api_routes] Tenancy enabled")


async def tenant_of(request) -> str:
    """Tenant id behind this request. See tenant_context for resolution order."""
    return await tenant_context.resolve_tenant_id(request)


# ─── In-memory stores for Appello UI endpoints ──────────────────────────
_reminder_contacts: list[dict] = []
_reminder_id_counter = 0
_analytics_action_items: Dict[str, list[dict]] = {}
_knowledge_files: list[dict] = []
_knowledge_id_counter = 0
_campaign_customers: list[dict] = []
_campaign_id_counter = 0
_campaign_state: Dict[str, str] = {"state": "idle"}
_app_settings: dict = {
    "sipProvider": "exotel",
    "concurrentChannels": 4,
    "inboundDid": "",
    "callRecording": False,
    "sttEngine": "whisper",
    "ttsEngine": "sarvam",
    "llmModel": "gpt-4o",
    "languages": ["en-IN", "ta-IN"],
    "agentTone": "warm",
    "maxTurnsBeforeEscalation": 6,
    "silenceTimeout": 3,
    "escalationFallback": "human",
    "callingWindowStart": "09:00",
    "callingWindowEnd": "18:00",
    "maxDailyAttempts": 3,
    "retryInterval": 60,
    "outboundConcurrency": 8,
    "postCallSms": False,
    "traiDndCheck": True,
}


# ─── Router ─────────────────────────────────────────────────────────────
router = APIRouter()


@router.get("/")
async def root():
    return {"status": "ok", "service": "appello-bridge", "version": "conversational-ya"}


@router.get("/health")
async def health():
    return {"status": "ok", "service": "appello-bridge"}


@router.get("/api/session")
async def get_session(scenario: str = "restaurant_booking"):
    """Returns scenario config to the frontend."""
    config = SCENARIOS.get(scenario, list(SCENARIOS.values())[0] if SCENARIOS else {})
    return {
        "instructions": config.get("instructions", ""),
        "welcome_message": config.get("welcome", ""),
        "speaker": config.get("speaker", "kabir"),
    }


# ─── WebRTC Realtime Session & Tool Execution ───────────────────────────

@router.post("/api/realtime/session")
async def create_realtime_session(request: Request):
    """
    Generate an ephemeral client secret for WebRTC browser connections.
    The browser uses this token to connect directly to Azure OpenAI Realtime
    via RTCPeerConnection — eliminating the WebSocket proxy double-hop.
    """
    body = await request.json()
    scenario_key = body.get("scenario", "restaurant_booking")
    phone_number = body.get("phone_number", "+919999999999")
    selected_lang = body.get("language", "hindi").lower()

    scenario = SCENARIOS.get(scenario_key, list(SCENARIOS.values())[0] if SCENARIOS else {})
    # All agents now use WebSocket + Sarvam TTS (native audio removed)
    use_native_audio = False

    # Build dynamic instructions (same logic as voice_pipeline in main.py)
    from datetime import date
    today_str = date.today().strftime("%Y-%m-%d (%A)")
    dynamic_instructions = scenario.get("instructions", "") + f"\n\nTODAY'S DATE: {today_str}"

    welcome_message = scenario.get("welcome", "")
    if scenario_key == "real_estate_lead":
        lang_greetings = {
            "hindi": "नमस्ते, क्या मैं मिस्टर अर्णव से बात कर रहा हूँ?",
            "tamil": "வணக்கம், நான் மிஸ்டர் அர்னவ்-கிட்ட பேசறேனா?",
            "telugu": "నమస్తే, నేను మిస్టర్ అర్నవ్ గారితో మాట్లాడుతున్నానा?",
            "kannada": "ನಮಸ್ತೆ, ನಾನು ಮಿಸ್ಟರ್ ಅರ್ನವ್ ಅವರ ಜೊತೆ ಮಾತಾಡ್ತಾ ಇದ್ದೀನಾ?"
        }
        welcome_message = lang_greetings.get(selected_lang, lang_greetings["hindi"])
        
        # Inject initial greeting constraint and the embedded PROJECT DATA
        lang_names = {
            "hindi": "Hindi/Hinglish (Devanagari script)",
            "tamil": "Tamil/Tanglish (Tamil script)",
            "telugu": "Telugu (Telugu script)",
            "kannada": "Kannada (Kannada script)"
        }
        target_lang = lang_names.get(selected_lang, lang_names["hindi"])
        dynamic_instructions += f"\n\nINITIAL LANGUAGE CONSTRAINT:\n- You MUST start the call in {target_lang} speaking exactly the welcome greeting: \"{welcome_message}\"."

        # Hardcode test name and product
        dynamic_instructions = dynamic_instructions.replace("{customer_name}", "Mr. Arnav")
        dynamic_instructions = dynamic_instructions.replace("{product_name}", REAL_ESTATE_PRODUCT_NAME)
    elif scenario_key == "feedback_agent":
        feedback_customer = body.get("customer_name", "Mr. Gautham")
        dynamic_instructions = dynamic_instructions.replace("{customer_name}", feedback_customer)
        dynamic_instructions = dynamic_instructions.replace("{product_name}", DEFAULT_PRODUCT_NAME)

    # Hydrate customer profile from Redis cache (only for collections/feedback, not restaurant booking/real estate)
    if _redis_cache and scenario_key not in ("restaurant_booking", "real_estate_lead"):
        customer_profile = await _redis_cache.get_customer(phone_number)
        if customer_profile:
            dynamic_instructions += f"\nDYNAMIC CUSTOMER PROFILE: {json.dumps(customer_profile)}"

    # Build session config for the ephemeral token (Azure GA protocol)
    from tools import SCENARIO_TOOLS
    session_config = {
        "session": {
            "type": "realtime",
            "model": AZURE_DEPLOYMENT,
            "output_modalities": ["audio"] if use_native_audio else ["text"],
            "instructions": dynamic_instructions,
            "audio": {
                "input": {
                    "turn_detection": None,  # Start with VAD off for greeting
                },
                "output": {
                    "voice": "echo" if use_native_audio else "alloy",
                    "speed": 1.15,
                },
            },
        }
    }

    # Always enable transcription for WebRTC to display user transcripts in browser
    # Force language to English ONLY for restaurant_booking scenario to prevent misdetecting English as other scripts/languages
    # For multilingual scenarios like real_estate_lead, omit 'language' so Whisper auto-detects what the user speaks.
    session_config["session"]["audio"]["input"]["transcription"] = {
        "model": "whisper-1"
    }
    if scenario_key == "restaurant_booking":
        session_config["session"]["audio"]["input"]["transcription"]["language"] = "en"

    # Inject tool schemas for all agents that have tools
    if scenario_key in SCENARIO_TOOLS:
        tools = SCENARIO_TOOLS[scenario_key]
        session_config["session"]["tools"] = tools
        session_config["session"]["tool_choice"] = "auto"

    # Generate ephemeral token from Azure (GA endpoint — no api-version param)
    # Retry up to 2 times to handle Azure cold-start 400 errors
    client_secret_url = f"{AZURE_ENDPOINT}/openai/v1/realtime/client_secrets"
    max_retries = 2
    last_error_text = ""

    try:
        async with aiohttp.ClientSession() as session:
            for attempt in range(max_retries + 1):
                async with session.post(
                    client_secret_url,
                    headers={
                        "api-key": AZURE_API_KEY,
                        "Content-Type": "application/json",
                    },
                    json=session_config,
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        # GA response: token is at top-level "value" field
                        ephemeral_token = data.get("value", "")
                        expires_at = data.get("expires_at_unix_timestamp", 0)
                        # Fallback: check nested client_secret structure
                        if not ephemeral_token:
                            client_secret = data.get("client_secret", {})
                            ephemeral_token = client_secret.get("value", "")
                            expires_at = client_secret.get("expires_at_unix_timestamp", expires_at)

                        # Create session ID and log call start
                        session_id = f"call_{uuid.uuid4().hex[:12]}"
                        if _db_store:
                            await _db_store.log_call_start(session_id, phone_number, scenario_key)
                            if scenario_key == "restaurant_booking":
                                asyncio.create_task(_db_store.init_restaurant_booking_log(session_id))
                            elif scenario_key in ("feedback_agent", "real_estate_lead"):
                                feedback_customer = "Mr. Arnav" if scenario_key == "real_estate_lead" else body.get("customer_name", "Mr. Gautham")
                                asyncio.create_task(_db_store.init_feedback_agent_log(
                                    session_id=session_id,
                                    customer_name=feedback_customer,
                                ))
                        if _redis_cache:
                            await _redis_cache.set_session(session_id, {
                                "phone_number": phone_number,
                                "scenario": scenario_key,
                                "status": "active",
                            })

                        # Calculate greeting duration for VAD delay
                        greeting_duration = max(3.0, min(10.0, len(welcome_message) / 14.0)) if welcome_message else 0

                        logger.info(f"[webrtc] Ephemeral token generated for session {session_id} (scenario={scenario_key}, expires={expires_at})")

                        return {
                            "session_id": session_id,
                            "ephemeral_token": ephemeral_token,
                            "expires_at": expires_at,
                            "calls_url": f"{AZURE_ENDPOINT}/openai/v1/realtime/calls",
                            "welcome_message": welcome_message,
                            "greeting_duration": greeting_duration,
                            "use_native_audio": use_native_audio,
                            "scenario": scenario_key,
                        }
                    else:
                        last_error_text = await resp.text()
                        logger.warning(f"[webrtc] Azure client_secrets attempt {attempt+1}/{max_retries+1} failed ({resp.status}): {last_error_text[:300]}")
                        if attempt < max_retries:
                            await asyncio.sleep(1)
                            continue
                        else:
                            logger.error(f"[webrtc] Azure client_secrets failed after {max_retries+1} attempts")
                            return {"error": f"Azure token generation failed: {resp.status}", "detail": last_error_text[:200]}
    except Exception as e:
        logger.error(f"[webrtc] Ephemeral token generation error: {e}")
        return {"error": f"Token generation failed: {str(e)}"}


@router.post("/api/tools/execute")
async def execute_tool_api(request: Request):
    """
    Execute a tool call forwarded from the browser's WebRTC data channel.
    The browser receives function_call events from Azure, forwards them here,
    and sends the result back to Azure via the data channel.
    """
    body = await request.json()
    tool_name = body.get("name", "")
    arguments = body.get("arguments", {})
    session_id = body.get("session_id", "")
    phone_number = body.get("phone_number", "+919999999999")
    scenario_key = body.get("scenario", "")

    if not tool_name:
        return {"error": "Missing tool name"}

    logger.info(f"[webrtc-tool] Executing {tool_name} for session {session_id}: {json.dumps(arguments)[:200]}")

    try:
        from tools import execute_tool
        result_str = await execute_tool(tool_name, arguments, session_id, phone_number, _db_store, scenario_key=scenario_key)
        logger.info(f"[webrtc-tool] {tool_name} result: {result_str[:200]}")
        return {"result": result_str}
    except Exception as e:
        logger.error(f"[webrtc-tool] {tool_name} execution failed: {e}")
        return {"error": f"Tool execution failed: {str(e)}"}


# ─── Greeting Cache API ─────────────────────────────────────────────────

@router.post("/api/call/cache-greeting")
async def cache_customer_greeting(request: Request):
    body = await request.json()
    customer_name = body.get("customer_name")
    customer_phone = body.get("customer_phone")
    if not customer_name or not customer_phone:
        return {"status": "error", "message": "customer_name and customer_phone are required"}

    scenario = SCENARIOS.get("feedback_agent")
    if not scenario:
        return {"status": "error", "message": "feedback_agent scenario not found"}

    welcome_text = scenario["welcome"].replace("{customer_name}", customer_name)
    speaker = scenario["speaker"]

    logger.info(f"[cache] Pre-synthesizing greeting for {customer_name} ({customer_phone}) in background...")

    try:
        async with aiohttp.ClientSession() as session:
            audio = await synthesize_tts(welcome_text, speaker, session, pace=1.03)
            if audio:
                phone_key = normalize_phone(customer_phone)
                _customer_greeting_cache[phone_key] = audio
                logger.info(f"[cache] Successfully cached greeting for phone_key {phone_key} ({len(audio)} bytes)")
                return {"status": "success", "message": f"Cached greeting for {customer_name}"}
            else:
                return {"status": "error", "message": "TTS synthesis returned no audio"}
    except Exception as e:
        logger.error(f"[cache] Background caching failed: {e}")
        return {"status": "error", "message": str(e)}


# ─── Exotel Outbound Call API ────────────────────────────────────────────

@router.post("/api/call/outbound")
async def trigger_outbound_call(request: Request):
    body = await request.json()
    customer_name = body.get("customer_name", "Mr. Gautham")
    customer_phone = body.get("customer_phone", "")
    product_name = body.get("product_name", DEFAULT_PRODUCT_NAME)
    contact_id = body.get("contact_id")

    if not customer_phone:
        return {"error": "customer_phone is required", "status": "failed"}
    if not EXOTEL_ACCOUNT_SID or not EXOTEL_API_KEY or not EXOTEL_API_TOKEN:
        return {"error": "Exotel credentials not configured", "status": "failed"}

    clean_phone = customer_phone.lstrip("0").lstrip("+91")
    _outbound_customer_map[clean_phone] = customer_name
    _outbound_customer_map[customer_phone] = customer_name
    _outbound_product_map[clean_phone] = product_name
    _outbound_product_map[customer_phone] = product_name
    language = body.get("language", "tamil")
    _outbound_language_map[clean_phone] = language
    _outbound_language_map[customer_phone] = language
    logger.info(f"[outbound] Mapped {customer_phone} -> {customer_name} (product: {product_name}, language: {language})")

    exotel_url = f"https://api.exotel.com/v1/Accounts/{EXOTEL_ACCOUNT_SID}/Calls/connect.json"
    flow_url = f"http://my.exotel.com/exoml/start/{EXOTEL_APP_FLOW_ID}"
    auth_string = b64.b64encode(f"{EXOTEL_API_KEY}:{EXOTEL_API_TOKEN}".encode()).decode()

    form_data = {
        "From": customer_phone,
        "CallerId": EXOTEL_CALLER_ID,
        "CallType": "trans",
        "Url": flow_url,
    }

    logger.info(f"[outbound] Triggering call to {customer_phone} via Exotel")

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                exotel_url,
                headers={
                    "Authorization": f"Basic {auth_string}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data=form_data,
            ) as resp:
                data = await resp.json()
                if resp.status == 200 and data.get("Call"):
                    call_sid = data["Call"].get("Sid", "")
                    _active_call_sid_map[call_sid] = customer_phone
                    _active_call_start_times[call_sid] = int(time.time() * 1000)
                    if contact_id:
                        _call_sid_to_contact_id[call_sid] = str(contact_id)
                    logger.info(f"[outbound] ✅ Call triggered! SID: {call_sid}")
                    return {
                        "status": "success",
                        "sid": call_sid,
                        "call_sid": call_sid,
                        "call_status": data["Call"].get("Status", ""),
                        "customer_name": customer_name,
                        "customer_phone": customer_phone,
                    }
                else:
                    logger.error(f"[outbound] ❌ Exotel rejected: {json.dumps(data)}")
                    return {"status": "failed", "error": data}
    except Exception as e:
        logger.error(f"[outbound] ❌ Failed: {e}")
        return {"status": "failed", "error": str(e)}


@router.post("/api/call/hangup")
async def hangup_call(request: Request):
    body = await request.json()
    call_sid = body.get("call_sid", "")
    if not call_sid:
        return {"error": "call_sid is required", "status": "failed"}

    exotel_url = f"https://api.exotel.com/v1/Accounts/{EXOTEL_ACCOUNT_SID}/Calls/{call_sid}.json"
    auth_string = b64.b64encode(f"{EXOTEL_API_KEY}:{EXOTEL_API_TOKEN}".encode()).decode()

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                exotel_url,
                headers={
                    "Authorization": f"Basic {auth_string}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data={"Status": "completed"},
            ) as resp:
                data = await resp.json()
                logger.info(f"[hangup] Call {call_sid} hangup response: {resp.status}")
                return {"status": "success", "data": data}
    except Exception as e:
        logger.error(f"[hangup] Failed: {e}")
        return {"status": "failed", "error": str(e)}


# ─── Live Transcript SSE ────────────────────────────────────────────────

@router.get("/api/call/{call_sid}/transcript-stream")
async def transcript_stream(call_sid: str):
    logger.info(f"[transcript-sse] Client connecting for call_sid={call_sid}, existing transcripts={len(_call_transcripts.get(call_sid, []))}")
    async def event_generator():
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        if call_sid not in _transcript_subscribers:
            _transcript_subscribers[call_sid] = []
        _transcript_subscribers[call_sid].append(q)
        logger.info(f"[transcript-sse] Subscribed to call_sid={call_sid}, total subscribers={len(_transcript_subscribers[call_sid])}")

        try:
            for turn in _call_transcripts.get(call_sid, []):
                yield f"data: {json.dumps(turn)}\n\n"
            while True:
                try:
                    turn = await asyncio.wait_for(q.get(), timeout=30.0)
                    if turn is None:
                        yield f"data: {json.dumps({'type': 'call_ended'})}\n\n"
                        break
                    yield f"data: {json.dumps(turn)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            subs = _transcript_subscribers.get(call_sid, [])
            if q in subs:
                subs.remove(q)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/api/call/{call_sid}/transcripts")
async def get_call_transcripts(call_sid: str):
    transcripts = _call_transcripts.get(call_sid, [])
    session_id = _call_sid_to_session.get(call_sid)
    return {
        "call_sid": call_sid,
        "session_id": session_id,
        "transcripts": transcripts,
        "count": len(transcripts),
    }


@router.get("/api/debug/transcript-state")
async def debug_transcript_state():
    """Debug endpoint: shows current transcript pub/sub state."""
    return {
        "active_call_sids": list(_active_call_sid_map.keys()),
        "active_call_phones": dict(_active_call_sid_map),
        "transcript_call_sids": list(_call_transcripts.keys()),
        "transcript_counts": {k: len(v) for k, v in _call_transcripts.items()},
        "subscriber_counts": {k: len(v) for k, v in _transcript_subscribers.items()},
        "session_mappings": dict(_call_sid_to_session),
    }


@router.get("/reminders")
async def get_reminders(request: Request):
    user_email = get_user_email_from_request(request)
    if _db_store and _db_store.pool:
        contacts = await _db_store.get_reminder_contacts(user_email)
        if contacts:
            return contacts
        # Seed a default contact for new users
        default = await _db_store.add_reminder_contact(user_email, {
            "name": "Karan Murthy",
            "phone": "+91 98765 43210",
            "location": "Bangalore",
            "priority": "Normal",
            "tags": ["Demo"],
            "notes": "Default demo contact",
            "domain": "restaurant",
            "status": "pending",
            "scheduledAt": None,
            "attributes": {},
            "totalAttempts": 3,
        })
        return [default]
    # Fallback to in-memory if DB not available
    return _reminder_contacts


@router.post("/reminders")
async def add_reminder(request: Request):
    global _reminder_id_counter
    user_email = get_user_email_from_request(request)
    body = await request.json()
    if _db_store and _db_store.pool:
        contact = await _db_store.add_reminder_contact(user_email, body)
        return contact
    # Fallback to in-memory
    _reminder_id_counter += 1
    contact = {
        "id": str(_reminder_id_counter),
        "name": body.get("name", ""),
        "phone": body.get("phone", ""),
        "location": body.get("location", ""),
        "priority": body.get("priority", "Normal"),
        "tags": body.get("tags", []),
        "notes": body.get("notes", ""),
        "domain": body.get("domain", "restaurant"),
        "status": body.get("status", "pending"),
        "scheduledAt": body.get("scheduledAt"),
        "attributes": body.get("attributes", {}),
        "callHistory": [],
        "attemptNumber": 0,
        "totalAttempts": body.get("totalAttempts", 3),
    }
    _reminder_contacts.append(contact)
    return contact


@router.patch("/reminders/{contact_id}/status")
async def update_reminder_status(contact_id: str, request: Request):
    body = await request.json()
    new_status = body.get("status", "pending")
    if _db_store and _db_store.pool:
        try:
            await _db_store.update_reminder_contact_status(int(contact_id), new_status)
            return {"status": "ok"}
        except Exception:
            pass
    # Fallback to in-memory
    for c in _reminder_contacts:
        if c["id"] == contact_id:
            c["status"] = new_status
            return {"status": "ok"}
    return {"error": "Contact not found"}


@router.post("/reminders/bulk-import")
async def bulk_import_reminders(request: Request):
    global _reminder_id_counter
    user_email = get_user_email_from_request(request)
    form = await request.form()
    file = form.get("file")
    domain = form.get("domain", "restaurant")

    if not file or not isinstance(file, UploadFile):
        return {"error": "No file provided or invalid file format"}

    content = await file.read()
    text = content.decode("utf-8")

    reader = csv.DictReader(io.StringIO(text))
    parsed_contacts = []
    for row in reader:
        contact_data: Dict[str, Any] = {
            "name": row.get("name", "").strip(),
            "phone": row.get("phone", "").strip(),
            "location": row.get("location", "").strip(),
            "priority": row.get("priority", "Normal").strip() or "Normal",
            "tags": [t.strip() for t in row.get("tags", "").split(",") if t.strip()],
            "notes": row.get("notes", "").strip(),
            "scheduledAt": row.get("scheduled_at") or row.get("scheduledAt") or None,
            "attributes": {},
        }
        if row.get("products_purchased"):
            contact_data["attributes"]["products_purchased"] = row["products_purchased"].strip()
        parsed_contacts.append(contact_data)

    if _db_store and _db_store.pool:
        imported = await _db_store.bulk_import_reminder_contacts(user_email, parsed_contacts, domain)
        logger.info(f"[reminders] Bulk imported {len(imported)} contacts to DB")
        return imported

    # Fallback to in-memory
    imported = []
    for data in parsed_contacts:
        _reminder_id_counter += 1
        contact = {
            "id": str(_reminder_id_counter),
            **data,
            "domain": domain,
            "status": "pending",
            "callHistory": [],
            "attemptNumber": 0,
            "totalAttempts": 3,
        }
        _reminder_contacts.append(contact)
        imported.append(contact)
    logger.info(f"[reminders] Bulk imported {len(imported)} contacts (in-memory)")
    return imported

@router.get("/lead-data/{session_id}")
async def get_lead_data(request: Request, session_id: str):
    """Fetch lead qualification data from feedback_agent_logs for a given session_id."""
    if not _db_store or not _db_store.pool:
        return {"status": "error", "message": "DB not available"}
    try:
        async with _db_store.acquire(await tenant_context.resolve_tenant_id(request)) as conn:
            row = await conn.fetchrow("""
                SELECT session_id, customer_name, product_review, satisfaction_level,
                       overall_experience, sentiment, escalation_required, call_summary
                FROM feedback_agent_logs
                WHERE session_id = $1;
            """, session_id)
            if not row:
                return {"status": "not_found"}
            data = dict(row)
            # Parse product_review into structured fields
            lead_info = {}
            pr = data.get("product_review", "")
            if pr and pr != "-":
                for part in pr.split(","):
                    part = part.strip()
                    if ":" in part:
                        k, v = part.split(":", 1)
                        lead_info[k.strip().lower().replace(" ", "_")] = v.strip()
            # Parse overall_experience for site visit, objection, callback
            oe = data.get("overall_experience", "")
            if oe and oe != "-":
                for part in oe.split(","):
                    part = part.strip()
                    if ":" in part:
                        k, v = part.split(":", 1)
                        lead_info[k.strip().lower().replace(" ", "_")] = v.strip()
            return {
                "status": "ok",
                "lead_info": lead_info,
                "sentiment": data.get("sentiment", "neutral"),
                "escalation_required": data.get("escalation_required", False),
                "call_summary": data.get("call_summary", ""),
            }
    except Exception as e:
        logger.error(f"[lead-data] Error fetching lead data: {e}")
        return {"status": "error", "message": str(e)}


@router.get("/calls/active")
async def get_active_calls():
    active = []
    for call_sid, phone in _active_call_sid_map.items():
        clean = phone.lstrip("+").lstrip("91").lstrip("0")
        customer_name = _outbound_customer_map.get(clean) or _outbound_customer_map.get(phone, "Unknown")
        
        # Determine agent type from outbound mapping
        product = _outbound_product_map.get(clean) or _outbound_product_map.get(phone)
        agent = "Real Estate Agent" if product == "real_estate_lead" else "AI Feedback"
        
        # Determine language from outbound mapping
        lang = _outbound_language_map.get(clean) or _outbound_language_map.get(phone) or "tamil"
        
        # Ensure we have a persistent start time
        if call_sid not in _active_call_start_times:
            _active_call_start_times[call_sid] = int(time.time() * 1000)
            
        active.append({
            "id": call_sid,
            "callerId": phone,
            "name": customer_name,
            "startedAt": _active_call_start_times[call_sid],
            "agent": agent,
            "language": lang.capitalize(),
            "status": "Active",
        })
    return active


@router.get("/calls/completed")
async def get_completed_calls(request: Request):
    if not _db_store or not _db_store.pool:
        return []
    try:
        async with _db_store.acquire(await tenant_context.resolve_tenant_id(request)) as conn:
            rows = await conn.fetch("""
                SELECT session_id, phone_number, scenario, status,
                       start_time, end_time, summary
                FROM calls
                WHERE status = 'completed'
                ORDER BY end_time DESC
                LIMIT 50;
            """)
            result = []
            for r in rows:
                duration = ""
                if r["start_time"] and r["end_time"]:
                    delta = r["end_time"] - r["start_time"]
                    mins = int(delta.total_seconds() // 60)
                    secs = int(delta.total_seconds() % 60)
                    duration = f"{mins}m {secs}s"
                result.append({
                    "id": r["session_id"],
                    "callerId": r["phone_number"],
                    "name": r["phone_number"],
                    "duration": duration,
                    "agent": "Ratan" if r["scenario"] == "feedback_agent" else r["scenario"],
                    "language": "Tamil" if r["scenario"] == "feedback_agent" else "English",
                    "outcome": "Completed",
                    "completedAt": r["end_time"].isoformat() if r["end_time"] else "",
                })
            return result
    except Exception as e:
        logger.error(f"[api] Error fetching completed calls: {e}")
        return []


@router.post("/calls/{call_id}/end")
async def end_active_call(call_id: str):
    if not call_id or call_id not in _active_call_sid_map:
        return {"error": "Call not found"}

    exotel_url = f"https://api.exotel.com/v1/Accounts/{EXOTEL_ACCOUNT_SID}/Calls/{call_id}.json"
    auth_string = b64.b64encode(f"{EXOTEL_API_KEY}:{EXOTEL_API_TOKEN}".encode()).decode()

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                exotel_url,
                headers={
                    "Authorization": f"Basic {auth_string}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data={"Status": "completed"},
            ) as resp:
                await resp.json()
                return {"status": "success"}
    except Exception as e:
        logger.error(f"[end-call] Failed: {e}")
        return {"status": "failed", "error": str(e)}


# ─── Dashboard ───────────────────────────────────────────────────────────

@router.get("/dashboard/metrics")
async def get_dashboard_metrics(request: Request, agent: str = ""):
    total_calls = 0
    active_calls = len(_active_call_sid_map)

    if _db_store and _db_store.pool:
        try:
            async with _db_store.acquire(await tenant_context.resolve_tenant_id(request)) as conn:
                total_calls = await conn.fetchval("SELECT COUNT(*) FROM calls") or 0
        except Exception:
            pass

    return {
        "totalCalls": total_calls,
        "activeCalls": active_calls,
        "connectedCalls": active_calls,
        "pendingFollowUps": len([c for c in _reminder_contacts if c["status"] == "pending"]),
    }


@router.get("/dashboard/extractions")
async def get_dashboard_extractions(request: Request, agent: str = ""):
    entities = []
    if _db_store and _db_store.pool:
        try:
            async with _db_store.acquire(await tenant_context.resolve_tenant_id(request)) as conn:
                rows = await conn.fetch("""
                    SELECT session_id, customer_name, call_datetime, product_review,
                           satisfaction_level, overall_experience, sentiment,
                           escalation_required
                    FROM feedback_agent_logs
                    ORDER BY created_at DESC
                    LIMIT 20;
                """)
                for r in rows:
                    entities.append({
                        "id": r["session_id"],
                        "type": "Enquiry",
                        "customerName": r["customer_name"] or "-",
                        "contact": "-",
                        "attributes": {
                            "product_review": r["product_review"] or "-",
                            "satisfaction_level": r["satisfaction_level"] or "-",
                            "overall_experience": r["overall_experience"] or "-",
                            "sentiment": r["sentiment"] or "-",
                        },
                        "status": "Action Required" if r["escalation_required"] else "Synced",
                        "timestamp": r["call_datetime"].isoformat() if r["call_datetime"] else "",
                    })
        except Exception as e:
            logger.error(f"[api] Error fetching extractions: {e}")
    return entities


@router.get("/dashboard/domains")
async def get_client_domains():
    return [
        {
            "id": "feedback",
            "name": "Sunrise Company - Feedback",
            "description": "Post-purchase feedback collection for automobile products",
            "icon": "📞",
            "status": "active",
        },
    ]


# ─── Analytics ───────────────────────────────────────────────────────────

@router.get("/analytics/calls")
async def analytics_calls(request: Request, agent: str = "", language: str = "", outcome: str = "", search: str = ""):
    result = []
    if _db_store and _db_store.pool:
        try:
            async with _db_store.acquire(await tenant_context.resolve_tenant_id(request)) as conn:
                rows = await conn.fetch("""
                    SELECT session_id, phone_number, scenario, status, start_time, end_time, summary
                    FROM calls
                    WHERE status = 'completed'
                    ORDER BY end_time DESC
                    LIMIT 100
                """)
                for r in rows:
                    duration = ""
                    if r["start_time"] and r["end_time"]:
                        delta = r["end_time"] - r["start_time"]
                        mins = int(delta.total_seconds() // 60)
                        secs = int(delta.total_seconds() % 60)
                        duration = f"{mins}m {secs}s"
                    result.append({
                        "id": r["session_id"],
                        "callerId": r["phone_number"],
                        "name": r["phone_number"],
                        "duration": duration,
                        "agent": "Ratan" if r["scenario"] == "feedback_agent" else r["scenario"],
                        "language": "Tamil" if r["scenario"] == "feedback_agent" else "English",
                        "outcome": r["status"],
                        "date": r["end_time"].isoformat() if r["end_time"] else "",
                        "sentiment": 0.5,
                        "transcript": "",
                        "summary": r.get("summary") or "",
                        "actionItems": _analytics_action_items.get(r["session_id"], []),
                    })
        except Exception as e:
            logger.error(f"[analytics] Error querying calls: {e}")
    return result


@router.get("/analytics/metrics")
async def analytics_metrics(request: Request, agent: str = ""):
    avg_duration = "0m 0s"
    sentiment_trend = "stable"
    escalation_count = 0
    csat_score = 0

    if _db_store and _db_store.pool:
        try:
            async with _db_store.acquire(await tenant_context.resolve_tenant_id(request)) as conn:
                total = await conn.fetchval("SELECT COUNT(*) FROM calls") or 0
                if total > 0:
                    csat_score = await conn.fetchval("SELECT AVG(coalesce(csat_score,0)) FROM feedback_agent_logs") or 0
                    escalation_count = await conn.fetchval("SELECT COUNT(*) FROM feedback_agent_logs WHERE escalation_required = true") or 0
        except Exception:
            pass

    return {
        "avgDuration": avg_duration,
        "sentimentTrend": sentiment_trend,
        "escalationCount": escalation_count,
        "csatScore": csat_score,
    }


@router.post("/analytics/calls/{call_id}/action-items/{item_id}/toggle")
async def toggle_action_item(call_id: str, item_id: str):
    items = _analytics_action_items.setdefault(call_id, [])
    for it in items:
        if it.get("id") == item_id:
            it["done"] = not bool(it.get("done", False))
            return {"status": "ok"}
    new = {"id": item_id, "text": f"Action {item_id}", "done": True}
    items.append(new)
    return {"status": "created", "item": new}


# ─── Knowledge Base ─────────────────────────────────────────────────────

@router.get("/kb/files")
async def kb_files(request: Request):
    user_email = get_user_email_from_request(request)
    if _db_store and _db_store.pool:
        return await _db_store.get_kb_files(user_email)
    return _knowledge_files


@router.post("/kb/files/upload")
async def upload_kb_file(
    request: Request,
    file: UploadFile = FastAPIFile(...),
    category: str = Form("faq"),
    agent_id: str = Form("restaurant_booking"),
):
    global _knowledge_id_counter
    user_email = get_user_email_from_request(request)
    content = await file.read()
    file_size = len(content)
    filename = file.filename or "unknown.pdf"

    chunk_count = 0
    extracted_text = ""
    menu_items = []

    # Use KBEngine for ingestion (per-agent Qdrant collections)
    if _kb_engine:
        # Delete old vectors first to prevent duplicates when re-uploading
        try:
            await _kb_engine.delete_file_vectors(filename, agent_id)
        except Exception as e:
            logger.warning(f"[kb] Error deleting old vectors for {filename}: {e}")
            
        is_csv = filename.lower().endswith(".csv")
        if is_csv:
            result = await _kb_engine.ingest_csv(content, filename, agent_id)
        else:
            result = await _kb_engine.ingest_pdf(content, filename, agent_id, category)
        chunk_count = result.get("chunks", 0)
        extracted_text = result.get("text", "")
        menu_items = result.get("items", [])
        logger.info(f"[kb] KBEngine ingested {filename} for agent '{agent_id}': {chunk_count} chunks")
    else:
        # Fallback: extract text only (no vector storage)
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(content))
            pages = [p.extract_text() or "" for p in reader.pages]
            extracted_text = "\n".join(pages)
            logger.info(f"[kb] Extracted {len(extracted_text)} chars from {filename} (no KBEngine)")
        except Exception as e:
            logger.error(f"[kb] PDF extraction error: {e}")

    # Persist file record
    item = None
    if _db_store and _db_store.pool:
        item = await _db_store.save_kb_file(user_email, filename, category, file_size, chunk_count, agent_id=agent_id)

    if not item:
        _knowledge_id_counter += 1
        item = {
            "id": str(_knowledge_id_counter),
            "name": filename,
            "category": category,
            "agent_id": agent_id,
            "format": file.content_type or "application/octet-stream",
            "size": str(file_size),
            "chunkCount": chunk_count,
            "status": "indexed",
            "uploadedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        _knowledge_files.append(item)

    item["extractedText"] = extracted_text[:2000] if extracted_text else ""
    item["menuItems"] = menu_items

    return item


@router.delete("/kb/files/{file_id}")
async def delete_kb_file(file_id: str, request: Request):
    # Delete from Qdrant vectors
    client = _get_qdrant_client()
    if client:
        try:
            from qdrant_client.models import Filter, FieldCondition, MatchValue
            # Find the file record to get filename
            user_email = get_user_email_from_request(request)
            if _db_store and _db_store.pool:
                files = await _db_store.get_kb_files(user_email)
                target_file = next((f for f in files if f["id"] == file_id), None)
                if target_file:
                    client.delete(
                        collection_name=QDRANT_COLLECTION,
                        points_selector=Filter(
                            must=[FieldCondition(key="filename", match=MatchValue(value=target_file["name"]))]
                        ),
                    )
                    logger.info(f"[kb] Deleted vectors for {target_file['name']} from Qdrant")
        except Exception as e:
            logger.warning(f"[kb] Error deleting Qdrant vectors: {e}")

    # Delete DB record
    if _db_store and _db_store.pool:
        try:
            await _db_store.delete_kb_file(int(file_id))
            return {"status": "ok", "deleted": 1}
        except Exception:
            pass

    global _knowledge_files
    before = len(_knowledge_files)
    _knowledge_files = [f for f in _knowledge_files if f.get("id") != file_id]
    return {"status": "ok", "deleted": before - len(_knowledge_files)}


@router.post("/kb/files/{file_id}/reindex")
async def reindex_kb_file(file_id: str):
    for f in _knowledge_files:
        if f.get("id") == file_id:
            f["status"] = "indexed"
            return {"status": "ok"}
    return {"error": "file not found"}


@router.get("/kb/data-sources")
async def kb_data_sources():
    return [
        {"id": "s3-menus", "name": "Menus (S3)", "description": "Menu PDFs uploaded to S3",
         "status": "connected", "lastSync": "2024-01-01T00:00:00Z"},
    ]


@router.post("/kb/query")
async def kb_query(request: Request):
    """Query the knowledge base using semantic search via KBEngine."""
    body = await request.json()
    query_text = body.get("query", "")
    agent_type = body.get("agent_type", "restaurant_booking")
    top_k = body.get("top_k", 3)

    if not query_text:
        return {"results": [], "error": "query is required"}

    if _kb_engine:
        results = await _kb_engine.search(query_text, agent_type=agent_type, top_k=top_k)
        return {
            "results": [
                {"text": r["text"], "filename": r["source"], "score": r["score"]}
                for r in results
            ]
        }

    return {"results": [], "error": "KBEngine not available"}


FSECURE_CHATBOT_PROMPT = """You are Mohit, a warm, professional customer support technician from "F-Secure". You help the user resolve their cybersecurity, VPN, password manager, or product queries.

# RESPONSE FORMAT RULES
- NEVER use markdown formatting symbols like bold (`**`), italic (`*`), headings (`###` or `#`), or bullet points (`-` or `*`).
- You MAY use numbered steps (1. 2. 3.) when providing instructions.
- NEVER start your response with greeting words like "Hi", "Hello", "Thanks", or conversational filler. Start directly with useful content.

# CONVERSATION RULES
- Only ask a clarifying question if the user's query is genuinely ambiguous AND the KB context below contains different instructions for different devices/OS. If the KB context has a single clear resolution, just provide it.
- Examples of when to ask: "How to set up VPN?" (no device specified, KB has separate steps per device). Examples of when NOT to ask: "How to reinstall F-Secure TOTAL on Windows?" (product and OS are already specified).
- Do NOT ask clarifying questions if the user has already provided the device, OS, product name, or leaked info — either in their current message or in the conversation history.
- Ask at most ONE clarifying question per topic. Once the user answers, provide the full solution. Do NOT ask follow-up sub-questions.
- Once you have enough context, immediately provide the complete step-by-step instructions from the KB. Do NOT keep asking sub-questions.
- Pay attention to the full conversation history. If the user changes topic mid-conversation, respond to their LATEST question, not earlier topics.

# KNOWLEDGE-BASE ANSWER FIDELITY (CRITICAL)
- When the KB context below contains step-by-step instructions or a resolution, you MUST reproduce those exact steps faithfully. Do NOT rephrase, summarize, skip steps, or invent your own steps.
- Preserve the exact URLs (e.g. https://my.f-secure.com/login), button names (e.g. "Release License", "Accept and Install"), menu paths (e.g. Settings > Apps > Apps & features), and technical terms from the KB article.
- You may clean up formatting for plain-text readability (e.g. convert bullets to numbered steps), add brief transitions between steps, and omit metadata like dates — but the actual instructions must match the KB article.
- If the KB context contains important warnings or notes (e.g. "export your passwords before uninstalling"), include them prominently.
- If no relevant topic is found in the provided context at all, say: "I don't have specific documentation on that, but let me connect you with our specialist team."
- NEVER mention third-party competitors like NordVPN, ExpressVPN, or CyberGhost.
- NEVER say "I am an AI" or break character. You are Mohit, a support technician.
"""

def parse_kb_source(text: str, filename: str, index: int) -> dict:
    import re
    # Try parsing 'question: ... | resolution: ... | source_url: ...'
    question = ""
    resolution = ""
    source_url = ""
    
    parts = text.split(" | ")
    for part in parts:
        part_stripped = part.strip()
        if part_stripped.startswith("question:"):
            question = part_stripped[len("question:"):].strip()
        elif part_stripped.startswith("resolution:"):
            resolution = part_stripped[len("resolution:"):].strip()
        elif part_stripped.startswith("source_url:"):
            source_url = part_stripped[len("source_url:"):].strip()
    
    if not question:
        question = text[:80] + "..."
    if not source_url:
        source_url = filename
    
    # Clean metadata timestamps (e.g. "28.8.2025Knowledge")
    resolution = re.sub(r'\d{1,2}\.\d{1,2}\.\d{4}Knowledge', '', resolution).strip()
    question_clean = re.sub(r'\d{1,2}\.\d{1,2}\.\d{4}Knowledge', '', question).strip()
    
    title = question_clean[:60] + "..." if len(question_clean) > 60 else question_clean
    
    keywords = [w.lower() for w in question_clean.split()[:5] if len(w) > 2]
    
    return {
        "id": f"kb-{index}-{hash(text) & 0xFFFFFFFFFF}",
        "title": title,
        "article": {
            "title": question_clean,
            "url": source_url
        },
        "keywords": keywords,
        "body": f"{question_clean}\n\n{resolution}" if resolution else question_clean
    }

@router.post("/chat")
async def chat_endpoint(request: Request):
    """
    Chat endpoint using Azure gpt-5-mini (Responses API)
    with Qdrant semantic search as a direct RAG injection.
    """
    body = await request.json()
    user_message = body.get("message", "")
    language = body.get("language", "en-US")
    history = body.get("history", []) # list of dicts: {"role": "user"/"agent", "text": "..."}
    
    if not user_message:
        return {"text": "Please provide a message.", "sources": []}
        
    logger.info(f"[chat] Incoming query: '{user_message}' | History size: {len(history)}")
        
    # 1. Build search query: for short follow-ups, prepend the most recent user question
    search_query = user_message
    if history and len(user_message.strip().split()) <= 3:
        # Find the most recent user message that is a real question (not a short answer)
        for msg in reversed(history):
            if msg.get("role") == "user":
                prev_text = msg.get("text", "")
                if len(prev_text.strip().split()) > 3:
                    search_query = f"{prev_text} {user_message}"
                    logger.info(f"[chat] Enriched search query: '{search_query}'")
                    break
        
    logger.info(f"[chat] Search query: '{search_query}'")
    
    endpoint = os.getenv("AZURE_OPENAI_CHAT_ENDPOINT", "")
    api_key = os.getenv("AZURE_OPENAI_CHAT_API_KEY", "")
    deployment = os.getenv("AZURE_OPENAI_CHAT_DEPLOYMENT", "gpt-5-mini")
    
    if not endpoint or not api_key:
        logger.error("[chat] Azure Chat endpoint or API key not configured.")
        return {"text": "Chat service is currently unavailable.", "sources": []}
        
    # 2. Search the Qdrant KB — use top_k=5 for better coverage of long articles
    kb_results = []
    if _kb_engine:
        kb_results = await _kb_engine.search(search_query, agent_type="fsecure_support", top_k=5)
        
    sources = []
    context_str = ""
    for idx, r in enumerate(kb_results):
        sources.append(parse_kb_source(r["text"], r["source"], idx))
        source_label = r["source"] if r["source"] != "unknown" else f"Article {idx+1}"
        context_str += f"\n--- KB Article: {source_label} ---\n{r['text']}\n"
        
    # 3. Build system prompt with retrieved context
    system_prompt = FSECURE_CHATBOT_PROMPT
    if context_str:
        system_prompt += f"\n\nBelow are the exact knowledge base articles retrieved for the user's question. Use these as your PRIMARY source. Reproduce the steps and instructions from these articles FAITHFULLY:\n{context_str}\n"
    else:
        system_prompt += "\n\nNo relevant documentation was found in the knowledge base. State that you don't have specific documentation on that, but offer to connect them with a specialist support team.\n"
        
    # Language handling for multilingual responses
    CHATBOT_LANG_NAMES = {
        "fi": "Finnish", "sv": "Swedish", "de": "German",
        "nl": "Dutch", "fr": "French", "en-US": "English (American)",
        "en-IN": "English", "ja": "Japanese",
    }
    lang_name = CHATBOT_LANG_NAMES.get(language, None)
    if language not in ("en-US", "en-IN", "en") and lang_name:
        system_prompt += f"""

# LANGUAGE OVERRIDE
- You MUST respond ONLY in {lang_name}. This overrides all previous language rules.
- The knowledge base context above is in English. Translate your answer accurately into {lang_name}.
- Keep technical product names (F-Secure, VPN, FREEDOME, ID Protection, etc.) untranslated.
- Write naturally in {lang_name} — do not do word-for-word translation.
- If the user types in {lang_name}, respond in {lang_name}. If the user types in English, still respond in {lang_name}.
"""
    elif language not in ("en-US", "en-IN", "en") and not lang_name:
        # Auto-detect: user typed in an unlisted language — instruct the LLM to mirror it
        system_prompt += f"\n- Detect the language of the user's message and respond in that same language. Translate KB content as needed.\n"

    is_responses_api = "openai/responses" in endpoint
    
    headers = {
        "api-key": api_key,
        "Content-Type": "application/json"
    }
    if not is_responses_api:
        headers["x-ms-model-mesh-model-name"] = deployment
    
    # 4. Build LLM input with full conversation history
    if is_responses_api:
        input_messages = [{"role": "system", "content": system_prompt}]
        for msg in history:
            role = "assistant" if msg.get("role") == "agent" else "user"
            input_messages.append({"role": role, "content": msg.get("text", "")})
        input_messages.append({"role": "user", "content": user_message})
        
        payload = {
            "model": deployment,
            "input": input_messages
        }
    else:
        messages = [{"role": "system", "content": system_prompt}]
        for msg in history:
            role = "assistant" if msg.get("role") == "agent" else "user"
            messages.append({"role": role, "content": msg.get("text", "")})
        messages.append({"role": "user", "content": user_message})
        
        payload = {
            "model": deployment,
            "messages": messages,
            "max_tokens": 500,
            "temperature": 0.3
        }
    
    # Retry logic for Azure serverless cold-start resilience
    last_error = None
    for attempt, timeout_secs in enumerate([20, 60], start=1):
        try:
            logger.info(f"[chat] Attempt {attempt}: Sending request to {endpoint} (timeout={timeout_secs}s)")
            timeout = aiohttp.ClientTimeout(total=timeout_secs)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(endpoint, headers=headers, json=payload) as resp:
                    if resp.status == 200:
                        resp_json = await resp.json()
                        if is_responses_api:
                            # Extract text from Responses API output format
                            final_text = ""
                            outputs = resp_json.get("output", [])
                            for out in outputs:
                                if out.get("type") == "message" and out.get("role") == "assistant":
                                    for content in out.get("content", []):
                                        if content.get("type") == "output_text":
                                            final_text = content.get("text", "")
                                            break
                            if not final_text:
                                final_text = "Sorry, I could not generate a response."
                        else:
                            final_text = resp_json["choices"][0]["message"]["content"]
                        
                        # Clean any markdown formatting the model might produce
                        import re as _re
                        final_text = _re.sub(r'\*\*([^*]+)\*\*', r'\1', final_text)
                        final_text = _re.sub(r'\*([^*]+)\*', r'\1', final_text)
                        final_text = _re.sub(r'#+\s*(.*)', r'\1', final_text)
                        final_text = _re.sub(r'^\s*[-*]\s+', r'', final_text, flags=_re.MULTILINE)
                        final_text = _re.sub(r'\d{1,2}\.\d{1,2}\.\d{4}Knowledge', '', final_text)
                        final_text = _re.sub(r'\n{3,}', '\n\n', final_text).strip()
                        
                        logger.info(f"[chat] Success response: {final_text[:80]}...")
                        
                        # Include tool call metadata
                        tool_call_info = None
                        if kb_results:
                            tool_call_info = {
                                "name": "query_knowledge_base",
                                "args": {"query": search_query[:48]}
                            }
                            
                        return {
                            "text": final_text,
                            "sources": sources,
                            "toolCall": tool_call_info
                        }
                    else:
                        err_text = await resp.text()
                        logger.error(f"[chat] Azure Chat API error ({resp.status}): {err_text}")
                        return {"text": f"Error: Chat service returned status code {resp.status}", "sources": []}
        except asyncio.TimeoutError:
            last_error = "Request timed out"
            logger.warning(f"[chat] Attempt {attempt} timed out after {timeout_secs}s, {'retrying...' if attempt == 1 else 'giving up.'}")
            continue
        except Exception as e:
            logger.exception("[chat] Exception in chat endpoint")
            return {"text": f"Error: {str(e)}", "sources": []}
    
    # Both attempts timed out
    logger.error(f"[chat] All attempts failed: {last_error}")
    return {"text": "The service is taking longer than usual. Please try again in a moment.", "sources": []}


# ─── Outbound Campaign ──────────────────────────────────────────────────

@router.get("/outbound/customers")
async def outbound_customers():
    return _campaign_customers


@router.get("/outbound/state")
async def outbound_state():
    return _campaign_state


@router.get("/outbound/stats")
async def outbound_stats():
    total = len(_campaign_customers)
    completed = len([c for c in _campaign_customers if c.get("status") == "completed"])
    committed = len([c for c in _campaign_customers if c.get("status") == "committed"])
    no_answer = len([c for c in _campaign_customers if c.get("status") == "no-answer"])
    escalated = len([c for c in _campaign_customers if c.get("status") == "escalated"])
    pending = len([c for c in _campaign_customers if c.get("status") == "pending"])
    return {
        "completed": completed,
        "committed": committed,
        "noAnswer": no_answer,
        "escalated": escalated,
        "pending": pending,
        "total": total,
    }


@router.post("/outbound/state")
async def set_outbound_state(request: Request):
    body = await request.json()
    state = body.get("state")
    if state:
        _campaign_state["state"] = state
        return {"status": "ok"}
    return {"error": "state is required"}


@router.post("/outbound/customers/upload")
async def upload_outbound_customers(file: UploadFile = FastAPIFile(...)):
    global _campaign_id_counter
    content = await file.read()
    text = content.decode("utf-8")
    reader = csv.DictReader(io.StringIO(text))
    imported = []
    for row in reader:
        _campaign_id_counter += 1
        cust = {
            "id": str(_campaign_id_counter),
            "name": row.get("name") or row.get("customer_name") or "",
            "phone": row.get("phone") or row.get("customer_phone") or "",
            "amountDue": float(row.get("amountDue") or row.get("amount_due") or 0),
            "dpdBucket": row.get("dpd_bucket") or row.get("dpdBucket") or "0-30",
            "status": row.get("status") or "pending",
            "attempts": int(row.get("attempts") or 0),
        }
        _campaign_customers.append(cust)
        imported.append(cust)
    return imported


# ─── Settings ────────────────────────────────────────────────────────────

@router.get("/settings")
async def get_settings():
    return _app_settings


@router.put("/settings")
async def put_settings(request: Request):
    body = await request.json()
    _app_settings.update(body)
    return _app_settings


@router.get("/system/health")
async def system_health():
    active_calls = len(_active_call_sid_map)
    return {"status": "healthy", "activeCalls": active_calls, "avgLatency": 120}


# ─── Post-Call Summary Generation ────────────────────────────────────────

async def generate_call_summary(transcript: str, phone_number: str, session_id: str):
    """Generate a concise call summary using Azure OpenAI Realtime WebSocket API.
    The realtime deployment does not support REST chat/completions, so we use
    a quick WebSocket session to generate the summary."""
    if not transcript or not transcript.strip():
        logger.warning(f"[summary] Empty transcript for session {session_id}, skipping summary")
        return None

    logger.info(f"[summary] Generating summary for session {session_id}, transcript length={len(transcript)} chars")

    # Build the Realtime WebSocket URL
    base = AZURE_ENDPOINT.replace("https://", "wss://")
    ws_url = (
        f"{base}/openai/realtime"
        f"?api-version={AZURE_API_VERSION}"
        f"&deployment={AZURE_DEPLOYMENT}"
    )

    try:
        import aiohttp as _aiohttp
        async with _aiohttp.ClientSession() as ws_session:
            async with ws_session.ws_connect(
                ws_url,
                headers={"api-key": AZURE_API_KEY},
                timeout=15,
            ) as ws:
                # 1. Configure the session for text-only summary
                await ws.send_json({
                    "type": "session.update",
                    "session": {
                        "modalities": ["text"],
                        "instructions": "You are a concise call summarizer. Summarize the call transcript in 2-3 English sentences. Include: sentiment, key outcome, and next steps if any. Output ONLY the summary, nothing else.",
                        "temperature": 0.6,
                        "max_response_output_tokens": 150,
                    }
                })

                # 2. Send the transcript as a user message and request a response
                trimmed = transcript[:1500]
                await ws.send_json({
                    "type": "conversation.item.create",
                    "item": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": f"Summarize this call:\n{trimmed}"}]
                    }
                })
                await ws.send_json({"type": "response.create"})

                # 3. Collect the text response
                summary_parts = []
                deadline = asyncio.get_event_loop().time() + 12  # 12s timeout

                async for msg in ws:
                    if asyncio.get_event_loop().time() > deadline:
                        logger.warning(f"[summary] WebSocket timeout for {session_id}")
                        break
                    if msg.type == _aiohttp.WSMsgType.TEXT:
                        import json as _json
                        data = _json.loads(msg.data)
                        t = data.get("type", "")
                        if t == "response.text.delta":
                            summary_parts.append(data.get("delta", ""))
                        elif t == "response.text.done":
                            final = data.get("text", "")
                            if final:
                                summary_parts = [final]
                            break
                        elif t == "response.done":
                            break
                        elif t == "error":
                            logger.error(f"[summary] Realtime error: {data}")
                            break
                    elif msg.type in (_aiohttp.WSMsgType.CLOSED, _aiohttp.WSMsgType.ERROR):
                        break

                summary = "".join(summary_parts).strip()
                if summary:
                    summary = summary[:500].strip()
                    if len(summary) == 500 and " " in summary:
                        summary = summary.rsplit(" ", 1)[0]
                    logger.info(f"[summary] Generated realtime summary for {session_id} ({len(summary)} chars): {summary[:80]}")
                    return summary
                else:
                    logger.warning(f"[summary] Realtime returned empty summary for {session_id}")

    except Exception as e:
        logger.error(f"[summary] Failed to generate call summary via Realtime WS: {e}")

    # Fallback: extract a brief summary from the raw transcript
    logger.info(f"[summary] Using transcript fallback for {session_id}")
    lines = [l.strip() for l in transcript.strip().split("\n") if l.strip()]
    if lines:
        summary_lines = lines[:4]
        fallback = " | ".join(summary_lines)
        if len(fallback) > 200:
            fallback = fallback[:197] + "..."
        return fallback

    return None


async def extract_lead_qualification_post_call(transcript: str, phone_number: str, session_id: str, db_store):
    """Extract lead qualification details from a call transcript using Azure OpenAI Realtime WebSocket,
    and save them asynchronously to Postgres (feedback_agent_logs table)."""
    if not transcript or not transcript.strip() or not db_store:
        return

    system_prompt = (
        "You are an AI data extractor. Analyze the conversation transcript between an agent (Maya) and a customer. "
        "Extract the lead qualification details into a valid JSON object. Do not include markdown formatting or backticks, just raw JSON:\n"
        "{\n"
        '  "lead_name": "string (full name or Unknown)",\n'
        '  "interest_status": "interested" | "not_interested" | "wrong_number" | "junk",\n'
        '  "qualification_stage": "hot" | "warm" | "junk",\n'
        '  "budget_range": "string (e.g. 1 crore or empty)",\n'
        '  "purchase_timeline": "string (e.g. within 30 days or empty)",\n'
        '  "funding_mode": "loan" | "cash" | "unspecified",\n'
        '  "site_visit_date": "string (e.g. tomorrow afternoon, Saturday morning, or empty)",\n'
        '  "objection_reason": "string (reason for rejection/disinterest or empty)",\n'
        '  "callback_time": "string (rescheduled callback time if requested or empty)",\n'
        '  "call_summary": "string (concise summary of the discussion)"\n'
        "}"
    )

    base = AZURE_ENDPOINT.replace("https://", "wss://")
    ws_url = f"{base}/openai/realtime?api-version={AZURE_API_VERSION}&deployment={AZURE_DEPLOYMENT}"

    try:
        import aiohttp as _aiohttp
        async with _aiohttp.ClientSession() as ws_session:
            async with ws_session.ws_connect(ws_url, headers={"api-key": AZURE_API_KEY}, timeout=15) as ws:
                await ws.send_json({
                    "type": "session.update",
                    "session": {
                        "modalities": ["text"],
                        "instructions": system_prompt,
                        "temperature": 0.6,
                        "max_response_output_tokens": 1000,
                    }
                })

                await ws.send_json({
                    "type": "conversation.item.create",
                    "item": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": f"Transcript:\n{transcript[:3000]}"}]
                    }
                })
                await ws.send_json({"type": "response.create"})

                parts = []
                deadline = asyncio.get_event_loop().time() + 15

                async for msg in ws:
                    if asyncio.get_event_loop().time() > deadline:
                        break
                    if msg.type == _aiohttp.WSMsgType.TEXT:
                        import json as _json
                        data = _json.loads(msg.data)
                        t = data.get("type", "")
                        if t == "response.text.delta":
                            parts.append(data.get("delta", ""))
                        elif t == "response.text.done":
                            final = data.get("text", "")
                            if final:
                                parts = [final]
                            break
                        elif t == "response.done":
                            break
                        elif t == "error":
                            logger.error(f"[post-call-log] Realtime error: {data}")
                            break
                    elif msg.type in (_aiohttp.WSMsgType.CLOSED, _aiohttp.WSMsgType.ERROR):
                        break

                content = "".join(parts).strip()
                if content.startswith("```"):
                    content = content.split("```", 2)[1]
                    if content.startswith("json"):
                        content = content[4:]
                content = content.strip()

                # Robust JSON extraction to handle unbraced or truncated LLM content
                args = {}
                try:
                    args = json.loads(content)
                except Exception as json_err:
                    # Attempt manual regex-based extraction of key-value pairs
                    logger.warning(f"[post-call-log] Direct JSON parsing failed ({json_err}). Attempting robust regex extraction...")
                    repaired = content.strip()
                    if not repaired.startswith("{"):
                        repaired = "{" + repaired
                    if not repaired.endswith("}"):
                        repaired = repaired + "}"
                    
                    try:
                        args = json.loads(repaired)
                    except Exception:
                        # Full regex fallback for severely broken/truncated JSON
                        try:
                            # 1. Match complete key-value strings or raw values
                            matches = re.findall(r'"([a-zA-Z0-9_]+)"\s*:\s*("(?:[^"\\]|\\.)*"|\d+|true|false|null)', repaired)
                            for key, val in matches:
                                if val.startswith('"') and val.endswith('"'):
                                    val = val[1:-1]
                                args[key] = val
                            
                            # 2. Match any truncated partial string value at the end of the text
                            partial_match = re.search(r'"([a-zA-Z0-9_]+)"\s*:\s*"([^"]*)$', content.strip())
                            if partial_match:
                                args[partial_match.group(1)] = partial_match.group(2)
                        except Exception as regex_err:
                            logger.error(f"[post-call-log] Regex fallback also failed: {regex_err}")

                try:
                    if not args:
                        raise ValueError("No data could be parsed/extracted from response content.")
                        
                    lead_name = args.get("lead_name", "Unknown")
                    interest_status = args.get("interest_status", "not_interested")
                    qualification_stage = args.get("qualification_stage", "junk")
                    budget_range = args.get("budget_range", "")
                    purchase_timeline = args.get("purchase_timeline", "")
                    funding_mode = args.get("funding_mode", "unspecified")
                    site_visit_date = args.get("site_visit_date", "")
                    objection_reason = args.get("objection_reason", "")
                    callback_time = args.get("callback_time", "")
                    call_summary = args.get("call_summary", "")

                    await db_store.update_feedback_agent_log(
                        session_id=session_id,
                        product_review=f"Interest: {interest_status}, Budget: {budget_range}, Timeline: {purchase_timeline}, Funding: {funding_mode}",
                        satisfaction_level="positive" if interest_status == "interested" else "negative",
                        overall_experience=f"Site visit: {site_visit_date}, Objection: {objection_reason}, Callback: {callback_time}",
                        call_summary=call_summary,
                        escalation_required=(qualification_stage == "hot"),
                    )
                    logger.info(
                        f"[post-call-log] Successfully extracted and saved lead info for {session_id}:\n"
                        f"  - Lead Name: {lead_name}\n"
                        f"  - Interest Status: {interest_status} (Stage: {qualification_stage})\n"
                        f"  - Budget Range: {budget_range}\n"
                        f"  - Site Visit Date: {site_visit_date}\n"
                        f"  - Summary: {call_summary}"
                    )
                except Exception as parse_err:
                    logger.error(f"[post-call-log] JSON parse failed: {parse_err}. Raw content: {content}")
    except Exception as e:
        logger.error(f"[post-call-log] Extraction failed: {e}")



def pcm_to_wav(pcm_data: bytes, sample_rate: int = 24000, channels: int = 1, sampwidth: int = 2) -> bytes:
    import wave
    import io
    wav_buf = io.BytesIO()
    with wave.open(wav_buf, "wb") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(sampwidth)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm_data)
    return wav_buf.getvalue()

@router.post("/api/transcribe")
async def transcribe_audio_api(request: Request):
    """
    Transcribe a segment of user audio using Azure OpenAI Whisper.
    Receives JSON: { "audio_base64": "...", "sample_rate": 24000 }
    """
    try:
        body = await request.json()
        audio_base64 = body.get("audio_base64", "")
        sample_rate = body.get("sample_rate", 24000)
        
        if not audio_base64:
            return {"text": ""}
            
        pcm_data = b64.b64decode(audio_base64)
        
        endpoint = os.getenv("AZURE_OPENAI_WHISPER_ENDPOINT", "")
        api_key = os.getenv("AZURE_OPENAI_WHISPER_API_KEY", "")
        
        if not endpoint or not api_key:
            logger.error("[transcribe] Whisper endpoint or API key not configured.")
            return {"error": "Whisper credentials not configured", "text": ""}
            
        wav_data = pcm_to_wav(pcm_data, sample_rate=sample_rate)
        
        form = aiohttp.FormData()
        form.add_field("file", wav_data, filename="audio.wav", content_type="audio/wav")
        
        async with aiohttp.ClientSession() as session:
            async with session.post(endpoint, headers={"api-key": api_key}, data=form) as resp:
                if resp.status == 200:
                    resp_json = await resp.json()
                    text = resp_json.get("text", "")
                    return {"text": text or ""}
                else:
                    err_msg = await resp.text()
                    logger.error(f"[transcribe] Whisper API failed ({resp.status}): {err_msg}")
                    return {"error": f"Whisper HTTP {resp.status}", "text": ""}
                    
    except Exception as e:
        logger.error(f"[api/transcribe] Transcription endpoint failed: {e}")
        return {"error": str(e), "text": ""}

