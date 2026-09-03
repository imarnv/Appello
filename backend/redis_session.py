"""
Appello Redis Session Manager
Handles real-time session caching and customer profile pre-hydration.
Uses Redis for O(1) lookups to eliminate database latency during live calls.
"""

import json
import logging
import os
from typing import Optional, Dict, Any

logger = logging.getLogger("appello")

try:
    import redis.asyncio as aioredis
except ImportError:
    aioredis = None  # type: ignore


class RedisSessionManager:
    def __init__(self, redis_url: Optional[str] = None):
        self.redis_url = redis_url or os.getenv("REDIS_URL", "")
        self.client: Optional[Any] = None

    async def connect(self):
        """Establish async Redis connection."""
        if not self.redis_url:
            logger.warning("⚠️ REDIS_URL not set. Skipping Redis integration — using in-memory fallback.")
            self._fallback: Dict[str, str] = {}
            return

        if aioredis is None:
            logger.warning("⚠️ redis package not installed. Using in-memory fallback.")
            self._fallback = {}
            return

        try:
            self.client = aioredis.from_url(self.redis_url, decode_responses=True)
            await self.client.ping()
            logger.info("🔴 Connected to Redis successfully")
        except Exception as e:
            logger.error(f"❌ Failed to connect to Redis: {e}")
            self.client = None
            self._fallback = {}

    async def close(self):
        if self.client:
            await self.client.close()

    async def set_session(self, session_id: str, data: Dict[str, Any], expire_seconds: int = 3600):
        """Store session data with optional TTL."""
        payload = json.dumps(data)
        if self.client:
            try:
                await self.client.set(f"session:{session_id}", payload, ex=expire_seconds)
            except Exception as e:
                logger.error(f"[redis] Error setting session: {e}")
        else:
            self._fallback[f"session:{session_id}"] = payload

    async def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve session data."""
        try:
            if self.client:
                raw = await self.client.get(f"session:{session_id}")
            else:
                raw = self._fallback.get(f"session:{session_id}")
            return json.loads(raw) if raw else None
        except Exception as e:
            logger.error(f"[redis] Error getting session: {e}")
            return None

    async def prehydrate_customer(self, phone: str, profile: Dict[str, Any]):
        """Pre-cache a customer profile for O(1) lookup during calls."""
        payload = json.dumps(profile)
        if self.client:
            try:
                await self.client.set(f"customer:{phone}", payload, ex=86400)  # 24h TTL
            except Exception as e:
                logger.error(f"[redis] Error caching customer: {e}")
        else:
            self._fallback[f"customer:{phone}"] = payload

    async def get_customer(self, phone: str) -> Optional[Dict[str, Any]]:
        """Fetch cached customer profile."""
        try:
            if self.client:
                raw = await self.client.get(f"customer:{phone}")
            else:
                raw = self._fallback.get(f"customer:{phone}")
            return json.loads(raw) if raw else None
        except Exception as e:
            logger.error(f"[redis] Error getting customer: {e}")
            return None

    # ── Generic Key-Value (used by KBEngine query cache) ─────────────

    async def get_raw(self, key: str) -> Optional[str]:
        """Get a raw string value by key."""
        try:
            if self.client:
                return await self.client.get(key)
            return getattr(self, "_fallback", {}).get(key)
        except Exception:
            return None

    async def set_raw(self, key: str, value: str, ttl: int = 300):
        """Set a raw string value with TTL."""
        try:
            if self.client:
                await self.client.set(key, value, ex=ttl)
            else:
                if not hasattr(self, "_fallback"):
                    self._fallback = {}
                self._fallback[key] = value
        except Exception as e:
            logger.error(f"[redis] Error in set_raw: {e}")

    # ── Session events ────────────────────────────────────────────────
    # Transcript fan-out uses an in-process dict, which is fine because the
    # websocket and its readers share a worker. A payment webhook does not: it
    # is an inbound HTTP request that can land anywhere, so it has to reach the
    # session over real pub/sub.

    @staticmethod
    def event_channel(session_id: str) -> str:
        return f"appello:session:{session_id}:events"

    async def publish_event(self, session_id: str, event: Dict[str, Any]) -> int:
        """Publish one event to whichever worker is holding this session.

        Returns the number of subscribers that received it — zero means the call
        has already ended, which is normal and not an error.
        """
        if not self.client:
            return 0
        try:
            return await self.client.publish(
                self.event_channel(session_id), json.dumps(event)
            )
        except Exception as e:
            logger.error(f"[redis] could not publish session event: {e}")
            return 0

    def subscribe_events(self, session_id: str):
        """A pubsub handle for this session, or None when Redis is unavailable.

        The caller owns it and must unsubscribe; it is deliberately not an async
        context manager, because the websocket's lifetime is what governs it.
        """
        if not self.client:
            return None
        return self.client.pubsub(ignore_subscribe_messages=True)

    async def clear_kb_cache(self, agent_type: str):
        """Clear all cached KB queries for an agent."""
        try:
            if self.client:
                keys = await self.client.keys(f"kb:{agent_type}:*")
                if keys:
                    await self.client.delete(*keys)
                    logger.info(f"[redis] Cleared {len(keys)} cached KB keys for agent '{agent_type}'")
            else:
                if hasattr(self, "_fallback"):
                    keys_to_del = [k for k in self._fallback.keys() if k.startswith(f"kb:{agent_type}:")]
                    for k in keys_to_del:
                        del self._fallback[k]
                    logger.info(f"[redis] Cleared {len(keys_to_del)} fallback cached KB keys for agent '{agent_type}'")
        except Exception as e:
            logger.error(f"[redis] Error clearing KB cache: {e}")

