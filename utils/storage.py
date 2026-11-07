"""媒体文件存储后端。

开发环境可使用本地 FileSystemStorage；配置 FastDFS 后，新上传文件通过
Nginx 暴露的 HTTP 上传代理写入 FastDFS。数据库中已有的本地相对路径仍
回退到 MEDIA_URL，便于从本地存储平滑迁移。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import quote, urljoin

import requests
from django.conf import settings
from django.core.files.storage import FileSystemStorage, Storage


class FastDFSStorageError(RuntimeError):
    """FastDFS 上传、删除或状态查询失败。"""


_FASTDFS_FILE_ID = re.compile(r"^group[0-9]+/")


def _is_fastdfs_file_id(name: str) -> bool:
    return bool(_FASTDFS_FILE_ID.match(name or ""))


def _media_url(name: str) -> str:
    return urljoin(settings.MEDIA_URL, quote(name, safe="/"))


class LocalMediaStorage(FileSystemStorage):
    """显式绑定 Django MEDIA_ROOT / MEDIA_URL 的本地存储。"""

    def __init__(self, *args, **kwargs) -> None:
        kwargs.setdefault("location", settings.MEDIA_ROOT)
        kwargs.setdefault("base_url", settings.MEDIA_URL)
        super().__init__(*args, **kwargs)


class FastDFSStorage(Storage):
    """通过 FastDFS HTTP 代理保存媒体文件。"""

    def _endpoint(self, setting_name: str) -> str:
        return str(getattr(settings, setting_name, "")).rstrip("/")

    def _headers(self) -> dict[str, str]:
        return {"X-FastDFS-Token": settings.FDFS_UPLOAD_TOKEN}

    def _legacy_path(self, name: str) -> Path:
        media_root = Path(settings.MEDIA_ROOT).resolve()
        candidate = (media_root / name).resolve()
        if candidate != media_root and media_root not in candidate.parents:
            raise FastDFSStorageError("非法媒体文件路径")
        return candidate

    def _save(self, name: str, content) -> str:
        extension = Path(name).suffix.lstrip(".").lower()
        payload = b"".join(content.chunks()) if hasattr(content, "chunks") else content.read()
        try:
            response = requests.post(
                self._endpoint("FDFS_UPLOAD_URL"),
                params={"ext": extension},
                data=payload,
                headers={
                    **self._headers(),
                    "Content-Type": "application/octet-stream",
                },
                timeout=settings.FDFS_TIMEOUT,
            )
            response.raise_for_status()
            file_id = response.json().get("file_id", "")
        except requests.HTTPError as exc:
            detail = exc.response.text[:500] if exc.response is not None else ""
            raise FastDFSStorageError(f"FastDFS 上传失败：{exc}；{detail}") from exc
        except (requests.RequestException, ValueError) as exc:
            raise FastDFSStorageError(f"FastDFS 上传失败：{exc}") from exc
        if not _is_fastdfs_file_id(file_id):
            raise FastDFSStorageError(f"FastDFS 返回了非法文件 ID：{file_id!r}")
        return file_id

    def delete(self, name: str) -> None:
        if not name:
            return
        if not _is_fastdfs_file_id(name):
            self._legacy_path(name).unlink(missing_ok=True)
            return
        try:
            response = requests.post(
                self._endpoint("FDFS_DELETE_URL"),
                json={"file_id": name},
                headers=self._headers(),
                timeout=settings.FDFS_TIMEOUT,
            )
            response.raise_for_status()
        except requests.HTTPError as exc:
            detail = exc.response.text[:500] if exc.response is not None else ""
            raise FastDFSStorageError(f"FastDFS 删除失败：{exc}；{detail}") from exc
        except requests.RequestException as exc:
            raise FastDFSStorageError(f"FastDFS 删除失败：{exc}") from exc

    def exists(self, name: str) -> bool:
        if not _is_fastdfs_file_id(name):
            return self._legacy_path(name).is_file()
        try:
            response = requests.head(
                self.url(name),
                timeout=settings.FDFS_TIMEOUT,
                allow_redirects=True,
            )
            return response.status_code == 200
        except requests.RequestException:
            return False

    def size(self, name: str) -> int:
        if not _is_fastdfs_file_id(name):
            return self._legacy_path(name).stat().st_size
        try:
            response = requests.head(
                self.url(name),
                timeout=settings.FDFS_TIMEOUT,
                allow_redirects=True,
            )
            response.raise_for_status()
            return int(response.headers["Content-Length"])
        except (requests.RequestException, KeyError, ValueError) as exc:
            raise FastDFSStorageError(f"FastDFS 文件大小查询失败：{exc}") from exc

    def url(self, name: str) -> str:
        if not _is_fastdfs_file_id(name):
            return _media_url(name)
        base_url = str(settings.FDFS_URL).rstrip("/") + "/"
        return urljoin(base_url, quote(name, safe="/"))

    def get_available_name(self, name: str, max_length: int | None = None) -> str:
        return name
