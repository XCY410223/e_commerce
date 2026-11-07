"""FastDFS 上传代理。

宿主机上的 Django 无法直接连接 Docker 容器内网中的 Storage 节点，
因此由本服务在容器网络内完成 FastDFS 二进制协议交互，并向上层提供
简单的 HTTP 上传/删除接口。仅依赖 Python 标准库。
"""

from __future__ import annotations

import hmac
import json
import os
import socket
import struct
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


HEADER = struct.Struct("!QBB")
TRACKER_QUERY_STORE_ONE = 101
TRACKER_QUERY_UPDATE = 103
STORAGE_UPLOAD_FILE = 11
STORAGE_DELETE_FILE = 12
GROUP_NAME_SIZE = 16
MAX_EXT_NAME_SIZE = 6

TRACKER_SERVER = os.getenv("TRACKER_SERVER", "fastdfs-tracker:22122")
UPLOAD_TOKEN = os.getenv("FDFS_UPLOAD_TOKEN", "dev-fastdfs-token")
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(100 * 1024 * 1024)))


class FastDFSProtocolError(RuntimeError):
    """FastDFS 返回错误或响应格式不合法。"""


def _read_exact(sock: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise FastDFSProtocolError("FastDFS 连接提前关闭")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _receive(sock: socket.socket) -> tuple[int, bytes]:
    body_size, command, status = HEADER.unpack(_read_exact(sock, HEADER.size))
    body = _read_exact(sock, body_size) if body_size else b""
    if status != 0:
        raise FastDFSProtocolError(f"FastDFS 命令 {command} 失败，状态码 {status}")
    return command, body


def _send(sock: socket.socket, command: int, body: bytes = b"") -> None:
    sock.sendall(HEADER.pack(len(body), command, 0) + body)


def _decode_c_string(value: bytes) -> str:
    return value.split(b"\x00", 1)[0].decode("utf-8", errors="replace")


class FastDFSClient:
    """只实现上传、删除所需的最小 FastDFS 协议。"""

    def __init__(self, tracker_server: str, timeout: float = 10.0) -> None:
        host, separator, port = tracker_server.rpartition(":")
        if not separator or not host or not port.isdigit():
            raise ValueError("TRACKER_SERVER 必须为 host:port 格式")
        self.tracker_host = host
        self.tracker_port = int(port)
        self.timeout = timeout

    def _connect(self, host: str, port: int) -> socket.socket:
        return socket.create_connection((host, port), timeout=self.timeout)

    def _query_store(self) -> tuple[str, str, int, int]:
        with self._connect(self.tracker_host, self.tracker_port) as sock:
            _send(sock, TRACKER_QUERY_STORE_ONE)
            _, response = _receive(sock)

        if len(response) < 40:
            raise FastDFSProtocolError("Tracker 返回的 Storage 信息不完整")
        group_raw, ip_raw, port, store_path = struct.unpack("!16s15sQB", response[:40])
        return _decode_c_string(group_raw), _decode_c_string(ip_raw), port, store_path

    def _query_update_storage(self, group: str, remote_name: str) -> tuple[str, int]:
        body = (
            group.encode("utf-8")[:GROUP_NAME_SIZE].ljust(GROUP_NAME_SIZE, b"\x00")
            + remote_name.encode("utf-8")
        )
        with self._connect(self.tracker_host, self.tracker_port) as sock:
            _send(sock, TRACKER_QUERY_UPDATE, body)
            _, response = _receive(sock)

        if len(response) < 39:
            raise FastDFSProtocolError("Tracker 返回的更新 Storage 信息不完整")
        _, ip_raw, port = struct.unpack("!16s15sQ", response[:39])
        return _decode_c_string(ip_raw), port

    def ready(self) -> None:
        """确认 Tracker 已能分配可用的 Storage。"""
        group, storage_ip, storage_port, _ = self._query_store()
        if not group or not storage_ip or not storage_port:
            raise FastDFSProtocolError("Tracker 尚未返回可用的 Storage")

    def upload(self, data: bytes, ext_name: str = "") -> str:
        ext = ext_name.strip().lstrip(".").lower().encode("ascii", errors="ignore")
        if len(ext) > MAX_EXT_NAME_SIZE:
            raise ValueError(f"文件扩展名不能超过 {MAX_EXT_NAME_SIZE} 个字符")

        group, storage_ip, storage_port, store_path = self._query_store()
        body = struct.pack("!BQ6s", store_path, len(data), ext) + data
        with self._connect(storage_ip, storage_port) as sock:
            _send(sock, STORAGE_UPLOAD_FILE, body)
            _, response = _receive(sock)

        if len(response) < GROUP_NAME_SIZE + 1:
            raise FastDFSProtocolError("Storage 返回的文件 ID 不完整")
        response_group = _decode_c_string(response[:GROUP_NAME_SIZE]) or group
        remote_name = response[GROUP_NAME_SIZE:].rstrip(b"\x00").decode("utf-8")
        return f"{response_group}/{remote_name}"

    def delete(self, file_id: str) -> None:
        group, separator, remote_name = file_id.partition("/")
        if not separator or not group or not remote_name:
            raise ValueError("文件 ID 必须为 group/remote_name 格式")

        storage_ip, storage_port = self._query_update_storage(group, remote_name)
        body = (
            group.encode("utf-8")[:GROUP_NAME_SIZE].ljust(GROUP_NAME_SIZE, b"\x00")
            + remote_name.encode("utf-8")
        )
        with self._connect(storage_ip, storage_port) as sock:
            _send(sock, STORAGE_DELETE_FILE, body)
            _receive(sock)


client = FastDFSClient(TRACKER_SERVER)


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "FastDFSUploader/1.0"

    def _json(self, status: int, payload: dict[str, object]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        supplied = self.headers.get("X-FastDFS-Token", "")
        return bool(UPLOAD_TOKEN) and hmac.compare_digest(supplied, UPLOAD_TOKEN)

    def _read_json(self) -> dict[str, object]:
        size = int(self.headers.get("Content-Length", "0"))
        if size <= 0 or size > 1024 * 1024:
            raise ValueError("JSON 请求体为空或过大")
        return json.loads(self.rfile.read(size).decode("utf-8"))

    def do_GET(self) -> None:  # noqa: N802
        if urlparse(self.path).path == "/healthz":
            try:
                client.ready()
                self._json(200, {"status": "ok"})
            except (FastDFSProtocolError, OSError, ValueError) as exc:
                self._json(503, {"status": "starting", "error": str(exc)})
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path not in {"/upload", "/delete"}:
            self._json(404, {"error": "not found"})
            return
        if not self._authorized():
            self._json(401, {"error": "上传令牌无效"})
            return

        try:
            if parsed.path == "/upload":
                size = int(self.headers.get("Content-Length", "0"))
                if size <= 0:
                    raise ValueError("上传内容为空")
                if size > MAX_UPLOAD_BYTES:
                    raise ValueError(f"文件超过上限 {MAX_UPLOAD_BYTES} 字节")
                ext_name = parse_qs(parsed.query).get("ext", [""])[0]
                data = self.rfile.read(size)
                file_id = client.upload(data, ext_name)
                self._json(200, {"file_id": file_id})
                return

            payload = self._read_json()
            file_id = str(payload.get("file_id", ""))
            client.delete(file_id)
            self._json(200, {"deleted": file_id})
        except (ValueError, FastDFSProtocolError, OSError) as exc:
            self._json(400, {"error": str(exc)})

    def log_message(self, format: str, *args: object) -> None:
        print(f"{self.address_string()} - {format % args}", flush=True)


if __name__ == "__main__":
    ThreadingHTTPServer.daemon_threads = True
    ThreadingHTTPServer(("0.0.0.0", 8889), RequestHandler).serve_forever()
