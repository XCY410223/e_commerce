import pymysql

# Django 会校验 mysqlclient 版本，PyMySQL 需伪装成符合要求的版本号
pymysql.version_info = (2, 2, 7, "final", 0)
pymysql.install_as_MySQLdb()
from .celery import app as celery_app

__all__ = ("celery_app",)
