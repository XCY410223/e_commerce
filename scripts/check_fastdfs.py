"""FastDFS 端到端自检：上传、下载、删除。"""

from __future__ import annotations

import io
import os
import sys
import uuid
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR / "apps"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402
import requests  # noqa: E402
from PIL import Image  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402
from django.core.files.base import ContentFile  # noqa: E402
from django.core.files.storage import default_storage  # noqa: E402


def make_png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), color=(46, 139, 87)).save(buffer, format="PNG")
    return buffer.getvalue()


def main() -> int:
    backend = settings.STORAGES["default"]["BACKEND"]
    print(f"存储后端：{backend}")
    print(f"FastDFS 网关：{settings.FDFS_URL}")
    if not backend.endswith("FastDFSStorage"):
        print("当前不是 FastDFS 存储，请修改 .env 中的 STORAGE_BACKEND。")
        return 2

    health_url = settings.FDFS_URL + "healthz"
    try:
        health = requests.get(health_url, timeout=settings.FDFS_TIMEOUT)
        health.raise_for_status()
        print(f"[OK] Nginx/上传代理健康检查：{health_url}")
    except requests.RequestException as exc:
        print(f"[FAIL] FastDFS 网关不可用：{exc}")
        print("       请先运行 docker compose up -d。")
        return 1

    file_name = f"healthcheck-{uuid.uuid4().hex}.png"
    png = make_png()
    file_id = ""
    try:
        file_id = default_storage.save(file_name, ContentFile(png))
        file_url = default_storage.url(file_id)
        response = requests.get(file_url, timeout=settings.FDFS_TIMEOUT)
        response.raise_for_status()
        if response.content != png:
            raise RuntimeError("下载内容与上传内容不一致")
        print(f"[OK] 上传成功：{file_id}")
        print(f"[OK] Nginx 下载成功：{file_url}")
        print(f"[OK] 文件大小：{default_storage.size(file_id)} 字节")
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] FastDFS 端到端检查失败：{exc}")
        return 1
    finally:
        if file_id:
            try:
                default_storage.delete(file_id)
                print(f"[OK] 测试文件已删除：{file_id}")
            except Exception as exc:  # noqa: BLE001
                print(f"[WARN] 测试文件删除失败：{exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
