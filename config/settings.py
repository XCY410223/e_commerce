"""
Django settings for e_commerce project.

技术栈：Django 6.1.2 + MySQL + Redis + Celery + (FastDFS 可选)
配置项通过项目根目录 .env 覆盖，敏感信息不硬编码。
"""

import os
import sys
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

# BASE_DIR = e_commerce/
BASE_DIR = Path(__file__).resolve().parent.parent

# 读取 .env（不存在时静默跳过，使用下面的默认值）
load_dotenv(BASE_DIR / ".env")

# 把 apps/ 加入模块搜索路径，因此 INSTALLED_APPS 中直接写 "user" 而非 "apps.user"
sys.path.insert(0, str(BASE_DIR / "apps"))


# --------------------------------------------------------------------------
# 基础
# --------------------------------------------------------------------------
SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "django-insecure-dev-only-change-me")

DEBUG = os.getenv("DJANGO_DEBUG", "1") == "1"

ALLOWED_HOSTS = [h.strip() for h in os.getenv("DJANGO_ALLOWED_HOSTS", "127.0.0.1,localhost").split(",") if h.strip()]
CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("CSRF_TRUSTED_ORIGINS", "").split(",")
    if origin.strip()
]
if os.getenv("TRUST_X_FORWARDED_PROTO", "0") == "1":
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")


# --------------------------------------------------------------------------
# 应用
# --------------------------------------------------------------------------
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # 第三方
    "tinymce",
    # 业务模块
    "user.apps.UserConfig",
    "goods.apps.GoodsConfig",
    "cart.apps.CartConfig",
    "order.apps.OrderConfig",
    "promotion.apps.PromotionConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        # 注意：原配置此处模板目录未加引号，会直接 NameError
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                # 业务全局上下文：导航分类、购物车角标
                "utils.context_processors.nav_types",
                "utils.context_processors.cart_count",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"


# --------------------------------------------------------------------------
# 数据库：默认 MySQL，设 DB_ENGINE=sqlite 可快速回退
# --------------------------------------------------------------------------
if os.getenv("DB_ENGINE", "mysql") == "sqlite":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.mysql",
            "NAME": os.getenv("DB_NAME", "e_commerce"),
            "USER": os.getenv("DB_USER", "root"),
            "PASSWORD": os.getenv("DB_PASSWORD", ""),
            "HOST": os.getenv("DB_HOST", "127.0.0.1"),
            "PORT": os.getenv("DB_PORT", "3306"),
            "OPTIONS": {
                "charset": "utf8mb4",
                "init_command": "SET sql_mode='STRICT_TRANS_TABLES'",
            },
        }
    }

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# 项目把 apps/ 加入 sys.path，应用测试模块的导入名是 order.tests / promotion.tests。
# 默认 discover 从仓库根目录把同样的文件识别成 apps.order.tests，会触发
# “模型未声明 app_label”；无标签执行测试时改为按 INSTALLED_APPS 的 app label 发现。
TEST_RUNNER = "config.test_runner.ProjectDiscoverRunner"


# --------------------------------------------------------------------------
# 认证：必须在首次 migrate 之前生效
# --------------------------------------------------------------------------
AUTH_USER_MODEL = "user.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LOGIN_URL = "/user/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/"

# 用户激活：链接站点地址与 token 有效期（秒）
SITE_URL = os.getenv("SITE_URL", "http://127.0.0.1:8000").rstrip("/")
USER_ACTIVATION_TIMEOUT = int(os.getenv("USER_ACTIVATION_TIMEOUT", "86400"))

# 公网隧道/反向代理域名以 SITE_URL 为准，避免 ALLOWED_HOSTS 与回调地址脱节。
SITE_HOST = urlparse(SITE_URL).hostname
if SITE_HOST and SITE_HOST not in ALLOWED_HOSTS:
    ALLOWED_HOSTS.append(SITE_HOST)
if SITE_URL and SITE_URL not in CSRF_TRUSTED_ORIGINS:
    CSRF_TRUSTED_ORIGINS.append(SITE_URL)


# --------------------------------------------------------------------------
# 支付：默认 mock（本地收银台），切换到 alipay 时需安装 python-alipay-sdk
# --------------------------------------------------------------------------
PAYMENT_PROVIDER = os.getenv("PAYMENT_PROVIDER", "mock").strip().lower()
PAYMENT_MOCK_SECRET = os.getenv("PAYMENT_MOCK_SECRET") or SECRET_KEY

PAYMENT_ALIPAY_APP_ID = os.getenv("PAYMENT_ALIPAY_APP_ID", "")
PAYMENT_ALIPAY_SELLER_ID = os.getenv("PAYMENT_ALIPAY_SELLER_ID", "")
PAYMENT_ALIPAY_PRIVATE_KEY = os.getenv("PAYMENT_ALIPAY_PRIVATE_KEY", "").replace("\\n", "\n")
PAYMENT_ALIPAY_PUBLIC_KEY = os.getenv("PAYMENT_ALIPAY_PUBLIC_KEY", "").replace("\\n", "\n")
PAYMENT_ALIPAY_DEBUG = os.getenv("PAYMENT_ALIPAY_DEBUG", "0") == "1"
PAYMENT_ALIPAY_GATEWAY = os.getenv("PAYMENT_ALIPAY_GATEWAY", "").strip() or (
    "https://openapi-sandbox.dl.alipaydev.com/gateway.do"
    if PAYMENT_ALIPAY_DEBUG
    else "https://openapi.alipay.com/gateway.do"
)
PAYMENT_ALIPAY_NOTIFY_URL = os.getenv(
    "PAYMENT_ALIPAY_NOTIFY_URL", f"{SITE_URL}/order/pay/notify/alipay/"
)
PAYMENT_ALIPAY_RETURN_URL = os.getenv(
    "PAYMENT_ALIPAY_RETURN_URL", f"{SITE_URL}/order/pay/return/alipay/"
)
# 支付宝支付方式：page = 电脑网站支付(alipay.trade.page.pay)，wap = 手机网站支付(alipay.trade.wap.pay)
PAYMENT_ALIPAY_PAY_METHOD = os.getenv("PAYMENT_ALIPAY_PAY_METHOD", "page").strip().lower()

# 未支付订单主动查询：创建后多久开始查、最多追溯多久、状态页查询节流
PAYMENT_QUERY_AFTER_SECONDS = int(os.getenv("PAYMENT_QUERY_AFTER_SECONDS", "300"))
PAYMENT_QUERY_MAX_AGE_DAYS = int(os.getenv("PAYMENT_QUERY_MAX_AGE_DAYS", "2"))
PAYMENT_QUERY_THROTTLE_SECONDS = int(os.getenv("PAYMENT_QUERY_THROTTLE_SECONDS", "5"))
PAYMENT_STATUS_POLL_INTERVAL = int(os.getenv("PAYMENT_STATUS_POLL_INTERVAL", "2"))

# --------------------------------------------------------------------------
# 促销：金额规则、紧急开关与邀请码有效期
# --------------------------------------------------------------------------
STANDARD_TRANSIT_PRICE = Decimal(os.getenv("STANDARD_TRANSIT_PRICE", "10.00"))
PROMOTION_PRICING_ENABLED = os.getenv("PROMOTION_PRICING_ENABLED", "1") == "1"
PROMOTION_COUPON_ENABLED = os.getenv("PROMOTION_COUPON_ENABLED", "1") == "1"
PROMOTION_REFERRAL_ENABLED = os.getenv("PROMOTION_REFERRAL_ENABLED", "0") == "1"
REFERRAL_CODE_MAX_AGE = int(os.getenv("REFERRAL_CODE_MAX_AGE", str(180 * 86400)))

# 未支付订单超时取消：默认 30 分钟后关闭，每分钟扫描一次，每批最多 100 单。
ORDER_UNPAID_TIMEOUT_SECONDS = int(os.getenv("ORDER_UNPAID_TIMEOUT_SECONDS", "1800"))
ORDER_CANCEL_CHECK_INTERVAL = max(
    1, int(os.getenv("ORDER_CANCEL_CHECK_INTERVAL", "60"))
)
ORDER_CANCEL_BATCH_SIZE = int(os.getenv("ORDER_CANCEL_BATCH_SIZE", "100"))

# 订单评论提交开关：紧急关闭写入口，不影响已审核评论展示。
ORDER_COMMENT_ENABLED = os.getenv("ORDER_COMMENT_ENABLED", "1") == "1"


# --------------------------------------------------------------------------
# 国际化
# --------------------------------------------------------------------------
LANGUAGE_CODE = "zh-hans"
TIME_ZONE = "Asia/Shanghai"
USE_I18N = True
USE_TZ = True


# --------------------------------------------------------------------------
# 静态文件与媒体文件
# --------------------------------------------------------------------------
STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "static_collected"

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

# FastDFS：上传经 Nginx 代理，下载地址与上传接口共用 8888 端口
FDFS_TRACKER_SERVER = os.getenv("FDFS_TRACKER_SERVER", "127.0.0.1:22122")
FDFS_URL = os.getenv("FDFS_URL", "http://127.0.0.1:8888/").rstrip("/") + "/"
FDFS_UPLOAD_URL = os.getenv("FDFS_UPLOAD_URL", f"{FDFS_URL}api/upload")
FDFS_DELETE_URL = os.getenv("FDFS_DELETE_URL", f"{FDFS_URL}api/delete")
FDFS_UPLOAD_TOKEN = os.getenv("FDFS_UPLOAD_TOKEN", "dev-fastdfs-token")
FDFS_TIMEOUT = float(os.getenv("FDFS_TIMEOUT", "15"))

# 文件存储后端（FastDFS 落地后改由环境变量切换为 utils.storage.FastDFSStorage）
STORAGES = {
    "default": {"BACKEND": os.getenv("STORAGE_BACKEND", "django.core.files.storage.FileSystemStorage")},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}


# --------------------------------------------------------------------------
# Redis：缓存 + 会话 + 购物车 + 浏览历史
# --------------------------------------------------------------------------
REDIS_LOCATION = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")

CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": REDIS_LOCATION,
        "OPTIONS": {
            "CLIENT_CLASS": "django_redis.client.DefaultClient",
            "SOCKET_CONNECT_TIMEOUT": 3,
            "SOCKET_TIMEOUT": 3,
            # 缓存故障时降级为直接查库，不因 Redis 抖动返回 500。
            "IGNORE_EXCEPTIONS": True,
            # redis-py 6+ 默认走 RESP3（握手用 HELLO），而 Windows 版 Redis 5.0 不认识该命令，
            # 会报 "unknown command HELLO"。显式退回 RESP2 即可兼容；若用 Redis 6+ 可改为 3。
            # 注意：django-redis 走的是「连接池」而非 Redis() 直连，protocol 必须写在
            # CONNECTION_POOL_KWARGS 里才会传给真正的 Connection。
            "CONNECTION_POOL_KWARGS": {"protocol": int(os.getenv("REDIS_PROTOCOL", "2"))},
        },
        "KEY_PREFIX": "ecommerce",
        "TIMEOUT": 3600,
    }
}

# 商品页面公共内容片段缓存（秒）。顶栏用户名/购物车等用户相关内容不进入缓存。
GOODS_INDEX_CACHE_TTL = int(os.getenv("GOODS_INDEX_CACHE_TTL", "60"))
GOODS_LIST_CACHE_TTL = int(os.getenv("GOODS_LIST_CACHE_TTL", "60"))
GOODS_NAV_CACHE_TTL = int(os.getenv("GOODS_NAV_CACHE_TTL", "300"))

# 购物车 Redis Hash 的滑动有效期（秒）；设为 0 表示永不过期。
CART_TTL = int(os.getenv("CART_TTL", "2592000"))

# 游客购物车（Cookie 里的 token → Redis Hash）有效期，默认 7 天。
GUEST_CART_TTL = int(os.getenv("GUEST_CART_TTL", "604800"))

SESSION_ENGINE = "django.contrib.sessions.backends.cache"
SESSION_CACHE_ALIAS = "default"
SESSION_COOKIE_AGE = 60 * 60 * 24 * 7  # 7 天


# --------------------------------------------------------------------------
# Celery（异步任务：激活邮件、页面静态化）
# --------------------------------------------------------------------------
CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", "redis://127.0.0.1:6379/1")
CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", "redis://127.0.0.1:6379/2")
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = TIME_ZONE
CELERY_BEAT_SCHEDULE = {
    "query-unpaid-orders": {
        "task": "order.query_unpaid_orders",
        "schedule": 300.0,
    },
    "cancel-expired-orders": {
        "task": "order.cancel_expired_orders",
        "schedule": float(ORDER_CANCEL_CHECK_INTERVAL),
    },
}


# --------------------------------------------------------------------------
# 邮件（Django 6 使用 MAILERS；开发期输出到控制台）
# --------------------------------------------------------------------------
MAIL_BACKEND = os.getenv("MAIL_BACKEND", "django.core.mail.backends.console.EmailBackend")
MAILER_OPTIONS = {}
if MAIL_BACKEND.endswith("smtp.EmailBackend"):
    MAILER_OPTIONS = {
        "host": os.getenv("EMAIL_HOST", "localhost"),
        "port": int(os.getenv("EMAIL_PORT", "25")),
        "username": os.getenv("EMAIL_HOST_USER", ""),
        "password": os.getenv("EMAIL_HOST_PASSWORD", ""),
        "use_tls": os.getenv("EMAIL_USE_TLS", "0") == "1",
        "use_ssl": os.getenv("EMAIL_USE_SSL", "0") == "1",
        "timeout": int(os.getenv("EMAIL_TIMEOUT", "10")),
    }
MAILERS = {
    "default": {
        "BACKEND": MAIL_BACKEND,
        "OPTIONS": MAILER_OPTIONS,
    },
}
DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL", "天天生鲜 <noreply@ecommerce.local>")


# --------------------------------------------------------------------------
# 富文本编辑器
# --------------------------------------------------------------------------
TINYMCE_DEFAULT_CONFIG = {
    # django-tinymce 5 内置新版 TinyMCE，主题使用 silver；advanced 是 TinyMCE 4 的旧主题。
    "theme": "silver",
    "width": 600,
    "height": 400,
    "menubar": False,
    "plugins": "link image lists table code",
    "toolbar": "undo redo | bold italic | alignleft aligncenter alignright | bullist numlist | link image | code",
}
