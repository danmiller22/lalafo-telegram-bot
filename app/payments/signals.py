"""Wake active payment screens after commit, without database polling."""
import asyncio

waiters: dict[int, set[asyncio.Event]] = {}


def notify(user_id: int):
    for event in tuple(waiters.get(user_id, ())):
        event.set()


def subscribe(user_id: int):
    events = waiters.setdefault(user_id, set())
    if len(events) >= 4:
        raise ValueError("Too many payment screens")
    event = asyncio.Event()
    events.add(event)
    return event


def unsubscribe(user_id: int, event):
    events = waiters.get(user_id)
    if events is not None:
        events.discard(event)
        if not events:
            waiters.pop(user_id, None)
