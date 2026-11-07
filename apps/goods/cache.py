"""商品页面缓存键与主动失效工具。"""

import logging

from django.core.cache import caches
from django.core.cache.utils import make_template_fragment_key

logger = logging.getLogger(__name__)

GOODS_INDEX_FRAGMENT = "goods_index"
GOODS_LIST_FRAGMENT = "goods_list"
GOODS_NAV_CACHE_KEY = "goods:nav_types:v1"


def invalidate_goods_page_cache():
    """清除首页和所有分类列表的模板片段缓存。"""
    try:
        cache = caches["default"]
        cache.delete(make_template_fragment_key(GOODS_INDEX_FRAGMENT))
        delete_pattern = getattr(cache, "delete_pattern", None)
        if callable(delete_pattern):
            delete_pattern(f"*template.cache.{GOODS_LIST_FRAGMENT}*")
    except Exception:
        logger.warning("清除商品页面缓存失败", exc_info=True)


def invalidate_goods_nav_cache():
    """清除顶部导航分类缓存。"""
    try:
        caches["default"].delete(GOODS_NAV_CACHE_KEY)
    except Exception:
        logger.warning("清除导航分类缓存失败", exc_info=True)
