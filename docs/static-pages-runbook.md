# 首页与分类列表静态化运维手册

## 1. 需求口径

- 首页 `/` 和分类列表 `/list/<type_id>/?sort=<default|price|sales>&page=<n>` 预生成为 HTML 文件。
- 只有**匿名、且没有 session Cookie、也没有游客购物车 Cookie** 的 `GET/HEAD` 请求读取静态页。
- 登录用户、游客购物车用户、带未知查询参数、非法页码或静态文件缺失时，回退现有动态视图。
- 商品、分类、商品图片、首页轮播、促销活动或促销范围变更后，立即下架旧静态页并通过 Celery 异步重建。
- Celery Beat 定时全量重建作为兜底；也可用管理命令手工构建。
- 关闭开关 `STATIC_PAGES_ENABLED=0` 后，所有请求回到动态视图，不依赖数据库迁移。

## 2. 代码入口

| 关注点 | 位置 |
|---|---|
| 静态页生成、发布、失效、命中判断 | `apps/goods/static_pages.py` |
| 动态视图与静态页共用的列表查询/排序 | `apps/goods/queries.py` |
| Celery 任务 `goods.generate_static_pages` | `apps/goods/tasks.py` |
| 手工构建命令 | `apps/goods/management/commands/build_static_pages.py` |
| 首页/列表命中入口 | `apps/goods/views.py` |
| 数据变更信号 | `apps/goods/signals.py`、`apps/promotion/signals.py` |
| 静态页模板 | `templates/goods/static_index.html`、`templates/goods/static_list.html` |
| 公共内容片段 | `templates/goods/_index_content.html`、`templates/goods/_list_content.html` |
| 配置项 | `config/settings.py`、`.env.example` |
| 测试 | `apps/goods/tests.py` |

## 3. 边界条件

- 空分类：仍生成第 1 页，展示“该分类下暂无上架商品”。
- 多页分类：按默认/价格/销量三种排序分别生成全部分页；排序追加 `id` 次级键，避免翻页重复或漏项。
- `sort` 非白名单、`page` 非正整数：不命中静态页，交给动态视图；非法页码按现有动态分页语义返回 404。
- 未知查询参数（如 `?foo=1`）：不命中静态页，避免把未参与生成的参数误映射到公共文件。
- 文件缺失或为空：回退动态视图，不返回 500。
- `HEAD`：允许命中，返回与 `GET` 一致的响应头，正文由服务器/测试客户端按 HTTP 语义处理。
- 数据变更发生在构建过程中：发布前与发布后各校验一次版本，发现变化则放弃/撤销本次发布。
- 两个构建任务并发：Redis 构建锁只允许一个实例发布；缓存不可用时退化为直接执行并记录告警。
- 单分类页数超过 `STATIC_PAGES_MAX_LIST_PAGES`、总页数超过 `STATIC_PAGES_MAX_TOTAL_PAGES`：截断并在日志与 `manifest.json` 的 `skipped_pages` 中记录，超出部分回退动态页。
- Windows 本地构建依赖目录重命名；若有进程长期占用静态文件，发布可能失败并保留旧目录，需停止占用后重试。

## 4. 安全审查

- 用户态隔离：命中静态页前检查 `sessionid` 与 `guest_cart` Cookie；登录用户、购物车用户始终走动态页。
- 隐私：静态 HTML 不包含用户名、购物车角标、session、CSRF token、消息提示或后台信息。
- 响应头：静态命中返回 `Cache-Control: no-cache, must-revalidate`、`Vary: Cookie`、`X-Static-Page: hit`。
- 代理缓存：Nginx/CDN 不得无条件缓存带 Cookie 的响应；若改为 Nginx 直接返回文件，必须复刻上述 Cookie 条件。
- 路径穿越：所有静态页路径经 `Path.resolve()` 校验，逃逸出 `STATIC_PAGES_ROOT`（含 `..`、绝对路径、指向外部的软链接）一律拒绝。
- 文件权限：POSIX 下文件 `0644`、目录 `0755`；静态页目录已被 `.gitignore` 忽略，不进入仓库。
- 媒体地址：页面中的图片 URL 使用构建时的 `FDFS_URL`，必须配置为对外可访问地址，避免把 `127.0.0.1` 或内网地址写进公开页面。
- 内容转义：静态页复用 Django 模板自动转义，商品名、促销名等数据不拼接原始 HTML。

## 5. 性能与资源

- 当前演示数据实测：37 个页面、约 255 KB、约 170 条 SQL、构建耗时约 330–400 ms。
- 单页平均约 6.7 KB，最大约 24 KB，一次性读入内存返回的开销可忽略。
- 构建先统计页数、再渲染、最后写临时目录并整体切换；构建期间不暴露半成品。
- 首页每个分类最多展示 4 个商品；分类列表每页固定 10 个商品。
- 每页查询使用 `prefetch_related("images")`；分类计数由 `Paginator` 缓存，避免重复 count，实测平均每页约 4–5 条 SQL。
- 默认每 300 秒兜底重建一次，数据变更另有 15 秒防抖窗口合并连续保存。
- 默认每分类/排序最多 500 页、单次构建最多 2000 页，防止异常数据或分类暴涨拖垮任务。
- 生产建议由 Nginx 直接返回静态文件，Django 命中逻辑作为回退；不要为静态页再叠加模板缓存。

## 6. 可维护性

- 动态视图和静态页生成共用 `goods/queries.py` 的过滤与排序规则，避免两套规则漂移。
- 首页与分类列表的公共内容抽成模板片段，动态页和静态页复用同一份 HTML 结构。
- 静态页发布摘要统一返回 `published/reason/pages/skipped_pages/duration_ms/version`，命令与 Celery 共用。
- 版本号、防抖标记、构建锁集中在 `static_pages.py`，不散落在视图或信号中。
- 新增排序方式时，只需扩展 `queries.LIST_SORTS` 与排序分支，并同步静态页模板的排序链接。

## 7. 测试

```powershell
.\.venv\Scripts\python.exe manage.py test goods.tests -v 2
.\.venv\Scripts\python.exe manage.py test -v 1
```

当前 20 个静态化测试覆盖：首页/列表/清单生成、匿名命中与响应头、HEAD、登录与游客 Cookie 回退、未知参数与非法页码、文件缺失/为空、开关关闭、空分类、多页、单分类页数上限、总页数上限、旧页清理、失效与版本递增、模型提交后失效、防抖只提交一次、构建锁、构建中变更放弃发布、发布后变更撤销页面、Celery 任务、路径穿越。

## 8. 监控

- 构建日志：`静态页发布完成 pages=... skipped=... duration_ms=...`；异常构建记录 `logger.warning/exception`。
- 任务结果：Celery 任务 `goods.generate_static_pages` 的返回字典包含 `published`、`reason`、`pages`、`skipped_pages`、`duration_ms`。
- 清单巡检：`static_pages/manifest.json` 的 `generated_at`、`page_count`、`skipped_pages`、`build_id`。
- 命中率：按 `X-Static-Page: hit` 统计匿名首页/列表命中比例；持续为 0 时检查目录权限、路径和任务状态。
- 回退量：动态视图 QPS 突增通常意味着静态页缺失、构建失败或版本反复失效。
- 构建失败：关注 `reason=locked`、`reason=superseded`、超页数告警和目录重命名错误。
- 建议告警：`generated_at` 超过 2 个重建周期未更新；构建失败连续 3 次；`skipped_pages > 0`；静态目录磁盘使用异常增长。

巡检命令：

```powershell
.\.venv\Scripts\python.exe manage.py shell -c "import json,pathlib; m=json.loads(pathlib.Path('static_pages/manifest.json').read_text(encoding='utf-8')); print({k:m[k] for k in ('generated_at','page_count','skipped_pages','duration_ms')})"
```

## 9. 发布与回滚

发布前：

1. 确认 `STATIC_PAGES_ROOT` 指向可写目录，磁盘余量足够。
2. 确认 `FDFS_URL` 是对外地址，避免静态页写入内网图片域名。
3. 执行 `manage.py check`、`makemigrations --check --dry-run`、`manage.py test -v 1`。
4. 执行 `manage.py build_static_pages`，检查 `manifest.json` 页数与耗时。

发布顺序：

1. 发布应用代码（无数据库迁移）。
2. 启动/重启 Celery worker，确保能发现 `goods.generate_static_pages`。
3. **只启动一个 Celery Beat 实例**，避免重复调度；Beat 与数据变更任务都受构建锁保护。
4. 执行一次 `manage.py build_static_pages` 预热。
5. 匿名访问首页和分类列表，确认 `X-Static-Page: hit`；登录用户确认无该响应头。

回滚：

1. 首选设置 `STATIC_PAGES_ENABLED=0` 并重启应用，所有请求立即回退动态视图。
2. 可选删除 `static_pages/` 目录，避免残留文件被误用。
3. 回滚应用代码即可；本功能没有数据库迁移，无需反向迁移。
4. 回滚后检查首页、分类列表、登录态页面和购物车角标；恢复时重新开启开关并手工构建一次。

## 10. 责任边界

| 角色 | 责任 |
|---|---|
| 产品 | 确认首页/分类列表可静态化的范围、更新时效与降级体验 |
| 开发 | 生成/发布/失效逻辑、共享查询、测试、日志与缺陷修复 |
| 测试 | 匿名/登录/游客、边界参数、并发构建、开关与回滚回归 |
| 运维 | 目录权限、磁盘、Celery worker/beat、Nginx 缓存策略、监控告警 |
| 安全 | Cookie 隔离、代理缓存、路径穿越、公开页面隐私复核 |
| 运营 | 商品/轮播/促销变更后的页面时效确认 |

## 11. 上线验收清单

- [ ] 匿名首页与分类列表命中静态页，响应头包含 `X-Static-Page: hit`。
- [ ] 登录用户、游客购物车用户不命中静态页，用户名与购物车角标正确。
- [ ] 静态 HTML 不含 CSRF token、session、用户名或本机绝对路径。
- [ ] 商品/分类/轮播/促销变更后旧页立即下架，任务完成后新页生效。
- [ ] `HEAD`、未知参数、非法页码、文件缺失均不会 500。
- [ ] 空分类、多页分类、页数上限和旧页清理符合预期。
- [ ] 全量测试通过，`manifest.json` 的页数/耗时/跳过页数可查询。
- [ ] 已演练 `STATIC_PAGES_ENABLED=0` 回滚。
