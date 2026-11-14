"""MongoDB access layer for the ticket system.

All database access goes through :class:`TicketStore`. It deliberately resolves
``bot.mongo_client`` on every call instead of caching a database/collection
handle, so that the bot's automatic reconnect (see ``main.py``) can swap the
client underneath us without leaving us holding a closed handle.

Every method degrades gracefully: if MongoDB is not configured/available it
returns ``None``/``False`` rather than raising, and callers surface a friendly
error to the user.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from pymongo import ASCENDING, ReturnDocument

from .config import (
    CONFIG_COLLECTION,
    COUNTERS_COLLECTION,
    DB_NAME,
    TICKETS_COLLECTION,
)

log = logging.getLogger(__name__)

OPEN_STATUSES = ("open", "claimed")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TicketStore:
    def __init__(self, bot: Any) -> None:
        self.bot = bot

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #
    def _db(self):
        client = getattr(self.bot, "mongo_client", None)
        if client is None:
            return None
        try:
            return client[DB_NAME]
        except Exception:
            log.exception("Failed to resolve ticket database")
            return None

    @property
    def available(self) -> bool:
        return self._db() is not None

    async def ensure_indexes(self) -> None:
        db = self._db()
        if db is None:
            return
        try:
            await db[CONFIG_COLLECTION].create_index([("guild_id", ASCENDING)], unique=True)
            await db[TICKETS_COLLECTION].create_index([("channel_id", ASCENDING)], unique=True)
            await db[TICKETS_COLLECTION].create_index(
                [("guild_id", ASCENDING), ("owner_id", ASCENDING), ("status", ASCENDING)]
            )
            await db[TICKETS_COLLECTION].create_index(
                [("guild_id", ASCENDING), ("status", ASCENDING)]
            )
            await db[TICKETS_COLLECTION].create_index(
                [("guild_id", ASCENDING), ("number", ASCENDING)]
            )
            await db[COUNTERS_COLLECTION].create_index([("guild_id", ASCENDING)], unique=True)
        except Exception:
            log.exception("Failed to create ticket indexes")

    # ------------------------------------------------------------------ #
    # configuration
    # ------------------------------------------------------------------ #
    async def get_config(self, guild_id: int) -> Optional[Dict[str, Any]]:
        db = self._db()
        if db is None:
            return None
        try:
            return await db[CONFIG_COLLECTION].find_one({"guild_id": guild_id})
        except Exception:
            log.exception("Failed to load ticket config for guild %s", guild_id)
            return None

    async def save_config(self, guild_id: int, data: Dict[str, Any]) -> bool:
        db = self._db()
        if db is None:
            return False
        payload = dict(data)
        payload["guild_id"] = guild_id
        payload["updated_at"] = utcnow()
        try:
            await db[CONFIG_COLLECTION].update_one(
                {"guild_id": guild_id}, {"$set": payload}, upsert=True
            )
            return True
        except Exception:
            log.exception("Failed to save ticket config for guild %s", guild_id)
            return False

    async def all_configs(self) -> List[Dict[str, Any]]:
        db = self._db()
        if db is None:
            return []
        try:
            return [doc async for doc in db[CONFIG_COLLECTION].find({})]
        except Exception:
            log.exception("Failed to load all ticket configs")
            return []

    # ------------------------------------------------------------------ #
    # counters
    # ------------------------------------------------------------------ #
    async def next_number(self, guild_id: int) -> Optional[int]:
        db = self._db()
        if db is None:
            return None
        try:
            doc = await db[COUNTERS_COLLECTION].find_one_and_update(
                {"guild_id": guild_id},
                {"$inc": {"seq": 1}},
                upsert=True,
                return_document=ReturnDocument.AFTER,
            )
            return int(doc.get("seq", 1))
        except Exception:
            log.exception("Failed to allocate ticket number for guild %s", guild_id)
            return None

    # ------------------------------------------------------------------ #
    # tickets
    # ------------------------------------------------------------------ #
    async def create_ticket(self, doc: Dict[str, Any]) -> bool:
        db = self._db()
        if db is None:
            return False
        try:
            await db[TICKETS_COLLECTION].insert_one(dict(doc))
            return True
        except Exception:
            log.exception("Failed to create ticket document for channel %s", doc.get("channel_id"))
            return False

    async def get_ticket(self, channel_id: int) -> Optional[Dict[str, Any]]:
        db = self._db()
        if db is None:
            return None
        try:
            return await db[TICKETS_COLLECTION].find_one({"channel_id": channel_id})
        except Exception:
            log.exception("Failed to fetch ticket for channel %s", channel_id)
            return None

    async def get_open_ticket_for_user(
        self, guild_id: int, user_id: int
    ) -> Optional[Dict[str, Any]]:
        db = self._db()
        if db is None:
            return None
        try:
            return await db[TICKETS_COLLECTION].find_one(
                {
                    "guild_id": guild_id,
                    "owner_id": user_id,
                    "status": {"$in": list(OPEN_STATUSES)},
                }
            )
        except Exception:
            log.exception("Failed to check open ticket for user %s", user_id)
            return None

    async def get_open_tickets(self, guild_id: Optional[int] = None) -> List[Dict[str, Any]]:
        db = self._db()
        if db is None:
            return []
        query: Dict[str, Any] = {"status": {"$in": list(OPEN_STATUSES)}}
        if guild_id is not None:
            query["guild_id"] = guild_id
        try:
            return [doc async for doc in db[TICKETS_COLLECTION].find(query)]
        except Exception:
            log.exception("Failed to load open tickets")
            return []

    async def update_ticket(self, channel_id: int, data: Dict[str, Any]) -> bool:
        db = self._db()
        if db is None:
            return False
        try:
            await db[TICKETS_COLLECTION].update_one({"channel_id": channel_id}, {"$set": dict(data)})
            return True
        except Exception:
            log.exception("Failed to update ticket for channel %s", channel_id)
            return False

    async def close_ticket(self, channel_id: int, data: Dict[str, Any]) -> bool:
        payload = dict(data)
        payload["status"] = "closed"
        payload["closed_at"] = payload.get("closed_at") or utcnow()
        return await self.update_ticket(channel_id, payload)

    async def delete_ticket(self, channel_id: int) -> bool:
        db = self._db()
        if db is None:
            return False
        try:
            await db[TICKETS_COLLECTION].delete_one({"channel_id": channel_id})
            return True
        except Exception:
            log.exception("Failed to delete ticket for channel %s", channel_id)
            return False

    async def count_tickets(self, guild_id: int) -> int:
        db = self._db()
        if db is None:
            return 0
        try:
            return int(await db[TICKETS_COLLECTION].count_documents({"guild_id": guild_id}))
        except Exception:
            log.exception("Failed to count tickets for guild %s", guild_id)
            return 0
