import asyncio
from contextlib import asynccontextmanager


class EventBus:
    def __init__(self):
        self.subscribers: set[asyncio.Queue] = set()

    def publish(self, event: dict):
        for queue in tuple(self.subscribers):
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(event)

    @asynccontextmanager
    async def subscribe(self):
        queue = asyncio.Queue(maxsize=128)
        self.subscribers.add(queue)
        try:
            yield queue
        finally:
            self.subscribers.discard(queue)
