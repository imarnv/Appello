"""
Appello Call Analytics — Dashboard REST API
Provides GET endpoints for the frontend dashboard to fetch payment follow-up
and spa booking logs. Mounted as a sub-router in main.py.
"""

import logging
from fastapi import APIRouter
from postgres_store import PostgresStore

logger = logging.getLogger("appello")

# The router is created here but the db_store instance is injected from main.py
# at mount time via the module-level variable.
_db_store: PostgresStore | None = None

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


def init(db_store: PostgresStore):
    """Inject the shared PostgresStore instance from main.py."""
    global _db_store
    _db_store = db_store
    logger.info("[analytics] Dashboard API initialized with shared db_store")


@router.get("/payment-followup")
async def get_payment_followup_logs():
    """Return all feedback and lead qualification call logs for the dashboard table."""
    if not _db_store:
        return {"logs": [], "count": 0, "error": "Database not connected"}
    logs = await _db_store.get_feedback_agent_logs(limit=100)
    return {"logs": logs, "count": len(logs)}


@router.get("/spa-booking")
async def get_spa_booking_logs():
    """Return all restaurant booking call logs for the dashboard table."""
    if not _db_store:
        return {"logs": [], "count": 0, "error": "Database not connected"}
    logs = await _db_store.get_restaurant_booking_logs(limit=100)
    return {"logs": logs, "count": len(logs)}
