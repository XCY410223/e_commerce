"""页面静态化：生成、命中、回退、边界与并发保护。"""

import json
import tempfile
from decimal import Decimal
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.core.cache import cache
from django.test import TestCase, override_settings

from .models import GoodsSKU, GoodsSPU, GoodsType, IndexTypeGoodsBanner
from .static_pages import (
    INDEX_PAGE,
    MANIFEST_FILE,
    STATIC_PAGE_BUILD_LOCK_KEY,
    STATIC_PAGE_PENDING_KEY,
    STATIC_PAGE_VERSION_KEY,
    _safe_page_path,
    build_static_pages,
    invalidate_static_pages,
    list_static_page_name,
    schedule_static_pages_refresh,
    serve_static_page,
)
from .tasks import generate_static_pages


class StaticPageTests(TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.settings_override = override_settings(
            STATIC_PAGES_ENABLED=True,
            STATIC_PAGES_AUTO_REFRESH=False,
            STATIC_PAGES_ROOT=self.root,
            STATIC_PAGES_MAX_LIST_PAGES=500,
            STATIC_PAGES_MAX_TOTAL_PAGES=2000,
            STATIC_PAGES_BUILD_LOCK_SECONDS=60,
            ALLOWED_HOSTS=["testserver", "localhost"],
            CACHES={
                "default": {
                    "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
                    "LOCATION": "static-page-tests",
                }
            },
        )
        self.settings_override.enable()
        cache.clear()

        self.goods_type = GoodsType.objects.create(name="静态页分类", logo="static")
        self.spu = GoodsSPU.objects.create(name="静态页商品")
        self.sku = self._create_sku("静态页测试苹果", "19.90", sales=2)
        IndexTypeGoodsBanner.objects.create(
            type=self.goods_type,
            sku=self.sku,
            index=0,
        )

    def tearDown(self):
        cache.delete(STATIC_PAGE_BUILD_LOCK_KEY)
        cache.delete(STATIC_PAGE_PENDING_KEY)
        self.temp_dir.cleanup()
        self.settings_override.disable()

    def _create_sku(self, name, price, sales=0, goods_type=None):
        return GoodsSKU.objects.create(
            type=goods_type or self.goods_type,
            spu=self.spu,
            name=name,
            price=Decimal(price),
            stock=10,
            sales=sales,
            status=GoodsSKU.STATUS_ONLINE,
        )

    def _list_path(self, sort, page, type_id=None):
        return self.root / list_static_page_name(
            type_id or self.goods_type.id, sort, page
        )

    def test_build_publishes_index_lists_and_manifest(self):
        result = build_static_pages()

        self.assertTrue(result["published"])
        self.assertGreaterEqual(result["pages"], 4)
        self.assertEqual(result["skipped_pages"], 0)
        self.assertTrue((self.root / INDEX_PAGE).is_file())
        self.assertTrue((self.root / MANIFEST_FILE).is_file())
        for sort in ("default", "price", "sales"):
            self.assertTrue(self._list_path(sort, 1).is_file())

        manifest = json.loads(
            (self.root / MANIFEST_FILE).read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["page_count"], result["pages"])
        self.assertEqual(len(manifest["pages"]), result["pages"])
        self.assertIn(INDEX_PAGE, manifest["pages"])
        self.assertGreaterEqual(manifest["duration_ms"], 0)
        self.assertIn("generated_at", manifest)

        html = (self.root / INDEX_PAGE).read_text(encoding="utf-8")
        self.assertIn(self.sku.name, html)
        self.assertIn('data-static-page="1"', html)
        self.assertNotIn('name="csrf-token"', html)
        self.assertNotIn("sessionid", html)

    def test_anonymous_index_and_list_hit_static_file(self):
        build_static_pages()
        client = self.client

        index_response = client.get("/")
        self.assertEqual(index_response.status_code, 200)
        self.assertEqual(index_response.headers["X-Static-Page"], "hit")
        self.assertEqual(
            index_response.headers["Cache-Control"], "no-cache, must-revalidate"
        )
        self.assertEqual(index_response.headers["Vary"], "Cookie")
        self.assertIn(self.sku.name, index_response.content.decode("utf-8"))

        list_response = client.get(
            f"/list/{self.goods_type.id}/?sort=price&page=1"
        )
        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(list_response.headers["X-Static-Page"], "hit")
        self.assertIn(self.sku.name, list_response.content.decode("utf-8"))

    def test_head_request_hits_static_page_without_body(self):
        build_static_pages()

        response = self.client.head("/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["X-Static-Page"], "hit")
        self.assertEqual(response.content, b"")
        self.assertTrue(
            response.headers["Content-Type"].startswith("text/html")
        )

    def test_session_or_guest_cart_cookie_falls_back_to_dynamic_page(self):
        build_static_pages()

        self.client.cookies[settings.SESSION_COOKIE_NAME] = "dummy-session"
        session_response = self.client.get("/")
        self.assertEqual(session_response.status_code, 200)
        self.assertNotIn("X-Static-Page", session_response.headers)

        guest_client = self.client_class()
        guest_client.cookies["guest_cart"] = "a" * 20
        guest_response = guest_client.get("/")
        self.assertEqual(guest_response.status_code, 200)
        self.assertNotIn("X-Static-Page", guest_response.headers)

    def test_unknown_query_invalid_sort_and_page_fall_back(self):
        build_static_pages()
        base = f"/list/{self.goods_type.id}/"

        for query in (
            "foo=1",
            "sort=hot",
            "page=0",
            "page=abc",
            "sort=price&page=-1",
        ):
            with self.subTest(query=query):
                response = self.client.get(f"{base}?{query}")
                # 非法页码由动态列表按既有语义返回 404；关键是绝不能命中静态文件，
                # 更不能返回 500 或旧页面。
                self.assertIn(response.status_code, (200, 404))
                self.assertNotIn("X-Static-Page", response.headers)

    def test_missing_or_empty_file_falls_back_to_dynamic_page(self):
        build_static_pages()

        (self.root / INDEX_PAGE).unlink()
        missing_file = self.client.get("/")
        self.assertEqual(missing_file.status_code, 200)
        self.assertNotIn("X-Static-Page", missing_file.headers)

        self._list_path("price", 1).write_text("", encoding="utf-8")
        empty_file = self.client.get(
            f"/list/{self.goods_type.id}/?sort=price&page=1"
        )
        self.assertEqual(empty_file.status_code, 200)
        self.assertNotIn("X-Static-Page", empty_file.headers)

    def test_disabled_setting_skips_static_pages(self):
        with override_settings(STATIC_PAGES_ENABLED=False):
            result = build_static_pages()
            self.assertFalse(result["published"])
            self.assertEqual(result["reason"], "disabled")
            self.assertFalse((self.root / INDEX_PAGE).exists())

            response = self.client.get("/")
            self.assertEqual(response.status_code, 200)
            self.assertNotIn("X-Static-Page", response.headers)

    def test_empty_category_generates_empty_list_page(self):
        empty_type = GoodsType.objects.create(name="空分类", logo="empty")

        result = build_static_pages()

        self.assertTrue(result["published"])
        page = (
            self.root
            / list_static_page_name(empty_type.id, "default", 1)
        )
        self.assertTrue(page.is_file())
        html = page.read_text(encoding="utf-8")
        self.assertIn("暂无上架商品", html)

    def test_multi_page_category_generates_every_page(self):
        for index in range(10):
            self._create_sku(
                f"多页商品{index}",
                f"{10 + index}.00",
                sales=index,
            )

        build_static_pages()

        for sort in ("default", "price", "sales"):
            self.assertTrue(self._list_path(sort, 1).is_file())
            self.assertTrue(self._list_path(sort, 2).is_file())
            self.assertFalse(self._list_path(sort, 3).exists())

        manifest = json.loads(
            (self.root / MANIFEST_FILE).read_text(encoding="utf-8")
        )
        self.assertIn(
            list_static_page_name(self.goods_type.id, "price", 2),
            manifest["pages"],
        )

    def test_page_cap_limits_generation_and_reports_skips(self):
        for index in range(10):
            self._create_sku(f"超限商品{index}", f"{20 + index}.00")

        with override_settings(STATIC_PAGES_MAX_LIST_PAGES=1):
            result = build_static_pages()

        self.assertTrue(result["published"])
        self.assertEqual(result["skipped_pages"], 3)
        for sort in ("default", "price", "sales"):
            self.assertTrue(self._list_path(sort, 1).is_file())
            self.assertFalse(self._list_path(sort, 2).exists())

    def test_total_page_cap_bounds_memory_and_disk(self):
        with override_settings(STATIC_PAGES_MAX_TOTAL_PAGES=2):
            result = build_static_pages()

        self.assertTrue(result["published"])
        self.assertEqual(result["pages"], 2)
        self.assertEqual(result["skipped_pages"], 2)
        self.assertTrue((self.root / INDEX_PAGE).is_file())
        self.assertTrue(self._list_path("default", 1).is_file())
        self.assertFalse(self._list_path("price", 1).is_file())

    def test_rebuild_removes_stale_pages(self):
        build_static_pages()
        stale_category = self.root / "list" / "999" / "default-1.html"
        stale_category.parent.mkdir(parents=True, exist_ok=True)
        stale_category.write_text("old", encoding="utf-8")
        stale_page = self._list_path("default", 99)
        stale_page.write_text("old", encoding="utf-8")

        build_static_pages()

        self.assertFalse(stale_category.exists())
        self.assertFalse(stale_page.exists())
        self.assertTrue((self.root / INDEX_PAGE).is_file())

    def test_invalidate_removes_pages_and_bumps_version(self):
        build_static_pages()
        version_before = cache.get(STATIC_PAGE_VERSION_KEY)

        invalidate_static_pages()

        self.assertNotEqual(cache.get(STATIC_PAGE_VERSION_KEY), version_before)
        self.assertFalse((self.root / INDEX_PAGE).exists())
        self.assertFalse((self.root / MANIFEST_FILE).exists())
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("X-Static-Page", response.headers)

    def test_model_commit_invalidates_old_pages(self):
        build_static_pages()
        self.assertTrue((self.root / INDEX_PAGE).is_file())

        with self.captureOnCommitCallbacks(execute=True):
            self.sku.name = "静态页测试苹果（更新）"
            self.sku.save()

        self.assertFalse((self.root / INDEX_PAGE).is_file())

    def test_debounce_schedules_only_one_task(self):
        with override_settings(
            STATIC_PAGES_AUTO_REFRESH=True,
            STATIC_PAGES_DEBOUNCE_SECONDS=60,
        ):
            with mock.patch(
                "goods.tasks.generate_static_pages.apply_async"
            ) as apply_async:
                self.assertTrue(schedule_static_pages_refresh())
                self.assertFalse(schedule_static_pages_refresh())
                self.assertEqual(apply_async.call_count, 1)

                cache.delete(STATIC_PAGE_PENDING_KEY)
                self.assertTrue(schedule_static_pages_refresh())
                self.assertEqual(apply_async.call_count, 2)

    def test_version_change_during_build_aborts_publish(self):
        with mock.patch(
            "goods.static_pages._current_version",
            side_effect=["v1", "v2"],
        ):
            result = build_static_pages()

        self.assertFalse(result["published"])
        self.assertEqual(result["reason"], "superseded")
        self.assertFalse((self.root / INDEX_PAGE).exists())

    def test_version_change_after_publish_revokes_pages(self):
        with mock.patch(
            "goods.static_pages._current_version",
            side_effect=["v1", "v1", "v2"],
        ):
            result = build_static_pages()

        self.assertFalse(result["published"])
        self.assertEqual(result["reason"], "superseded")
        self.assertFalse((self.root / INDEX_PAGE).exists())
        self.assertFalse((self.root / MANIFEST_FILE).exists())

    def test_build_lock_prevents_concurrent_publish(self):
        cache.add(STATIC_PAGE_BUILD_LOCK_KEY, "another-worker", 60)

        result = build_static_pages()

        self.assertFalse(result["published"])
        self.assertEqual(result["reason"], "locked")
        self.assertFalse((self.root / INDEX_PAGE).exists())

    def test_celery_task_builds_pages_and_clears_pending_marker(self):
        cache.set(STATIC_PAGE_PENDING_KEY, "1", 60)

        result = generate_static_pages.apply().get()

        self.assertTrue(result["published"])
        self.assertTrue((self.root / INDEX_PAGE).is_file())
        self.assertIsNone(cache.get(STATIC_PAGE_PENDING_KEY))

    def test_path_traversal_is_rejected(self):
        with self.assertRaises(ValueError):
            _safe_page_path("../escape.html")
        with self.assertRaises(ValueError):
            _safe_page_path(str(self.root.parent / "escape.html"))

        request = self.client.get("/").wsgi_request
        self.assertIsNone(serve_static_page(request, "../../escape.html"))
