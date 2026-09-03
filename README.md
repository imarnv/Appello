# Appello

Voice agents that answer on the first ring — and answer out of your documents
rather than out of a language model's memory.

**Live:** [appello-ai.vercel.app](https://appello-ai.vercel.app)

Appello is two things in one repository:

| | |
| --- | --- |
| `src/` | The Next.js site, including a **Try it** panel that places a real call to a real agent from the browser. |
| `backend/` | The Python voice bridge that serves that call. Realtime speech, retrieval over the customer's own documents, SIP telephony, transcripts and analytics. See **[backend/README.md](backend/README.md)** for the full architecture. |

Nothing on the site is a mock-up of the backend. The **Document search** agent
is answering from a live vector store of 5,753 passages of vehicle service
documentation while you listen, and the panel says so explicitly on the rare
path where it falls back to a recording.

## What it demonstrates

**Grounded answers, spoken.** Ask the Document search agent for a brake-fluid
replacement interval and it searches 5,753 indexed passages, then reads the
interval back exactly as the manual prints it and names the document and page it
came from. Every figure is quoted, never rounded, never inferred.

**Search that does not stall the call.** A realtime model that calls a tool and
waits leaves dead air. The agent instead calls `initialize_search`, speaks a
short filler while the search runs in the background, then calls
`fetch_search_results` — so the retrieval cost is spent underneath speech and
the caller hears no gap.

**Dialect, not translation.** For Hindi, Tamil and Telugu the agent speaks the
register a service engineer actually uses on the phone — the sentence in the
caller's language with part names, units and grades left in English — rather
than a literary translation that is harder for a technician to follow. The
language picker offers only the languages an agent genuinely has a greeting and
a language override for, not a marketing count.

**Mid-call tool calls.** Tool calls execute inside the live session: the
restaurant agent writes a real reservation to Postgres while the caller is still
on the line and speaks the result in its next breath.

## Running it

```bash
npm install
npm run dev          # http://localhost:3000
```

With no configuration the site talks to the hosted bridge, so the live call
works out of the box. To develop against a bridge on your own machine:

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # fill in at least GEMINI_API_KEY
python main.py                # http://localhost:8000
```

```bash
echo "NEXT_PUBLIC_VOICE_BRIDGE_URL=ws://localhost:8000" > .env.local
```

The panel reads the bridge's `/health` on load and only offers a live call when
it answers; otherwise it plays a recorded conversation and labels it as one.

## Layout

```
src/app/                 Routes, global styles, fonts
src/components/          Sections, navigation, the WebGL voice field
src/components/try/      The call panel: TrySection, Transcript, useCall
src/lib/voiceClient.ts   WebSocket + Web Audio client for the bridge
src/lib/verticals.ts     The agents, their languages, sources and fallbacks
src/lib/signatures.ts    Per-language visual signatures for the voice field
backend/                 The Python voice bridge — see backend/README.md
```

## Notes

`.env*` files are gitignored and hold live credentials; only `.env.example` is
committed. The source PDFs behind the document-search corpus are licensed OEM
manuals and are not in this repository — `backend/ingest_manuals.py` documents
how to rebuild the collection from your own copies.
