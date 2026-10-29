"""In-process pub/sub that fans server events out to every open SSE stream."""

import asyncio
from dataclasses import dataclass


@dataclass(frozen=True)
class Message:
    kind: str  # "webhook" (new event row; data = seq) or "tasks" (cards changed)
    data: str = ""


class Broadcaster:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[Message]] = set()

    def subscribe(self) -> asyncio.Queue[Message]:
        q: asyncio.Queue[Message] = asyncio.Queue(maxsize=200)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[Message]) -> None:
        self._subscribers.discard(q)

    def publish(self, msg: Message) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                # A stuck browser; it will replay from SQLite on reconnect.
                self._subscribers.discard(q)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)


broadcaster = Broadcaster()
