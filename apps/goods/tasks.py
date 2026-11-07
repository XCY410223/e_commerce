"""商品页面静态化任务。"""

from celery import shared_task
from django.core.cache import cache

from .static_pages import STATIC_PAGE_PENDING_KEY, build_static_pages


@shared_task(name="goods.generate_static_pages")
def generate_static_pages():
    """生成首页以及所有分类列表的静态页。"""
    try:
        cache.delete(STATIC_PAGE_PENDING_KEY)
    except Exception:
        pass
    return build_static_pages()
