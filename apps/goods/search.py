"""商品搜索。

MySQL 使用 InnoDB FULLTEXT（ngram）做全文匹配并按相关度排序。查询侧先用 jieba
切词，多词时拼成 BOOLEAN MODE 的 `+"词"` 表达式做 AND 检索，避免“红富士苹果500g”
这类长词被拆散后命中一堆无关商品；布尔检索无结果时退回整串的自然语言检索，
数据库不是 MySQL、关键词过短或全文索引不可用时，再回退到 LIKE。
"""

import logging
import re
import threading

from django.db import DatabaseError, connection
from django.db.models import Case, FloatField, IntegerField, Q, When
from django.db.models.expressions import RawSQL

from .models import GoodsSKU

try:
    import jieba
except ImportError:  # 未安装分词库时退化为整串检索，不影响搜索可用性
    jieba = None

logger = logging.getLogger(__name__)

SORT_RELEVANCE = "relevance"
SORT_PRICE = "price"
SORT_SALES = "sales"
VALID_SORTS = {SORT_RELEVANCE, SORT_PRICE, SORT_SALES}
MAX_KEYWORD_LENGTH = 50
# 分词后最多参与 AND 检索的词数，避免长句拼出超大表达式
MAX_TERMS = 5
# 电商场景里高频但没有区分度的虚词，切出来后直接丢弃
STOPWORDS = frozenset({"的", "了", "和", "与", "及", "是", "在", "有"})
# jieba 默认词典切不准的生鲜品类词，启动后补充进去
USER_WORDS = ("车厘子", "红富士", "阿克苏", "JJ级", "三文鱼", "950ml")

# 字段必须与迁移里创建的 FULLTEXT 索引完全一致
_MATCH_SQL = "MATCH(`name`, `desc`, `spec`) AGAINST (%s IN NATURAL LANGUAGE MODE)"
_BOOLEAN_MATCH_SQL = "MATCH(`name`, `desc`, `spec`) AGAINST (%s IN BOOLEAN MODE)"
# 布尔模式的操作符和引号不能出现在词里，交给 translate 一次性剔除
_BOOLEAN_OPERATORS = str.maketrans("", "", '+-<>()~*"@')

_jieba_lock = threading.Lock()
_jieba_ready = False


def normalize_keyword(value):
    """去掉首尾空格并限制长度，视图和查询层共用。"""
    return (value or "").strip()[:MAX_KEYWORD_LENGTH]


def _ensure_jieba():
    """首次使用时补充自定义词典，加锁避免多线程重复初始化。"""
    global _jieba_ready
    if jieba is None or _jieba_ready:
        return
    with _jieba_lock:
        if _jieba_ready:
            return
        for word in USER_WORDS:
            jieba.add_word(word)
        _jieba_ready = True


def tokenize(keyword):
    """jieba 精确模式切词，过滤单字、停用词并去重；分词库缺失时返回空列表。"""
    if jieba is None:
        return []
    _ensure_jieba()

    terms = []
    for raw in jieba.cut(keyword, cut_all=False):
        term = re.sub(r"\s+", " ", raw).strip()
        # ngram_token_size=2，单字无法命中全文索引，留着只会把结果筛成空
        if len(term) < 2 or term in STOPWORDS:
            continue
        if term not in terms:
            terms.append(term)
        if len(terms) >= MAX_TERMS:
            break
    return terms


def boolean_expression(terms):
    """把词列表拼成 `+"词1" +"词2"` 形式的 AND 表达式。"""
    cleaned = []
    for term in terms:
        term = term.translate(_BOOLEAN_OPERATORS).strip()
        if term:
            cleaned.append('+"%s"' % term)
    return " ".join(cleaned)


def _with_name_hit(queryset, keyword, terms=None):
    """名称命中的商品排在只命中简介的商品前面。

    走布尔检索时要求名称同时覆盖全部分词，和 AND 的筛选口径保持一致。
    """
    condition = Q(name__icontains=keyword)
    if terms and len(terms) >= 2:
        condition = Q()
        for term in terms:
            condition &= Q(name__icontains=term)

    return queryset.annotate(
        name_hit=Case(
            When(condition, then=0),
            default=1,
            output_field=IntegerField(),
        )
    )


def _like_queryset(keyword):
    return _with_name_hit(
        GoodsSKU.objects.online().filter(
            Q(name__icontains=keyword)
            | Q(desc__icontains=keyword)
            | Q(spec__icontains=keyword)
        ),
        keyword,
    )


def _fulltext_queryset(keyword, terms=None):
    """terms 非空时走布尔 AND 检索，否则按整串关键词做自然语言检索。"""
    if terms:
        lookup = RawSQL(_BOOLEAN_MATCH_SQL, [boolean_expression(terms)], output_field=FloatField())
    else:
        lookup = RawSQL(_MATCH_SQL, [keyword], output_field=FloatField())

    return _with_name_hit(
        GoodsSKU.objects.online()
        .annotate(relevance=lookup)
        .filter(relevance__gt=0),
        keyword,
        terms,
    )


def _apply_sort(queryset, sort):
    if sort == SORT_PRICE:
        return queryset.order_by("price", "name_hit", "-create_time")
    if sort == SORT_SALES:
        return queryset.order_by("-sales", "name_hit", "-create_time")

    ordering = []
    if "name_hit" in queryset.query.annotations:
        ordering.append("name_hit")
    if "relevance" in queryset.query.annotations:
        ordering.extend(["-relevance", "-sales", "-create_time"])
    else:
        ordering.extend(["-sales", "-create_time"])
    return queryset.order_by(*ordering)


def _fulltext_queryset_with_fallback(keyword):
    """分词后布尔 AND 检索优先，无结果时依次退回自然语言检索与 LIKE。"""
    terms = tokenize(keyword)
    if len(terms) >= 2:
        queryset = _fulltext_queryset(keyword, terms)
        if queryset.exists():
            return queryset

    queryset = _fulltext_queryset(keyword)
    if queryset.exists():  # 全文无结果时回退，兼容停用词等场景
        return queryset
    return _like_queryset(keyword)


def search_skus(keyword, sort=SORT_RELEVANCE):
    """返回命中的在线 SKU，自动处理短词与全文索引异常。"""
    keyword = normalize_keyword(keyword)
    if not keyword:
        return GoodsSKU.objects.none()
    if sort not in VALID_SORTS:
        sort = SORT_RELEVANCE

    # ngram_token_size=2，单字查询无法命中 FULLTEXT，直接走 LIKE
    if connection.vendor != "mysql" or len(keyword) < 2:
        return _apply_sort(_like_queryset(keyword).prefetch_related("images"), sort)

    try:
        queryset = _fulltext_queryset_with_fallback(keyword)
    except DatabaseError:
        logger.warning("全文检索不可用，回退到 LIKE 查询", exc_info=True)
        queryset = _like_queryset(keyword)

    return _apply_sort(queryset.prefetch_related("images"), sort)
