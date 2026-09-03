/**
 * Scripted demo conversations.
 *
 * These mirror the agents in `backend/` one for one: same businesses, same
 * people, same knowledge. Four of the five keep their facts in the system
 * prompt and are sent no tool declarations, so every answer comes from the
 * knowledge block named in the citation — those block names are the real
 * section headings from the prompts.
 *
 * ggs_support is the exception. It is given two tool declarations,
 * initialize_search and fetch_search_results, and answers out of a Qdrant
 * collection of 5,753 chunks drawn from 19 commercial-vehicle service
 * documents. Its citations are real document names and page numbers, and the
 * figures in its scripted turns are quoted from passages the live collection
 * returns.
 */

export type Turn = {
  who: "agent" | "caller";
  text: string;
  /** The knowledge block in the agent's prompt that the answer came from */
  cite?: { source: string; span: string };
  /** An outcome the agent commits to at the end of the call */
  action?: string;
  /** Milliseconds from end-of-speech to first audio out */
  latency?: number;
};

/** One entry in the caller-language picker. */
export type CallOption = {
  /** Picker code — also selects the Signature that shapes the 3D field. */
  code: string;
  /**
   * Backend `accent`. Only the restaurant agent branches on it: "indian"
   * pins language_code to en-IN, "american" leaves Gemini on its default.
   */
  accent?: "indian" | "american";
  /** Overrides the signature's dialect label on the chip. */
  dialect?: string;
  /**
   * What to put in the config message's `language` field, when that differs
   * from the picker code. The endpoint-security agent keys its greetings off
   * bare codes ("de", "fr", "ja"), while the picker needs the regional tag to
   * find a signature for the voice field.
   */
  backendLanguage?: string;
};

/** Stable identity for an option, since one code can appear twice. */
export const optionKey = (o: CallOption) => `${o.code}|${o.accent ?? ""}`;

export type Vertical = {
  id: string;
  /** Nav label */
  label: string;
  /** `scenario` value sent to the backend on the voice socket */
  scenario: string;
  /**
   * How this agent's branch reads the `language` field of the config message.
   * Most take a bare name ("english", "tamil"); ggs_support parses a BCP-47
   * code and keys its greeting table off it, so it gets the picker code as-is.
   */
  languageFormat?: "name" | "bcp47";
  /** The business the agent answers for */
  business: string;
  /** The person the agent plays */
  persona: string;
  /** One line on what this agent is for */
  premise: string;
  /** Where the agent runs */
  channel: string;
  /**
   * What the caller can pick. Taken from each agent's own LANGUAGE RULES — the
   * platform speaks 94, but an individual agent is deliberately scoped, and
   * offering more here would be a lie.
   */
  options: CallOption[];
  /** The knowledge the agent answers from */
  sources: string[];
  turns: Turn[];
};

export const VERTICALS: Vertical[] = [
  {
    id: "documents",
    label: "Document search",
    scenario: "ggs_support",
    // The scenario key stays `ggs_support`: it is the wire contract with the
    // deployed bridge. It reads `language` as a BCP-47 code, not a name.
    languageFormat: "bcp47",
    business: "Fleet Service Desk",
    persona: "Gaurav",
    premise:
      "Answers from 5,753 passages of authentic vehicle service documentation, out loud, mid-call.",
    channel: "SIP · inbound",
    // Every code here has a real greeting and a real language override in the
    // ggs_support branch of test_realtime_gemini.py. The agent searches in
    // English whatever the caller speaks, because the manuals are in English.
    options: [
      { code: "en-IN", dialect: "Indian" },
      { code: "hi-IN" },
      { code: "ta-IN" },
      { code: "te-IN" },
      { code: "ja-JP" },
      { code: "de-DE" },
    ],
    // The 19 documents actually embedded in the kb_ggs_support collection,
    // largest first. Bracketed headers on every passage carry the document,
    // page and section, which is what the agent cites.
    sources: [
      "Parts Catalogue",
      "Diagnostics Software Manual",
      "Service Manual",
      "Maintenance Manual",
      "Owner's Manual",
      "HEV & EV Systems Guide",
      "Damage Code Reference",
      "Service Bulletins",
      "Warranty Manual",
    ],
    turns: [
      { who: "agent", text: "", latency: 240 },
      {
        who: "caller",
        text: "I need the brake fluid replacement interval on the light-duty truck.",
      },
      {
        who: "agent",
        text: "One moment, let me pull up those details. Brake fluid takes a visual inspection at thirty, sixty, ninety and a hundred and twenty thousand kilometres, and a replacement every twenty-four months.",
        cite: {
          source: "Maintenance Manual — page 40",
          span: "PERIODIC INSPECTION SCHEDULE · replacement every 24 months",
        },
        latency: 780,
      },
      { who: "caller", text: "Which fluid should the workshop use?" },
      {
        who: "agent",
        text: "SAE J1703, FMVSS number 116, DOT3. Don't mix it with a different grade or with any mineral oil — the boiling point drops and the brake rubber parts swell, and the brakes stop working properly.",
        cite: {
          source: "Maintenance Manual — page 122",
          span: "48. BRAKE FLUID · recommended fluid and mixing warning",
        },
        latency: 640,
      },
      { who: "caller", text: "Anything to do once it's changed?" },
      {
        who: "agent",
        text: "Keep the level at the MAX mark on the tank throughout, and reset the maintenance information memory on the Multi-information system afterwards — every time, without fail.",
        cite: {
          source: "Maintenance Manual — page 122",
          span: "keep level at MAX · reset maintenance information memory",
        },
        action: "3 documents cited · 0 figures paraphrased",
        latency: 590,
      },
    ],
  },
  {
    id: "endpoint",
    label: "Endpoint security",
    scenario: "fsecure_support",
    // Same BCP-47 handling as the fleet desk, but this agent's greeting table
    // is keyed on bare codes for everything except English.
    languageFormat: "bcp47",
    business: "Endpoint Security Desk",
    persona: "Mohit",
    premise:
      "Walks a caller through a fix, one step at a time, out of 68 scraped support articles.",
    channel: "SIP · inbound",
    // Every code here has a real greeting and a real language override in the
    // fsecure_support branch. The picker code selects the voice field's
    // signature; backendLanguage is what the bridge is actually sent.
    options: [
      { code: "en-IN", dialect: "Indian" },
      { code: "fi-FI", backendLanguage: "fi" },
      { code: "sv-SE", backendLanguage: "sv" },
      { code: "de-DE", backendLanguage: "de" },
      { code: "nl-NL", backendLanguage: "nl" },
      { code: "fr-FR", backendLanguage: "fr" },
      { code: "ja-JP", backendLanguage: "ja" },
    ],
    // The article topics actually present in the scraped collection.
    sources: [
      "VPN connectivity",
      "Installation & updates",
      "Subscriptions & licences",
      "Account & password reset",
      "Device management",
      "Platform-specific fixes",
    ],
    turns: [
      { who: "agent", text: "", latency: 250 },
      {
        who: "caller",
        text: "I've forgotten my account password and I can't get in.",
      },
      {
        who: "agent",
        text: "Let me check the steps for that. Go to the login page and click \u201cForgot your password?\u201d, then enter your email address. A reset link comes to your inbox \u2014 check spam if it doesn't arrive \u2014 and you set a new password from there.",
        cite: {
          source: "Account & password reset",
          span: "Forgot your password? \u2192 email \u2192 reset link \u2192 new password",
        },
        latency: 690,
      },
      {
        who: "caller",
        text: "Got it. Is the new VPN any faster than the old one?",
      },
      {
        who: "agent",
        text: "The updated VPN technology focuses on speed, connectivity and reliability. It's designed to make your connection feel almost as fast as when you're not using a VPN at all, especially on nearby servers.",
        cite: {
          source: "VPN connectivity",
          span: "faster speeds \u00b7 connectivity \u00b7 reliability",
        },
        latency: 610,
      },
      { who: "caller", text: "That's all I needed, thanks." },
      {
        who: "agent",
        text: "Happy to help. Thank you for calling the Endpoint Security Desk \u2014 have a great day.",
        action: "2 articles cited \u00b7 resolved without escalation",
        latency: 240,
      },
    ],
  },
  {
    id: "restaurant",
    label: "Restaurant",
    scenario: "restaurant_booking",
    business: "The Royal Plate",
    persona: "David",
    premise:
      "Takes bookings and answers menu questions while the floor is full.",
    channel: "SIP · inbound",
    // "Speak ONLY in natural, standard English." The accent is the one thing
    // that varies: the branch pins en-IN for Indian, and leaves Gemini on its
    // own default for American.
    options: [
      { code: "en-IN", accent: "indian", dialect: "Indian" },
      { code: "en-IN", accent: "american", dialect: "American" },
    ],
    sources: [
      "RESTAURANT DETAILS",
      "CHEF'S SPECIAL",
      "POPULAR NON-VEG ITEMS",
      "SPECIAL OCCASIONS",
    ],
    turns: [
      { who: "agent", text: "", latency: 210 },
      {
        who: "caller",
        text: "Do you have a table for six this Saturday, around eight?",
      },
      {
        who: "agent",
        text: "Saturday at eight works nicely — dinner runs until eleven. Could I take your name?",
        cite: {
          source: "RESTAURANT DETAILS",
          span: "Dinner 7–11 PM · 80 seats · 2, 4, 6, 8-seater tables",
        },
        latency: 290,
      },
      { who: "caller", text: "It's Arnav. What's the chef's special tonight?" },
      {
        who: "agent",
        text: "The Chef's Special Thali is twelve ninety-nine — paneer lababdar, dal makhani, naan, rice and gulab jamun.",
        cite: {
          source: "CHEF'S SPECIAL",
          span: "Chef's Special Thali (Veg) — ₹1,299",
        },
        latency: 340,
      },
      { who: "caller", text: "Lovely. It's my wife's birthday, actually." },
      {
        who: "agent",
        text: "Oh wonderful — we'll arrange a complimentary dessert and table decoration. Six of you, Saturday at eight. Valet parking is on us.",
        cite: {
          source: "SPECIAL OCCASIONS",
          span: "birthday → complimentary dessert + table decoration",
        },
        action: "table held · 6 guests · Sat 8:00 PM",
        latency: 260,
      },
    ],
  },
  {
    id: "realestate",
    label: "Real estate",
    scenario: "real_estate_lead",
    business: "Urban Rise",
    persona: "Maya",
    premise: "Qualifies enquiries and books site visits without a callback.",
    channel: "SIP · outbound",
    // "Speak in natural Tamil mixed with English words."
    options: [{ code: "ta-IN" }],
    sources: [
      "PROJECT KNOWLEDGE — Estancia",
      "Pricing",
      "Amenities",
      "Raunaq Avinya Villa",
    ],
    turns: [
      { who: "agent", text: "", latency: 190 },
      { who: "caller", text: "Yes, speaking. You're calling about Estancia?" },
      {
        who: "agent",
        text: "That's right — Maya from Urban Rise. Estancia is fifteen acres with sixty percent open area, in Guduvancheri right on GST Road.",
        cite: {
          source: "PROJECT KNOWLEDGE — Estancia",
          span: "Guduvancheri, GST Road, Chennai · 15 acres · 60% open",
        },
        latency: 240,
      },
      { who: "caller", text: "What are you asking for a three-bedroom?" },
      {
        who: "agent",
        text: "Three BHK starts at seventy-five lakhs, two BHK at fifty-two. There's a pickleball court, rooftop garden and a half-olympic pool included.",
        cite: {
          source: "Pricing · Amenities",
          span: "2BHK from ₹52L · 3BHK from ₹75L · pool free of charge",
        },
        latency: 310,
      },
      { who: "caller", text: "Could I see it on Saturday morning?" },
      {
        who: "agent",
        text: "Saturday morning is open. I'll send the floor plans and price sheet on WhatsApp before you come.",
        action: "site visit · Saturday AM · brochure sent",
        latency: 220,
      },
    ],
  },
  {
    id: "payments",
    label: "Payment follow-up",
    scenario: "payment_followup",
    business: "Easy Loans App",
    persona: "Mohan",
    premise:
      "Chases an overdue EMI politely, and offers a way to settle it there and then.",
    channel: "SIP · outbound",
    // The only agent with a language branch: hindi / tamil / telugu / english.
    // Its English is always Indian, so there is no accent choice to offer.
    options: [
      { code: "en-IN", dialect: "Indian" },
      { code: "hi-IN" },
      { code: "ta-IN" },
      { code: "te-IN" },
    ],
    sources: ["LOAN & EMI KNOWLEDGE", "Payment Methods", "Tenure & Schedule"],
    turns: [
      { who: "agent", text: "", latency: 180 },
      { who: "caller", text: "Yes, this is Arnav." },
      {
        who: "agent",
        text: "This is about your pending EMI of five thousand rupees, which was due on the fifth. Is now a good time?",
        cite: {
          source: "LOAN & EMI KNOWLEDGE",
          span: "Pending EMI ₹5,000 · due 5th · currently overdue",
        },
        latency: 250,
      },
      {
        who: "caller",
        text: "It's fine. Remind me how many I have left?",
      },
      {
        who: "agent",
        text: "You've paid four of twelve on a sixty-thousand loan. This is the fifth month, so seven remain after this one.",
        cite: {
          source: "Tenure & Schedule",
          span: "₹60,000 over 12 months · 4 paid · 5th pending",
        },
        latency: 330,
      },
      { who: "caller", text: "Can you send me something I can pay on?" },
      {
        who: "agent",
        text: "Five thousand rupees — shall I open that now? UPI, net banking or card all work on it.",
        cite: {
          source: "Payment Methods",
          span: "UPI · Net Banking · Debit · Credit · Wallet · secure link",
        },
        latency: 270,
      },
      { who: "caller", text: "Yes, go ahead." },
      {
        who: "agent",
        // Deliberately matches what the deployed bridge actually does today.
        // The create_payment_link path exists in backend/ but is not enabled on
        // the hosted bridge, so depicting a checkout opening here would show a
        // capability this recording cannot back up.
        text: "I'll send you a secure payment link — UPI, net banking or card all work on it.",
        action: "payment link sent",
        latency: 320,
      },
    ],
  },
  {
    id: "feedback",
    label: "Feedback",
    scenario: "feedback_agent",
    business: "Sunrise Company",
    persona: "Ratan",
    premise: "Collects post-delivery feedback and escalates the unhappy calls.",
    channel: "SIP · outbound",
    // agents/feedback_agent.txt → language_rules: tamil_only
    options: [{ code: "ta-IN" }],
    sources: ["CALL FLOW", "Rating thresholds", "Escalation policy"],
    turns: [
      { who: "agent", text: "", latency: 200 },
      { who: "caller", text: "Yes, that's me." },
      {
        who: "agent",
        text: "I'm Ratan from Sunrise Company. You had a delivery today — quick feedback, how is the product quality?",
        cite: {
          source: "CALL FLOW",
          span: "one question per turn · wait for the answer",
        },
        latency: 230,
      },
      { who: "caller", text: "The product is good, but the box was dented." },
      {
        who: "agent",
        text: "Sorry about the packaging. On a scale of one to ten, how satisfied are you overall?",
        latency: 280,
      },
      { who: "caller", text: "About a four, because of that." },
      {
        who: "agent",
        text: "Sorry again — anything five or below I take straight to the regional manager. Thank you for the time, it genuinely helps.",
        cite: {
          source: "Rating thresholds · Escalation policy",
          span: "score ≤ 5 → escalate to Regional Manager",
        },
        action: "record_feedback · escalation_required: true",
        latency: 210,
      },
    ],
  },
];

/**
 * The agent's opening line, in the caller's own language.
 *
 * For English, Hindi and Tamil these are the exact greetings the backend
 * speaks. The rest are written for the demo — the backend's `language` field
 * currently accepts only english, hindi, tamil and telugu, so the other
 * languages here are not yet wired to a real call.
 */
const GREETINGS: Record<string, string> = {
  "en-IN": "{b}, good evening. How can I help?",
  "hi-IN": "नमस्ते, क्या मेरी बात अर्नव जी से हो रही है?",
  "ta-IN": "ஹலோ சார், வணக்கம், நான் Mr. Arnav-கிட்ட பேசறனா?",
  "te-IN": "హలో సర్, నమస్కారం, నేను అర్నవ్ గారితో మాట్లాడుతున్నానా?",
  "ar-AE": "{b}، مساء الخير. كيف يمكنني مساعدتك؟",
  "zh-CN": "{b}，您好。请问有什么可以帮您？",
  "es-MX": "{b}, buenas tardes. ¿En qué le puedo ayudar?",
  "pt-BR": "{b}, boa tarde. Como posso ajudar?",
  "ja-JP": "{b}でございます。ご用件をお伺いします。",
  "de-DE": "{b}, guten Abend. Wie kann ich Ihnen helfen?",
  "fr-FR": "{b}, bonsoir. Comment puis-je vous aider ?",
  "bn-IN": "{b}, নমস্কার। আমি কীভাবে সাহায্য করতে পারি?",
  "id-ID": "{b}, selamat sore. Ada yang bisa saya bantu?",
};

/** The restaurant agent answers inbound, so it greets by name in English. */
const RESTAURANT_EN =
  "Good evening! Welcome to The Royal Plate. I'm David, how can I help you?";

/**
 * The desk's greeting is hardcoded in the backend rather than generated, so
 * the recording says exactly what a real call says. Copied verbatim from
 * FLEET_DESK_GREETINGS in backend/test_realtime_gemini.py.
 */
const FLEET_DESK_GREETINGS: Record<string, string> = {
  "en-IN":
    "Hello, this is the Fleet Service Desk, I am Gaurav, how may I help you?",
  "hi-IN":
    "नमस्ते, यह Fleet Service Desk है, मैं गौरव हूँ, मैं आपकी क्या सहायता कर सकता हूँ?",
  "ta-IN":
    "வணக்கம், இது Fleet Service Desk, நான் கௌரவ், உங்களுக்கு நான் எவ்வாறு உதவ முடியும்?",
  "te-IN":
    "నమస్తే, ఇది Fleet Service Desk, నేను గౌరవ్, మీకు ఏ విధంగా సహాయం చేయగలను?",
  "ja-JP":
    "こんにちは、Fleet Service Deskのガウラヴです。どのようなご用件でしょうか？",
  "de-DE":
    "Hallo, hier ist der Fleet Service Desk, mein Name ist Gaurav, wie kann ich Ihnen helfen?",
};

/**
 * Copied verbatim from FSECURE_GREETINGS in backend/test_realtime_gemini.py,
 * keyed here by picker code rather than by the bare code the bridge uses.
 */
const ENDPOINT_GREETINGS: Record<string, string> = {
  "en-IN":
    "Hello, this is the Endpoint Security Desk, I am Mohit, how may I help you?",
  "fi-FI":
    "Hei, täällä Endpoint Security Desk, Mohit täällä, kuinka voin auttaa?",
  "sv-SE":
    "Hej, det här är Endpoint Security Desk, jag heter Mohit, hur kan jag hjälpa dig?",
  "de-DE":
    "Hallo, hier ist der Endpoint Security Desk, mein Name ist Mohit, wie kann ich Ihnen helfen?",
  "nl-NL":
    "Hallo, dit is de Endpoint Security Desk, ik ben Mohit, hoe kan ik u helpen?",
  "fr-FR":
    "Bonjour, ici l'Endpoint Security Desk, je suis Mohit, comment puis-je vous aider?",
  "ja-JP":
    "こんにちは、Endpoint Security Deskのモヒトです。どのようなご用件でしょうか？",
};

export function greeting(code: string, business: string): string {
  if (business === "The Royal Plate" && code === "en-IN") return RESTAURANT_EN;
  if (business === "Fleet Service Desk") {
    return FLEET_DESK_GREETINGS[code] ?? FLEET_DESK_GREETINGS["en-IN"];
  }
  if (business === "Endpoint Security Desk") {
    return ENDPOINT_GREETINGS[code] ?? ENDPOINT_GREETINGS["en-IN"];
  }
  const template = GREETINGS[code] ?? GREETINGS["en-IN"];
  return template.replace("{b}", business);
}

/**
 * Maps a picker language onto the `language` value the backend accepts.
 * Null means the backend has no branch for it yet and would fall back to its
 * own default — useful to know before the demo is wired to real audio.
 *
 * This table is for the four name-keyed agents only. ggs_support is declared
 * languageFormat: "bcp47" and is sent the picker code untouched, which is why
 * it can offer Japanese and German while the others cannot.
 */
export const BACKEND_LANGUAGE: Record<string, string | null> = {
  "en-IN": "english",
  "hi-IN": "hindi",
  "ta-IN": "tamil",
  "te-IN": "telugu",
  "ar-AE": null,
  "zh-CN": null,
  "es-MX": null,
  "pt-BR": null,
  "ja-JP": null,
  "de-DE": null,
  "fr-FR": null,
  "bn-IN": null,
  "id-ID": null,
};

/** Languages written right-to-left, for the transcript's text direction. */
export const RTL = new Set(["ar-AE"]);
