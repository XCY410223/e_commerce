"""全局模板上下文。

提供母版 base.html 需要的两个变量：
    nav_types   —— 导航栏商品种类（全站共用，按 id 升序）
    cart_count  —— 当前购物车角标数量（未登录读游客 Cookie 购物车，无则为 0）

Redis 不可用时一律降级为空/0，绝不让基础设施故障拖垮页面渲染。
"""

import logging

from django.conf import settings
from django.core.cache import cache

from goods.cache import GOODS_NAV_CACHE_KEY
from goods.models import GoodsType

logger = logging.getLogger(__name__)


def nav_types(request):
    try:
        items = cache.get(GOODS_NAV_CACHE_KEY)
        if items is None:
            items = list(GoodsType.objects.values("id", "name"))
            cache.set(GOODS_NAV_CACHE_KEY, items, settings.GOODS_NAV_CACHE_TTL)
        return {"nav_types": items}
    except Exception:  # 迁移未完成 / 数据库未就绪时仍要能渲染后台与错误页
        logger.debug("加载导航分类失败", exc_info=True)
        return {"nav_types": []}


def cart_count(request):
    # 购物车页已经算过一次明细，直接复用，避免同一请求重复读 Redis 和商品表
    summary = getattr(request, "_cart_summary", None)
    if summary is not None:
        return {"cart_count": summary[1]}

    count = 0
    try:
        from utils.cart import cart_count_ref, sku_cart_ref

        ref = sku_cart_ref(request)
        if ref is not None:
            count = cart_count_ref(ref)
    except Exception:
        logger.debug("读取购物车数量失败", exc_info=True)
        count = 0
    return {"cart_count": count}
