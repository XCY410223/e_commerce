"""项目测试发现器：兼容 apps/ 裸模块导入结构。"""

from pathlib import Path

from django.apps import apps
from django.conf import settings
from django.test.runner import DiscoverRunner


class ProjectDiscoverRunner(DiscoverRunner):
    """不指定标签时，只从已安装且包含 tests.py 的应用发现测试。"""

    def build_suite(self, test_labels=None, **kwargs):
        if not test_labels:
            apps_dir = Path(settings.BASE_DIR) / "apps"
            test_labels = [
                app_config.label
                for app_config in apps.get_app_configs()
                if apps_dir in Path(app_config.path).parents
                and (Path(app_config.path) / "tests.py").exists()
            ]
        return super().build_suite(test_labels or None, **kwargs)
