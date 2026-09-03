"""
Appello — Tool Definitions & Execution
Azure OpenAI Realtime function-calling tool schemas and the execute_tool() dispatcher.
Only scenarios that need mid-call data fetching define tools here.
"""

import asyncio
import json
import logging
from typing import Dict

logger = logging.getLogger("appello")

# ── KBEngine singleton (injected from main.py at startup) ─────────────
_kb_engine = None

def set_kb_engine(engine):
    """Called from main.py to inject the shared KBEngine instance."""
    global _kb_engine
    _kb_engine = engine

def _get_kb_engine():
    return _kb_engine

# Per-scenario KB search shape. The default suits the small hand-curated
# collections: the globally best 3 chunks.
#
# ggs_support is different. Its collection now holds both the 9 short original
# GGS docs and the E-Canter technical library (~2,000 manual pages, ~5,700
# chunks). Flat top-k there lets the big manuals bury the small ones — a "how do
# I jump start the truck" query filled all 6 slots with mediocre DTOM pages about
# ignition switches and never surfaced jump start.pdf at all. Grouping by
# filename asks for the best 2 chunks from each of the 4 best-matching
# documents, which keeps depth where the answer is while guaranteeing a short
# document can still be seen. Requires the keyword payload index on `filename`
# that ingest_manuals.py creates.
#
# `expand_top_pages` then serves the best two matches as their COMPLETE source
# page rather than the single chunk that scored highest. A periodic inspection
# table or a repair procedure spans several chunks, so without this the agent
# reads out part of a schedule and stops — which on a technical support line is
# worse than saying nothing.
_KB_SEARCH: Dict[str, dict] = {
    "ggs_support": {
        "top_k": 4,
        "group_by": "filename",
        "group_size": 2,
        # Only the best match is served as its whole page. Expanding two put two
        # multi-thousand-character pages in every tool response, which is the
        # single largest contributor to the context growth described below.
        "expand_top_pages": 1,
    },
}
_KB_SEARCH_DEFAULT = {"top_k": 3}

# Ceiling on the text one search hands the model, in characters.
#
# Tool responses stay in the Live session's history, so every search permanently
# enlarges the context for the rest of the call. Unbounded, five questions in
# this corpus accumulated ~19,000 tokens of manual text, and a realtime model's
# time-to-first-token climbs steeply with context — heard by the caller as the
# agent going quiet after "let me check" from roughly the fourth question on.
#
# The budget is spent top-down, so the best-matching passage (the whole stitched
# page, which holds the actual answer) is always sent intact and the weaker
# supporting passages are what get dropped.
_KB_CONTEXT_BUDGET = 6000
_KB_MIN_PASSAGES = 2


def _fit_to_budget(results: list) -> list:
    """Trim retrieved passages to a context budget, best-first.

    Always keeps `_KB_MIN_PASSAGES` even if oversized — an answer that is too
    long beats no answer — then admits further passages only while they fit.
    """
    blocks = []
    used = 0
    for i, r in enumerate(results):
        text = r["text"]
        if i >= _KB_MIN_PASSAGES and used + len(text) > _KB_CONTEXT_BUDGET:
            continue
        blocks.append({"text": text, "source": r["source"], "relevance": r["score"]})
        used += len(text)
    return blocks

# Shared KB tool schema used across multiple scenarios
_QUERY_KB_TOOL = {
    "type": "function",
    "name": "query_knowledge_base",
    "description": "Search the knowledge base for relevant information. Use this when the customer asks about specific details like prices, policies, menu items, opening hours, or any factual information that might be in uploaded documents. Returns the most relevant text passages.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query describing what information is needed, e.g. 'price of butter chicken', 'restaurant opening hours', 'cancellation policy'"
            }
        },
        "required": ["query"]
    }
}


# ─── Tool Schemas (Azure OpenAI Realtime function calling) ───────────────

SCENARIO_TOOLS: Dict[str, list] = {
    "restaurant_booking": [
        {
            "type": "function",
            "name": "check_table_availability",
            "description": "Check available table/time slots at The Royal Plate on a given date. Call this whenever the customer asks about table availability or open slots. Optionally filter by party size.",
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {
                        "type": "string",
                        "description": "Date to check availability for, in YYYY-MM-DD format. Convert relative dates like 'tomorrow', 'this Saturday' to this format."
                    },
                    "party_size": {
                        "type": "integer",
                        "description": "Number of guests. Used to filter tables that can accommodate the party. Omit to see all available slots."
                    },
                    "meal_period": {
                        "type": "string",
                        "description": "'lunch' or 'dinner'. Omit to see both."
                    }
                },
                "required": ["date"]
            }
        },
        {
            "type": "function",
            "name": "reserve_table",
            "description": "Reserve a table for the customer. Only call this after confirming all details (name, date, time, party size) with the customer.",
            "parameters": {
                "type": "object",
                "properties": {
                    "customer_name": {
                        "type": "string",
                        "description": "Full name of the customer"
                    },
                    "date": {
                        "type": "string",
                        "description": "Reservation date in YYYY-MM-DD format"
                    },
                    "time": {
                        "type": "string",
                        "description": "Reservation time slot, e.g. '7:00 PM', '8:30 PM'"
                    },
                    "party_size": {
                        "type": "integer",
                        "description": "Number of guests dining"
                    },
                    "special_requests": {
                        "type": "string",
                        "description": "Any special requests like birthday decoration, anniversary, dietary restrictions, window seating preference, etc."
                    }
                },
                "required": ["customer_name", "date", "time", "party_size"]
            }
        },
        {
            "type": "function",
            "name": "pre_order_food",
            "description": "Record a pre-order so the kitchen can prepare dishes before the customer arrives. Call this after confirming items, quantities, and arrival time with the customer.",
            "parameters": {
                "type": "object",
                "properties": {
                    "customer_name": {
                        "type": "string",
                        "description": "Full name of the customer"
                    },
                    "items": {
                        "type": "string",
                        "description": "Comma-separated list of ordered items with quantities, e.g. '2x Truffle Mushroom Galouti, 1x Royal Dum Biryani (Chicken), 1x Dal Makhani'"
                    },
                    "total_amount": {
                        "type": "string",
                        "description": "Total estimated cost, e.g. '₹1,890'"
                    },
                    "arrival_time": {
                        "type": "string",
                        "description": "Expected arrival time, e.g. '8:00 PM'"
                    },
                    "date": {
                        "type": "string",
                        "description": "Date of visit in YYYY-MM-DD format"
                    }
                },
                "required": ["customer_name", "items", "total_amount", "arrival_time", "date"]
            }
        },
        {
            "type": "function",
            "name": "get_my_bookings",
            "description": "Retrieve existing reservations for the caller's phone number.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        },
        _QUERY_KB_TOOL,
    ],
    "feedback_agent": [
        {
            "type": "function",
            "name": "record_feedback",
            "description": "Record structured customer feedback after collecting all three data points: product review, satisfaction level, and overall experience. Call this ONCE after the customer has answered all feedback questions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "customer_name": {
                        "type": "string",
                        "description": "Full name of the customer being called"
                    },
                    "product_review": {
                        "type": "string",
                        "description": "Customer's feedback about the product quality and satisfaction, in whatever language they spoke"
                    },
                    "satisfaction_level": {
                        "type": "string",
                        "enum": ["positive", "negative", "neutral"],
                        "description": "Overall satisfaction: 'positive' if score > 7 or happy, 'negative' if score <= 5 or complaints, 'neutral' if score 6-7 or mixed"
                    },
                    "overall_experience": {
                        "type": "string",
                        "description": "Customer's feedback about the overall purchase experience (delivery, packaging, service), in whatever language they spoke"
                    },
                    "call_summary": {
                        "type": "string",
                        "description": "A concise 200 character actual summary of the entire conversation."
                    },
                    "escalation_required": {
                        "type": "boolean",
                        "description": "Set to true if ANY negative feedback was received that needs Regional Manager attention"
                    }
                },
                "required": ["customer_name", "product_review", "satisfaction_level", "overall_experience", "call_summary", "escalation_required"]
            }
        },
    _QUERY_KB_TOOL,
    ],
    "real_estate_lead": [{
            "type": "function",
            "name": "record_lead_qualification",
            "description": "Record the lead qualification outcome. Call this once you have finished the lead qualification checklist or if the lead explicitly states they are not interested/wrong number.",
            "parameters": {
                "type": "object",
                "properties": {
                    "lead_name": {
                        "type": "string",
                        "description": "Full name of the lead"
                    },
                    "interest_status": {
                        "type": "string",
                        "enum": ["interested", "not_interested", "wrong_number", "junk"],
                        "description": "Whether the lead is interested or not, wrong number, or junk (no answer/hangup)"
                    },
                    "qualification_stage": {
                        "type": "string",
                        "enum": ["hot", "warm", "junk"],
                        "description": "Hot: engaged in Q&A + budget fits + timeline under 30 days + agreed to site visit. Warm: engaged but no visit commitment, or timeline unclear. Junk: no answer, hangup, explicit disinterest, wrong number, or budget mismatch."
                    },
                    "budget_range": {
                        "type": "string",
                        "description": "Customer's budget range, if provided (e.g. '80 Lakhs - 1 Crore', 'Under 50 Lakhs')"
                    },
                    "purchase_timeline": {
                        "type": "string",
                        "description": "Purchase timeline (e.g. 'within 30 days', '2-3 months', 'unclear')"
                    },
                    "funding_mode": {
                        "type": "string",
                        "enum": ["loan", "cash", "unspecified"],
                        "description": "Mode of funding: bank loan, self-funded/cash, or unspecified"
                    },
                    "site_visit_date": {
                        "type": "string",
                        "description": "Specific date/time for the site visit (e.g. '2026-07-15 11:00 AM') if agreed. Do not infer; must be explicitly confirmed."
                    },
                    "objection_reason": {
                        "type": "string",
                        "description": "Reason for objection or disinterest (e.g., 'already purchased', 'wrong number', 'just browsing', 'too expensive', 'rescheduled')"
                    },
                    "callback_time": {
                        "type": "string",
                        "description": "Reschedule callback time if they requested to call later"
                    },
                    "call_summary": {
                        "type": "string",
                        "description": "Brief summary of the call and discussion details."
                    }
                },
                "required": ["lead_name", "interest_status", "qualification_stage", "call_summary"]
            }
        },
    ],
    "fsecure_support": [
        _QUERY_KB_TOOL,
    ],
    "ggs_support": [
        _QUERY_KB_TOOL,
    ],
}


async def execute_tool(tool_name: str, args: dict, session_id: str, phone_number: str, db_store, scenario_key: str = "") -> str:
    """Execute a tool call from Azure OpenAI and return the result as a JSON string.
    
    Args:
        tool_name: Name of the function to execute
        args: Parsed arguments from Azure
        session_id: Current call session ID
        phone_number: Caller's phone number
        db_store: PostgresStore instance for database operations
        scenario_key: Which scenario is active (for context-aware responses)
    """
    try:
        if tool_name == "check_table_availability":
            slots = await db_store.get_restaurant_availability(
                slot_date=args.get("date"),
                party_size=args.get("party_size"),
                meal_period=args.get("meal_period"),
            )
            if slots:
                return json.dumps({"available_tables": slots, "count": len(slots)})
            else:
                return json.dumps({"available_tables": [], "count": 0, "message": "No tables available for the requested criteria."})

        elif tool_name == "reserve_table":
            result = await db_store.reserve_table(
                customer_name=args.get("customer_name", "Guest"),
                phone_number=phone_number,
                slot_date=args.get("date"),
                slot_time=args.get("time"),
                party_size=args.get("party_size", 2),
                special_requests=args.get("special_requests", ""),
                session_id=session_id,
            )
            return json.dumps(result)

        elif tool_name == "pre_order_food":
            result = await db_store.pre_order_food(
                customer_name=args.get("customer_name", "Guest"),
                phone_number=phone_number,
                items=args.get("items", ""),
                total_amount=args.get("total_amount", ""),
                arrival_time=args.get("arrival_time", ""),
                slot_date=args.get("date", ""),
                session_id=session_id,
            )
            return json.dumps(result)

        elif tool_name == "get_my_bookings":
            bookings = await db_store.get_customer_bookings(phone_number)
            if bookings:
                return json.dumps({"bookings": bookings, "count": len(bookings)})
            else:
                return json.dumps({"bookings": [], "count": 0, "message": "No existing reservations found for this number."})

        elif tool_name == "record_feedback":
            customer_name = args.get("customer_name", "Unknown")
            product_review = args.get("product_review", "")
            satisfaction_level = args.get("satisfaction_level", "neutral")
            overall_experience = args.get("overall_experience", "")
            call_summary = args.get("call_summary", "")
            escalation_required = args.get("escalation_required", False)

            if db_store:
                asyncio.create_task(db_store.update_feedback_agent_log(
                    session_id=session_id,
                    product_review=product_review,
                    satisfaction_level=satisfaction_level,
                    overall_experience=overall_experience,
                    call_summary=call_summary,
                    escalation_required=escalation_required,
                ))

            logger.info(f"[tool] Feedback recorded for {customer_name}: satisfaction={satisfaction_level}, escalation={escalation_required}")
            return json.dumps({
                "status": "success",
                "message": f"Feedback for {customer_name} has been recorded successfully.",
                "escalation_required": escalation_required,
            })

        elif tool_name == "record_lead_qualification":
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

            if db_store:
                asyncio.create_task(db_store.update_feedback_agent_log(
                    session_id=session_id,
                    product_review=f"Interest: {interest_status}, Budget: {budget_range}, Timeline: {purchase_timeline}, Funding: {funding_mode}",
                    satisfaction_level="positive" if interest_status == "interested" else "negative",
                    overall_experience=f"Site visit: {site_visit_date}, Objection: {objection_reason}, Callback: {callback_time}",
                    call_summary=call_summary,
                    escalation_required=(qualification_stage == "hot"),
                ))

            logger.info(f"[tool] Lead qualification recorded for {lead_name}: stage={qualification_stage}, interest={interest_status}")

            escalation_required = (qualification_stage == "hot")
            return json.dumps({
                "status": "success",
                "message": f"Lead qualification for {lead_name} recorded. Stage: {qualification_stage}.",
                "lead_name": lead_name,
                "qualification_stage": qualification_stage,
                "escalation_required": escalation_required,
            })

        elif tool_name == "query_knowledge_base":
            query = args.get("query", "")
            if not query:
                return json.dumps({"error": "query parameter is required"})

            # Use KBEngine for real Qdrant vector search (per-agent collection)
            try:
                from kb_engine import KBEngine
                kb = _get_kb_engine()
                if kb:
                    try:
                        results = await kb.search(
                            query,
                            agent_type=scenario_key,
                            **_KB_SEARCH.get(scenario_key, _KB_SEARCH_DEFAULT),
                        )
                    except Exception as search_err:
                        # The lookup itself broke — a network fault, not an empty
                        # shelf. Saying "we have no documentation on that" here
                        # would be a lie the caller cannot tell from the truth,
                        # so the agent is told to retry instead.
                        logger.error(f"[tool] KB lookup FAILED (not a miss) for '{query[:50]}': {search_err}")
                        return json.dumps({
                            "found": False,
                            "error": "lookup_unavailable",
                            "context": [],
                            "message": (
                                "The knowledge base lookup did not complete — this is a "
                                "temporary connection problem, NOT a sign the information "
                                "is missing. Tell the caller you are having a brief "
                                "technical issue retrieving it and try the search once more. "
                                "Do NOT tell them the documentation does not exist."
                            ),
                        })
                    if results:
                        context_blocks = _fit_to_budget(results)
                        logger.info(
                            f"[tool] KB search '{query[:50]}' → {len(context_blocks)}/{len(results)} passages, "
                            f"{sum(len(b['text']) for b in context_blocks):,} chars for {scenario_key}"
                        )
                        return json.dumps({
                            "found": True,
                            "context": context_blocks,
                            "message": "Here is relevant information from the knowledge base."
                        })

                # Fallback to mock data if no real database search results are found
                mock_context = []
                if scenario_key == "real_estate_lead":
                    mock_context = [
                        {
                            "text": "Estancia Apartments is located in Guduvancheri, Chennai, right on GST Road. It is just a 5-minute walk from Guduvancheri railway station, close to SRM University. Pricing: 2 BHK starts from ₹52 Lakhs, 3 BHK starts from ₹75 Lakhs. Amenities include a swimming pool, gym, clubhouse, power backup, and landscaped gardens.",
                            "source": "estancia_brochure.txt",
                            "relevance": 0.95
                        },
                        {
                            "text": "Raunaq Avinya Villa in Chrompet is a premium residential independent villa project located near Chennai Airport. Pricing starts from ₹1.2 Crores. Amenities include a fully equipped gym, clubhouse, private terrace garden, 24/7 security, gated community, and CCTV surveillance.",
                            "source": "chrompet_villa_brochure.txt",
                            "relevance": 0.95
                        },
                        {
                            "text": "Individual House in Anna Nagar is located in a premium residential area near MGM Healthcare Hospital. It is a spacious, modern 4 BHK independent individual house. Pricing starts from ₹3.5 Crores. Specifications include private parking garage, modular kitchen, high-end marble flooring, terrace garden, and gated community security.",
                            "source": "annanagar_house_brochure.txt",
                            "relevance": 0.95
                        }
                    ]
                elif scenario_key == "restaurant_booking":
                    mock_context = [
                        {
                            "text": "Butter Chicken is priced at ₹320. Garlic Naan is priced at ₹80. Paneer Tikka is priced at ₹280. Veg Biryani is ₹250.",
                            "source": "restaurant_menu.txt",
                            "relevance": 0.95
                        },
                        {
                            "text": "The Royal Plate hours: Lunch is served from 12 PM to 3 PM. Dinner is served from 7 PM to 11 PM daily. Located in Indiranagar, Bangalore.",
                            "source": "restaurant_info.txt",
                            "relevance": 0.90
                        }
                    ]
                
                if mock_context:
                    logger.info(f"[tool] KB search returned mock fallback results for {scenario_key}")
                    return json.dumps({
                        "found": True,
                        "context": mock_context,
                        "message": "Here is relevant information from the knowledge base (mock fallback)."
                    })

                logger.warning(f"[tool] KB search returned no results for: {query[:60]}")
                return json.dumps({
                    "found": False,
                    "context": [],
                    "message": "No relevant information found in the knowledge base for this query."
                })
            except Exception as e:
                logger.error(f"[tool] KB query failed: {e}")
                return json.dumps({
                    "found": False,
                    "context": [],
                    "message": f"Knowledge base search failed: {str(e)}"
                })

        else:
            return json.dumps({"error": f"Unknown tool: {tool_name}"})
    except Exception as e:
        logger.error(f"[tool] Execution error for {tool_name}: {e}")
        return json.dumps({"error": f"Tool execution failed: {str(e)}"})
