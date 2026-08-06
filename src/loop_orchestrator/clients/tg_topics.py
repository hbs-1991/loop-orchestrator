"""Forum topics per run (Bot API 10.0; works in private chats too).

Adapted from a sibling bot project's topic manager: every operation is fail-safe.
A chat without topics, a missing right or a rate limit degrades to None/no-op,
never to a raised exception. A None thread id means "deliver flat" — exactly
what the bot did before threads existed.
"""
import logging

import httpx

log = logging.getLogger(__name__)

TOPIC_NAME_LIMIT = 128  # Telegram's limit on a forum-topic name


class TopicManager:
    def __init__(self, http: httpx.AsyncClient, chat_id: int):
        self._http = http
        self.chat_id = chat_id

    async def _call(self, method: str, payload: dict) -> dict | bool:
        r = await self._http.post(f"/{method}", json=payload)
        r.raise_for_status()
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"{method} returned ok=false")
        return data["result"]

    async def create(self, name: str) -> int | None:
        try:
            result = await self._call("createForumTopic", {
                "chat_id": self.chat_id, "name": name[:TOPIC_NAME_LIMIT]})
            return int(result["message_thread_id"])
        except Exception:
            log.warning("createForumTopic failed for chat=%s; delivering flat",
                        self.chat_id, exc_info=True)
            return None

    async def rename(self, thread_id: int, name: str) -> None:
        try:
            await self._call("editForumTopic", {
                "chat_id": self.chat_id, "message_thread_id": thread_id,
                "name": name[:TOPIC_NAME_LIMIT]})
        except Exception:
            log.warning("editForumTopic failed for chat=%s thread=%s",
                        self.chat_id, thread_id, exc_info=True)

    async def close(self, thread_id: int) -> None:
        try:
            await self._call("closeForumTopic", {
                "chat_id": self.chat_id, "message_thread_id": thread_id})
        except Exception:
            log.warning("closeForumTopic failed for chat=%s thread=%s",
                        self.chat_id, thread_id, exc_info=True)
