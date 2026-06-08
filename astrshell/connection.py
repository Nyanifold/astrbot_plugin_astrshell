"""UDS/TCP connection registry and broadcast.

Consolidates the retained parts of astrshell/connection.py.
Removed: _init_buffer, broadcast_init, _ready_event, signal_ready,
         _idle_timeout, _idle_loop, _check_idle, wait_idle_shutdown.
"""
import asyncio
import os
import time
from dataclasses import dataclass


def _is_tcp(addr: str) -> bool:
    """Return True if addr looks like host:port (TCP), False if a file path (UDS)."""
    if addr.startswith("/") or addr.startswith("~"):
        return False
    parts = addr.rsplit(":", 1)
    return len(parts) == 2 and parts[1].isdigit()


@dataclass
class ClientConnection:
    """Lightweight record of a connected client.

    FROM: astrshell/connection.py ClientConnection
    Removed: reader field (not needed for broadcast/send).
    """
    conn_id: str
    session_id: str
    writer: asyncio.StreamWriter
    connected_at: float


class ConnectionManager:
    """Manages UDS server lifecycle and connected client registry.

    FROM: astrshell/connection.py ConnectionManager — stripped down.
    Removed: init buffering, ready_event, idle timeout.
    Retained: register/unregister, send, broadcast, start/close.
    """

    def __init__(self) -> None:
        self._connections: dict[str, ClientConnection] = {}
        self._lock = asyncio.Lock()
        self._server: asyncio.Server | None = None
        self._start_time: float = time.time()

    def connection_count(self) -> int:
        return len(self._connections)

    def register(self, conn_id: str, session_id: str,
                 writer: asyncio.StreamWriter) -> None:
        """Register a new connection after successful handshake."""
        self._connections[conn_id] = ClientConnection(
            conn_id=conn_id,
            session_id=session_id,
            writer=writer,
            connected_at=time.time(),
        )

    def unregister(self, conn_id: str) -> None:
        """Remove a connection (on disconnect or error)."""
        self._connections.pop(conn_id, None)

    async def send(self, conn_id: str, data: bytes) -> None:
        """Send data to one specific connection. Removes dead connections."""
        async with self._lock:
            conn = self._connections.get(conn_id)
        if conn is None:
            return
        try:
            conn.writer.write(data)
            await conn.writer.drain()
        except OSError:
            self.unregister(conn_id)
            try:
                conn.writer.close()
            except OSError:
                pass

    async def broadcast(self, data: bytes) -> None:
        """Send data to all connected clients. Removes dead connections."""
        async with self._lock:
            snapshot = list(self._connections.items())
        dead: list[str] = []
        for conn_id, conn in snapshot:
            try:
                conn.writer.write(data)
                await conn.writer.drain()
            except OSError:
                dead.append(conn_id)
                try:
                    conn.writer.close()
                except OSError:
                    pass
        for conn_id in dead:
            self.unregister(conn_id)

    async def start(self, socket_path: str, connection_handler) -> None:
        """Start server at socket_path, calling connection_handler per connection.

        socket_path can be:
          - A file path (UDS): e.g. ~/.astrshell/daemon.sock
          - A host:port string (TCP): e.g. 127.0.0.1:7890 or :7890
        FROM: astrshell/connection.py start_server() — simplified signature.
        """
        if _is_tcp(socket_path):
            host, port_str = socket_path.rsplit(":", 1)
            self._server = await asyncio.start_server(
                connection_handler,
                host=host or "127.0.0.1",
                port=int(port_str),
            )
        else:
            try:
                os.unlink(socket_path)
            except FileNotFoundError:
                pass
            self._server = await asyncio.start_unix_server(
                connection_handler, path=socket_path
            )

    async def serve_forever(self) -> None:
        """Block until the server is stopped (via close() or CancelledError)."""
        assert self._server is not None, "call start() first"
        async with self._server:
            await self._server.serve_forever()

    async def close(self) -> None:
        """Close all connections and the server socket.

        Writers are closed first so connection handler coroutines unblock
        (their reader gets EOF), allowing server.wait_closed() to resolve.
        """
        async with self._lock:
            conns = list(self._connections.values())
            self._connections.clear()
        for conn in conns:
            try:
                conn.writer.close()
                await conn.writer.wait_closed()
            except OSError:
                pass
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    def get_writer(self, conn_id: str):
        """Return the StreamWriter for conn_id, or None if not connected."""
        conn = self._connections.get(conn_id)
        return conn.writer if conn is not None else None

    def get_status(self) -> dict:
        """Return status dict for the //status slash command."""
        import os as _os
        return {
            "connection_count": len(self._connections),
            "uptime_seconds": int(time.time() - self._start_time),
            "pid": _os.getpid(),
        }
