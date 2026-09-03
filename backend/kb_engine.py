"""
Appello — Knowledge Base Engine
Centralized KB search with per-agent Qdrant collections and Redis query caching.
Optimized for <20ms mid-call retrieval latency.

Usage:
    kb = KBEngine(redis_cache)
    await kb.initialize()
    results = await kb.search("butter chicken price", agent_type="restaurant_booking")
"""

import asyncio
import csv
import hashlib
import io
import json
import logging
import os
import re
import time
import uuid
from typing import Any, Dict, List, Optional

import aiohttp

logger = logging.getLogger("appello")


class KBUnavailable(RuntimeError):
    """The KB lookup could not be completed — a transport fault, not an empty result.

    Distinguished from "no matches" so a caller never reports missing
    documentation when the truth is that the query never reached the store.
    """

# ─── Config ──────────────────────────────────────────────────────────────
QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY", None)
AZURE_EMBEDDING_ENDPOINT = os.getenv("AZURE_OPENAI_EMBEDDING_ENDPOINT", "").rstrip("/")
AZURE_EMBEDDING_API_KEY = os.getenv("AZURE_OPENAI_EMBEDDING_API_KEY", "")
AZURE_EMBEDDING_DEPLOYMENT = os.getenv("AZURE_OPENAI_EMBEDDING_DEPLOYMENT", "text-embedding-3-small")
AZURE_EMBEDDING_API_VERSION = os.getenv("AZURE_OPENAI_EMBEDDING_API_VERSION", "2023-05-15")

VECTOR_DIM = 1536  # text-embedding-3-small dimension
CACHE_TTL_S = 300  # 5 min Redis cache for KB query results
CHUNK_SIZE = 1500
CHUNK_OVERLAP = 200


class KBEngine:
    """Per-agent Qdrant vector search with Redis caching."""

    def __init__(self, redis_cache=None):
        self._qdrant = None
        self._redis = redis_cache
        self._http: Optional[aiohttp.ClientSession] = None
        self._embedding_cache: Dict[str, List[float]] = {}  # in-memory LRU for hot embeddings
        self._known_collections: set = set()  # collections confirmed to exist

    # ── Lifecycle ─────────────────────────────────────────────────────

    async def initialize(self):
        """Connect to Qdrant and warm up HTTP session."""
        try:
            from qdrant_client import QdrantClient
            self._qdrant = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY, timeout=5.0)
            logger.info(f"[kb-engine] Connected to Qdrant at {QDRANT_URL}")
        except Exception as e:
            logger.warning(f"[kb-engine] Qdrant not available: {e}")
        self._http = aiohttp.ClientSession()

    async def close(self):
        if self._http and not self._http.closed:
            await self._http.close()

    # ── Collection Management ─────────────────────────────────────────

    def _collection_name(self, agent_type: str) -> str:
        """Per-agent collection: kb_restaurant_booking, kb_feedback_agent, etc."""
        return f"kb_{agent_type}"

    def _ensure_collection(self, agent_type: str):
        """Create collection if it doesn't exist."""
        if not self._qdrant:
            return
        try:
            from qdrant_client.models import Distance, VectorParams
            col_name = self._collection_name(agent_type)
            existing = [c.name for c in self._qdrant.get_collections().collections]
            if col_name not in existing:
                self._qdrant.create_collection(
                    collection_name=col_name,
                    vectors_config=VectorParams(size=VECTOR_DIM, distance=Distance.COSINE),
                )
                logger.info(f"[kb-engine] Created collection '{col_name}'")
        except Exception as e:
            logger.error(f"[kb-engine] Error creating collection: {e}")

    # ── Embedding ─────────────────────────────────────────────────────

    async def _embed(self, text: str) -> Optional[List[float]]:
        """Compute embedding via Azure text-embedding-3-small. Cached in-memory."""
        cache_key = hashlib.md5(text.encode()).hexdigest()
        if cache_key in self._embedding_cache:
            return self._embedding_cache[cache_key]

        if not AZURE_EMBEDDING_ENDPOINT or not AZURE_EMBEDDING_API_KEY:
            logger.warning("[kb-engine] Embedding credentials not configured")
            return None

        url = (
            f"{AZURE_EMBEDDING_ENDPOINT}/openai/deployments/"
            f"{AZURE_EMBEDDING_DEPLOYMENT}/embeddings?api-version={AZURE_EMBEDDING_API_VERSION}"
        )
        try:
            session = self._http or aiohttp.ClientSession()
            async with session.post(
                url,
                headers={"api-key": AZURE_EMBEDDING_API_KEY, "Content-Type": "application/json"},
                json={"input": text[:8000]},
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    vec = data["data"][0]["embedding"]
                    # Cache (keep max 200 entries)
                    if len(self._embedding_cache) > 200:
                        oldest = next(iter(self._embedding_cache))
                        del self._embedding_cache[oldest]
                    self._embedding_cache[cache_key] = vec
                    return vec
                else:
                    err = await resp.text()
                    logger.error(f"[kb-engine] Embedding API error ({resp.status}): {err[:200]}")
        except Exception as e:
            logger.error(f"[kb-engine] Embedding failed: {e}")
        return None

    # ── Search ────────────────────────────────────────────────────────

    async def search(
        self,
        query: str,
        agent_type: str,
        top_k: int = 3,
        group_by: Optional[str] = None,
        group_size: int = 1,
        expand_top_pages: int = 0,
    ) -> List[Dict[str, Any]]:
        """
        Semantic search against agent's KB collection.
        1. Check Redis cache (hash of query + agent_type)
        2. If miss → embed query → Qdrant search → cache result
        Returns list of {text, source, score} dicts.

        `group_by` names a payload field to diversify results across — pass
        "filename" and Qdrant returns the best `group_size` chunks from each of
        the `top_k` best-matching documents, instead of the globally best
        `top_k` chunks. This matters once one collection holds documents of very
        different sizes: a 1,500-chunk manual otherwise fills every slot with
        mediocre near-misses and buries the one short document that actually
        answers the question. Grouping requires a keyword payload index on the
        field. Leave unset for the flat top-k behaviour.

        `expand_top_pages` serves complete answers instead of fragments. A
        maintenance schedule or a repair procedure routinely spans several
        chunks of one page, and vector search returns only the fragment that
        scored best — so the agent reads out half a table and stops. For the
        best `expand_top_pages` results, every other chunk of the same page is
        fetched and stitched back in printed order, giving the agent the whole
        procedure. Needs `page` and `filename` payload indexes, and a
        `chunk_index` payload to order by.
        """
        t0 = time.monotonic()
        cache_key = (
            f"kb:{agent_type}:{group_by or '-'}:{group_size}:{top_k}:{expand_top_pages}:"
            f"{hashlib.md5(query.encode()).hexdigest()}"
        )

        # 1. Redis cache check
        if self._redis:
            try:
                cached = await self._redis.get_raw(cache_key)
                if cached:
                    results = json.loads(cached)
                    logger.info(f"[kb-engine] Cache HIT for '{query[:40]}' ({(time.monotonic()-t0)*1000:.1f}ms)")
                    return results
            except Exception:
                pass

        # 2. Compute embedding
        embedding = await self._embed(query)
        if not embedding:
            logger.warning(f"[kb-engine] No embedding for query: {query[:50]}")
            return []

        # 3. Qdrant search
        if not self._qdrant:
            return []

        col_name = self._collection_name(agent_type)
        try:
            # Collection existence is cached after the first confirmed sighting.
            # It used to be re-checked on every search, which put a second
            # network round trip in front of every query — extra latency, and an
            # extra chance to fail, for a fact that does not change mid-call.
            #
            # If the check itself cannot be made, fall through to the query
            # rather than bailing: this is an optimisation, and treating an
            # unreachable server here as "collection not found" is exactly the
            # confusion that makes a network fault look like missing content.
            if col_name not in self._known_collections:
                try:
                    existing = [c.name for c in self._qdrant.get_collections().collections]
                    self._known_collections.update(existing)
                    if col_name not in existing:
                        logger.warning(f"[kb-engine] Collection '{col_name}' not found")
                        return []
                except Exception as e:
                    logger.warning(
                        f"[kb-engine] could not verify collection '{col_name}' ({e}); "
                        f"attempting the query anyway"
                    )

            def run_query():
                if group_by:
                    groups = self._qdrant.query_points_groups(
                        collection_name=col_name,
                        query=embedding,
                        group_by=group_by,
                        limit=top_k,
                        group_size=group_size,
                    ).groups
                    # Flatten back to a ranked list: groups arrive best-first,
                    # and hits within a group are already ordered by score.
                    return [hit for g in groups for hit in g.hits]
                return self._qdrant.query_points(
                    collection_name=col_name,
                    query=embedding,
                    limit=top_k,
                ).points

            # A transient DNS or connection fault must not read as "we have no
            # documentation on that" — mid-call that is indistinguishable from a
            # genuine miss, and the agent apologises for a gap that isn't there.
            # Retry briefly, rebuilding the client so a poisoned connection pool
            # cannot keep failing, and let a hard failure raise so the caller can
            # say the lookup broke rather than that the answer doesn't exist.
            results_raw = None
            last_err = None
            for attempt in range(3):
                try:
                    # qdrant-client is synchronous. Called directly it would
                    # block the event loop for the whole round trip to Frankfurt,
                    # stalling the audio being streamed to and from the caller.
                    # Off-thread it costs the same wall time but keeps the call
                    # audible.
                    results_raw = await asyncio.to_thread(run_query)
                    break
                except Exception as e:
                    last_err = e
                    logger.warning(
                        f"[kb-engine] search attempt {attempt+1}/3 failed: {e}"
                    )
                    if attempt < 2:
                        await asyncio.sleep(0.4 * (attempt + 1))
                        try:
                            from qdrant_client import QdrantClient
                            self._qdrant = QdrantClient(
                                url=QDRANT_URL, api_key=QDRANT_API_KEY, timeout=8.0
                            )
                        except Exception:
                            pass
            if results_raw is None:
                raise KBUnavailable(f"KB lookup unavailable: {last_err}")

            results = [
                {
                    "text": r.payload.get("text", ""),
                    "source": r.payload.get("filename", "unknown"),
                    "score": round(r.score, 3),
                    "metadata": {
                        k: v for k, v in r.payload.items()
                        if k not in ("text", "filename", "embedding")
                    },
                }
                for r in results_raw
            ]

            if expand_top_pages:
                # Also off-thread: each expansion is another blocking round trip,
                # and the caller is waiting on audio the whole time.
                results = await asyncio.to_thread(
                    self._expand_pages, col_name, results, expand_top_pages
                )

            elapsed_ms = (time.monotonic() - t0) * 1000
            logger.info(f"[kb-engine] Search '{query[:40]}' → {len(results)} results ({elapsed_ms:.1f}ms)")

            # 4. Cache in Redis
            if self._redis and results:
                try:
                    await self._redis.set_raw(cache_key, json.dumps(results), ttl=CACHE_TTL_S)
                except Exception:
                    pass

            return results
        except KBUnavailable:
            raise
        except Exception as e:
            logger.error(f"[kb-engine] Search error: {e}")
            return []

    def _expand_pages(
        self, col_name: str, results: List[Dict[str, Any]], how_many: int
    ) -> List[Dict[str, Any]]:
        """Replace the top hits with their full source page, in printed order.

        Only distinct pages count toward `how_many`, so two chunks of the same
        page collapse into one expanded passage rather than consuming two slots.
        Any page that fails to expand is left exactly as it was — a partial
        answer still beats no answer.
        """
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        expanded: List[Dict[str, Any]] = []
        seen_pages: set = set()
        budget = how_many

        for res in results:
            meta = res.get("metadata") or {}
            page = meta.get("page")
            filename = res.get("source")
            key = (filename, page)

            if key in seen_pages:
                continue  # already served whole by an earlier expansion
            if budget <= 0 or page is None or not filename or filename == "unknown":
                expanded.append(res)
                seen_pages.add(key)
                continue

            try:
                points, _ = self._qdrant.scroll(
                    collection_name=col_name,
                    scroll_filter=Filter(
                        must=[
                            FieldCondition(key="filename", match=MatchValue(value=filename)),
                            FieldCondition(key="page", match=MatchValue(value=page)),
                        ]
                    ),
                    limit=40,
                    with_payload=True,
                    with_vectors=False,
                )
            except Exception as e:
                logger.warning(f"[kb-engine] page expansion failed for {filename} p{page}: {e}")
                expanded.append(res)
                seen_pages.add(key)
                continue

            if len(points) <= 1:
                expanded.append(res)
                seen_pages.add(key)
                continue

            ordered = sorted(points, key=lambda p: (p.payload or {}).get("chunk_index", 0))
            merged = self._stitch(ordered)
            expanded.append({**res, "text": merged, "metadata": {**meta, "expanded_chunks": len(ordered)}})
            seen_pages.add(key)
            budget -= 1

        return expanded

    @staticmethod
    def _stitch(ordered_points) -> str:
        """Join a page's chunks, dropping the repeated context header and the
        lines duplicated by chunk overlap."""
        out: List[str] = []
        for i, p in enumerate(ordered_points):
            lines = (p.payload or {}).get("text", "").splitlines()
            if i > 0 and lines and lines[0].startswith("["):
                lines = lines[1:]  # header is already on the first chunk
            for line in lines:
                if not out or line.strip() != out[-1].strip():
                    out.append(line)
        return "\n".join(out)

    # ── Ingestion — PDF ───────────────────────────────────────────────

    async def ingest_pdf(
        self, content: bytes, filename: str, agent_type: str, category: str = "general"
    ) -> Dict[str, Any]:
        """Extract text from PDF, chunk, embed, store in agent's Qdrant collection."""
        extracted_text = ""
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(content))
            pages = [p.extract_text() or "" for p in reader.pages]
            extracted_text = "\n".join(pages)
            logger.info(f"[kb-engine] Extracted {len(extracted_text)} chars from {filename}")
        except Exception as e:
            logger.error(f"[kb-engine] PDF extraction error: {e}")
            return {"chunks": 0, "text": "", "items": []}

        chunks = self._chunk_text(extracted_text)
        menu_items = self._extract_structured_items(extracted_text, agent_type)

        # Background embed and store
        asyncio.create_task(self._store_chunks(chunks, filename, agent_type, category))

        return {
            "chunks": len(chunks),
            "text": extracted_text[:2000],
            "items": menu_items,
        }

    # ── Ingestion — CSV ───────────────────────────────────────────────

    async def ingest_csv(
        self, content: bytes, filename: str, agent_type: str
    ) -> Dict[str, Any]:
        """Parse CSV rows, create searchable text per row, embed and store."""
        text = content.decode("utf-8", errors="replace")
        reader = csv.DictReader(io.StringIO(text))
        rows = list(reader)
        if not rows:
            return {"chunks": 0, "rows": 0, "items": []}

        # Build one text chunk per row for vector search
        chunks = []
        items = []
        for row in rows:
            # Create a natural language representation of the row
            parts = [f"{k}: {v}" for k, v in row.items() if v and v.strip()]
            chunk_text = " | ".join(parts)
            if chunk_text.strip():
                chunks.append(chunk_text)
                items.append({k.strip().lower().replace(" ", "_"): v.strip() for k, v in row.items() if v})

        logger.info(f"[kb-engine] Parsed {len(chunks)} rows from CSV {filename}")

        # Background embed and store
        asyncio.create_task(
            self._store_chunks(chunks, filename, agent_type, "csv_data")
        )

        return {"chunks": len(chunks), "rows": len(rows), "items": items}

    # ── Customer Context Lookup ───────────────────────────────────────

    async def get_customer_context(
        self, phone: str, agent_type: str
    ) -> Optional[Dict[str, Any]]:
        """Look up customer profile from KB by phone number for outbound calls."""
        # Normalize phone to last 10 digits
        digits = re.sub(r"\D", "", phone)
        phone_10 = digits[-10:] if len(digits) >= 10 else digits

        # Search Qdrant with phone number as query
        results = await self.search(f"customer phone {phone_10}", agent_type, top_k=1)
        if results:
            meta = results[0].get("metadata", {})
            text = results[0].get("text", "")
            # Try to extract structured data from the text
            return {"raw_text": text, **meta}
        return None

    # ── Internal Helpers ──────────────────────────────────────────────

    def _chunk_text(self, text: str) -> List[str]:
        """Split text into overlapping chunks."""
        chunks = []
        start = 0
        while start < len(text):
            end = start + CHUNK_SIZE
            chunk = text[start:end].strip()
            if chunk:
                chunks.append(chunk)
            start = end - CHUNK_OVERLAP
        return chunks

    async def _store_chunks(
        self, chunks: List[str], filename: str, agent_type: str, category: str
    ):
        """Embed and upsert chunks into Qdrant."""
        if not self._qdrant or not chunks:
            return

        self._ensure_collection(agent_type)
        col_name = self._collection_name(agent_type)

        from qdrant_client.models import PointStruct
        points = []
        for i, chunk in enumerate(chunks):
            embedding = await self._embed(chunk)
            if embedding:
                points.append(PointStruct(
                    id=str(uuid.uuid4()),
                    vector=embedding,
                    payload={
                        "text": chunk,
                        "filename": filename,
                        "agent_type": agent_type,
                        "category": category,
                        "chunk_index": i,
                    },
                ))

        if points:
            for batch_start in range(0, len(points), 100):
                batch = points[batch_start:batch_start + 100]
                self._qdrant.upsert(collection_name=col_name, points=batch)
            logger.info(f"[kb-engine] Stored {len(points)} vectors in '{col_name}' for {filename}")
            if self._redis:
                try:
                    await self._redis.clear_kb_cache(agent_type)
                except Exception as e:
                    logger.error(f"[kb-engine] Failed to clear Redis cache on store: {e}")

    def _extract_structured_items(self, text: str, agent_type: str) -> List[dict]:
        """Extract structured items (menu items, property details, etc.) from text."""
        if agent_type == "restaurant_booking":
            return self._extract_menu_items(text)
        return []

    def _extract_menu_items(self, text: str) -> List[dict]:
        """Extract food items and prices from text."""
        items = []
        patterns = [
            r'([A-Za-z][A-Za-z\s&/,\(\)\-\']+?)\s*[–—\-\.]+\s*[₹Rs\.]*\s*(\d+(?:\.\d{1,2})?)',
            r'([A-Za-z][A-Za-z\s&/,\(\)\-\']+?)\s+₹\s*(\d+(?:\.\d{1,2})?)',
            r'([A-Za-z][A-Za-z\s&/,\(\)\-\']+?)\s+Rs\.?\s*(\d+(?:\.\d{1,2})?)',
        ]
        seen = set()
        for pattern in patterns:
            for match in re.finditer(pattern, text):
                name = match.group(1).strip()
                price = match.group(2).strip()
                if 3 <= len(name) <= 60 and name.lower() not in seen:
                    try:
                        val = float(price)
                        if 5 <= val <= 50000:
                            seen.add(name.lower())
                            items.append({
                                "name": name,
                                "price": f"₹{int(val)}",
                                "category": "General",
                                "description": "",
                                "available": True,
                            })
                    except ValueError:
                        pass
        return items

    # ── Delete ────────────────────────────────────────────────────────

    async def delete_file_vectors(self, filename: str, agent_type: str):
        """Remove all vectors for a specific file from the agent's collection."""
        if not self._qdrant:
            return
        try:
            from qdrant_client.models import Filter, FieldCondition, MatchValue
            col_name = self._collection_name(agent_type)
            self._qdrant.delete(
                collection_name=col_name,
                points_selector=Filter(
                    must=[FieldCondition(key="filename", match=MatchValue(value=filename))]
                ),
            )
            logger.info(f"[kb-engine] Deleted vectors for '{filename}' from '{col_name}'")
            if self._redis:
                await self._redis.clear_kb_cache(agent_type)
        except Exception as e:
            logger.warning(f"[kb-engine] Delete error: {e}")

    async def clear_collection(self, agent_type: str):
        """Drop and recreate an agent's collection."""
        if not self._qdrant:
            return
        col_name = self._collection_name(agent_type)
        try:
            self._qdrant.delete_collection(collection_name=col_name)
            self._ensure_collection(agent_type)
            logger.info(f"[kb-engine] Cleared collection '{col_name}'")
            if self._redis:
                await self._redis.clear_kb_cache(agent_type)
        except Exception as e:
            logger.warning(f"[kb-engine] Clear error: {e}")
