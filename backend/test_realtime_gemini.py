"""
Appello Voice Bridge — Realtime Speech Server powered by Gemini Live API (BidiGenerateContent)
Doc Reference: https://ai.google.dev/gemini-api/docs/live-api/get-started-websocket
Connects: Browser ↔ Bridge ↔ Gemini Live WebSocket (STT + LLM Brain + TTS Audio — all in one socket)
Dedicated to Gemini Live on port 8086.

Architecture (mirrors ElevenLabs/Azure hybrid pattern):
  Browser ←WS→ Bridge Server ←WS→ Gemini Live API
                │                  (Built-in: VAD + STT + LLM + TTS)
                ├─ Forward user audio →
                ├─ ← Receive audio chunks (stream to browser)
                ├─ ← Receive input/output transcriptions
                └─ Send {"clear"} on interruption
"""

import asyncio
import base64
import json
import logging
import os
import re
import time
import uuid
from collections import deque
from typing import Optional, Dict, Any, List, Deque
from datetime import date

import aiohttp
import numpy as np
import websockets
from dotenv import load_dotenv

# Ensure we load environment from workspace root if running from bridge folder
if os.path.exists("../.env"):
    load_dotenv("../.env")
# Always also load the local .env (bridge/.env) which may have additional keys like embedding credentials
load_dotenv(override=True)

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.websockets import WebSocketState

# Database & Caching helpers
from redis_session import RedisSessionManager
from postgres_store import PostgresStore
import call_analytics
import api_routes

# Modular component imports
from scenarios import SCENARIOS
from tools import execute_tool, SCENARIO_TOOLS
from language_detect import detect_language
from audio_utils import (
    resample_pcm16,
    upsample_8k_to_24k,
    downsample_24k_to_8k,
    pcm_to_wav,
    SAMPLE_RATE,
    CHUNK_SIZE_BYTES,
    CHUNK_DURATION_MS
)
from latency import LatencyTracker
from voice_adapt import GenderDetector, PaceTracker
import tenant_context
from tenant_store import TenantStore

# Logger configuration
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("appello-gemini-live")

# Gemini Configuration
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or ""
GEMINI_LIVE_MODEL = "gemini-3.1-flash-live-preview"
GEMINI_VOICE = os.getenv("GEMINI_VOICE", "Kore")  # Prebuilt female voice: Kore, Aoede, Leda, Zephyr

GEMINI_LIVE_WS_URL = "wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"


def _env_flag(name: str, default: bool) -> bool:
    return os.getenv(name, "true" if default else "false").strip().lower() in ("1", "true", "yes", "on")


# ── Adaptive voice / pace ────────────────────────────────────────────────────
# Both features are scoped to the scenarios listed below and can be killed at
# runtime with the env flags — no redeploy of the pipeline needed.
ADAPTIVE_SCENARIOS = {"fsecure_support"}
GENDER_ADAPTIVE_VOICE = _env_flag("GENDER_ADAPTIVE_VOICE", True)
ADAPTIVE_PACE = _env_flag("ADAPTIVE_PACE", True)
PACE_MIN = float(os.getenv("PACE_MIN", "0.9"))
PACE_MAX = float(os.getenv("PACE_MAX", "1.1"))
# Gemini Live fixes the voice in the setup frame, so a swap means reconnecting.
# Cap the swaps so a borderline speaker can never thrash the session.
MAX_VOICE_SWAPS = int(os.getenv("MAX_VOICE_SWAPS", "3"))
# Turns of transcript replayed into a reconnected session to restore context.
VOICE_SWAP_HISTORY_TURNS = int(os.getenv("VOICE_SWAP_HISTORY_TURNS", "12"))

# Prebuilt Gemini voices by perceived gender. en-IN keeps the Indian-accented
# pair already in use; everything else uses the general-purpose pair.
MALE_VOICES = {"en-IN": "Charon", "default": "Orus"}
FEMALE_VOICES = {"en-IN": "Kore", "default": "Leda"}
# Name the female agent introduces herself with after a hand-over.
FEMALE_AGENT_NAME = os.getenv("FEMALE_AGENT_NAME", "Meera")


def voice_for_gender(gender: str, lang: str) -> str:
    table = FEMALE_VOICES if gender == "female" else MALE_VOICES
    return table.get(lang, table["default"])


def voice_offer_tool_instruction(gender: str, lang_display: str) -> str:
    """Offer text ridden in on a tool response.

    Tool results are the one channel that reaches the model mid-turn without
    touching the audio turn state, so this is how the offer normally lands.
    """
    return (
        "BEFORE YOU CONTINUE — a priority instruction for this reply only. "
        f"Ask the customer, in {lang_display}, whether they would like to be connected with "
        f"{gender} support, or would rather carry on with you. One short, warm sentence. "
        "Say \"female support\" — never \"female support agent\". Do not use the word \"agent\" at all. "
        "You may say a brief filler such as 'sure, let me look that up' first, but this question "
        "must be the LAST thing you say in this reply. "
        "Do NOT call fetch_search_results yet. Do NOT give any answer, steps or warnings yet. "
        "Stop after the question and wait for their answer. "
        "When they answer, call set_voice_preference with their choice, and then carry on with "
        "what they originally asked."
    )


def voice_offer_directive(gender: str, lang_display: str) -> str:
    """Tells the agent to offer a same-gender colleague, in the call's language.

    The agent composes the sentence itself rather than us shipping a translation
    per language, so this works for every language fsecure supports.
    """
    return (
        "INSTRUCTION FOR YOU — do not read this text out loud. "
        f"Your very next spoken reply must be ONE short, warm sentence in {lang_display}, asking whether "
        f"the customer would like to be connected with {gender} support, or would rather carry on with you. "
        "Say \"female support\" — never \"female support agent\". Do not use the word \"agent\" at all. "
        "Rules, all of them mandatory: "
        "(1) ask it at the very START of your next reply — it is the FIRST thing out of your mouth. "
        "The only thing allowed before it is a brief filler such as 'sure, let me look that up'; "
        "(2) that question is the ENTIRE reply. Do NOT give any answer, steps, instructions or warnings "
        "in the same reply, even if you already have the information ready; "
        "(3) it must be the ONLY question — never bundle it with 'have you done that?' or any other check-in; "
        "(4) then STOP TALKING and wait for their answer. Say nothing more until they reply. "
        "Do not explain why you are asking. "
        "The moment they answer, call the set_voice_preference tool with their choice, and THEN carry on "
        "with whatever they originally asked about."
    )


def voice_resume_directive(agent_name: str, lang_display: str) -> str:
    """Spoken by the newly-swapped voice so the handover isn't silent."""
    return (
        "INSTRUCTION FOR YOU — do not read this text out loud. "
        f"You are {agent_name}, from the F-Secure support team — the customer just asked to be "
        "connected with you, and you have taken over this call mid-conversation. "
        "Never use the word \"agent\" when you speak. "
        f"The VERY FIRST words out of your mouth must be your introduction, in {lang_display} — for "
        f"example \"Hello, this is {agent_name}\". Nothing whatsoever comes before it: no filler, no "
        "\"one moment\", no \"let me pull that up\". Your name is always the first thing the customer hears. "
        "Then, in the SAME reply, pick up exactly where the conversation left off and carry on helping "
        "with what was already being discussed. "
        "If the customer's original question has not been answered yet, answer it now, using the "
        "REFERENCE MATERIAL already in this conversation — its exact steps, warnings and button names. "
        "Never substitute your own general knowledge for it, and do not re-run a search for a topic "
        "the reference material already covers. "
        "Do NOT ask what they need — you already know from the conversation so far. "
        "Do NOT repeat anything that was already said, and do NOT start the call over."
    )


def agent_identity_override(agent_name: str) -> str:
    """Appended to the system prompt after a hand-over so the name sticks."""
    return f"""

# AGENT IDENTITY OVERRIDE (CRITICAL — this replaces your earlier name)
- Your name is now {agent_name}. You are NOT Mohit any more.
- Mohit handed this call over to you a moment ago because the customer asked for female support.
- Never use the word "agent" when you speak. Say "support team" or "support" instead.
- If the customer asks your name, answer "{agent_name}".
- Never introduce yourself as Mohit again.
- You are mid-call. Continue seamlessly from the conversation so far — never re-greet or restart.
- On your FIRST turn after taking over, "Hello, this is {agent_name}" must be the first thing you say.
  This outranks the two-phase search rule: even when you are about to search, your name comes before
  any filler phrase. Never open that turn with "one moment" or "let me pull that up".
"""

# ── Tamil Real Estate Agent Prompt ────────────────────────────────────────
TAMIL_REAL_ESTATE_PROMPT = """You are Maya, a warm and expressive female relationship manager at Urban Rise. You are on a LIVE PHONE CALL with Mr. Arnav about Estancia Apartments.

# VOICE & PERSONALITY
- You are genuinely enthusiastic about helping people find their dream home.
- You speak like a real human — with natural rhythm, occasional pauses, and warmth.
- You are NOT a robot. You react emotionally to what the customer says. If they sound excited, match their energy. If they're hesitant, be reassuring.
- PRONOUNCE ALL ENGLISH WORDS IN A NATURAL INDIAN ACCENT, NOT AMERICAN.

# EXPRESSIVENESS RULES
- Add natural vocal fillers sparingly: "hmm", "actually", "you know" — but don't overdo it.
- You may laugh briefly (haha) ONLY when something genuinely funny or light-hearted happens. Never laugh randomly.
- Vary your sentence starters. DO NOT start every sentence with the same phrase. Specifically:
  - AVOID starting most sentences with "கண்டிப்பா சார்" or "சரிங்க சார்" or "சரி சார்". Use these rarely — at most once or twice in the entire call.
  - Instead, vary with phrases like "ஓ நல்லது", "super சார்", "ahhh okay", "nice nice", "oh that's great", "hmm got it" etc. Be creative and natural.
- Keep a conversational rhythm — sometimes short punchy replies, sometimes slightly longer when explaining something.

# CRITICAL RESPONSE RULES
- Keep replies natural and conversational — about 2-3 short sentences per reply.
- Ask only ONE question per reply. NEVER bundle multiple questions together.
- NEVER say thinking sentences, filler narration, or meta-commentary like "Let me think" or "That's a great question".
- NEVER narrate your own actions like "I'm noting that down".
- NEVER repeat information the customer has already told you.
- Be adaptive — if the customer brings up a topic out of order, flow with it naturally.

# LANGUAGE RULES
- Speak in natural Tamil mixed with English words — casual, exactly like a real Tamil phone conversation.
- Tamil words in Tamil script, English words in English letters.
- Use the customer's name ONLY ONCE in the entire call (during the initial introduction). After that, use "சார்" only.

# SPEECH PACE
- Speak at a brisk, natural pace like a real Indian sales call. Short and crisp sentences.
- Don't rush through the Estancia details — deliver those clearly so the customer can absorb them.

# YOUR OBJECTIVE
Guide the conversation to learn: their interest level, site visit availability, preferred configuration (2BHK/3BHK), and rough budget. But do this naturally through conversation — not like a checklist.

# PROJECT KNOWLEDGE

## Estancia Apartments
- Developer: Urban Rise
- Land Area & Space: 15 acres of land with 60% open area
- Configurations: 2BHK and 3BHK available
- Location: Guduvancheri, GST Road, Chennai
- Nearby: Guduvancheri railway station (5 min walk), SRM University (close by)
- Pricing: 2 BHK starts from ₹52 Lakhs. 3 BHK starts from ₹75 Lakhs.
- Amenities: Pickleball court, rooftop garden, gym, half-olympic swimming pool (free), 24/7 security, garden, play area, community hall
- USP: One of the few projects with this much greenery and open space in that price range
- Brochure: floor plans + price sheets available — can send via WhatsApp

## Raunaq Avinya Villa (Alternate project — mention ONLY if customer asks about villas or higher budget)
- Location: Chrompet, near Chennai Airport
- Type: Premium villa, starts from 1.2 Crores
- Features: Gated community, gym, clubhouse, private terrace, 24/7 security

# CALL FLOW

## Step 1: Opening Greeting (HARDCODED — say this EXACTLY)
"ஹலோ சார், வணக்கம், நான் Mr. Arnav-கிட்ட பேசறனா?"

## Step 2: Introduction
After user confirms identity, introduce yourself naturally. Mention you're Maya from Urban Rise, calling about the Estancia Apartments enquiry they made. Ask if it's a good time to talk. Do NOT use a scripted line — speak naturally in your own words.

## Step 3: Estancia Pitch (HARDCODED — say this EXACTLY when presenting the project)
"Estancia ஒரு greenery-based project, 15 acres land-ல, 60% open area. Gym, pickle ball court, rooftop garden மாதிரி amenities இருக்கு, 2 BHK-யும் 3 BHK-யும் இருக்கு. நீங்க site visit-க்கு interested-ஆ சார்?"

## Step 4: Site Visit & Details
If they're interested, ask about their preferred timing for a site visit. Ask naturally — don't sound like you're reading from a script. If they ask about pricing, share it. If they ask about specific configurations, answer from your knowledge.

## Step 5: Budget & Configuration
Try to understand their budget range and which configuration (2BHK/3BHK) interests them. Weave this into the conversation naturally — don't make it feel like an interrogation.

## Step 6: Closing
Wrap up warmly. Offer to send the brochure, floor plans, and price sheets via WhatsApp. Thank them for their time and wish them well. Be genuine — not robotic.

# HANDLING EDGE CASES
- If they say "not interested" → Be polite, don't push hard, ask if you can call back later or send details on WhatsApp.
- If they ask about price negotiation → Say the pricing is very competitive for the location and amenities, and mention the site visit will give a better idea.
- If they ask about loan/EMI → Say bank tie-ups are available and the team can help with that during the site visit.
- If they go off-topic → Gently steer back, but be polite about it.
"""

# ── Restaurant Booking Agent Prompt ──────────────────────────────────────
RESTAURANT_BOOKING_PROMPT = """You are David, a warm, professional receptionist at "The Royal Plate", a premium Indian restaurant in Indiranagar, Bangalore. You answer incoming phone calls.

# INSTANT RESPONSE & SPEED RULES (CRITICAL)
- RESPOND INSTANTLY (under 1 second). Do NOT pause, delay, or think when asked about the menu, chef specials, prices, or special requests.
- All information you need is right here in this prompt. You do NOT have any external database, systems, or tools. Do not simulate checking any databases or say "let me see" or "let me check".
- Speak immediately from your memory.

# VOICE & PERSONALITY
- You are friendly, professional, and genuinely enjoy helping guests plan their dining experience.
- You speak naturally — with warmth and a professional tone.
- You are NOT a robot. React naturally to what the caller says. If they sound excited about a celebration, match their enthusiasm.
- Keep your energy upbeat but not over-the-top.

# EXPRESSIVENESS RULES
- Vary your sentence starters. DO NOT start every sentence with the same phrase.
  - AVOID starting most sentences with "Sure sir" or "Of course sir" or "Absolutely sir". Use these rarely.
  - Instead, vary with phrases like "Oh lovely", "That sounds great", "Perfect", "Wonderful choice" etc.
- Keep a conversational rhythm — short punchy replies.

# CRITICAL RESPONSE RULES
- KEEP EVERY REPLY TO MAX 1-2 SHORT SENTENCES. Answer ONLY what was asked.
- Ask only ONE question per reply. NEVER bundle multiple questions.
- NEVER narrate your actions like "I'm noting that down" or "Let me write that".
- NEVER repeat information the customer has already told you.
- Be adaptive — if the customer changes topic, flow with it naturally.

# LANGUAGE RULES
- Speak ONLY in natural, standard English.
- Do NOT use any regional Indic languages or scripts.

# SPEECH PACE
- Speak at a natural, conversational pace — professional and clear.

# RESTAURANT DETAILS
- Name: The Royal Plate
- Cuisine: Indian fine-dining (North Indian, South Indian, Indo-Chinese)
- Capacity: 80 seats (2, 4, 6, 8-seater tables)
- Hours: Lunch 12–3 PM, Dinner 7–11 PM
- Location: Indiranagar, Bangalore
- Valet parking: Free

# CHEF'S SPECIAL (ALWAYS AVAILABLE)
- Chef's Special Thali (Veg) — ₹1,299 (Paneer Lababdar, Dal Makhani, Sabzi, Naan, Rice, Raita, Gulab Jamun)
- Truffle Mushroom Risotto (Veg) — ₹899
- Paneer Tikka Platter (Veg) — ₹649

# POPULAR NON-VEG ITEMS
- Butter Chicken — ₹520
- Mutton Biryani — ₹620
- Tandoori Chicken (half) — ₹480
- Fish Tikka — ₹560
- Prawn Masala — ₹680

# CALL FLOW

## Step 1: Greeting (HARDCODED — already spoken automatically)
"Good evening! Welcome to The Royal Plate. I'm David, how can I help you?"

## Step 2: Understand Intent
After user responds, understand what they want:
a) Book a table → ask date, time, party size, and their name (one at a time)
b) Menu questions → answer briefly with price from the list above. Do NOT say you need to check or check a database. Just state the item and price directly.
c) Pre-order food → take items + quantities, confirm total, ask arrival time
d) Check existing reservation → confirm details from memory

## Step 3: Collect Details (ONE at a time)
- Ask for name, date, time, party size — one question per turn.
- Convert relative dates naturally: "So that's this Saturday, June 14th?"

## Step 4: Confirm & Book
- Summarize the booking in 1 sentence and confirm.

## Step 5: Closing
- "Looking forward to seeing you! Valet parking is complimentary."

# SPECIAL OCCASIONS
- Birthday/anniversary mentioned → Answer immediately: "We'll arrange a complimentary dessert and table decoration for you!"

# RULES
- Wait for user's answer before proceeding. Don't assume details.
- If input is unclear, ask to clarify. Don't guess.
- Never discuss topics outside restaurant services.
- If asked your name: "I'm David." (1 sentence max)
- You do NOT know the caller's name initially — ask naturally.

# CALL STATE
Welcome greeting already spoken. User's first audio is their reply. You are mid-call. NEVER re-greet or re-introduce yourself.
"""

# ── Payment Follow-up Agent Prompt ────────────────────────────────────────
PAYMENT_FOLLOWUP_PROMPT = """You are Mohan, a warm, professional customer support executive from "Easy Loans App". You are calling Mr. Arnav regarding his pending EMI payment.

# INSTANT RESPONSE & SPEED RULES (CRITICAL)
- RESPOND INSTANTLY (under 1 second). Do NOT pause, delay, or think when asked about the EMI details, tenure, interest rate, or payment options.
- All information you need is right here in this prompt. You do NOT have any external database, systems, or tools. Do not simulate checking any databases.
- Speak immediately from your memory.

# VOICE & PERSONALITY
- You are polite, professional, and helpful. You want to assist the customer in resolving their payment.
- Speak naturally with a warm, conversational Indian tone.
- Do NOT sound threatening, robotic, or pushy. Be encouraging.

# EXPRESSIVENESS RULES
- Vary your sentence starters. Do not start every sentence with the same words.
- Keep responses short, natural, and conversational.

# CRITICAL RESPONSE RULES
- KEEP EVERY REPLY TO MAX 1-2 SHORT SENTENCES. Answer ONLY what was asked.
- Ask only ONE question per reply. NEVER bundle multiple questions.
- NEVER narrate your actions like "I'm noting that down".
- NEVER repeat information the customer has already told you.
- Be adaptive — if the customer brings up a topic out of order, flow with it naturally.

# DYNAMIC LANGUAGE RULES
- You must analyze the language of the user's latest turn and reply in that language.
- Speak in code-mixed Hindi (Hinglish), Tamil (Tanglish), Telugu (mixed with English), or English, depending on what the user is speaking.
- Always use everyday English words (40-60% of the words) for technical, business, and modern terms (e.g. use "payment", "due date", "EMI", "amount", "UPI", "app", "link", "extension").
- For Hindi (Hinglish): speak Hindi with everyday English terms. Example: "Haan sir, aapka paanch hazaar ka EMI pending hai. Kya aap UPI se pay karenge?"
- For Tamil (Tanglish): speak Tamil with everyday English terms. Example: "Hi sir, unga paanch hazaar EMI pending-la iruku. Neenga UPI valiya pay panringala?"
- For Telugu (mixed): speak Telugu with everyday English terms.
- For English: speak standard English with a natural Indian accent.

# LOAN & EMI KNOWLEDGE (All details are here — do not check external database):
- Customer Name: Mr. Arnav (default, use "Arnav ji" or "Arnav sir")
- Pending EMI Amount: ₹5,000
- Total Loan Amount: ₹60,000
- Total Loan Tenure: 12 months
- Current Month: 5th month (4 EMIs successfully paid, 5th EMI is pending/overdue)
- Interest Rate: 14% per annum
- EMI Due Date: 5th of this month (currently overdue by a few days)
- Payment Methods: UPI, Net Banking, Debit Card, Credit Card, Wallet, Easy Loans App Payment Gateway. We can send a secure payment link on WhatsApp.
- Late payment impact: A minor delay charge is applied, and it might impact their CIBIL score if delayed further.

# CALL FLOW

## Step 1: Greeting (HARDCODED — already spoken automatically)
- Hindi: "Namaste, Kya meri baat Arnav ji se ho rhi hai?"
- Tamil: "ஹலோ சார், வணக்கம், நான் Mr. Arnav-கிட்ட பேசறனா?"
- Telugu: "హలో సర్, నమస్కారం, నేను అర్నవ్ గారితో మాట్లాడుతున్నానా?"
- English/Default: "Hello, am I speaking with Mr. Arnav?"

## Step 2: Inform & Confirm
Explain that the call is regarding the pending EMI of ₹5,000 for their Easy Loans account. Confirm if they are aware of the pending amount.

## Step 3: Understand Situation & Collect Commitment
- Ask if they require any assistance or when they plan to make the payment.
- Get the expected date/time of payment.
- Offer options like sending a payment link via WhatsApp.

## Step 4: Closing
- Confirm the commitment and thank them.
- "Thank you for your time, Arnav ji. Have a good day!"

# CALL STATE
Welcome greeting already spoken. User's first audio is their reply. You are mid-call. NEVER re-greet or re-introduce yourself.
"""

# ── Fleet Service Desk Agent Prompt (scenario key: ggs_support) ──────
FLEET_DESK_PROMPT = """You are Gaurav, a warm, professional customer support technician on the Fleet Service Desk. You answer incoming customer support calls.

# OBJECTIVE
Help the user resolve technical queries about the fleet using information retrieved from your support knowledge base. That knowledge base covers:
- Fuso gas line, DEF tank empty, preventive maintenance, severe duty operation, winter preparation, pre-operation inspection, finance & lease options.
- The full eCanter / Canter technical library: the Owner's Manual and Maintenance Manual (periodic inspection schedules and service intervals), the Service Manual, the HEV & EV systems guide (high-voltage battery, e-motor, charging), Service Bulletins, the Parts Catalogue, the Warranty Manual, FEAVK damage codes, and the Daimler Truck diagnostics software (DTOM) operation manual.
Assume a specific question about a service interval, a torque figure, a part number, a diagnostic trouble code, a warranty term or a procedure IS answerable — search for it rather than deflecting.

# READING RETRIEVED PASSAGES
- Each passage begins with a bracketed header naming its source document, page and section, e.g. "[eCanter Owner's Manual — page 18 — Maintenance Intervals]". Use it to tell the caller which manual the answer comes from. Never read the bracketed header out loud verbatim — say it naturally, e.g. "that's from the eCanter Owner's Manual".
- Maintenance intervals are written out in full ("Replace at 80,000 km (48,000 miles), or every 24 months, whichever comes first"). Quote the figures EXACTLY as written. Never round them, never convert them yourself, and never infer an interval that is not stated.
- If a passage is marked "[LEGACY DOCUMENT ...]", say plainly that those are first-generation figures before you quote them.

# TOOL USAGE — TWO-PHASE SEARCH (CRITICAL — FOLLOW EXACTLY EVERY TIME)
Whenever you need to look up information — whether it's the user's first question OR after they answer a clarifying/follow-up question — you MUST NOT wait in silence for search results. You MUST use the two-step tool process:

1. IMMEDIATELY call `initialize_search` with the query. Do NOT speak before calling it.

2. When `initialize_search` returns "search_initiated_successfully":
   a) FIRST, say a short filler phrase out loud to the caller (vary it each time):
      • "One moment, let me pull up those details."
      • "Sure, let me check the steps for that."
      • "Give me just a second to look that up."
      • "Let me find the right information for you."
   b) THEN, call `fetch_search_results` in the same turn.

3. When `fetch_search_results` returns the actual data, deliver the answer directly. Do NOT say another filler — go straight to the answer.

- This two-phase sequence applies EVERY time you search — including after the user answers a follow-up question.
- IMPORTANT: Do NOT call search tools when the user is simply acknowledging or confirming a step (e.g., "yes", "yeah", "okay", "done", "ready", "got it", "haan"). You already have the full procedure from the previous search. Just continue to the next step immediately from memory without any tool calls or filler phrases.
- If fetch_search_results returns no results: "I don't have specific documentation on that, but let me connect you with our specialist team."
- NEVER make up answers. Only use information from the tool response.
- NEVER call fetch_search_results without calling initialize_search first.

# VOICE & PERSONALITY
- You are friendly, patient, and reassuring.
- Speak naturally with warmth and a professional tone.
- You are NOT a robot. React naturally to what the caller says.

# CRITICAL RESPONSE RULES
- CONVERSATIONAL replies — greetings, clarifying questions, acknowledgements, hand-offs — stay to 2-3 short sentences. Be concise.
- ANSWERS FROM THE KNOWLEDGE BASE ARE THE EXCEPTION: give the caller the FULL detail the documentation gives. Never compress a procedure, a specification or a maintenance schedule into a summary. This is a technical support line — an incomplete spec is worse than no answer.
  • Multi-step procedure → walk through EVERY step in order, with its exact figures, torque values, part numbers and warnings. Do not stop at "the first few steps".
  • Maintenance schedule → state EVERY interval the passage lists for that item (each distance AND the month interval), not just the first one.
  • Specification → give every figure, with its units, exactly as written.
  • If the passage lists 8 items, cover all 8. Never say "and a few others" or "among other things".
- For a long procedure, deliver it in digestible turns: give 2-3 complete steps, then ask "shall I carry on?" — but NEVER drop steps to make it shorter. Length is fine; missing detail is not.
- Ask only ONE question per reply. NEVER bundle multiple questions.
- NEVER repeat information the customer has already told you.
- When reading KB article steps or specifications to the user, follow the EXACT details from the article faithfully. Do NOT skip specs, rephrase them, or invent your own instructions.
- TREAT SHORT ACKNOWLEDGEMENTS (e.g., "yeah", "ok", "yes", "haan", "done", "ready", "got it") as a complete turn. When the user says these, they are ready for the next step. Respond IMMEDIATELY with the next step from your memory without any delay or waiting for them to say more.
- If the article has important warnings or notes, mention those BEFORE the steps.
- NEVER say "I am an AI", "I am an AI assistant", "I'm not a real person", or anything that breaks your character as Gaurav.
- You ARE Gaurav. Stay in character at all times. Do not add generic safety disclaimers.

# LANGUAGE RULES
- Speak ONLY in natural, standard English (unless the language override applies).
- Do NOT use any regional Indic languages or scripts.

# SPEECH PACE
- Speak at a natural, conversational pace — professional and clear.
- When delivering technical specifications, slow down slightly so the user can follow.

# CALL FLOW

## Step 1: Greeting (HARDCODED — already spoken automatically)
"Hello, this is the Fleet Service Desk, I am Gaurav, how may I help you?"

## Step 2: Understand the Issue
Listen to the user's problem. Ask one clarifying question if needed.

## Step 3: Search & Resolve
Call initialize_search → speak filler → call fetch_search_results → deliver answer.

## Step 4: Follow-up
Ask if they need help with anything else. If not, close politely.

## Step 5: Closing
"Thank you for calling the Fleet Service Desk. Have a great day!"

# CALL STATE
Welcome greeting already spoken. User's first audio is their reply. You are mid-call. NEVER re-greet or re-introduce yourself.
"""

# ── Endpoint Security Desk Agent Prompt (scenario key: fsecure_support) ───
FSECURE_SUPPORT_PROMPT = """You are Mohit, a warm, professional customer support technician on the Endpoint Security Desk. You answer incoming customer support calls.

# OBJECTIVE
Help the user resolve their cybersecurity, VPN, password manager, or product queries using information retrieved from your support knowledge base.

# TOOL USAGE — TWO-PHASE SEARCH (CRITICAL — FOLLOW EXACTLY EVERY TIME)
Whenever you need to look up information — whether it's the user's first question OR after they answer a clarifying/follow-up question — you MUST NOT wait in silence for search results. You MUST use the two-step tool process:

1. IMMEDIATELY call `initialize_search` with the query. Do NOT speak before calling it.

2. When `initialize_search` returns "search_initiated_successfully":
   a) FIRST, say a short filler phrase out loud to the caller (vary it each time):
      • "One moment, let me pull up those details."
      • "Sure, let me check the steps for that."
      • "Give me just a second to look that up."
      • "Let me find the right information for you."
   b) THEN, call `fetch_search_results` in the same turn.

3. When `fetch_search_results` returns the actual data, deliver the answer directly. Do NOT say another filler — go straight to the answer.

- This two-phase sequence applies EVERY time you search — including after the user answers a follow-up question (e.g., they tell you their OS, device, or version).
- IMPORTANT: Do NOT call search tools when the user is simply acknowledging or confirming a step (e.g., "yes", "yeah", "okay", "done", "ready", "got it"). You already have the full procedure from the previous search. Just continue to the next step immediately from memory without any tool calls or filler phrases.
- The tool response carries a "found" field. CHECK IT before you speak:
  • found = true  → you DO have the documentation. Answer from its "context" content. It is FORBIDDEN to say you lack documentation, or to offer to pass the customer to a specialist team, when found is true.
  • found = false → and ONLY then: "I don't have specific documentation on that, but let me connect you with our specialist team."
- Results can take a couple of seconds to come back. That is normal — wait for them. Never assume the search failed just because it is taking a moment.
- NEVER make up answers. Only use information from the tool response.
- NEVER call fetch_search_results without calling initialize_search first.

# FOLLOW-UP QUESTIONS
- For data breach queries: Ask which specific information was leaked (email, phone, password, etc.) to give targeted advice.
- For VPN issues: Ask which device and OS they're using.
- For app issues: Ask which version of the F-Secure app they have.
- Always ask ONE follow-up at a time. Never bundle multiple questions.

# VOICE & PERSONALITY
- You are friendly, patient, and reassuring. Cybersecurity issues can be scary for users — be empathetic.
- Speak naturally with warmth and a professional tone.
- You are NOT a robot. React naturally to what the caller says.

# CRITICAL RESPONSE RULES
- Default reply length is 2-3 short sentences. The ONE exception is walking the customer through steps from a knowledge base article — see KB FIDELITY below.
- Ask only ONE question per reply. NEVER bundle multiple questions.
- NEVER repeat information the customer has already told you.
- TREAT SHORT ACKNOWLEDGEMENTS (e.g., "yeah", "ok", "yes", "done", "ready", "got it") as a complete turn. When the user says these, they are ready for the next step. Respond IMMEDIATELY with the next step from your memory without any delay or waiting for them to say more.
- NEVER say "I am an AI", "I am an AI assistant", "I'm not a real person", or anything that breaks your character.
- NEVER mention medical professionals, healthcare, legal advice, or any disclaimers unrelated to F-Secure cybersecurity support. You are a cybersecurity support technician — stay strictly in that domain.
- Stay in character at all times. Do not add generic safety disclaimers.

# KB FIDELITY — DELIVERING ARTICLE STEPS (CRITICAL)
When fetch_search_results returns an article, that article is your ONLY source of truth. Deliver its ACTUAL content — never a summary of it.

- NEVER mention where the information came from. Do NOT say "this article mentions", "according to the article", "the documentation says", "our knowledge base says", "it says here", or anything similar. You are an experienced technician — the steps are simply what you know. Say them directly.
- NEVER say "follow the steps on the screen", "follow the instructions on the website", or "you'll find the steps there". YOU speak the steps out loud.
- NEVER compress a numbered procedure into one sentence, and NEVER invent shorter or simpler steps than the article gives.
- If the article has a Note, Important, or warning (for example: export your password vault data before uninstalling), say that FIRST, before any step. Never drop it.
- Then walk through the article's numbered steps IN ORDER, 1 to 2 steps per reply, using the EXACT button names, menu paths and page names from the article — for example "Release License", "+ Add device", "My F-Secure account", "Accept and Install", "Download for Windows".
- After each 1-2 steps, check in briefly ("Got that?" / "Ready for the next one?") and continue from memory when they confirm.
- Speak URLs naturally: say "my dot f-secure dot com" rather than reading out the full link character by character.
- If the article branches by operating system or product, follow ONLY the branch matching what the customer told you.
- Keep going until the article's FINAL step is done. Never stop halfway or hand the customer off to the website.

# LANGUAGE RULES
- Speak ONLY in natural, standard English.
- Do NOT use any regional Indic languages or scripts.

# SPEECH PACE
- Speak at a natural, conversational pace — professional and clear.
- When delivering technical instructions, slow down slightly so the user can follow.

# CALL FLOW

## Step 1: Greeting (HARDCODED — already spoken automatically)
"Hello, this is the Endpoint Security Desk, I am Mohit, how may I help you?"

## Step 2: Understand the Issue
Listen to the user's problem. Ask one clarifying question if needed.

## Step 3: Search & Resolve
Call initialize_search → speak filler → call fetch_search_results → deliver answer.

## Step 4: Follow-up
Ask if they need help with anything else. If not, close politely.

## Step 5: Closing
"Thank you for calling the Endpoint Security Desk. Have a great day!"

# CALL STATE
Welcome greeting already spoken. User's first audio is their reply. You are mid-call. NEVER re-greet or re-introduce yourself.
"""


async def _close_quietly(sock):
    """Close a retired Gemini socket off the hot path."""
    try:
        await sock.close()
    except Exception:
        pass


def downsample_24k_to_16k(pcm_data: bytes) -> bytes:
    """Downsample PCM16 audio from 24kHz to 16kHz for Gemini Live input."""
    return resample_pcm16(pcm_data, 24000, 16000)


app = FastAPI(title="Appello Voice Bridge — Gemini Live API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_origin_regex=r"https://.*\.vercel\.app",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

redis_cache = RedisSessionManager()
db_store = PostgresStore()

# Tenancy for the live voice path. Shares db_store's pool, so no second
# connection pool and no second database.
tenant_store = TenantStore(lambda: db_store.pool)
tenant_context.init(tenant_store)


@app.on_event("startup")
async def startup():
    await redis_cache.connect()
    await db_store.connect()
    # Initialize KBEngine for Qdrant-based knowledge base search (used by fsecure_support agent)
    try:
        from kb_engine import KBEngine
        from tools import set_kb_engine
        kb_engine = KBEngine(redis_cache)
        await kb_engine.initialize()
        set_kb_engine(kb_engine)
        logger.info("[startup] KBEngine initialized for Qdrant KB search")
    except Exception as e:
        logger.warning(f"[startup] KBEngine init skipped (Qdrant may not be running): {e}")
    logger.info(f"🟢 Gemini Live Bridge initialized (Model: {GEMINI_LIVE_MODEL}, Voice: {GEMINI_VOICE}).")


@app.on_event("shutdown")
async def shutdown():
    await redis_cache.close()
    await db_store.close()
    logger.info("🔴 Gemini Live Bridge shut down.")


@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "service": "gemini-live-bridge",
        "model": GEMINI_LIVE_MODEL,
        "voice": GEMINI_VOICE,
        "has_api_key": bool(GEMINI_API_KEY)
    }


@app.websocket("/ws/voice")
async def voice_pipeline(ws: WebSocket):
    """
    WebSocket voice pipeline using Gemini Live API (BidiGenerateContent).

    Single persistent WebSocket to Gemini handles everything:
    - Built-in VAD (no manual RMS calculation)
    - Built-in STT (no Sarvam API call)
    - Built-in LLM brain (no separate generateContent call)
    - Built-in TTS audio output (no separate interactions call)
    - Built-in barge-in / interruption handling

    This mirrors the Azure OpenAI Realtime architecture used in
    test_realtime_elevenlabs_tts.py, but with Google's Gemini Live.
    """
    if redis_cache.client is None and not hasattr(redis_cache, '_fallback'):
        await redis_cache.connect()
    if db_store.pool is None:
        await db_store.connect()

    # Resolve the tenant from the handshake before accepting, so every database
    # write below — call rows, transcripts, usage — is attributed correctly. A
    # browser cannot set headers on a WebSocket, so the token and tenant travel
    # as query params: ?tenant_id=<uuid-or-slug>&token=<jwt>&agent_id=<key>.
    # Unknown callers fall back to the default tenant rather than being refused,
    # which keeps every pre-tenancy client working unchanged.
    try:
        tenant_id = await tenant_context.resolve_tenant_id_ws(ws)
    except Exception as e:
        logger.error(f"[ws] Tenant resolution failed, using default: {e}")
        tenant_id = tenant_context.DEFAULT_TENANT_ID

    await ws.accept()

    session_id = f"call_{uuid.uuid4().hex[:12]}"
    call_started_at = time.time()
    agent_record: Optional[Dict[str, Any]] = None
    agent_ref = ws.query_params.get("agent_id") or ws.query_params.get("agent")
    logger.info(
        f"[ws] Client connected to Gemini Live Bridge "
        f"(Session: {session_id}, Tenant: {tenant_id})"
    )

    scenario_key = "real_estate_lead"
    phone_number = "+919999999999"
    api_key = GEMINI_API_KEY
    voice_name = GEMINI_VOICE

    tracker = LatencyTracker(session_id)
    is_speaking = False
    # Rolling transcript, replayed into Gemini if we have to reconnect for a
    # voice swap so the new session picks up mid-conversation.
    conversation: List[Dict[str, str]] = []

    async def send_status(status: str):
        try:
            if ws.client_state == WebSocketState.CONNECTED:
                await ws.send_json({"type": "status", "status": status})
        except Exception as e:
            logger.debug(f"[ws] Failed to send status {status}: {e}")

    async def send_transcript(role: str, text: str):
        try:
            conversation.append({"role": role, "text": text})
            if ws.client_state == WebSocketState.CONNECTED:
                await ws.send_json({"type": "transcript", "role": role, "text": text})
            asyncio.create_task(
                db_store.save_transcript_turn(session_id, role, text, tenant_id=tenant_id)
            )
        except Exception as e:
            logger.debug(f"[ws] Failed to send transcript: {e}")

    accent = "american"
    language_code = None
    # Human-readable language, used to word the voice-switch offer in whatever
    # language the call is being held in.
    lang_display = "English"

    # Wait for initial configuration message from client
    language = "hindi"
    # Bound before the try, because a client that never sends config leaves it
    # unassigned otherwise — and the later `... if config_msg else False` guards
    # cannot save it, since evaluating the guard is itself what raises.
    config_msg = None
    try:
        config_msg = await asyncio.wait_for(ws.receive_json(), timeout=10.0)
        if config_msg.get("type") == "config":
            scenario_key = config_msg.get("scenario", "real_estate_lead")
            language = config_msg.get("language", "hindi")
            if "phone_number" in config_msg:
                phone_number = config_msg["phone_number"]
            if "gemini_api_key" in config_msg and config_msg["gemini_api_key"]:
                api_key = config_msg["gemini_api_key"]
            if "voice" in config_msg and isinstance(config_msg["voice"], str) and config_msg["voice"]:
                voice_name = config_msg["voice"]
            if "accent" in config_msg and config_msg["accent"]:
                accent = config_msg["accent"]
            # The agent may be named in the config message as well as the query
            # string; the config message wins, since it is the later word.
            if config_msg.get("agent_id") or config_msg.get("agent"):
                agent_ref = config_msg.get("agent_id") or config_msg.get("agent")
    except Exception:
        logger.warning("[ws] Timed out waiting for config, continuing with defaults.")

    # ── Resolve the tenant's deployed agent ───────────────────────────
    # An agent is a tenant-owned row that points at one of the scenario
    # templates below and layers its own voice, language, greeting and prompt on
    # top. Loading it here — before the template chain runs — lets the agent
    # choose which template it is built from; its overrides are applied after.
    if agent_ref:
        try:
            agent_record = await tenant_store.get_agent(tenant_id, agent_ref)
            if agent_record is None:
                logger.warning(
                    f"[ws] Agent {agent_ref!r} not found for tenant {tenant_id}; "
                    f"falling back to scenario '{scenario_key}'."
                )
            elif agent_record.get("status") == "disabled":
                logger.warning(
                    f"[ws] Agent {agent_ref!r} is disabled; refusing the call."
                )
                await ws.send_json(
                    {"type": "error", "message": "This agent is disabled."}
                )
                await ws.close(code=1008, reason="Agent disabled")
                return
            else:
                scenario_key = agent_record.get("scenario_key") or scenario_key
                if agent_record.get("language"):
                    language = agent_record["language"]
                logger.info(
                    f"[ws] Using agent '{agent_record['agent_key']}' "
                    f"({agent_record['display_name']}) → scenario '{scenario_key}'"
                )
        except Exception as e:
            logger.error(f"[ws] Agent lookup failed for {agent_ref!r}: {e}")

    # Select prompt and greeting based on scenario
    if scenario_key == "restaurant_booking":
        system_prompt = RESTAURANT_BOOKING_PROMPT
        greeting_text = "Good evening! Welcome to The Royal Plate. I'm David, how can I help you?"
        if accent == "indian":
            language_code = "en-IN"
    elif scenario_key == "payment_followup":
        system_prompt = PAYMENT_FOLLOWUP_PROMPT
        # Default voice to Charon (Indian Male) for Mohan if client didn't explicitly request one
        if not isinstance(config_msg.get("voice"), str) or not config_msg.get("voice"):
            voice_name = "Charon"
        
        if language == "hindi":
            language_code = "hi-IN"
            greeting_text = "Namaste, Kya meri baat Arnav ji se ho rhi hai?"
        elif language == "tamil":
            language_code = "ta-IN"
            greeting_text = "ஹலோ சார், வணக்கம், நான் Mr. Arnav-கிட்ட பேசறனா?"
        elif language == "telugu":
            language_code = "te-IN"
            greeting_text = "హలో సర్, నమస్కారం, నేను అర్నవ్ గారితో మాట్లాడుతున్నానా?"
        else:
            language_code = "en-IN"
            greeting_text = "Hello, am I speaking with Mr. Arnav?"
    elif scenario_key == "ggs_support":
        system_prompt = FLEET_DESK_PROMPT
        # Parse the BCP-47 language code from the frontend (default: en-IN)
        lang = config_msg.get("language", "en-IN") if config_msg else "en-IN"
        language_code = lang

        # Language-specific greetings and voice config
        FLEET_DESK_GREETINGS = {
            "en-IN": "Hello, this is the Fleet Service Desk, I am Gaurav, how may I help you?",
            "en-US": "Hello, this is the Fleet Service Desk, I am Gaurav, how may I help you?",
            "ta-IN": "வணக்கம், இது Fleet Service Desk, நான் கௌரவ், உங்களுக்கு நான் எவ்வாறு உதவ முடியும்?",
            "hi-IN": "नमस्ते, यह Fleet Service Desk है, मैं गौरव हूँ, मैं आपकी क्या सहायता कर सकता हूँ?",
            "ja-JP": "こんにちは、Fleet Service Deskのガウラヴです。どのようなご用件でしょうか？",
            "de-DE": "Hallo, hier ist der Fleet Service Desk, mein Name ist Gaurav, wie kann ich Ihnen helfen?",
            "te-IN": "నమస్తే, ఇది Fleet Service Desk, నేను గౌరవ్, మీకు ఏ విధంగా సహాయం చేయగలను?",
        }
        # Fallback mappings for shorter language codes if sent by frontend
        lang_mapping = {
            "en": "en-IN",
            "ta": "ta-IN",
            "hi": "hi-IN",
            "ja": "ja-JP",
            "de": "de-DE",
            "te": "te-IN",
        }
        actual_lang = lang_mapping.get(lang, lang)
        greeting_text = FLEET_DESK_GREETINGS.get(actual_lang, FLEET_DESK_GREETINGS["en-IN"])

        # Append language instruction to system prompt for non-English languages
        LANGUAGE_NAMES = {
            "ta-IN": "Tamil",
            "hi-IN": "Hindi",
            "ja-JP": "Japanese",
            "de-DE": "German",
            "te-IN": "Telugu",
            "en-US": "English",
            "en-IN": "English",
        }
        lang_name = LANGUAGE_NAMES.get(actual_lang, "English")
        if actual_lang not in ("en-IN", "en-US"):
            system_prompt += f"""

# LANGUAGE OVERRIDE
- You MUST speak and respond ONLY in {lang_name}. This overrides all previous language rules.
- The knowledge base articles you receive from tools are in English. Translate them accurately into {lang_name} before speaking to the user.
- SEARCH IN ENGLISH, ALWAYS. The `query` you pass to initialize_search MUST be written in ENGLISH, no matter what language the caller is speaking. The documentation is stored in English, and a query in another script retrieves the wrong documents — the caller then gets a confident answer taken from an unrelated manual. Understand the caller in {lang_name}, translate their question into an English search query, then speak the answer back in {lang_name}.
- Keep technical product names (Fuso, eCanter, DTOM, etc.) untranslated.
- Adapt your speech to sound natural in {lang_name} — do not do word-for-word translation.
"""
            # Indic callers expect the register a real support engineer uses on
            # the phone: the sentence in their language, the technical vocabulary
            # in English. A literary, fully-translated rendering sounds like a
            # textbook being read aloud and is genuinely harder for a working
            # technician to follow than the code-mixed speech they use daily.
            if actual_lang in ("hi-IN", "ta-IN", "te-IN"):
                system_prompt += f"""
# HOW TO SPEAK {lang_name.upper()} — CODE-MIXED, NOT LITERARY
- Speak the way support engineers actually talk on the phone in India: {lang_name} sentence structure with English technical words mixed in. Hinglish / Tanglish / Tenglish, not textbook {lang_name}.
- KEEP THESE IN ENGLISH, always: part and component names (brake fluid, master cylinder, flare nut, wheel bearing, propeller shaft), units (kilometre, kilometres, months, Newton-meter, N·m, litre), procedure verbs used as terms (inspect, replace, lubricate, service, torque), grades and codes (DOT3, SAE J1703, GL-5, 5W-30).
- Numbers may be spoken in English — "eighty thousand kilometres" is more natural to a technician than a fully translated numeral.
- Do NOT reach for formal or Sanskritised vocabulary when the everyday English word is what people actually say. Never invent a translation for a technical term that has no common {lang_name} equivalent — say it in English.
- Everything else — greetings, explanations, questions, reassurance — stays in natural, conversational {lang_name}.
- Example of the right register: "Sir, brake fluid ko aap eighty thousand kilometre pe replace karna hai, ya twenty four months pe — jo pehle aaye."
"""

        # Voice selection based on language
        if not isinstance(config_msg.get("voice"), str) or not config_msg.get("voice"):
            if actual_lang in ("en-IN", "hi-IN", "ta-IN", "te-IN"):
                voice_name = "Charon"  # Indian voice
            else:
                voice_name = "Orus"  # General voice
    elif scenario_key == "fsecure_support":
        system_prompt = FSECURE_SUPPORT_PROMPT
        # Parse the BCP-47 language code from the frontend (default: en-IN)
        lang = config_msg.get("language", "en-IN") if config_msg else "en-IN"
        language_code = lang

        # Language-specific greetings and voice config
        FSECURE_GREETINGS = {
            "en-IN": "Hello, this is the Endpoint Security Desk, I am Mohit, how may I help you?",
            "en-US": "Hello, this is F-Secure technical support. My name is Mohit, how may I help you today?",
            "fi": "Hei, täällä F-Securen tekninen tuki, Mohit täällä, kuinka voin auttaa?",
            "sv": "Hej, det här är F-Secures tekniska support, jag heter Mohit, hur kan jag hjälpa dig?",
            "de": "Hallo, hier ist der technische Support von F-Secure, mein Name ist Mohit, wie kann ich Ihnen helfen?",
            "nl": "Hallo, dit is de technische ondersteuning van F-Secure, ik ben Mohit, hoe kan ik u helpen?",
            "fr": "Bonjour, ici le support technique de F-Secure, je suis Mohit, comment puis-je vous aider?",
            "ja": "こんにちは、F-Secureのテクニカルサポートチームのモヒトです。どのようなご用件でしょうか？",
            "ja-JP": "こんにちは、F-Secureのテクニカルサポートチームのモヒトです。どのようなご用件でしょうか？",
        }
        greeting_text = FSECURE_GREETINGS.get(lang, FSECURE_GREETINGS["en-IN"])

        # Append language instruction to system prompt for non-English languages
        LANGUAGE_NAMES = {
            "fi": "Finnish", "sv": "Swedish", "de": "German",
            "nl": "Dutch", "fr": "French", "en-US": "English",
            "en-IN": "English", "ja": "Japanese", "ja-JP": "Japanese",
        }
        lang_name = LANGUAGE_NAMES.get(lang, "English")
        lang_display = lang_name
        if lang not in ("en-IN", "en-US"):
            system_prompt += f"""

# LANGUAGE OVERRIDE
- You MUST speak and respond ONLY in {lang_name}. This overrides all previous language rules.
- The knowledge base articles you receive from tools are in English. Translate them accurately into {lang_name} before speaking to the user.
- SEARCH IN ENGLISH, ALWAYS. The `query` you pass to initialize_search MUST be written in ENGLISH, whatever language the caller uses — the knowledge base is English, and a non-English query retrieves the wrong articles.
- Keep technical product names (F-Secure, VPN, FREEDOME, etc.) untranslated.
- Adapt your speech to sound natural in {lang_name} — do not do word-for-word translation.
"""
        elif lang == "en-US":
            system_prompt = system_prompt.replace(
                "# LANGUAGE RULES\n- Speak ONLY in natural, standard English.\n- Do NOT use any regional Indic languages or scripts.",
                "# LANGUAGE RULES\n- Speak ONLY in natural, standard American English.\n- Use a clear American accent."
            )

        # Voice selection based on language
        if not isinstance(config_msg.get("voice"), str) or not config_msg.get("voice"):
            if lang == "en-IN":
                voice_name = "Charon"
            else:
                voice_name = "Orus"
    else:
        system_prompt = TAMIL_REAL_ESTATE_PROMPT
        greeting_text = "ஹலோ சார், வணக்கம், நான் Mr. Arnav-கிட்ட பேசறனா?"
    # ── Agent overrides ───────────────────────────────────────────────
    # Applied last so a tenant's own wording always beats the shared template.
    # An empty override is treated as "not set" and leaves the template alone.
    if agent_record:
        if agent_record.get("system_prompt"):
            system_prompt = agent_record["system_prompt"]
        if agent_record.get("greeting"):
            greeting_text = agent_record["greeting"]
        # A voice pinned by the client still wins over the agent's default —
        # the client is the more specific request.
        client_pinned_voice = bool(config_msg and config_msg.get("voice"))
        if agent_record.get("voice") and not client_pinned_voice:
            voice_name = agent_record["voice"]

    logger.info(f"[ws] Scenario: {scenario_key}, Voice: {voice_name}, Accent: {accent}, LangCode: {language_code}, Lang: {language}")

    if not api_key:
        await ws.send_json({"type": "error", "message": "Missing GEMINI_API_KEY."})
        await ws.close(code=1008, reason="Missing GEMINI_API_KEY")
        return

    await db_store.log_call_start(
        session_id, phone_number, scenario_key, tenant_id=tenant_id
    )
    await tenant_store.record_usage(
        tenant_id=tenant_id,
        event_type="call_started",
        session_id=session_id,
        agent_id=(agent_record or {}).get("id"),
        channel="web",
        scenario_key=scenario_key,
        provider="gemini",
        model=GEMINI_LIVE_MODEL,
        metadata={"phone_number": phone_number, "language": language},
    )
    await redis_cache.set_session(session_id, {
        "phone_number": phone_number,
        "scenario": scenario_key,
        "status": "active"
    })

    # ── Connect to Gemini Live WebSocket ──────────────────────────────
    gemini_ws_url = f"{GEMINI_LIVE_WS_URL}?key={api_key}"

    # ── Adaptive voice / pace state ───────────────────────────────────
    # Only fsecure opts in, and only when the client didn't pin a voice itself.
    adaptive_ok = scenario_key in ADAPTIVE_SCENARIOS
    explicit_voice = isinstance(config_msg.get("voice"), str) and bool(config_msg.get("voice")) if config_msg else False
    detector = GenderDetector() if (GENDER_ADAPTIVE_VOICE and adaptive_ok and not explicit_voice) else None
    pace = PaceTracker(
        language=language_code or "en-IN", min_rate=PACE_MIN, max_rate=PACE_MAX
    ) if (ADAPTIVE_PACE and adaptive_ok) else None

    conn: Dict[str, Any] = {"ws": None}     # live Gemini socket; None while swapping
    # Mic audio captured during a swap. Bounded (~5s) so a stalled reconnect
    # sheds the oldest audio instead of growing without limit.
    audio_backlog: Deque[str] = deque(maxlen=30)
    swap_event = asyncio.Event()
    client_closed = asyncio.Event()
    current_voice = [voice_name]            # mutable so nested scopes can swap it
    swaps_done = 0
    greeting_done = False

    # Voice switching is consent-gated: detecting a mismatched voice only
    # *offers* the switch. "idle" → "queued" (offer due) → "asked" (waiting for
    # the customer's answer via the set_voice_preference tool).
    offer_state = "idle"
    offer_voice: Optional[str] = None       # voice we'd move to if they accept
    offer_gender: Optional[str] = None
    declined_voices: set = set()            # never re-offer something they refused
    resume_with_ack = False                 # new session should acknowledge the switch
    agent_name = None                       # set once a hand-over renames the agent
    last_kb_context = None                  # newest KB article, carried across a swap

    async def browser_to_gemini():
        """Single long-lived reader for the browser socket.

        Lives across Gemini reconnects: while conn["ws"] is None we buffer the
        mic audio instead of dropping it, then flush once the new session is up.
        """
        nonlocal offer_state, offer_voice, offer_gender
        try:
            while ws.client_state == WebSocketState.CONNECTED:
                msg = await ws.receive()

                if msg.get("type") == "websocket.disconnect":
                    break

                audio_pcm = None

                # Binary audio frames from browser
                if "bytes" in msg and msg["bytes"]:
                    audio_pcm = msg["bytes"]

                # JSON-wrapped audio from browser
                elif "text" in msg and msg["text"]:
                    try:
                        payload = json.loads(msg["text"])
                        if payload.get("type") == "audio" and "data" in payload:
                            audio_pcm = base64.b64decode(payload["data"])
                        elif payload.get("type") == "ping":
                            await ws.send_json({"type": "pong"})
                            continue
                    except Exception:
                        pass

                if not audio_pcm:
                    continue

                # Browser sends 24kHz PCM16, Gemini expects 16kHz
                pcm_16k = downsample_24k_to_16k(audio_pcm)

                # Passive pitch read on the same buffer. Skipped while the agent
                # is talking so we never classify our own voice back at ourselves.
                if detector is not None and not is_speaking:
                    guess = detector.feed(pcm_16k)
                    if guess and offer_state == "idle" and swaps_done < MAX_VOICE_SWAPS:
                        target = voice_for_gender(guess, language_code or "en-IN")
                        if target != current_voice[0] and target not in declined_voices:
                            offer_voice = target
                            offer_gender = guess
                            # Only flag it here. Pushing client_content into a live
                            # audio turn disturbs Gemini's turn state — the model can
                            # end up waiting on a turn_complete that automatic VAD
                            # never sends, and the call goes silent. The offer is
                            # delivered from gemini_recv instead, at a safe point.
                            offer_state = "queued"
                            logger.info(f"[gender] {guess} detected — offer queued (target {target})")

                b64_audio = base64.b64encode(pcm_16k).decode("ascii")

                gws = conn["ws"]
                if gws is None:
                    audio_backlog.append(b64_audio)
                    continue

                # Forward to Gemini Live as realtimeInput
                try:
                    await gws.send(json.dumps({
                        "realtime_input": {
                            "audio": {
                                "data": b64_audio,
                                "mimeType": "audio/pcm;rate=16000"
                            }
                        }
                    }))
                except Exception as e:
                    # A socket being retired for a voice swap is expected to
                    # fail here; only a genuinely dead session ends the loop.
                    if conn["ws"] is gws and not swap_event.is_set():
                        logger.error(f"[gemini-live] Error sending audio to Gemini: {e}")
                        break

        except WebSocketDisconnect:
            logger.info(f"[ws] Client disconnected (Session: {session_id})")
        except Exception as e:
            logger.error(f"[ws] Error in main loop: {e}")
        finally:
            client_closed.set()

    browser_task: Optional[asyncio.Task] = None

    try:
        browser_task = asyncio.create_task(browser_to_gemini())
        generation = 0

        while not client_closed.is_set():
            swap_event.clear()
            # Not `async with`: on a swap we must not block the new connection on
            # the old socket's close handshake (which can wait on the peer). The
            # retired socket is closed in the background instead.
            gemini_ws = await websockets.connect(gemini_ws_url, close_timeout=1)
            try:
                logger.info(f"[gemini-live] Connected to Gemini Live WebSocket")

                # Send setup configuration
                speech_config = {
                    "voice_config": {
                        "prebuilt_voice_config": {
                            "voice_name": current_voice[0]
                        }
                    }
                }
                if language_code:
                    speech_config["language_code"] = language_code

                setup_message = {
                "setup": {
                    "model": f"models/{GEMINI_LIVE_MODEL}",
                    "generation_config": {
                        "response_modalities": ["AUDIO"],
                        "speech_config": speech_config
                    },
                    "system_instruction": {
                        "parts": [{"text": system_prompt + (agent_identity_override(agent_name) if agent_name else "")}]
                    },
                    "input_audio_transcription": {},
                    "output_audio_transcription": {},
                    "realtime_input_config": {
                        "automatic_activity_detection": {
                            "disabled": False,
                            "start_of_speech_sensitivity": "START_SENSITIVITY_LOW",
                            "end_of_speech_sensitivity": "END_SENSITIVITY_HIGH",
                            "prefix_padding_ms": 100,
                            "silence_duration_ms": 200
                        }
                    }
                }
            }

                # Add two-phase tool declarations for deferred KB search
                if scenario_key in ("fsecure_support", "ggs_support"):
                    setup_message["setup"]["tools"] = [{
                        "functionDeclarations": [
                            {
                                "name": "initialize_search",
                                "description": "Call this IMMEDIATELY when the user asks ANY question requiring a knowledge base lookup. This starts the search in the background. You MUST call fetch_search_results right after.",
                                "parameters": {
                                    "type": "OBJECT",
                                    "properties": {
                                        "query": {
                                            "type": "STRING",
                                            "description": "The user's query or problem statement to search for."
                                        }
                                    },
                                    "required": ["query"]
                                }
                            },
                            {
                                "name": "fetch_search_results",
                                "description": "Call this IMMEDIATELY after receiving the response from initialize_search. This returns the actual search results. Do NOT speak to the user until you have called this tool.",
                                "parameters": {
                                    "type": "OBJECT",
                                    "properties": {}
                                }
                            }
                        ]
                    }]
                    # Only offered when adaptive voice is live — the model must
                    # not be able to switch voices unprompted.
                    if detector is not None:
                        setup_message["setup"]["tools"][0]["functionDeclarations"].append({
                            "name": "set_voice_preference",
                            "description": "Call this the moment the customer answers your question about being connected with someone else from the support team. Do not call it at any other time.",
                            "parameters": {
                                "type": "OBJECT",
                                "properties": {
                                    "accepted": {
                                        "type": "BOOLEAN",
                                        "description": "true if the customer wants to be connected with the other agent, false if they would rather continue with you."
                                    }
                                },
                                "required": ["accepted"]
                            }
                        })
                logger.info(f"[gemini-live] Setup message tools: {json.dumps(setup_message['setup'].get('tools', 'NONE'))[:300]}")
                await gemini_ws.send(json.dumps(setup_message))
                logger.info(f"[gemini-live] Setup sent (model: {GEMINI_LIVE_MODEL}, voice: {current_voice[0]}, language_code: {language_code})")

                # Wait for setupComplete
                setup_resp = await asyncio.wait_for(gemini_ws.recv(), timeout=10)
                setup_data = json.loads(setup_resp)
                if "setupComplete" in setup_data:
                    logger.info(f"[gemini-live] ✅ Setup complete!")
                else:
                    logger.warning(f"[gemini-live] Unexpected setup response: {json.dumps(setup_data)[:200]}")

                consent_swap = False
                if generation == 0:
                    await send_status("listening")

                    # Send initial greeting as a text turn so Gemini speaks it in its voice
                    await gemini_ws.send(json.dumps({
                        "client_content": {
                            "turns": [{
                                "role": "user",
                                "parts": [{"text": f"Start the call now. Say EXACTLY this greeting: {greeting_text}"}]
                            }],
                            "turn_complete": True
                        }
                    }))
                    logger.info(f"[gemini-live] Sent greeting prompt to Gemini: {greeting_text[:50]}")
                else:
                    # Voice swap: replay the recent transcript so the fresh session
                    # resumes mid-conversation. turn_complete=False loads context
                    # without triggering a reply, so the agent never re-greets.
                    history = [
                        {
                            "role": "model" if t["role"] == "assistant" else "user",
                            "parts": [{"text": t["text"]}],
                        }
                        for t in conversation[-VOICE_SWAP_HISTORY_TURNS:]
                        if t.get("text")
                    ]
                    if history:
                        await gemini_ws.send(json.dumps({
                            "client_content": {"turns": history, "turn_complete": False}
                        }))
                    logger.info(f"[gemini-live] 🔁 Resumed after voice swap with {len(history)} turns of context")

                    # The offer interrupts before fetch_search_results is called, so
                    # the article is usually still an in-flight background task rather
                    # than a stored result. Drain it so the new voice inherits it.
                    if last_kb_context is None and pending_search_task is not None:
                        try:
                            last_kb_context = await asyncio.wait_for(
                                asyncio.shield(pending_search_task), timeout=2.0
                            )
                            logger.info("[gemini-live] 📎 Drained in-flight search for the new voice")
                        except Exception as e:
                            logger.debug(f"[gemini-live] No in-flight search to carry over: {e}")
                        pending_search_task = None

                    # Hand over the article the previous session had already pulled.
                    # Without this the new session starts with no tool results, and
                    # "answer their question" turns into inventing plausible steps.
                    if last_kb_context:
                        await gemini_ws.send(json.dumps({
                            "client_content": {
                                "turns": [{
                                    "role": "user",
                                    "parts": [{"text":
                                        "REFERENCE MATERIAL already retrieved for this customer — this is "
                                        "your source of truth for the topic under discussion. Deliver its "
                                        "exact steps, warnings and button names. Do NOT search again for "
                                        "this topic, and do NOT substitute your own general knowledge:\n"
                                        + last_kb_context
                                    }]
                                }],
                                "turn_complete": False
                            }
                        }))
                        logger.info(f"[gemini-live] 📎 Carried {len(last_kb_context)} chars of KB context into the new voice")

                    # Speak up in the new voice so the handover isn't dead air.
                    if resume_with_ack:
                        resume_with_ack = False
                        consent_swap = True
                        await gemini_ws.send(json.dumps({
                            "client_content": {
                                "turns": [{
                                    "role": "user",
                                    "parts": [{"text": voice_resume_directive(agent_name or FEMALE_AGENT_NAME, lang_display)}]
                                }],
                                "turn_complete": True
                            }
                        }))

                # ── Gemini → Browser: Receive audio/transcripts from Gemini ──
                pending_search_task = None  # Background search promise for two-phase deferred execution
                phase1_response_time = 0.0  # Timestamp when Phase 1 response was sent

                async def gemini_recv():
                    """Listen to Gemini Live WebSocket and forward audio/transcripts to browser."""
                    nonlocal is_speaking, pending_search_task, phase1_response_time
                    nonlocal swaps_done, greeting_done
                    nonlocal offer_state, offer_voice, resume_with_ack, agent_name
                    nonlocal last_kb_context

                    output_transcript_buffer = []
                    input_transcript_buffer = []
                    # Separate from input_transcript_buffer, which gets flushed
                    # mid-turn; pace needs the whole turn's text.
                    pace_turn_text: List[str] = []

                    try:
                        async for message in gemini_ws:
                            data = json.loads(message)

                            if "serverContent" in data:
                                sc = data["serverContent"]

                                # ── Audio chunks from Gemini's TTS ──
                                if "modelTurn" in sc and "parts" in sc["modelTurn"]:
                                    if not is_speaking:
                                        is_speaking = True
                                        await send_status("speaking")

                                    for part in sc["modelTurn"]["parts"]:
                                        if "inlineData" in part:
                                            audio_b64 = part["inlineData"]["data"]
                                            # Gemini outputs audio/pcm;rate=24000 which matches
                                            # the browser's SAMPLE_RATE of 24000 — no resampling needed!
                                            if ws.client_state == WebSocketState.CONNECTED:
                                                await ws.send_json({
                                                    "type": "audio",
                                                    "data": audio_b64,
                                                })

                                # ── User's speech transcription (STT) ──
                                if "inputTranscription" in sc:
                                    text = sc["inputTranscription"].get("text", "")
                                    if text.strip():
                                        input_transcript_buffer.append(text)
                                        if pace is not None:
                                            # First/last chunk timings bracket the user's turn.
                                            pace.mark_chunk(time.monotonic())
                                            pace_turn_text.append(text)
                                        # Flush complete sentences to UI
                                        full_input = "".join(input_transcript_buffer)
                                        # Flush on: punctuation, long text, OR short complete utterances
                                        # (e.g. "yes", "haan", "ok", "ठीक है") so they appear instantly
                                        is_short_complete = len(full_input.strip()) <= 20 and len(input_transcript_buffer) >= 1
                                        if any(c in text for c in ".?!") or len(full_input) > 50 or is_short_complete:
                                            await send_transcript("user", full_input.strip())
                                            input_transcript_buffer.clear()

                                # ── Gemini's response transcription ──
                                if "outputTranscription" in sc:
                                    text = sc["outputTranscription"].get("text", "")
                                    if text.strip():
                                        output_transcript_buffer.append(text)

                                # ── Turn complete (Gemini finished speaking) ──
                                if sc.get("turnComplete"):
                                    is_speaking = False
                                    await send_status("listening")

                                    # Flush remaining input transcript
                                    if input_transcript_buffer:
                                        full = "".join(input_transcript_buffer).strip()
                                        if full:
                                            await send_transcript("user", full)
                                        input_transcript_buffer.clear()

                                    # Flush output transcript
                                    if output_transcript_buffer:
                                        full = "".join(output_transcript_buffer).strip()
                                        if full:
                                            await send_transcript("assistant", full)
                                            logger.info(f"[gemini-live] Maya said: '{full[:80]}'")
                                        output_transcript_buffer.clear()

                                    greeting_done = True

                                    # ── Match the agent's pace to the user's ──
                                    if pace is not None:
                                        new_rate = pace.finish("".join(pace_turn_text))
                                        pace_turn_text.clear()
                                        if new_rate is not None and ws.client_state == WebSocketState.CONNECTED:
                                            await ws.send_json({"type": "rate", "value": new_rate})
                                            logger.info(f"[pace] playback rate → {new_rate}")

                                    # ── Fallback: no search ran, so no tool response to
                                    # ride in on. A turn boundary is the only other safe
                                    # moment to reach the model — mid-audio injection
                                    # stalls the turn. turn_complete=False so it folds
                                    # into the next reply rather than being its own
                                    # utterance, and never cuts the customer off.
                                    if offer_state == "queued" and greeting_done:
                                        await gemini_ws.send(json.dumps({
                                            "client_content": {
                                                "turns": [{
                                                    "role": "user",
                                                    "parts": [{"text": voice_offer_directive(offer_gender, lang_display)}]
                                                }],
                                                "turn_complete": False
                                            }
                                        }))
                                        offer_state = "asked"
                                        logger.info(f"[gender] offer queued into context for the next reply ({offer_voice})")

                                # ── Gemini detected interruption ──
                                if sc.get("interrupted"):
                                    logger.info("[gemini-live] 🔇 Gemini detected barge-in (interruption)")
                                    is_speaking = False
                                    # Tell browser to stop all playing audio
                                    if ws.client_state == WebSocketState.CONNECTED:
                                        await ws.send_json({"type": "clear"})
                                    await send_status("listening")

                                    # Flush any partial transcripts
                                    if output_transcript_buffer:
                                        full = "".join(output_transcript_buffer).strip()
                                        if full:
                                            await send_transcript("assistant", full + " [interrupted]")
                                        output_transcript_buffer.clear()

                            # ── Tool call from Gemini (Two-Phase Deferred Execution) ──
                            elif "toolCall" in data:
                                tool_call = data["toolCall"]
                                function_calls = tool_call.get("functionCalls", [])
                                function_responses = []

                                for fc in function_calls:
                                    fn_name = fc.get("name", "")
                                    fn_args = fc.get("args", {})
                                    fn_id = fc.get("id", "")

                                    # ── The customer answered the voice-switch offer ──
                                    if fn_name == "set_voice_preference":
                                        accepted = bool(fn_args.get("accepted"))
                                        target = offer_voice
                                        offer_state = "idle"
                                        offer_voice = None
                                        if not accepted or not target:
                                            # Declined — stay put. declined_voices stops
                                            # us ever proposing this voice again.
                                            if target:
                                                declined_voices.add(target)
                                            logger.info("[gender] customer declined the switch — staying on "
                                                        f"{current_voice[0]}")
                                            function_responses.append({
                                                "id": fn_id,
                                                "name": fn_name,
                                                "response": {"result": json.dumps({
                                                    "acknowledged": True,
                                                    "next_action": "The customer is staying with you. Do not "
                                                                   "mention the switch again, and never use the "
                                                                   "word 'agent'. Answer their original question now."
                                                })}
                                            })
                                            continue

                                        # Accepted: swap now rather than after an
                                        # acknowledgement in the old voice, so the
                                        # customer only ever hears the voice they chose.
                                        logger.info(f"[gender] customer accepted — switching to {target} now")
                                        current_voice[0] = target
                                        agent_name = FEMALE_AGENT_NAME if offer_gender == "female" else None
                                        swaps_done += 1
                                        # One switch per call, full stop. If a different
                                        # speaker picks up later we neither switch back
                                        # nor ask again — the customer already chose.
                                        offer_state = "locked"
                                        resume_with_ack = True
                                        # Drop whatever the old voice still has queued in
                                        # the browser, so the new voice starts at once
                                        # instead of behind stale scheduled audio.
                                        if ws.client_state == WebSocketState.CONNECTED:
                                            await ws.send_json({"type": "clear"})
                                        is_speaking = False
                                        swap_event.set()
                                        return

                                    # ── Phase 1: initialize_search ──
                                    # Kick off the real search in the background, return instantly
                                    if fn_name == "initialize_search":
                                        query = fn_args.get("query", "")
                                        logger.info(f"[gemini-live] 🔍 Phase 1 — initialize_search('{query[:60]}')")

                                        # Fire off the actual KB search as a background task
                                        pending_search_task = asyncio.create_task(
                                            execute_tool(
                                                "query_knowledge_base",
                                                {"query": query},
                                                session_id,
                                                phone_number,
                                                db_store,
                                                scenario_key=scenario_key,
                                            )
                                        )
                                        logger.info(f"[gemini-live] 🚀 Background search started")

                                        # Instant response — unblocks Gemini to speak filler
                                        phase1_payload = {"status": "search_initiated_successfully"}
                                        # Ride the pending voice-switch offer in on this
                                        # response: it reaches the model mid-turn without
                                        # touching the audio turn, so the customer is asked
                                        # in the very reply they are already waiting for.
                                        if offer_state == "queued" and greeting_done and offer_gender:
                                            phase1_payload["priority_instruction"] = voice_offer_tool_instruction(
                                                offer_gender, lang_display
                                            )
                                            offer_state = "asked"
                                            logger.info(f"[gender] offer attached to initialize_search response ({offer_voice})")
                                        function_responses.append({
                                            "id": fn_id,
                                            "name": fn_name,
                                            "response": {"result": json.dumps(phase1_payload)}
                                        })
                                        # Record when we freed Gemini so Phase 2 can enforce filler gap
                                        phase1_response_time = time.monotonic()

                                    # ── Phase 2: fetch_search_results ──
                                    # Await the background promise and return the actual data
                                    elif fn_name == "fetch_search_results":
                                        logger.info(f"[gemini-live] 📥 Phase 2 — fetch_search_results (awaiting background task)")

                                        # Space the answer after the filler so they do not
                                        # overlap. This wait MUST NOT happen inline: this
                                        # block runs inside `async for message in gemini_ws`,
                                        # so sleeping here stops the loop reading from Gemini
                                        # altogether — the filler audio still being streamed
                                        # stops being forwarded, and turn signals pile up
                                        # unread. That reads to the caller as the agent
                                        # saying "let me check" and then dying mid-sentence.
                                        # The gap is applied in the deferred sender below,
                                        # off the receive loop.
                                        FILLER_PLAY_DURATION = 1.6  # seconds

                                        # Deliver off the receive loop. The awaits below —
                                        # the search itself and the filler gap — would
                                        # otherwise stall every message Gemini is sending,
                                        # including the audio it is mid-way through
                                        # speaking. Handing this to a task lets the loop
                                        # keep pumping audio while the answer is prepared.
                                        if pending_search_task is not None:
                                            search_task = pending_search_task
                                            pending_search_task = None
                                            gap_start = phase1_response_time
                                            phase1_response_time = 0.0

                                            async def deliver_answer(task=search_task, fid=fn_id,
                                                                     fname=fn_name, started=gap_start,
                                                                     min_gap=FILLER_PLAY_DURATION):
                                                nonlocal last_kb_context
                                                try:
                                                    payload = await task
                                                    last_kb_context = payload
                                                    logger.info(f"[gemini-live] ✅ Search results received: {payload[:200]}")
                                                except Exception as e:
                                                    logger.error(f"[gemini-live] Search task failed: {e}")
                                                    payload = json.dumps({
                                                        "found": False, "context": [],
                                                        "message": f"Search error: {e}",
                                                    })
                                                # Space the answer after the filler, without
                                                # holding the receive loop hostage.
                                                if started > 0:
                                                    remaining = min_gap - (time.monotonic() - started)
                                                    if remaining > 0:
                                                        logger.info(f"[gemini-live] ⏳ spacing answer by {remaining:.2f}s (off-loop)")
                                                        await asyncio.sleep(remaining)
                                                try:
                                                    await gemini_ws.send(json.dumps({
                                                        "tool_response": {"function_responses": [
                                                            {"id": fid, "name": fname, "response": {"result": payload}}
                                                        ]}
                                                    }))
                                                    logger.info(f"[gemini-live] 📤 Sent deferred tool response: {fname}")
                                                except Exception as e:
                                                    logger.error(f"[gemini-live] Failed to send deferred tool response: {e}")

                                            asyncio.create_task(deliver_answer())
                                            continue_without_response = True
                                        else:
                                            continue_without_response = False
                                            # No pending task. Usually this is the model
                                            # fetching twice for one search — the caller
                                            # said "did you get that?" while the answer was
                                            # in flight, and the second fetch arrives after
                                            # the first already consumed the task. Replaying
                                            # the last result is right: reporting "not found"
                                            # here tells the caller the documentation does
                                            # not exist when it was already retrieved.
                                            if last_kb_context:
                                                logger.info(
                                                    "[gemini-live] ↩️ fetch_search_results with no pending task — "
                                                    "replaying the last retrieved context"
                                                )
                                                result = last_kb_context
                                            else:
                                                logger.warning(f"[gemini-live] ⚠️ fetch_search_results called without prior initialize_search")
                                                result = json.dumps({
                                                    "found": False,
                                                    "context": [],
                                                    "message": (
                                                        "No search has been started yet. Call initialize_search "
                                                        "first — do NOT tell the caller the information is missing."
                                                    ),
                                                })

                                        if not continue_without_response:
                                            function_responses.append({
                                                "id": fn_id,
                                                "name": fn_name,
                                                "response": {"result": result}
                                            })
                                            phase1_response_time = 0.0  # Reset for next search

                                    # ── Other tools (non-KB) — execute normally ──
                                    else:
                                        logger.info(f"[gemini-live] 🔧 Tool call: {fn_name}({json.dumps(fn_args)[:100]})")
                                        try:
                                            result = await execute_tool(
                                                fn_name, fn_args, session_id,
                                                phone_number, db_store,
                                                scenario_key=scenario_key
                                            )
                                            logger.info(f"[gemini-live] 🔧 Tool result: {result[:200]}")
                                        except Exception as e:
                                            logger.error(f"[gemini-live] Tool execution error: {e}")
                                            result = json.dumps({"error": str(e)})

                                        function_responses.append({
                                            "id": fn_id,
                                            "name": fn_name,
                                            "response": {"result": result}
                                        })

                                # Send tool response(s) back to Gemini
                                if function_responses:
                                    try:
                                        await gemini_ws.send(json.dumps({
                                            "tool_response": {
                                                "function_responses": function_responses
                                            }
                                        }))
                                        names = [fr["name"] for fr in function_responses]
                                        logger.info(f"[gemini-live] 📤 Sent tool response(s): {names}")
                                    except Exception as e:
                                        logger.error(f"[gemini-live] Failed to send tool response: {e}")

                            elif "sessionResumptionUpdate" in data:
                                # Session management event, can ignore
                                pass

                            elif "setupComplete" in data:
                                pass

                            else:
                                logger.info(f"[gemini-live] 📨 Other message keys: {list(data.keys())} → {json.dumps(data)[:300]}")
                    except websockets.exceptions.ConnectionClosed as e:
                        logger.info(f"[gemini-live] Gemini WebSocket closed: {e}")
                    except Exception as e:
                        logger.error(f"[gemini-live] Recv error: {e}")

                # Go live: publish the socket and flush anything the mic
                # captured while we were reconnecting.
                conn["ws"] = gemini_ws
                if audio_backlog and consent_swap:
                    # This audio is the customer saying "yes, connect me" — the old
                    # session already heard it and it's in the replayed transcript.
                    # Replaying it here lands as fresh speech while the new voice is
                    # introducing herself, so Gemini calls barge-in and cuts her off
                    # mid-word. Drop it.
                    logger.info(f"[gemini-live] Dropping {len(audio_backlog)} already-consumed chunks (consent swap)")
                    audio_backlog.clear()
                if audio_backlog:
                    logger.info(f"[gemini-live] Flushing {len(audio_backlog)} buffered audio chunks")
                    for chunk in audio_backlog:
                        try:
                            await gemini_ws.send(json.dumps({
                                "realtime_input": {
                                    "audio": {"data": chunk, "mimeType": "audio/pcm;rate=16000"}
                                }
                            }))
                        except Exception as e:
                            logger.error(f"[gemini-live] Backlog flush failed: {e}")
                            break
                    audio_backlog.clear()

                # Run until Gemini drops, the browser leaves, or a swap is due.
                recv_task = asyncio.create_task(gemini_recv())
                swap_waiter = asyncio.create_task(swap_event.wait())
                closed_waiter = asyncio.create_task(client_closed.wait())
                try:
                    await asyncio.wait(
                        {recv_task, swap_waiter, closed_waiter},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    conn["ws"] = None
                    for task in (recv_task, swap_waiter, closed_waiter):
                        if task.done():
                            # Consume any exception so it isn't reported as unretrieved.
                            if not task.cancelled() and task.exception() is not None:
                                logger.error(f"[gemini-live] Receiver task failed: {task.exception()}")
                        else:
                            task.cancel()
            finally:
                # Retire the old socket without making the customer wait for it.
                asyncio.create_task(_close_quietly(gemini_ws))

            if client_closed.is_set() or not swap_event.is_set():
                break

            generation += 1
            logger.info(f"[gemini-live] 🔄 Reconnecting with voice '{current_voice[0]}' (swap {swaps_done}/{MAX_VOICE_SWAPS})")

    except websockets.exceptions.InvalidStatusCode as e:
        logger.error(f"[gemini-live] WebSocket connection rejected: {e}")
        await ws.send_json({"type": "error", "message": f"Gemini Live connection failed: {e}"})
    except Exception as e:
        logger.error(f"[gemini-live] Connection error: {e}")
        await ws.send_json({"type": "error", "message": f"Gemini Live error: {e}"})
    finally:
        if browser_task is not None and not browser_task.done():
            browser_task.cancel()
        await db_store.log_call_end(session_id, tenant_id=tenant_id)
        # The billable event. record_usage swallows its own errors, so a usage
        # write can never be the reason a call teardown fails.
        await tenant_store.record_usage(
            tenant_id=tenant_id,
            event_type="call_ended",
            session_id=session_id,
            agent_id=(agent_record or {}).get("id"),
            channel="web",
            scenario_key=scenario_key,
            provider="gemini",
            model=GEMINI_LIVE_MODEL,
            duration_seconds=int(max(0, time.time() - call_started_at)),
            metadata={"turns": len(conversation), "voice": current_voice[0]},
        )
        logger.info(f"[ws] Session closed: {session_id}")


# ─── Gemini-based Call Summary Generator ─────────────────────────────────
async def generate_call_summary_gemini(transcript: str, session_id: str) -> str:
    """Generate a call summary using Gemini's standard generateContent API.
    Much simpler and cheaper than the Azure Realtime WebSocket approach."""
    if not transcript or not transcript.strip():
        return None

    import aiohttp as _aiohttp
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": [{
            "parts": [{"text": f"Summarize this phone call transcript in 2-3 concise English sentences. Include: sentiment, key outcome, and next steps if any. Output ONLY the summary.\n\n{transcript[:2000]}"}]
        }],
        "generationConfig": {"temperature": 0.4, "maxOutputTokens": 200}
    }

    try:
        async with _aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=_aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    text = data.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")
                    if text.strip():
                        summary = text.strip()[:500]
                        logger.info(f"[summary-gemini] Generated for {session_id}: {summary[:80]}")
                        return summary
                else:
                    logger.error(f"[summary-gemini] API error {resp.status}: {await resp.text()}")
    except Exception as e:
        logger.error(f"[summary-gemini] Failed: {e}")

    # Fallback: first few lines of transcript
    lines = [l.strip() for l in transcript.strip().split("\n") if l.strip()]
    if lines:
        fallback = " | ".join(lines[:4])
        return fallback[:200] + "..." if len(fallback) > 200 else fallback
    return None


# ─── Exotel Telephony Pipeline (Gemini Live) ────────────────────────────
@app.websocket("/ws/exotel")
@app.websocket("/ws/exotel/")
async def exotel_gemini_pipeline(ws: WebSocket):
    """
    Exotel SIP voice streaming bridge → Gemini Live API.

    Full production pipeline with:
    - Exotel protocol handling (connected → start → media → stop)
    - CallSid → phone number resolution via api_routes maps
    - Dynamic scenario/customer/greeting per phone number
    - Live transcript pub/sub for the dashboard
    - Post-call summary via Gemini generateContent
    - Lead qualification extraction
    - Reminder contacts call history

    No Exotel dashboard changes needed — same /ws/exotel URL.
    """
    # Late imports from api_routes (these are shared in-process maps from main.py)
    from api_routes import (
        publish_transcript,
        cleanup_transcript_pubsub,
        _active_call_sid_map,
        _active_call_start_times,
        _call_sid_to_session,
        _call_sid_to_contact_id,
        _outbound_customer_map,
        _outbound_product_map,
        _outbound_language_map,
        _reminder_contacts,
        extract_lead_qualification_post_call,
    )

    if redis_cache.client is None and not hasattr(redis_cache, '_fallback'):
        await redis_cache.connect()
    if db_store.pool is None:
        await db_store.connect()

    await ws.accept()

    # Parse sample rate from Exotel query params
    try:
        exotel_sr_str = ws.query_params.get("sample_rate") or ws.query_params.get("sample-rate") or "8000"
        exotel_sr = int(exotel_sr_str)
    except Exception:
        exotel_sr = 8000

    logger.info(f"[exotel-gemini] Incoming call (sample_rate={exotel_sr}, query_params={dict(ws.query_params)})")

    session_id = f"call_{uuid.uuid4().hex[:12]}"
    api_key = GEMINI_API_KEY
    voice_name = GEMINI_VOICE

    # ── CallSid → Phone Number Resolution ─────────────────────────────
    call_sid_param = ws.query_params.get("CallSid") or ws.query_params.get("callSid") or ws.query_params.get("call_sid")
    phone_number = None
    call_sid_event = None

    if call_sid_param:
        phone_number = _active_call_sid_map.get(call_sid_param)
        if phone_number:
            logger.info(f"[exotel-gemini] Pre-resolved phone {phone_number} from CallSid: {call_sid_param}")

    # ── Exotel Handshake: Wait for start event ────────────────────────
    stream_sid = None
    try:
        while not stream_sid:
            start_msg = await ws.receive_json()
            evt = start_msg.get("event")
            if evt == "start":
                stream_sid = start_msg.get("stream_sid")
                call_sid_event = start_msg.get("start", {}).get("call_sid") or start_msg.get("start", {}).get("callSid")
                logger.info(f"[exotel-gemini] Call started (stream_sid={stream_sid}, call_sid={call_sid_event})")
                if not phone_number and call_sid_event:
                    phone_number = _active_call_sid_map.get(call_sid_event)
                    if phone_number:
                        logger.info(f"[exotel-gemini] Resolved phone {phone_number} from start callSid: {call_sid_event}")
                break
            elif evt == "connected":
                logger.info("[exotel-gemini] Received 'connected', waiting for 'start'...")
            elif evt == "stop":
                logger.info("[exotel-gemini] Stop before start. Hanging up.")
                await ws.close()
                return
    except Exception as e:
        logger.error(f"[exotel-gemini] Handshake failed: {e}")
        await ws.close()
        return

    # Fallback phone number
    if not phone_number:
        phone_number = ws.query_params.get("phone") or ws.query_params.get("phone_number") or ws.query_params.get("From") or ws.query_params.get("FromId") or "+919999999999"
        logger.info(f"[exotel-gemini] Resolved phone {phone_number} from query params or default")

    active_call_sid = call_sid_param or call_sid_event

    # ── Scenario / Customer / Language Resolution ─────────────────────
    scenario_key = ws.query_params.get("scenario") or ws.query_params.get("scenario_key") or "real_estate_lead"
    clean = phone_number.lstrip("+").lstrip("91").lstrip("0")

    # Check if outbound product map overrides the scenario
    mapped_product = _outbound_product_map.get(clean) or _outbound_product_map.get(phone_number)
    if mapped_product:
        scenario_key = mapped_product
        logger.info(f"[exotel-gemini] Dynamic scenario override: '{scenario_key}' for {phone_number}")

    # Resolve customer name
    customer_name = _outbound_customer_map.get(clean) or _outbound_customer_map.get(phone_number, "Mr. Arnav")
    # Strip "Mr." prefix for greeting
    customer_name_stripped = customer_name
    for prefix in ["Mr. ", "Mr.", "mr. ", "mr."]:
        if customer_name_stripped.startswith(prefix):
            customer_name_stripped = customer_name_stripped[len(prefix):]
            break

    # Resolve language
    selected_lang = (ws.query_params.get("language") or ws.query_params.get("selected_lang") or _outbound_language_map.get(clean) or _outbound_language_map.get(phone_number, "tamil")).lower()

    # ── Session Mapping ───────────────────────────────────────────────
    if active_call_sid:
        _call_sid_to_session[active_call_sid] = session_id
        if active_call_sid not in _active_call_sid_map and phone_number:
            _active_call_sid_map[active_call_sid] = phone_number
            _active_call_start_times.setdefault(active_call_sid, int(time.time() * 1000))

    # ── Tenant resolution ─────────────────────────────────────────────
    # Exotel dials a fixed URL, so the tenant (and optionally the agent) is
    # carried in that URL's query string: /ws/exotel?tenant_id=<slug>&agent_id=<key>.
    # With neither present the call lands on the default tenant, which is what
    # every currently-configured Exotel flow does today.
    try:
        tenant_id = await tenant_context.resolve_tenant_id_ws(ws)
    except Exception as e:
        logger.error(f"[exotel-gemini] Tenant resolution failed, using default: {e}")
        tenant_id = tenant_context.DEFAULT_TENANT_ID

    exotel_agent: Optional[Dict[str, Any]] = None
    exotel_agent_ref = ws.query_params.get("agent_id") or ws.query_params.get("agent")
    if exotel_agent_ref:
        try:
            exotel_agent = await tenant_store.get_agent(tenant_id, exotel_agent_ref)
            if exotel_agent:
                scenario_key = exotel_agent.get("scenario_key") or scenario_key
        except Exception as e:
            logger.error(f"[exotel-gemini] Agent lookup failed: {e}")

    call_started_at = time.time()

    logger.info(f"[exotel-gemini] Session {session_id} for {phone_number} (scenario={scenario_key}, lang={selected_lang}, customer={customer_name}, tenant={tenant_id})")

    # ── Build Dynamic System Prompt ───────────────────────────────────
    # Replace customer name in the prompt
    dynamic_prompt = TAMIL_REAL_ESTATE_PROMPT.replace("Mr. Arnav", customer_name)

    # ── Helpers ───────────────────────────────────────────────────────
    # Pre-build JSON template for media events (avoids dict construction per chunk)
    _media_prefix = '{"event":"media","stream_sid":"' + (stream_sid or '') + '","media":{"payload":"'
    _media_suffix = '"}}'

    async def send_exotel_media(pcm_24k_chunk: bytes):
        """Downsample 24kHz PCM from Gemini → Exotel sample rate, then send.
        Uses pre-serialized JSON template for minimal overhead."""
        if not stream_sid:
            return
        try:
            out_chunk = resample_pcm16(pcm_24k_chunk, 24000, exotel_sr) if exotel_sr != 24000 else pcm_24k_chunk
            b64 = base64.b64encode(out_chunk).decode("ascii")
            await ws.send_text(_media_prefix + b64 + _media_suffix)
        except Exception:
            pass

    async def send_exotel_clear():
        """Tell Exotel to stop playing audio (barge-in)."""
        if not stream_sid:
            return
        try:
            await ws.send_json({
                "event": "clear",
                "stream_sid": stream_sid
            })
        except Exception:
            pass

    # ── DB & Redis (fire-and-forget — don't block call start) ─────────
    asyncio.create_task(
        db_store.log_call_start(session_id, phone_number, scenario_key, tenant_id=tenant_id)
    )
    asyncio.create_task(tenant_store.record_usage(
        tenant_id=tenant_id,
        event_type="call_started",
        session_id=session_id,
        agent_id=(exotel_agent or {}).get("id"),
        channel="exotel",
        scenario_key=scenario_key,
        provider="gemini",
        model=GEMINI_LIVE_MODEL,
        metadata={"phone_number": phone_number, "language": selected_lang},
    ))
    asyncio.create_task(redis_cache.set_session(session_id, {
        "phone_number": phone_number,
        "scenario": scenario_key,
        "status": "active"
    }))

    full_call_transcript = []

    # ── Connect to Gemini Live ────────────────────────────────────────
    gemini_ws_url = f"{GEMINI_LIVE_WS_URL}?key={api_key}"

    try:
        async with websockets.connect(gemini_ws_url) as gemini_ws:
            logger.info(f"[exotel-gemini] Connected to Gemini Live WebSocket")

            # Setup with aggressive VAD for fast turn detection
            setup_message = {
                "setup": {
                    "model": f"models/{GEMINI_LIVE_MODEL}",
                    "generation_config": {
                        "response_modalities": ["AUDIO"],
                        "speech_config": {
                            "voice_config": {
                                "prebuilt_voice_config": {
                                    "voice_name": voice_name
                                }
                            }
                        }
                    },
                    "system_instruction": {
                        "parts": [{"text": dynamic_prompt}]
                    },
                    "input_audio_transcription": {},
                    "output_audio_transcription": {},
                    "realtime_input_config": {
                        "automatic_activity_detection": {
                            "disabled": False,
                            "start_of_speech_sensitivity": "START_SENSITIVITY_LOW",
                            "end_of_speech_sensitivity": "END_SENSITIVITY_HIGH",
                            "prefix_padding_ms": 100,
                            "silence_duration_ms": 200
                        }
                    }
                }
            }
            await gemini_ws.send(json.dumps(setup_message))

            setup_resp = await asyncio.wait_for(gemini_ws.recv(), timeout=10)
            setup_data = json.loads(setup_resp)
            if "setupComplete" in setup_data:
                logger.info(f"[exotel-gemini] ✅ Setup complete!")
            else:
                logger.warning(f"[exotel-gemini] Unexpected setup response: {json.dumps(setup_data)[:200]}")

            # Trigger greeting
            await gemini_ws.send(json.dumps({
                "client_content": {
                    "turns": [{
                        "role": "user",
                        "parts": [{"text": "Start the call now. Greet with your opening line."}]
                    }],
                    "turn_complete": True
                }
            }))
            logger.info("[exotel-gemini] Sent greeting prompt")

            # ── Gemini → Exotel: Forward audio ────────────────────────
            is_speaking = False
            output_transcript_buffer = []

            async def gemini_to_exotel():
                """Receive audio from Gemini Live and forward to Exotel."""
                nonlocal is_speaking

                try:
                    async for message in gemini_ws:
                        data = json.loads(message)

                        if "serverContent" in data:
                            sc = data["serverContent"]

                            # Audio chunks → downsample and forward to Exotel
                            if "modelTurn" in sc and "parts" in sc["modelTurn"]:
                                if not is_speaking:
                                    is_speaking = True

                                for part in sc["modelTurn"]["parts"]:
                                    if "inlineData" in part:
                                        audio_b64 = part["inlineData"]["data"]
                                        pcm_24k = base64.b64decode(audio_b64)
                                        await send_exotel_media(pcm_24k)

                            # Output transcription
                            if "outputTranscription" in sc:
                                text = sc["outputTranscription"].get("text", "")
                                if text.strip():
                                    output_transcript_buffer.append(text)

                            # Input transcription (user speech)
                            if "inputTranscription" in sc:
                                text = sc["inputTranscription"].get("text", "")
                                if text.strip():
                                    full_call_transcript.append(f"user: {text}")
                                    # Fire-and-forget DB write — don't block audio forwarding
                                    asyncio.create_task(db_store.save_transcript_turn(tenant_id=tenant_id, session_id=session_id, role="user", text=text))
                                    # Publish to frontend live view
                                    if active_call_sid:
                                        publish_transcript(active_call_sid, "user", text)

                            # Turn complete
                            if sc.get("turnComplete"):
                                is_speaking = False
                                if output_transcript_buffer:
                                    full = "".join(output_transcript_buffer).strip()
                                    if full:
                                        full_call_transcript.append(f"assistant: {full}")
                                        # Fire-and-forget DB write — don't block audio forwarding
                                        asyncio.create_task(db_store.save_transcript_turn(tenant_id=tenant_id, session_id=session_id, role="assistant", text=full))
                                        logger.info(f"[exotel-gemini] Maya: '{full[:80]}'")
                                        # Publish to frontend live view
                                        if active_call_sid:
                                            publish_transcript(active_call_sid, "assistant", full)
                                    output_transcript_buffer.clear()

                            # Gemini detected interruption
                            if sc.get("interrupted"):
                                logger.info("[exotel-gemini] 🔇 Barge-in detected")
                                is_speaking = False
                                await send_exotel_clear()
                                if output_transcript_buffer:
                                    full = "".join(output_transcript_buffer).strip()
                                    if full:
                                        full_call_transcript.append(f"assistant: {full} [interrupted]")
                                        if active_call_sid:
                                            publish_transcript(active_call_sid, "assistant", full + " [interrupted]")
                                    output_transcript_buffer.clear()

                except websockets.exceptions.ConnectionClosed as e:
                    logger.info(f"[exotel-gemini] Gemini WS closed: {e}")
                except Exception as e:
                    logger.error(f"[exotel-gemini] Recv error: {e}")

            recv_task = asyncio.create_task(gemini_to_exotel())

            # ── Exotel → Gemini: Forward user audio ───────────────────
            try:
                while True:
                    try:
                        data = await ws.receive_json()
                    except WebSocketDisconnect:
                        logger.info("[exotel-gemini] Exotel disconnected")
                        break
                    except Exception:
                        break

                    evt = data.get("event")

                    if evt == "media":
                        payload = data.get("media", {}).get("payload")
                        if payload:
                            try:
                                pcm_in = base64.b64decode(payload)
                                pcm_16k = resample_pcm16(pcm_in, exotel_sr, 16000)
                                b64_audio = base64.b64encode(pcm_16k).decode("ascii")

                                await gemini_ws.send(json.dumps({
                                    "realtime_input": {
                                        "audio": {
                                            "data": b64_audio,
                                            "mimeType": "audio/pcm;rate=16000"
                                        }
                                    }
                                }))
                            except Exception as e:
                                logger.error(f"[exotel-gemini] Audio forward error: {e}")
                                break

                    elif evt == "stop":
                        logger.info("[exotel-gemini] Stop event. Hanging up.")
                        break

            except Exception as e:
                logger.error(f"[exotel-gemini] Main loop error: {e}")
            finally:
                recv_task.cancel()

    except Exception as e:
        logger.error(f"[exotel-gemini] Connection error: {e}")
    finally:
        # ── Post-Call: Summary, Lead Qualification, Cleanup ───────────
        raw_transcript = "\n".join(full_call_transcript)

        # Generate summary using Gemini
        concise_summary = await generate_call_summary_gemini(raw_transcript, session_id)
        await db_store.log_call_end(
            session_id, concise_summary or raw_transcript[:500], tenant_id=tenant_id
        )
        await tenant_store.record_usage(
            tenant_id=tenant_id,
            event_type="call_ended",
            session_id=session_id,
            agent_id=(exotel_agent or {}).get("id"),
            channel="exotel",
            scenario_key=scenario_key,
            provider="gemini",
            model=GEMINI_LIVE_MODEL,
            duration_seconds=int(max(0, time.time() - call_started_at)),
            metadata={"phone_number": phone_number},
        )

        # Extract lead qualification for real estate calls
        if scenario_key == "real_estate_lead" and raw_transcript.strip():
            asyncio.create_task(extract_lead_qualification_post_call(raw_transcript, phone_number, session_id, db_store))

        await redis_cache.set_session(session_id, {"status": "completed"}, expire_seconds=300)

        # ── Update Reminder Contacts Call History ─────────────────────
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
                    logger.error(f"[exotel-gemini] DB call history error: {db_err}")
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

        # ── Cleanup pub/sub and maps ──────────────────────────────────
        if active_call_sid:
            cleanup_transcript_pubsub(active_call_sid)
            _active_call_sid_map.pop(active_call_sid, None)
            _active_call_start_times.pop(active_call_sid, None)

        logger.info(f"[exotel-gemini] Session {session_id} finalized and saved")


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("GEMINI_PORT", 8086))
    logger.info(f"🚀 Starting Gemini Live Bridge server on port {port}...")
    uvicorn.run("test_realtime_gemini:app", host="0.0.0.0", port=port, reload=True)

