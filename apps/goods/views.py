"""商品模块视图（骨架实现）。

已完成：首页楼层、分类列表（排序 + 分页）、Redis 页面缓存、商品详情、
全文搜索（MySQL FULLTEXT + ngram）、浏览历史读写。
待补：首页静态化；全文检索可选升级 Haystack / Elasticsearch。
"""

import logging
from collections import defaultdict

from django.conf import settings
from django.core.cache import caches
from django.core.cache.utils import make_template_fragment_key
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.views.generic import DetailView, ListView

from utils.pagination import (
    PaginationContextMixin,
    elided_page_range,
    querystring_without_page,
)
from order.services import visible_comments_for_sku
from promotion.cache import pricing_version
from promotion.services import decorate_skus_with_prices

from .history import read_history, record_history
from .models import (
    GoodsSKU,
    GoodsType,
    IndexGoodsBanner,
    IndexPromotionBanner,
    IndexTypeGoodsBanner,
)
from .search import SORT_RELEVANCE, VALID_SORTS, normalize_keyword, search_skus

logger = logging.getLogger(__name__)

def _get_cached_fragment(fragment_name, vary_on=()):
    """读取模板片段缓存；Redis 故障时降级为正常渲染。"""
    try:
        cache_key = make_template_fragment_key(fragment_name, vary_on)
        return caches["default"].get(cache_key)
    except Exception:
        logger.warning("读取商品页面缓存失败 fragment=%s", fragment_name, exc_info=True)
        return None


def index(request):
    """首页：轮播 + 促销 + 各分类楼层。"""
    cached_content = _get_cached_fragment("goods_index")
    if cached_content is not None:
        return render(request, "index.html", {"cached_content": cached_content})

    types = list(GoodsType.objects.all())
    type_banners_by_type = defaultdict(list)
    type_banners = (
        IndexTypeGoodsBanner.objects.filter(
            type_id__in=[goods_type.id for goods_type in types],
            display_type=IndexTypeGoodsBanner.DISPLAY_TYPE_SKU,
            sku__status=GoodsSKU.STATUS_ONLINE,
            sku__is_delete=False,
        )
        .select_related("sku")
        .prefetch_related("sku__images")
        .order_by("type_id", "index")
    )
    for banner in type_banners:
        bucket = type_banners_by_type[banner.type_id]
        if len(bucket) < 4:
            bucket.append(banner)
    for goods_type in types:
        goods_type.banners = type_banners_by_type[goods_type.id]

    goods_banners = list(
        IndexGoodsBanner.objects.filter(
            sku__status=GoodsSKU.STATUS_ONLINE,
            sku__is_delete=False,
        )
        .select_related("sku")
        .prefetch_related("sku__images")
        .order_by("index")[:4]
    )
    promotion_banners = list(
        IndexPromotionBanner.objects.filter(is_active=True).order_by("index")[:4]
    )
    banner_skus = [banner.sku for banner in goods_banners]
    for goods_type in types:
        banner_skus.extend(banner.sku for banner in goods_type.banners)
    decorate_skus_with_prices(banner_skus)

    context = {
        "types": types,
        "goods_banners": goods_banners,
        "promotion_banners": promotion_banners,
        "index_cache_ttl": settings.GOODS_INDEX_CACHE_TTL,
    }
    return render(request, "index.html", context)


def promotion_detail(request, slug):
    """促销活动落地页：活动说明、参与商品和行动按钮。"""
    promotion = get_object_or_404(
        IndexPromotionBanner.objects.select_related("category", "promotion"),
        slug=slug,
        is_active=True,
    )

    products = GoodsSKU.objects.none()
    if promotion.category_id:
        products = (
            GoodsSKU.objects.filter(
                type_id=promotion.category_id,
                status=GoodsSKU.STATUS_ONLINE,
                is_delete=False,
            )
            .select_related("type", "spu")
            .prefetch_related("images")
            .order_by("-sales", "id")[:8]
        )
    else:
        products = (
            GoodsSKU.objects.online()
            .select_related("type", "spu")
            .prefetch_related("images")
            .order_by("-sales", "id")[:8]
        )
    products = list(products)
    decorate_skus_with_prices(products)

    if promotion.cta_url:
        cta_url = promotion.cta_url
    elif promotion.category_id:
        cta_url = reverse("goods:list", args=[promotion.category_id])
    elif promotion.url:
        cta_url = promotion.url
    else:
        cta_url = reverse("goods:index")

    other_promotions = (
        IndexPromotionBanner.objects.filter(is_active=True)
        .exclude(pk=promotion.pk)
        .order_by("index")[:3]
    )
    rule_promotion = promotion.promotion
    promotion_live = bool(rule_promotion and rule_promotion.is_effective(timezone.now()))

    return render(
        request,
        "goods/promotion.html",
        {
            "promotion": promotion,
            "products": products,
            "cta_url": cta_url,
            "other_promotions": other_promotions,
            "rule_promotion": rule_promotion,
            "promotion_live": promotion_live,
        },
    )


class GoodsListView(PaginationContextMixin, ListView):
    """分类商品列表：支持销量/价格排序与分页。"""

    model = GoodsSKU
    template_name = "goods/list.html"
    paginate_by = 10
    page_kwarg = "page"

    def get(self, request, *args, **kwargs):
        self.goods_type = get_object_or_404(GoodsType, id=self.kwargs["type_id"])
        self.sort = request.GET.get("sort", "default")
        self.promotion_price_version = pricing_version()
        cached_content = _get_cached_fragment(
            "goods_list", [request.get_full_path(), self.promotion_price_version]
        )
        if cached_content is not None:
            return render(
                request,
                "goods/list.html",
                {
                    "cached_content": cached_content,
                    "goods_type": self.goods_type,
                    "sort": self.sort,
                    "promotion_price_version": self.promotion_price_version,
                },
            )
        return super().get(request, *args, **kwargs)

    def get_queryset(self):
        if not hasattr(self, "goods_type"):
            self.goods_type = get_object_or_404(GoodsType, id=self.kwargs["type_id"])
        if not hasattr(self, "sort"):
            self.sort = self.request.GET.get("sort", "default")
        queryset = GoodsSKU.objects.online().filter(type=self.goods_type).prefetch_related("images")
        if self.sort == "price":
            queryset = queryset.order_by("price")
        elif self.sort == "sales":
            queryset = queryset.order_by("-sales")
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        page_obj = context.get("page_obj")
        if page_obj is not None:
            decorate_skus_with_prices(list(page_obj.object_list))
        context.update(
            {
                "goods_type": self.goods_type,
                "sort": self.sort,
                "list_cache_ttl": settings.GOODS_LIST_CACHE_TTL,
                "promotion_price_version": getattr(
                    self, "promotion_price_version", pricing_version()
                ),
            }
        )
        return context


class GoodsDetailView(DetailView):
    """商品详情：SKU 主图/多图/SPU 富文本详情，并记录浏览历史。"""

    model = GoodsSKU
    template_name = "goods/detail.html"
    context_object_name = "sku"
    pk_url_kwarg = "sku_id"

    def get_queryset(self):
        return (
            GoodsSKU.objects.online()
            .select_related("type", "spu")
            .prefetch_related("images")
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        sku = self.object
        history_skus = []
        if self.request.user.is_authenticated:
            history_skus = read_history(self.request.user.id, exclude_sku_id=sku.id)
        spec_skus = list(
            GoodsSKU.objects.online()
            .filter(spu_id=sku.spu_id)
            .order_by("price")
        )
        new_skus = list(
            GoodsSKU.objects.online()
            .filter(type=sku.type)
            .exclude(id=sku.id)
            .prefetch_related("images")[:2]
        )
        decorate_skus_with_prices([sku, *spec_skus, *new_skus, *history_skus])
        reviews = visible_comments_for_sku(sku.id)
        context.update(
            {
                "images": sku.images.all(),
                "new_skus": new_skus,
                "spec_skus": spec_skus,
                "history_skus": history_skus,
                "reviews": reviews,
                "review_count": len(reviews),
            }
        )
        return context

    def get(self, request, *args, **kwargs):
        response = super().get(request, *args, **kwargs)
        if request.user.is_authenticated:
            record_history(request.user.id, self.object.id)
        return response


def search(request):
    """关键词搜索：全文匹配 + 相关度/价格/销量排序。"""
    keyword = normalize_keyword(request.GET.get("q", ""))
    sort = request.GET.get("sort", SORT_RELEVANCE)
    if sort not in VALID_SORTS:
        sort = SORT_RELEVANCE

    skus = search_skus(keyword, sort)
    paginator = Paginator(skus, 10)
    page_obj = paginator.get_page(request.GET.get("page", 1))
    decorate_skus_with_prices(list(page_obj.object_list))

    return render(
        request,
        "goods/search.html",
        {
            "keyword": keyword,
            "sort": sort,
            "page_obj": page_obj,
            "page_range": elided_page_range(page_obj),
            "querystring": querystring_without_page(request, {"q": keyword}),
        },
    )
