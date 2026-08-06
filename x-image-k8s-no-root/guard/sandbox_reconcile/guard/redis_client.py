from __future__ import annotations

"""Redis/Redis Cluster 基础客户端。

该模块只封装 guard 需要的窄用途 Redis 能力，不承载业务上报语义。
支持 standalone Redis，也支持 Redis Cluster 的 MOVED/ASK 重定向。
"""

from dataclasses import dataclass
import socket
from typing import Any


class RedisClientError(RuntimeError):
    pass


class RedisCommandError(RedisClientError):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message
        self.code = message.split(maxsplit=1)[0] if message else ""


@dataclass(frozen=True)
class RedisNode:
    host: str
    port: int


@dataclass(frozen=True)
class RedisRedirect:
    kind: str
    slot: int
    node: RedisNode


def parse_redis_node(value: str) -> RedisNode:
    text = str(value).strip()
    if not text:
        raise RedisClientError("redis node is empty")
    if ":" not in text:
        raise RedisClientError(f"redis node must be host:port: {text}")
    host, port_text = text.rsplit(":", 1)
    host = host.strip()
    if not host:
        raise RedisClientError(f"redis node host is empty: {text}")
    try:
        port = int(port_text)
    except Exception as exc:
        raise RedisClientError(f"redis node port invalid: {text}") from exc
    if port <= 0:
        raise RedisClientError(f"redis node port invalid: {text}")
    return RedisNode(host=host, port=port)


def parse_redis_nodes(value: Any) -> list[RedisNode]:
    if isinstance(value, str):
        raw_items = [item.strip() for item in value.split(",") if item.strip()]
    elif isinstance(value, list):
        raw_items = value
    else:
        raise RedisClientError("redis.nodes must be array or comma separated string")
    nodes = [parse_redis_node(str(item)) for item in raw_items]
    if not nodes:
        raise RedisClientError("redis.nodes is empty")
    return nodes


def _encode_command(*parts: str | int | bytes) -> bytes:
    chunks = [f"*{len(parts)}\r\n".encode("ascii")]
    for part in parts:
        data = part if isinstance(part, bytes) else str(part).encode("utf-8")
        chunks.append(f"${len(data)}\r\n".encode("ascii"))
        chunks.append(data)
        chunks.append(b"\r\n")
    return b"".join(chunks)


def _read_line(fp) -> bytes:
    line = fp.readline()
    if not line:
        raise RedisClientError("redis connection closed")
    if not line.endswith(b"\r\n"):
        raise RedisClientError("redis response line missing CRLF")
    return line[:-2]


def _read_response(fp) -> Any:
    prefix = fp.read(1)
    if not prefix:
        raise RedisClientError("redis connection closed")
    if prefix == b"+":
        return _read_line(fp).decode("utf-8", errors="replace")
    if prefix == b"-":
        raise RedisCommandError(_read_line(fp).decode("utf-8", errors="replace"))
    if prefix == b":":
        return int(_read_line(fp))
    if prefix == b"$":
        length = int(_read_line(fp))
        if length < 0:
            return None
        data = fp.read(length)
        trailer = fp.read(2)
        if trailer != b"\r\n":
            raise RedisClientError("redis bulk response missing CRLF")
        return data
    if prefix == b"*":
        count = int(_read_line(fp))
        if count < 0:
            return None
        return [_read_response(fp) for _ in range(count)]
    raise RedisClientError(f"unsupported redis response prefix: {prefix!r}")


class RedisConnection:
    def __init__(
        self,
        node: RedisNode,
        *,
        password: str = "",
        username: str = "",
        connect_timeout_seconds: float = 1.0,
        command_timeout_seconds: float = 2.0,
    ):
        self.node = node
        self.password = password
        self.username = username
        self.connect_timeout_seconds = connect_timeout_seconds
        self.command_timeout_seconds = command_timeout_seconds
        self.sock = None
        self.fp = None

    def __enter__(self) -> "RedisConnection":
        self.sock = socket.create_connection(
            (self.node.host, self.node.port),
            timeout=self.connect_timeout_seconds,
        )
        self.sock.settimeout(self.command_timeout_seconds)
        self.fp = self.sock.makefile("rb")
        if self.password:
            if self.username:
                self.execute("AUTH", self.username, self.password)
            else:
                self.execute("AUTH", self.password)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if self.fp is not None:
                self.fp.close()
        finally:
            if self.sock is not None:
                self.sock.close()

    def execute(self, *parts: str | int | bytes) -> Any:
        if self.sock is None or self.fp is None:
            raise RedisClientError("redis connection not opened")
        self.sock.sendall(_encode_command(*parts))
        return _read_response(self.fp)


def _key_hash_tag(key: str) -> str:
    start = key.find("{")
    if start < 0:
        return key
    end = key.find("}", start + 1)
    if end < 0 or end == start + 1:
        return key
    return key[start + 1 : end]


def _crc16(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def redis_key_slot(key: str) -> int:
    tag = _key_hash_tag(str(key))
    return _crc16(tag.encode("utf-8")) % 16384


def _parse_redirect(message: str) -> RedisRedirect | None:
    # MOVED 3999 127.0.0.1:6381 / ASK 3999 127.0.0.1:6381
    fields = str(message).strip().split()
    if len(fields) < 3 or fields[0] not in {"MOVED", "ASK"}:
        return None
    try:
        slot = int(fields[1])
    except Exception:
        return None
    return RedisRedirect(kind=fields[0], slot=slot, node=parse_redis_node(fields[2]))


def _decode_bulk(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


class RedisClient:
    def __init__(
        self,
        nodes: list[RedisNode],
        *,
        password: str = "",
        username: str = "",
        connect_timeout_seconds: float = 1.0,
        command_timeout_seconds: float = 2.0,
        max_attempts: int = 3,
        max_redirects: int = 5,
    ):
        if not nodes:
            raise RedisClientError("redis nodes are empty")
        self.nodes = nodes
        self.password = password
        self.username = username
        self.connect_timeout_seconds = connect_timeout_seconds
        self.command_timeout_seconds = command_timeout_seconds
        self.max_attempts = max(1, max_attempts)
        self.max_redirects = max(0, max_redirects)
        self._slot_nodes: dict[int, RedisNode] = {}
        self._next_node_index = 0

    def _connection(self, node: RedisNode) -> RedisConnection:
        return RedisConnection(
            node,
            password=self.password,
            username=self.username,
            connect_timeout_seconds=self.connect_timeout_seconds,
            command_timeout_seconds=self.command_timeout_seconds,
        )

    def _next_startup_node(self) -> RedisNode:
        node = self.nodes[self._next_node_index % len(self.nodes)]
        self._next_node_index += 1
        return node

    def _candidate_node(self, slot: int | None) -> RedisNode:
        if slot is not None and slot in self._slot_nodes:
            return self._slot_nodes[slot]
        return self._next_startup_node()

    def _execute_on_node(
        self,
        node: RedisNode,
        command: tuple[str | int | bytes, ...],
        *,
        asking: bool = False,
    ) -> Any:
        with self._connection(node) as conn:
            if asking:
                asking_res = conn.execute("ASKING")
                if str(asking_res) != "OK":
                    raise RedisClientError(f"unexpected ASKING response: {asking_res!r}")
            return conn.execute(*command)

    def execute(self, *parts: str | int | bytes, key: str | None = None) -> Any:
        if not parts:
            raise RedisClientError("redis command is empty")
        command = tuple(parts)
        slot = redis_key_slot(key) if key else None
        attempts = 0
        redirects = 0
        last_error = ""
        next_node: RedisNode | None = self._candidate_node(slot)
        asking = False

        while attempts < self.max_attempts + self.max_redirects:
            node = next_node or self._candidate_node(slot)
            next_node = None
            attempts += 1
            try:
                return self._execute_on_node(node, command, asking=asking)
            except RedisCommandError as exc:
                redirect = _parse_redirect(exc.message)
                if redirect is None or redirects >= self.max_redirects:
                    raise
                redirects += 1
                if redirect.kind == "MOVED":
                    self._slot_nodes[redirect.slot] = redirect.node
                    if slot is not None:
                        self._slot_nodes[slot] = redirect.node
                    asking = False
                else:
                    asking = True
                next_node = redirect.node
                last_error = exc.message
                continue
            except Exception as exc:
                asking = False
                last_error = str(exc)
                next_node = self._candidate_node(slot)
                continue
        raise RedisClientError(last_error or "redis command failed")

    def ping(self) -> bool:
        return str(self.execute("PING")) == "PONG"

    def get(self, key: str) -> str | None:
        return _decode_bulk(self.execute("GET", key, key=key))

    def set(self, key: str, value: str, *, ttl_seconds: int | None = None) -> bool:
        if ttl_seconds is not None and int(ttl_seconds) > 0:
            res = self.execute("SET", key, value, "EX", int(ttl_seconds), key=key)
        else:
            res = self.execute("SET", key, value, key=key)
        return str(res) == "OK"

    def delete(self, key: str) -> int:
        return int(self.execute("DEL", key, key=key))

    def expire(self, key: str, ttl_seconds: int) -> bool:
        return int(self.execute("EXPIRE", key, int(ttl_seconds), key=key)) == 1


def build_redis_client(cfg: dict[str, Any]) -> RedisClient:
    redis_cfg = cfg.get("redis", {}) or {}
    if not isinstance(redis_cfg, dict):
        raise RedisClientError("redis must be object")
    nodes = parse_redis_nodes(redis_cfg.get("nodes") or ["127.0.0.1:6379"])
    return RedisClient(
        nodes,
        password=str(redis_cfg.get("password") or ""),
        username=str(redis_cfg.get("username") or ""),
        connect_timeout_seconds=float(redis_cfg.get("connect_timeout_seconds", 1.0)),
        command_timeout_seconds=float(redis_cfg.get("command_timeout_seconds", 2.0)),
        max_attempts=int(redis_cfg.get("max_attempts", 3)),
        max_redirects=int(redis_cfg.get("max_redirects", 5)),
    )
