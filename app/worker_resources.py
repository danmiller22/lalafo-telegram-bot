import asyncio

# On the small hosted instance, only one native image/source worker may run.
inventory_worker_lock = asyncio.Lock()
