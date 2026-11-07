"""首页与分类列表的静态页生成、失效和匿名请求回退。

静态页只面向没有登录会话、也没有游客购物车 Cookie 的匿名 GET/HEAD 请求：
登录用户、游客购物车用户继续走动态视图，避免用户名、购物车角标和 CSRF
令牌被写进所有用户共用的 HTML 文件，或被代理错误复用。

发布策略：先在目标目录同级的临时目录完整写出，再整体替换正式目录；
构建期间数据发生变更时放弃发布，避免把半新半旧的页面暴露给访问者。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
import uuid
from collections import defaultdict
from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.core.cache import cache
from django.core.paginator import EmptyPage, Paginator
from django.http import HttpResponse
from django.template.loader import render_to_string
from django.utils import timezone

from promotion.services import decorate_skus_with_prices
from utils.cart import GUEST_CART_COOKIE

from .models import (
    GoodsSKU,
    GoodsType,
    IndexGoodsBanner,
    IndexPromotionBanner,
    IndexTypeGoodsBanner,
)
from .queries import LIST_PAGE_SIZE, LIST_SORTS, list_queryset

logger = logging.getLogger(__name__)

INDEX_PAGE = "index.html"
MANIFEST_FILE = "manifest.json"
STATIC_PAGE_VERSION_KEY = "goods:static_pages:version"
STATIC_PAGE_PENDING_KEY = "goods:static_pages:pending"
STATIC_PAGE_BUILD_LOCK_KEY = "goods:static_pages:build_lock"


def build_index_context(types=None):
    """构造首页公共内容，供动态视图和静态页生成共用。"""
    if types is None:
        types = GoodsType.objects.all()
    types = list(types)
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

    return {
        "types": types,
        "goods_banners": goods_banners,
        "promotion_banners": promotion_banners,
    }


def list_static_page_name(type_id, sort="default", page=1):
    """返回分类列表静态页的相对路径。"""
    if sort not in LIST_SORTS:
        raise ValueError(f"unsupported sort: {sort}")
    try:
        type_number = int(type_id)
        page_number = int(page)
    except (TypeError, ValueError) as exc:
        raise ValueError("type_id and page must be integers") from exc
    if page_number < 1:
        raise ValueError("page must be positive")
    return f"list/{type_number}/{sort}-{page_number}.html"


def static_list_page_name_for_request(request, type_id):
    """从查询参数解析可用的静态分类页；未知参数交给动态视图。"""
    if set(request.GET.keys()) - {"sort", "page"}:
        return None
    sort = request.GET.get("sort") or "default"
    if sort not in LIST_SORTS:
        return None
    raw_page = request.GET.get("page") or "1"
    if not raw_page.isdigit():
        return None
    try:
        return list_static_page_name(type_id, sort, int(raw_page))
    except ValueError:
        return None


def can_serve_static_page(request):
    """只有匿名且无购物车的 GET/HEAD 请求可以读公共静态页。"""
    if not getattr(settings, "STATIC_PAGES_ENABLED", False):
        return False
    if request.method not in {"GET", "HEAD"}:
        return False
    if request.COOKIES.get(settings.SESSION_COOKIE_NAME):
        return False
    if request.COOKIES.get(GUEST_CART_COOKIE):
        return False
    return True


def serve_static_page(request, relative_name):
    """命中静态文件时返回 HttpResponse，否则返回 None 交给动态视图。

    静态页只有几十 KB，一次性读入内存可以附带准确的 Content-Length，
    也避免 FileResponse 的长连接占用；生产环境应由 Nginx 直接返回文件。
    """
    if not can_serve_static_page(request):
        return None
    try:
        path = _safe_page_path(relative_name)
        if not path.is_file():
            return None
        content = path.read_bytes()
    except ValueError:
        # 路径穿越或非法页面名：只记录一行，避免把完整堆栈刷进访问日志。
        logger.warning("拒绝非法静态页路径 page=%s", relative_name)
        return None
    except OSError:
        logger.warning("读取静态页失败 page=%s", relative_name, exc_info=True)
        return None
    if not content:
        return None
    response = HttpResponse(content, content_type="text/html; charset=utf-8")
    response["Cache-Control"] = "no-cache, must-revalidate"
    response["Vary"] = "Cookie"
    response["X-Static-Page"] = "hit"
    return response


def bump_static_page_version():
    """记录一次数据变更，防止并发构建发布旧快照。"""
    version = uuid.uuid4().hex
    try:
        cache.set(STATIC_PAGE_VERSION_KEY, version, None)
    except Exception:
        logger.warning("写入静态页版本失败", exc_info=True)
        return None
    return version


def invalidate_static_pages():
    """立即下架旧静态页并递增版本；后续由 Celery 或命令重建。"""
    bump_static_page_version()
    try:
        _remove_published_pages(_pages_root())
    except (OSError, ValueError):
        logger.warning("清理静态页失败", exc_info=True)


def schedule_static_pages_refresh():
    """数据提交后异步重建；开发环境默认关闭，避免依赖 Celery 才能保存数据。"""
    if not settings.STATIC_PAGES_ENABLED or not settings.STATIC_PAGES_AUTO_REFRESH:
        return False
    try:
        should_schedule = cache.add(
            STATIC_PAGE_PENDING_KEY,
            "1",
            timeout=settings.STATIC_PAGES_DEBOUNCE_SECONDS,
        )
    except Exception:
        logger.warning("静态页防抖标记写入失败，直接提交任务", exc_info=True)
        should_schedule = True
    if should_schedule is None:
        # django-redis 在 IGNORE_EXCEPTIONS 下故障时返回 None，此时宁可多提交一次。
        should_schedule = True
    if not should_schedule:
        return False
    try:
        from .tasks import generate_static_pages

        generate_static_pages.apply_async(countdown=2)
        logger.info("已提交静态页重建任务")
        return True
    except Exception:
        logger.warning("提交静态页重建任务失败", exc_info=True)
        return False


def build_static_pages():
    """生成首页和全部分类/排序/分页静态页，返回本次构建摘要。"""
    if not getattr(settings, "STATIC_PAGES_ENABLED", False):
        return {
            "published": False,
            "reason": "disabled",
            "pages": 0,
            "skipped_pages": 0,
        }

    lock_token = _acquire_build_lock()
    if lock_token is None:
        logger.info("已有静态页构建任务在运行，本次跳过")
        return {
            "published": False,
            "reason": "locked",
            "pages": 0,
            "skipped_pages": 0,
        }

    started = time.monotonic()
    try:
        return _build_static_pages_locked(started)
    finally:
        _release_build_lock(lock_token)


def _build_static_pages_locked(started):
    version_before = _current_version()
    types = list(GoodsType.objects.all())
    base_context = _static_base_context(types)
    max_pages = max(
        1, int(getattr(settings, "STATIC_PAGES_MAX_LIST_PAGES", 500))
    )
    max_total_pages = max(
        1, int(getattr(settings, "STATIC_PAGES_MAX_TOTAL_PAGES", 2000))
    )
    skipped_pages = 0
    page_specs = []

    # 先只统计页数，再按总上限截断，最后才渲染，避免分类暴涨时一次性占用大量内存。
    for goods_type in types:
        for sort in LIST_SORTS:
            paginator = Paginator(list_queryset(goods_type, sort), LIST_PAGE_SIZE)
            page_numbers = list(paginator.page_range)
            if len(page_numbers) > max_pages:
                skipped_pages += len(page_numbers) - max_pages
                page_numbers = page_numbers[:max_pages]
                logger.warning(
                    "分类静态页数量超过上限 type=%s sort=%s total=%s max=%s",
                    goods_type.id,
                    sort,
                    paginator.num_pages,
                    max_pages,
                )
            for page_number in page_numbers:
                page_specs.append((goods_type, sort, paginator, page_number))

    total_pages = len(page_specs) + 1
    if total_pages > max_total_pages:
        skipped_pages += total_pages - max_total_pages
        page_specs = page_specs[: max(0, max_total_pages - 1)]
        logger.warning(
            "静态页总数超过上限 total=%s max=%s",
            total_pages,
            max_total_pages,
        )

    pages = [
        (
            INDEX_PAGE,
            render_to_string(
                "goods/static_index.html",
                {**base_context, **build_index_context(types=types)},
            ),
        )
    ]
    for goods_type, sort, paginator, page_number in page_specs:
        context = _list_page_context(paginator, goods_type, sort, page_number)
        if context is None:
            skipped_pages += 1
            continue
        pages.append(
            (
                list_static_page_name(goods_type.id, sort, page_number),
                render_to_string(
                    "goods/static_list.html",
                    {**base_context, **context},
                ),
            )
        )

    if version_before is not None and _current_version() != version_before:
        logger.info("静态页构建被更新版本取代，放弃发布")
        return {
            "published": False,
            "reason": "superseded",
            "pages": 0,
            "skipped_pages": skipped_pages,
        }

    root = _pages_root()
    temp_root = root.parent / f".{root.name}.{uuid.uuid4().hex}.tmp"
    duration_ms = round((time.monotonic() - started) * 1000, 1)
    manifest = {
        "generated_at": timezone.now().isoformat(),
        "build_id": uuid.uuid4().hex,
        "version": version_before or "",
        "page_count": len(pages),
        "skipped_pages": skipped_pages,
        "duration_ms": duration_ms,
        "pages": sorted(relative for relative, _html in pages),
    }
    pages.append(
        (MANIFEST_FILE, json.dumps(manifest, ensure_ascii=False, indent=2))
    )

    try:
        _write_pages(temp_root, pages)
        _replace_directory(temp_root, root)
    finally:
        _remove_tree(temp_root)

    version_after = _current_version()
    if version_before is not None and version_after != version_before:
        _remove_published_pages(root)
        logger.info("静态页发布后数据再次变更，已撤销本次发布")
        return {
            "published": False,
            "reason": "superseded",
            "pages": 0,
            "skipped_pages": skipped_pages,
        }

    logger.info(
        "静态页发布完成 pages=%s skipped=%s duration_ms=%s",
        len(manifest["pages"]),
        skipped_pages,
        duration_ms,
    )
    return {
        "published": True,
        "pages": manifest["page_count"],
        "skipped_pages": skipped_pages,
        "root": str(root),
        "version": manifest["version"],
        "duration_ms": duration_ms,
    }


def _list_page_context(paginator, goods_type, sort, page_number):
    try:
        page_obj = paginator.page(page_number)
    except EmptyPage:
        # 统计页数后、渲染前数据被删除：跳过该页，让访问回退动态视图。
        logger.info(
            "静态页分页已失效，跳过 type=%s sort=%s page=%s",
            goods_type.id,
            sort,
            page_number,
        )
        return None
    decorate_skus_with_prices(list(page_obj.object_list))
    return {
        "goods_type": goods_type,
        "sort": sort,
        "page_obj": page_obj,
        "page_range": paginator.get_elided_page_range(
            page_obj.number,
            on_each_side=2,
            on_ends=1,
        ),
        "querystring": "" if sort == "default" else f"sort={sort}",
    }


def _static_base_context(types):
    return {
        "static_page": True,
        "nav_types": [{"id": t.id, "name": t.name} for t in types],
        "cart_count": 0,
        "user": AnonymousUser(),
        "messages": [],
        "keyword": "",
    }


def _pages_root():
    return Path(settings.STATIC_PAGES_ROOT).resolve()


def _safe_page_path(relative_name):
    return _safe_child(_pages_root(), relative_name)


def _safe_child(root, relative_name):
    root = Path(root).resolve()
    path = (root / relative_name).resolve()
    if path != root and root not in path.parents:
        raise ValueError(f"static page escaped root: {relative_name}")
    return path


def _current_version():
    try:
        return cache.get(STATIC_PAGE_VERSION_KEY)
    except Exception:
        logger.warning("读取静态页版本失败", exc_info=True)
        return None


def _acquire_build_lock():
    token = uuid.uuid4().hex
    timeout = int(getattr(settings, "STATIC_PAGES_BUILD_LOCK_SECONDS", 600))
    try:
        acquired = cache.add(STATIC_PAGE_BUILD_LOCK_KEY, token, timeout=timeout)
    except Exception:
        logger.warning("读取静态页构建锁失败，继续执行", exc_info=True)
        return ""
    if acquired is None:
        # 缓存不可用时无法判断其他实例是否在构建，退化为直接执行。
        logger.warning("静态页构建锁不可用，跳过并发保护")
        return ""
    return token if acquired else None


def _release_build_lock(token):
    if not token:
        return
    try:
        if cache.get(STATIC_PAGE_BUILD_LOCK_KEY) == token:
            cache.delete(STATIC_PAGE_BUILD_LOCK_KEY)
    except Exception:
        logger.warning("释放静态页构建锁失败", exc_info=True)


def _write_pages(temp_root, pages):
    temp_root.mkdir(parents=True, exist_ok=True)
    _ensure_dir_mode(temp_root)
    for relative, text in pages:
        path = _safe_child(temp_root, relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        _ensure_dir_mode(path.parent)
        path.write_text(text, encoding="utf-8", newline="\n")
        _ensure_file_mode(path)


def _replace_directory(source, target):
    """用已经写好的临时目录整体替换正式目录。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    backup = target.parent / f".{target.name}.{uuid.uuid4().hex}.bak"
    had_live = target.exists()
    if had_live:
        target.rename(backup)
    try:
        source.rename(target)
    except OSError:
        if had_live and not target.exists():
            backup.rename(target)
        raise
    if had_live:
        _remove_tree(backup)


def _remove_published_pages(root):
    if not root.is_dir():
        return
    for relative in (INDEX_PAGE, MANIFEST_FILE):
        _safe_child(root, relative).unlink(missing_ok=True)
    list_root = _safe_child(root, "list")
    if list_root.is_dir():
        shutil.rmtree(list_root, ignore_errors=True)


def _remove_tree(path):
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path, ignore_errors=True)


def _ensure_dir_mode(path):
    if os.name != "nt":
        try:
            os.chmod(path, 0o755)
        except OSError:
            pass


def _ensure_file_mode(path):
    if os.name != "nt":
        try:
            os.chmod(path, 0o644)
        except OSError:
            pass
