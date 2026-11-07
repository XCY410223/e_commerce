"""活动价格缓存版本号。

商品列表的模板片段缓存把版本号作为 varying key；活动规则一改就递增版本，
既不需要遍历删除全部列表缓存，也不会继续展示旧活动价。
"""

import logging

from django.core.cache import caches

logger = logging.getLogger(__name__)
CACHE_KEY = "promotion:pricing:version"


def pricing_version():
    try:
        cache = caches["default"]
        version = cache.get(CACHE_KEY)
        if version is None:
            version = 1
            cache.add(CACHE_KEY, version, timeout=None)
        return version
    except Exception:
        logger.warning("读取活动价格缓存版本失败", exc_info=True)
        return 0


def bump_pricing_version():
    try:
        cache = caches["default"]
        try:
            cache.incr(CACHE_KEY)
        except ValueError:
            cache.set(CACHE_KEY, 1, timeout=None)
        from goods.cache import invalidate_goods_page_cache

        invalidate_goods_page_cache()
    except Exception:
        logger.warning("刷新活动价格缓存版本失败", exc_info=True)
