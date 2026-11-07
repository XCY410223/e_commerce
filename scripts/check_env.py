"""环境自检：确认依赖、数据库、Redis 三件套是否就绪。

用法：.venv\\Scripts\\python.exe scripts\\check_env.py
"""

import importlib
import os
import socket
import sys
from pathlib import Path
from urllib.parse import urlparse

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR / "apps"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

OK, MISS = "[OK]", "[MISS]"

print("=" * 56)
print("1. Python 与依赖")
print("=" * 56)
print(f"{OK} python   {sys.version.split()[0]}")
for mod in ["django", "pymysql", "redis", "django_redis", "PIL", "tinymce", "dotenv", "celery", "itsdangerous"]:
    try:
        m = importlib.import_module(mod)
        print(f"{OK} {mod:12s} {getattr(m, '__version__', '')}")
    except ImportError:
        print(f"{MISS} {mod:12s}（未安装）")

import django  # noqa: E402

django.setup()
from django.conf import settings  # noqa: E402

print()
print("=" * 56)
print("2. 数据库")
print("=" * 56)
db = settings.DATABASES["default"]
print(f"{OK} ENGINE   {db['ENGINE']}")
print(f"{OK} NAME     {db['NAME']}")
try:
    from django.db import connection

    with connection.cursor() as cur:
        cur.execute("SELECT VERSION()")
        print(f"{OK} 连接成功 {cur.fetchone()[0]}")
except Exception as exc:
    print(f"{MISS} 连接失败 {exc}")

print()
print("=" * 56)
print("3. Redis（缓存 / 会话 / 购物车）")
print("=" * 56)
try:
    from django_redis import get_redis_connection

    conn = get_redis_connection("default")
    conn.set("ecommerce:health", "1", ex=5)
    print(f"{OK} {settings.REDIS_LOCATION} -> {conn.get('ecommerce:health')}")
except Exception as exc:
    print(f"{MISS} Redis 不可用：{exc}")
    print("      Windows 可用 docker run -d -p 6379:6379 redis:7-alpine 启动")

print()
print("=" * 56)
print("4. FastDFS（上传代理与 Nginx 网关）")
print("=" * 56)
backend = settings.STORAGES["default"]["BACKEND"]
print(f"{OK} 存储后端 {backend}")
tracker_host, _, tracker_port = settings.FDFS_TRACKER_SERVER.rpartition(":")
try:
    with socket.create_connection((tracker_host, int(tracker_port)), timeout=3):
        print(f"{OK} Tracker  {settings.FDFS_TRACKER_SERVER}")
except (OSError, ValueError) as exc:
    print(f"{MISS} Tracker  {settings.FDFS_TRACKER_SERVER}：{exc}")
try:
    import requests

    health_url = urlparse(settings.FDFS_URL + "healthz").geturl()
    response = requests.get(health_url, timeout=3)
    response.raise_for_status()
    print(f"{OK} Nginx    {settings.FDFS_URL}")
except Exception as exc:  # noqa: BLE001
    print(f"{MISS} Nginx    {settings.FDFS_URL}：{exc}")
    print("      Windows 可运行 docker compose up -d 启动 FastDFS")

print()
print("=" * 56)
print("5. 已落库的业务表")
print("=" * 56)
try:
    from django.db import connection

    tables = connection.introspection.table_names()
    for t in sorted(t for t in tables if t.startswith("df_")):
        print(f"{OK} {t}")
    print(f"合计 {len([t for t in tables if t.startswith('df_')])} 张")
except Exception as exc:
    print(f"{MISS} {exc}")
