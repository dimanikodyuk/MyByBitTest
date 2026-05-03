"""
WebSocket для live оновлень
"""

from fastapi import WebSocket, WebSocketDisconnect
from typing import List, Dict, Any
import asyncio
import json
from datetime import datetime


class ConnectionManager:
    """Менеджер WebSocket з'єднань"""

    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: Dict[str, Any]):
        """Надсилання повідомлення всім клієнтам"""
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except:
                pass


manager = ConnectionManager()


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket ендпоінт для live даних"""
    await manager.connect(websocket)
    try:
        while True:
            # Отримуємо повідомлення від клієнта (ping)
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)