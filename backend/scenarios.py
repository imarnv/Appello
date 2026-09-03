"""
Appello — Scenario Registry
Loads agent prompt files from the `agents/` directory and builds the SCENARIOS dict.
Each .txt file has a YAML-like frontmatter (speaker, welcome, language_rules) separated
by '---' from the full system prompt body.

Adding a new agent:
  1. Create bridge/agents/new_agent.txt with frontmatter + prompt
  2. If the agent needs tools, register them in tools.py
  3. Done — this module auto-discovers the file on import.
"""

import logging
import os
from typing import Dict, Any, Optional

logger = logging.getLogger("appello")


# ─── Shared Instruction Blocks ──────────────────────────────────────────
# Appended to every agent's instructions based on their language_rules setting.

LANGUAGE_RULES = """
LANGUAGE RULES (CRITICAL — MUST FOLLOW):
- DYNAMIC LANGUAGE SWITCHING: The user's language is dynamic. You MUST analyze the user's LATEST turn.
- INSTANT SCRIPT SWITCHING:
  * If the user switches languages, you MUST switch with them on your VERY NEXT reply. NEVER get locked in a language.
  * If the user's latest turn is in Hindi or Hinglish → your reply MUST be in Devanagari script (Hinglish).
  * If the user's latest turn is in Tamil or Tanglish → your reply MUST be in Tamil script (Tanglish).
  * Do NOT copy the language or script of your previous assistant responses if the user has changed their language. The user's latest utterance always overrides history.
- ALWAYS code-mix with English (40-60% of the words should be everyday English words):
  * For Hindi/Hinglish → write in Devanagari script. Transliterate all English words into Devanagari script.
    EXAMPLE RESPONSE STYLE (Hinglish):
    ✅ "जी सर, आपका EMI पेमेंट पेंडिंग है। क्या आप UPI से पे करेंगे?"
    ✅ "ओके, मैं आपको सेवन डे एक्सटेंशन दे सकता हूँ।"
    ❌ WRONG (pure Hindi): "जी श्रीमान, आपका ऋण भुगतान शेष है। क्या आप एकीकृत भुगतान से करेंगे?"
    ❌ WRONG (pure Hindi): "धन्यवाद बताने के लिए" → USE "थैंक्स बताने के लिए"
  * For Tamil/Tanglish → write in Tamil script. Transliterate all English words into Tamil script.
    EXAMPLE RESPONSE STYLE (Tanglish):
    ✅ "ஹாய், உங்க கார்ட்ல சில ஐட்டம்ஸ் பெண்டிங்ல இருக்கு. செக் பண்ணலாமா?"
    ✅ "ஓகே, நான் டிஸ்கவுண்ட் கோட் அப்ளை பண்றேன்।"
    ❌ WRONG (pure/literary Tamil): "வணக்கம், உங்கள் கடன் செலுத்துதல் நிலுவையில் உள்ளது."
    ❌ WRONG (pure Tamil): "நன்றி சொன்னதற்கு" → USE "தேங்க்ஸ் சொன்னதற்கு"
  * If the user spoke in Telugu → reply in Telugu script with 40-60% English code-mixing.
  * If the user spoke in Kannada → reply in Kannada script with 40-60% English code-mixing.
  * If the user spoke in English → reply in English.
- KEY PRINCIPLE: Talk like an educated urban Indian professional on a phone call. Use English for all technical, business, and modern terms. Use the regional language for connectors, pronouns, and casual phrasing.
- NEVER use heavy literary, classical, or textbook vocabulary in any regional language.
- Stick to ONE script per response. Do not mix Devanagari and Latin scripts in a single reply.
- NEVER announce language transitions.
- Shape responses as 1-2 short sentences (max 6-8 words each), separated by periods. This is CRITICAL for low-latency TTS pipelining.
- SPEAKING PACE: You MUST speak at a very fast, brisk, and highly energetic pace (equivalent to 1.25x normal speed) to sound completely natural, lively, and alert. Avoid dragging your words or talking slowly. Keep your speech fast and brief.
- NO MECHANICAL ACKNOWLEDGMENTS: You MUST NEVER start your responses with mechanical acknowledgment fillers such as "Okay", "Got it", "Sure", "Understood", "Right", "Okay, got it", etc. Answer the user directly without any introductory conversational fillers.
"""

LANGUAGE_RULES_REAL_ESTATE = """
DYNAMIC LANGUAGE RULES (CRITICAL — MUST FOLLOW):
1. STICK TO THE FIRST CHOSEN LANGUAGE (STICK TO GREETING LANGUAGE):
   - You MUST stick to the language of your welcome greeting (the first chosen language, e.g., Tamil/Tanglish or Hindi/Hinglish) as your primary conversation language.
   - If the user speaks in English, do NOT switch to pure English, and do NOT switch to a different regional language (e.g., do not switch from Tanglish to Hinglish). Continue replying in the first chosen language (e.g. Tanglish/Tamil if you started in Tamil).
   - Do NOT consider English input as a trigger to change languages. Stick to the greeting's language unless the user deliberately speaks in a different regional language.
2. DYNAMIC REGIONAL LANGUAGE SWITCHING:
   - If (and only if) the user explicitly and deliberately switches the conversation to a different regional language (other than English or your current language), switch your response to that language:
     * User deliberately speaks Hindi/Hinglish → reply in Hinglish (Devanagari script).
     * User deliberately speaks Tamil/Tanglish → reply in Tanglish (Tamil script).
     * User deliberately speaks Telugu → reply in Telugu (code-mixed).
     * User deliberately speaks Kannada → reply in Kannada (code-mixed).
   - If the user switches back, switch your language instantly. Do not continue in the old language.
3. NO PURE ENGLISH SENTENCES:
   - You MUST NEVER speak or generate a single sentence entirely in English. Every single response sentence must be code-mixed, containing both regional language words/connectors and everyday English terms.
4. STYLE & CONVERSATIONAL NATURALNESS (CRITICAL):
   - COLLOQUIAL LOCAL PHRASING: You MUST use natural, everyday colloquial phrasing (Hinglish, Tanglish, etc.). NEVER use hard, formal, classical, or textbook regional language words. Use simple, common words that people use in daily life conversations.
   - BREVITY IS KING: Keep EVERY response to MAX 1-2 short sentences. Answer ONLY what the user asked — nothing more. Do NOT volunteer extra info.
     - User asks "swimming pool hai?" → "Haan, half-Olympic size swimming pool hai." DONE. Do NOT also mention gym, garden, etc.
     - User asks "2 BHK price?" → "2 BHK 52 lakhs se start hota hai." DONE. Do NOT also list 3 BHK prices.
     - User asks "location kahan hai?" → "Guduvancheri mein, GST Road pe. Guduvancheri station se 5 minute." DONE.
   - Speak like a friendly human sales executive. Skip mechanical fillers like "Understood", "Sure", "Okay", "Got it" — just answer directly.
   - NEVER talk to yourself or repeat back what the user said.
   - Ask only ONE question at a time. Never list checklist items or options.
   - PRONUNCIATION / ACCENT: Whenever you speak any English word in the middle of any sentence, you MUST pronounce it using a natural Indian English accent. Do NOT use an American or British accent for English words.
   - NO MECHANICAL ACKNOWLEDGMENTS: You MUST NEVER start your responses with mechanical acknowledgment fillers such as "Okay", "Got it", "Sure", "Understood", "Right", "Okay, got it", etc. Answer the user directly without any introductory conversational fillers.
- SPEAKING PACE: You MUST speak at a very fast, brisk, and highly energetic pace (equivalent to 1.25x normal speed) to sound completely natural, lively, and alert. Avoid dragging your words or talking slowly. Keep your speech fast and brief.
"""

LANGUAGE_RULES_ENGLISH = """
LANGUAGE RULES (CRITICAL — MUST FOLLOW):
- You MUST speak only in complete, standard, natural English.
- Do NOT use any regional Indic languages or scripts.
"""


TAMIL_ONLY_LANGUAGE_RULES = """
LANGUAGE RULES (CRITICAL — MUST FOLLOW):
- You MUST reply ONLY in Tamil script (Tanglish — Tamil script with everyday English words transliterated into Tamil script).
- You MUST NEVER speak or generate a single sentence entirely in English. Under no circumstances should any response sentence be purely English.
- Every single sentence you output must be code-mixed, containing everyday regional Tamil words and connectors along with English terms.
- COLLOQUIAL LOCAL PHRASING: You MUST use natural, everyday colloquial phrasing. NEVER use hard, formal, classical, or textbook Tamil words (e.g. do not use அனுக்கிரகம், நிதி, கருத்து, திருப்தி, தயவுசெய்து, கொள்முதல், வழங்கல்). Use simple, common words that people use in daily life conversations.
- Transliterate all English words into Tamil script.
  EXAMPLE RESPONSE STYLE (Tanglish):
  ✅ "வணக்கம், நான் Ratan, Sunrise Company-ல இருந்து பேசுறேன்."
  ✅ "ஓகே, நான் feedback-ஐ record பண்றேன்."
  ❌ WRONG (pure English): "Hello, this is Ratan from Sunrise Company."
  ❌ WRONG (pure/literary Tamil): "வணக்கம், Sunrise Company-லிருந்து அழைக்கிறேன்."
- Keep responses concise: 2-3 short sentences max.
- PRONUNCIATION / ACCENT: Whenever you speak any English word in the middle of any sentence, you MUST pronounce it using a natural Indian English accent. Do NOT use an American or British accent for English words.
- NO MECHANICAL ACKNOWLEDGMENTS: You MUST NEVER start your responses with mechanical acknowledgment fillers such as "Okay", "Got it", "Sure", "Understood", "Right", "Okay, got it", etc. Answer the user directly without any introductory conversational fillers.
- SPEAKING PACE: You MUST speak at a very fast, brisk, and highly energetic pace (equivalent to 1.25x normal speed) to sound completely natural, lively, and alert. Avoid dragging your words or talking slowly. Keep your speech fast and brief.
"""

NO_REGREET_RULE = """
NO RE-GREETING RULE:
- The opening greeting has already been spoken automatically. The first audio you hear is the user's REPLY to that greeting.
- You are mid-call from your very first response. NEVER greet, introduce yourself, or ask identity-confirmation questions.
- If the user pauses or is unclear, politely ask them to repeat. NEVER restart with a greeting.
"""

ACTIVE_LISTENING_RULE = """
ACTIVE LISTENING & MEMORY RULES:
- Answer the user's actual question FIRST. Then steer back to your goal.
- You already introduced yourself ONCE at the start of the call. NEVER re-introduce yourself, your name, or your company again unless the user explicitly asks for it. Just continue the conversation naturally.
- NEVER repeat a sentence you already said. Rephrase or move forward.
- If you don't know a specific fact, say so honestly — never invent numbers.
- End every turn with a concrete next step or a clarifying question.
"""


# ─── Scenario Loader ────────────────────────────────────────────────────

def _parse_agent_file(filepath: str) -> Dict[str, Any]:
    """Parse a single agent .txt file into a scenario config dict."""
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    # Split on first '---' separator
    if "---" not in content:
        raise ValueError(f"Agent file {filepath} missing '---' separator between frontmatter and prompt body")

    parts = content.split("---", 1)
    header_text = parts[0].strip()
    prompt_body = parts[1].strip()

    # Parse frontmatter (simple key: value pairs)
    config = {}
    for line in header_text.splitlines():
        line = line.strip()
        if ":" in line:
            key, value = line.split(":", 1)
            config[key.strip()] = value.strip()

    speaker = config.get("speaker", "kabir")
    welcome = config.get("welcome", "")
    language_rules = config.get("language_rules", "multilingual")
    # use_native_audio removed — all agents use WebSocket + Sarvam TTS pipeline

    # Append shared rules based on language_rules setting
    filename = os.path.basename(filepath)
    if filename == "real_estate_lead.txt":
        full_instructions = prompt_body + NO_REGREET_RULE + ACTIVE_LISTENING_RULE + LANGUAGE_RULES_REAL_ESTATE
    elif language_rules == "tamil_only":
        full_instructions = prompt_body + NO_REGREET_RULE + ACTIVE_LISTENING_RULE + TAMIL_ONLY_LANGUAGE_RULES
    elif filename == "restaurant_booking.txt":
        full_instructions = prompt_body + NO_REGREET_RULE + ACTIVE_LISTENING_RULE + LANGUAGE_RULES_ENGLISH
    else:
        full_instructions = prompt_body + NO_REGREET_RULE + ACTIVE_LISTENING_RULE + LANGUAGE_RULES

    return {
        "speaker": speaker,
        "welcome": welcome,
        "instructions": full_instructions,
    }


def load_scenarios(agents_dir: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """Load all agent .txt files from the agents/ directory into a SCENARIOS dict.
    Keys are the filename stems (e.g. 'feedback_agent', 'restaurant_booking')."""
    if agents_dir is None:
        agents_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agents")

    scenarios: Dict[str, Dict[str, Any]] = {}

    if not os.path.isdir(agents_dir):
        logger.warning(f"[scenarios] Agents directory not found: {agents_dir}")
        return scenarios

    for filename in sorted(os.listdir(agents_dir)):
        if not filename.endswith(".txt"):
            continue
        scenario_key = filename[:-4]  # strip .txt
        filepath = os.path.join(agents_dir, filename)
        try:
            scenarios[scenario_key] = _parse_agent_file(filepath)
            logger.info(f"[scenarios] Loaded agent: {scenario_key} (speaker={scenarios[scenario_key]['speaker']})")
        except Exception as e:
            logger.error(f"[scenarios] Failed to load {filename}: {e}")

    return scenarios


# ─── Module-level singleton ─────────────────────────────────────────────
# Loaded once on import. All other modules import SCENARIOS from here.
SCENARIOS = load_scenarios()

if not SCENARIOS:
    logger.warning("[scenarios] No agent files found! The system will have no scenarios available.")
else:
    logger.info(f"[scenarios] {len(SCENARIOS)} scenario(s) loaded: {list(SCENARIOS.keys())}")
