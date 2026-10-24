import asyncio
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
from jose import JWTError, jwt
from redis.asyncio import Redis

from app.core.config import settings
from app.core.redis import get_redis
from app.core.security import ALGORITHM

router = APIRouter()


@router.websocket("/ws/tasks")
async def tasks_websocket_endpoint(
    websocket: WebSocket,
    token: str = Query(...),
):
    """
    Subscribes connected clients to live board updates, task transitions,
    and timesheet locking events broadcast across the Redis 'task_events' channel.
    """
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGORITHM])
        user_id = payload.get("sub")
        if not user_id:
            await websocket.close(code=1008)
            return
    except JWTError:
        await websocket.close(code=1008)
        return

    await websocket.accept()
    redis: Redis = await anext(get_redis())
    pubsub = redis.pubsub()
    await pubsub.subscribe("task_events")

    try:
        while True:
            # Poll Redis Pub/Sub channel non-blockingly
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if message and message["type"] == "message":
                await websocket.send_text(message["data"].decode("utf-8"))
            await asyncio.sleep(0.01)
    except WebSocketDisconnect:
        await pubsub.unsubscribe("task_events")
        await pubsub.close()