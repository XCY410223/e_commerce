"""Celery 入口。

启动 worker（Windows 必须加 --pool=solo）：
    celery -A config.celery worker -l info --pool=solo
"""

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("e_commerce")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks(["celery_tasks", "order"])
