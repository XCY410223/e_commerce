# e_commerce · 天天生鲜 B2C 电商平台

Django 6.1.2 + MySQL + Redis 的 B2C 生鲜电商骨架项目（服务端渲染模板）。

## 一、快速启动

```bash
# 1. 克隆仓库并创建虚拟环境
git clone https://github.com/XCY410223/e_commerce.git
cd e_commerce
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

# 2. 复制环境变量模板并按本机环境修改
cp .env.example .env

# 3. 环境自检（依赖 / MySQL / Redis / 业务表）
python scripts/check_env.py

# 4. 建表
python manage.py migrate

# 5. 首次灌入演示数据（6 个品类、72 个 SKU、216 张商品图、轮播、促销、演示账号）
#    注意：--reset 会清空订单、地址和非超管用户；库里有需要保留的订单时不要执行
python scripts/seed.py --reset

# 6. 预生成首页与分类列表静态页（匿名访客命中；生产由 Celery 重建）
python manage.py build_static_pages

# 7. 启动
python manage.py runserver 127.0.0.1:8000

# 8. 另开终端启动 Celery worker（激活邮件、静态页重建；Windows 必须加 --pool=solo）
celery -A config.celery worker -l info --pool=solo

# 9. 再另开终端启动定时任务（超时订单取消、支付对账、静态页兜底重建）
celery -A config.celery beat -l info
```

演示账号：`demo` / `demo1234`（已带一条默认收货地址）
后台地址：http://127.0.0.1:8000/admin/ （需先 `python manage.py createsuperuser`）

## 二、FastDFS + Nginx

项目使用 Docker Compose 运行 FastDFS，Nginx 统一提供文件下载与上传入口：

- `fastdfs-tracker`：Tracker 服务（容器内 22122）。
- `fastdfs-storage`：Storage 服务（容器内 23000）及自带 FastDFS Nginx（容器内 8888）。
- `fastdfs-uploader`：容器内 HTTP 上传代理，负责与 Tracker/Storage 进行 FastDFS 二进制协议交互。
- `fastdfs-nginx`：宿主机入口 Nginx，监听 `127.0.0.1:8888`；`/group*/` 反向代理到 Storage Nginx，`/api/upload`、`/api/delete` 代理到上传代理。

```powershell
# 1. 确认 Docker Desktop 已启动
docker info

# 2. 一键拉取镜像、启动并做端到端检查（推荐）
powershell -ExecutionPolicy Bypass -File scripts\start_fastdfs.ps1

# 或者手动执行
docker compose up -d
docker compose ps

# 3. 检查 Nginx 入口
Invoke-WebRequest http://127.0.0.1:8888/healthz -UseBasicParsing

# 4. 上传 -> 下载 -> 删除 端到端自检
.venv\Scripts\python.exe scripts\check_fastdfs.py
```

`.env` 已切换为 FastDFS 存储。数据库里原有的 `goods/xxx.jpg` 等本地相对路径会继续从 `/media/` 读取；新上传图片保存为 `group1/M00/...` 并由 Nginx 返回。数据存放于 Docker 命名卷，执行 `docker compose down` 不会丢文件；只有 `docker compose down -v` 会清空。

上传接口使用 `FDFS_UPLOAD_TOKEN` 鉴权，默认值仅适合本机开发。不要直接把 8888 端口暴露到公网；生产环境应修改令牌并通过防火墙或鉴权网关限制访问。

### 批量导入商品图

图片按 `{SKU_ID}_{商品名}.jpg` 命名（如 `89_烟台红富士苹果500g.jpg`），命令会通过默认存储上传到 FastDFS，并更新对应 SKU 的 `GoodsImage`：

```powershell
.venv\Scripts\python.exe manage.py import_goods_images --dir "$HOME\Pictures\e-commerce" --with-details
```

- 默认只替换主图；`--with-details` 会用原图裁剪两张细节图，保持每个 SKU 3 张图。
- `--dry-run` 只校验不写库；`--only 89,90` 只处理指定 SKU；`--overwrite` 强制重传已在 FastDFS 的图片。
- 已在 FastDFS 的图片默认跳过，命令可以安全重复执行；文件名与 SKU 名称不一致时会跳过并提示。

首页轮播、促销卡片和品类图标使用另一条命令，文件名约定如下：

```powershell
# banner_0_89_烟台红富士苹果500g.jpg -> 轮播 index=0、SKU 89
# promo_0_新客立减20元.jpg          -> 促销 index=0
# logo_新鲜水果.jpg                 -> 品类“新鲜水果”
.venv\Scripts\python.exe manage.py import_site_images --dir "$HOME\Pictures\e-commerce"
```

`import_site_images` 同样支持 `--dry-run`、`--overwrite`，以及 `--kind banner|promo|logo` 只处理某一类。

## 三、目录结构

```
e_commerce/
├── manage.py
├── requirements.txt / .env / .env.example / .gitignore
├── apps/
│   ├── user/    注册/邮件激活/登录/退出/个人中心/收货地址
│   ├── goods/   首页/分类列表/详情/全文搜索/浏览历史
│   ├── cart/    购物车（Redis Hash + 清理补偿待办）
│   ├── order/   确认页/事务下单/订单列表/订单详情/快照/支付结果（mock/支付宝适配）
│   └── promotion/ 活动规则/统一算价/优惠券/邀请历史/订单优惠快照
├── config/      settings / urls / celery 入口
├── db/          base_model.py（审计字段抽象基类）
├── utils/       cart.py（购物车读写）context_processors.py（导航/角标）
├── celery_tasks/  Celery 异步任务（激活邮件）
├── scripts/     check_env.py / seed.py / smoke_test.py
├── static/      css / js
├── templates/   base.html + index.html + {goods,user,cart,order}/
├── media/       上传图片（开发期本地存储）
├── docker/      FastDFS 上传代理与 Nginx 配置
├── docker-compose.yml  Tracker / Storage / 上传代理 / Nginx
└── docs/        项目框架搭建计划书
```

**视图组织约定**

- 标准列表/详情页使用类视图：商品列表 `GoodsListView`、商品详情 `GoodsDetailView`、订单列表 `OrderListView`。
- 登录/退出使用 Django 认证类视图：`UserLoginView`、`UserLogoutView`。
- 注册、地址管理、购物车操作、下单事务等包含多分支或事务逻辑的流程继续使用函数视图。
- 退出接口仅接受 POST 请求，页面顶部退出按钮已改为带 CSRF Token 的表单提交。
- 需要登录的 HTML 页面使用 `login_required` / `LoginRequiredMixin`，未登录时由 Django 自动附加 `next` 参数并跳转登录页。
- 购物车 AJAX 接口对游客开放：未登录也能加购，后端下发 `guest_cart` Cookie（HttpOnly + SameSite=Lax）；`utils.decorators.ajax_login_required` 保留给后续需要登录的 AJAX 接口，返回 `401 + login_url`，避免前端把登录页 HTML 当作 JSON 解析。
- 登录成功后优先回到安全的 `next` 地址（只允许站内地址）；没有 `next` 时回到首页。

**模型管理器**

- `GoodsSKU.objects.online()`：只查询上架商品，可继续链式过滤和排序。
- `Address.objects.for_user(user)` / `Address.objects.default_for(user)`：按用户查询地址、读取默认地址。
- `OrderInfo.objects.for_user(user)`：只查询指定用户的订单。
- 所有继承 `BaseModel` 的模型默认由 `SoftDeleteManager` 自动过滤 `is_delete=True` 的记录。
- `Model.all_objects` 可查询包含软删除记录的全部数据；`soft_delete()` / `restore()` / `hard_delete()` 分别用于软删除、恢复和物理删除。
- 软删除按外键规则级联：`CASCADE` 子记录一起软删除，`PROTECT` 存在关联数据时抛出 `ProtectedError`。
- 通用排序、分页和一次性查询不封装，避免管理器承担过多职责。

**商品图片**

- `GoodsSKU` 不再保存冗余主图字段，主图统一存放在 `GoodsImage`。
- `GoodsImage.is_main=True` 标识主图；保存主图时在事务内锁定 SKU 行，再取消其他图片的主图标记，避免并发保存后出现多张主图。
- 列表页使用 `prefetch_related("images")`，模板通过 `sku.primary_image` 读取主图，避免 N+1 查询。

**商品搜索**

- 迁移 `goods.0004_goodssku_fulltext` 创建全文索引，`goods.0005_fulltext_add_spec` 将其重建为 `FULLTEXT(name, desc, spec) WITH PARSER ngram`，依赖 MySQL 8+；`ngram_token_size` 使用默认值 2。
- 查询侧用 jieba 精确模式切词（最多 5 个词，过滤单字与“的/了/和/与/及/是/在/有”等停用词），多词拼成 `+"词1" +"词2"` 走 `IN BOOLEAN MODE`，实现“所有词都要命中”的 AND 语义，避免 `红富士苹果500g` 被 ngram 拆散后返回无关商品。
- 自定义词典补充 `车厘子 / 红富士 / 阿克苏 / JJ级 / 三文鱼 / 950ml`；`jieba` 0.42 只发源码包，Python 3.13 环境需先 `pip install setuptools`，再 `pip install jieba --no-build-isolation`。若未安装或导入失败，`search.py` 直接退化为整串检索，功能不受影响。
- 布尔检索无结果时退回 `IN NATURAL LANGUAGE MODE`，再按“名称覆盖全部词 → 相关度 → 销量 → 创建时间”排序；也支持价格、销量排序。
- 关键词不足 2 个字符、全文索引不可用或全文无结果时，自动回退到 `name/desc/spec` 的 `LIKE` 匹配。
- 关键词最多保留 50 个字符；搜索页输出 `noindex,follow`，避免搜索结果被搜索引擎大量收录。
- 搜索页不缓存；分页链接会保留 `q` 和 `sort` 参数并做 URL 编码。

**用户激活流程**

- 注册成功后账号 `is_active=False`，Celery 异步任务 `user.send_activation_email` 发送激活邮件。
- 邮箱在数据库层唯一（`user.0003_alter_user_email`），注册仍会预检查用户名/邮箱；并发注册触发唯一键冲突时转换为“已被注册”的表单提示，不返回 500。
- 激活 token 由 `itsdangerous.URLSafeTimedSerializer` 生成，封装 `action + uid + email`，默认 24 小时有效。
- 激活链接格式为 `/user/activate/<token>/`，`uid/email` 封装在 token 载荷中，避免在 URL 查询参数中直接暴露用户 ID。
- itsdangerous 提供的是签名和时效校验，不是真正的加密；token 中不要放入密码等敏感信息。
- 开发环境邮件后端为 console，激活邮件会输出在 Celery worker 终端；部署时设置 `MAIL_BACKEND` 和 `.env.example` 中的 `EMAIL_*` 参数切换 SMTP。
- `SITE_URL` 必须配置为外部可访问的站点地址，否则邮件中的链接会指向 `127.0.0.1`。

**页面缓存**

- 首页和分类列表使用 Redis 模板片段缓存，默认 60 秒，可通过 `GOODS_INDEX_CACHE_TTL`、`GOODS_LIST_CACHE_TTL` 调整。
- 只缓存公共内容区域；顶部用户名、购物车角标、退出表单仍按当前用户实时渲染。
- 商品详情、搜索、购物车、订单和用户中心不缓存。
- 首页轮播只展示已上架、未软删除的 SKU；分类楼层与分类列表同样过滤 `status=STATUS_ONLINE`。
- 后台保存或删除商品种类、SKU、商品图片、首页轮播、促销活动和分类展示记录后，通过 `post_save` / `post_delete` 信号主动清除首页及分类列表缓存；软删除通过 `soft_delete_completed` 信号触发同样的清理。
- 商品种类变化会同时清除顶部导航缓存；导航仅缓存 `id/name`，默认 300 秒，可通过 `GOODS_NAV_CACHE_TTL` 调整。

**页面静态化**

- 首页和分类列表支持预生成 HTML：匿名且无 `sessionid`、无 `guest_cart` Cookie 的 `GET/HEAD` 请求优先读取静态页。
- 登录用户、游客购物车用户、未知查询参数、非法页码或文件缺失时自动回退动态视图，避免用户态内容和 CSRF 信息被公共缓存复用。
- 静态命中响应带 `X-Static-Page: hit`、`Cache-Control: no-cache, must-revalidate` 和 `Vary: Cookie`；Nginx/CDN 不要无条件缓存带 Cookie 的响应。
- 商品、分类、图片、轮播、促销或促销范围变化后，旧静态页立即下架，Celery 任务 `goods.generate_static_pages` 异步重建；Celery Beat 默认每 300 秒兜底重建。
- 手工构建：`python manage.py build_static_pages`。生成结果写入 `STATIC_PAGES_ROOT`（默认 `static_pages/`，已被 `.gitignore` 忽略），`manifest.json` 记录页数、耗时、跳过页数和构建 ID。
- 关键配置：`STATIC_PAGES_ENABLED`、`STATIC_PAGES_AUTO_REFRESH`、`STATIC_PAGES_ROOT`、`STATIC_PAGES_REFRESH_INTERVAL`、`STATIC_PAGES_DEBOUNCE_SECONDS`、`STATIC_PAGES_MAX_LIST_PAGES`、`STATIC_PAGES_MAX_TOTAL_PAGES`。
- 完整的安全、监控与回滚说明见 `docs/static-pages-runbook.md`。

**首页轮播与图片**

- 首页轮播最多展示 4 个 SKU，首图立即加载，其余图片懒加载；存在多张时每 4.5 秒自动切换，支持圆点选择。
- 鼠标悬停或浏览器标签页隐藏时暂停自动轮播，并尊重 `prefers-reduced-motion` 设置。
- 首页右侧促销卡片进入站内活动页 `/promo/<slug>/`，后台可维护活动简介、规则、参与分类、按钮文字与链接。
- 分类楼层使用一次查询读取全部 `IndexTypeGoodsBanner`，再按分类分组，每个分类最多保留 4 条，避免按分类逐个查询。
- 商品列表、搜索、详情缩略图、购物车和个人中心图片统一声明宽高、`loading="lazy"` 与 `decoding="async"`，减少布局偏移和首屏阻塞。

**促销活动与统一算价**

- 当前上线三个活动：新客立减 20 元、生鲜满 99 元免运费、周末海鲜水产 5 折。邀请好友得优惠券活动已下线，首页入口和默认奖励规则由迁移 `promotion.0002_remove_invite_campaign` 清理；邀请相关模型和历史数据保留用于审计，默认 `PROMOTION_REFERRAL_ENABLED=0`，用户中心在没有启用中的邀请奖励规则时自动隐藏入口。
- 唯一算价入口是 `promotion.services.build_quote()`。商品展示、购物车、确认订单、提交订单和支付金额都使用同一套服务端计算，前端传入的金额只用于一致性校验，不能决定优惠。
- 商品行保存原价、成交价、行优惠和活动名称；订单保存优惠前应付、商品优惠、优惠券优惠、运费减免、优惠合计和最终应付。活动结束或改名后，历史订单仍按快照展示。
- 新客券在账号激活、进入个人中心或结算页时幂等补发；仅限无已支付订单的已激活用户，每人一张、默认 30 天有效，且不与周末折扣叠加。
- 免运费按商品活动价小计判断，优惠券不降低门槛；98.99 元仍需 10 元运费，99.00 元免运费。标准运费由 `STANDARD_TRANSIT_PRICE` 配置，默认 10.00 元。
- 周末折扣只对时间窗内、参与分类或 SKU 范围中的上架商品生效；折扣价按分四舍五入，单笔优惠不超过活动配置上限。活动开始前和结束后自动回退原价。
- 优惠券与邀请奖励通过唯一 `grant_key`、数据库唯一约束和事务行锁保证幂等；提交订单时在事务内重新算价、锁定 SKU、核销优惠券，金额或资格变化会拒绝下单并要求重新确认。
- 活动规则在后台 `Promotion` 维护，首页文案在 `IndexPromotionBanner` 维护；执行 `python manage.py init_promotions` 可补齐缺失规则并关联已有首页入口。活动或范围保存、删除后会递增价格缓存版本并清理首页/分类页缓存。
- 上线、监控和回滚步骤见 `docs/promotion-runbook.md`。快速巡检执行 `python manage.py promotion_status --strict`；接入监控可使用 `--json`。

**购物车存储与一致性**

- 购物车按用户保存在 Redis Hash：`cart:<user_id>`，field 为 `sku_id`，value 为数量；`CART_TTL` 默认 30 天，读取和写入都会滑动续期，设为 0 可关闭过期。
- 加入、修改、读取、角标统计和提交订单共用 `utils/cart.py` 的有效商品规则：商品必须未软删除、状态为上线且库存大于 0。
- 加购使用 Lua 原子校验“已有数量 + 本次数量 ≤ 当前库存”；修改数量同样不得超过库存，避免并发加购绕过上限。
- 读取时会把超过当前库存的数量校正为库存值，并清理已下架、已软删除或格式错误的 Hash 字段；购物车明细和顶部角标使用同一结果，不会出现数量不一致。
- 提交订单在事务内再次按 `select_for_update()` 锁定 SKU，校验上架状态和库存，防止读取后商品下架或库存被并发修改。
- 下单清理按订单商品快照扣减数量，而不是直接删除整个 Hash；使用 `order_id` 幂等标记，允许用户在下单后又添加同一 SKU，重复调用也不会重复扣减。
- Redis 清理失败会写入 `CartCleanupTask` 待办表，用户后续访问购物车时自动重试；也可执行 `python manage.py retry_cart_cleanup` 批量补偿。重试按用户 60 秒节流：先看缓存窗口，再用 `exists()` 确认是否真有待办，避免每个页面都查一次待办表。
- 游客购物车存放在 `cart:guest:<token>`，Cookie 里只有随机 token（`GUEST_CART_TTL` 默认 7 天）；登录成功时由 `merge_guest_cart()` 用 Lua 按 SKU 数量相加合并进 `cart:<user_id>`，合并后删除游客键；只有合并成功才清掉 Cookie，Redis 异常时保留 Cookie 以便下次登录重试，超出库存的部分在读取时按库存值收敛。
- 购物车页对游客同样可访问，未登录时“去结算”换成“登录后结算”，登录后购物车已经合并，可直接继续下单。
- 详情页数量框带上库存上限（`data-qty-max`），`+` 按钮点不到超库存；加购请求期间按钮禁用，避免连点累加两次，收到 `stock_limit` 时数量自动回填到库存值。
- 购物车页把本次读取结果挂在 `request._cart_summary`，母版角标直接复用，同一请求不会重复读 Redis 和商品表。
- 加入顺序单独记在 ZSET `cart:<id>:seq` / `cart:guest:<token>:seq`（score 为加入时间），列表按“最近加入在前”展示；加购用 `ZADD NX`，同一商品重复加购不会改变位置，删除、下单清理、游客合并都会同步维护这张顺序表。
- 购物车页的 `+` 同样带 `data-qty-max`，点不到超库存；`-` 在只剩 1 件时禁用，避免误删；删除按钮改走 `cart:delete`，`update` 的 `count<=0` 仍保留给脚本/兼容调用。
- 修改数量带 `expected`（页面看到的旧数量）做 Redis 内 CAS：其他标签页已改过就返回 `409 cart_conflict`，不会后写覆盖；不传 `expected` 时保持原有的绝对覆盖语义，供内部调用使用。
- 删除同样携带 `expected` 并走 Lua CAS，避免删除请求把另一个标签页刚加进来的商品一并移除。
- 购物车页操作后按接口返回的行数量/金额/合计/角标做局部刷新，不整页 reload；请求期间整行按钮禁用防连点，购物车清空时才回落到整页渲染空态。
- 读取时被自动移除的失效商品会计数返回，页面顶部提示“有 N 件商品已下架或售罄，已自动移除”；数量被压回库存的行会在数量下方提示“已调整为库存上限 N 件”。

**订单生成与事务**

- 确认页 `order:place` 只读：读购物车、地址、支付方式，运费固定 10 元，`total_pay = 商品总额 + 运费`，不写数据库。
- 提交 `order:commit` 在一个 `transaction.atomic()` 内完成：锁 SKU → 校验上架/库存 → 扣库存递增销量 → 写 `OrderInfo` → 写 `OrderGoods`（成交价快照）。
- 事务内先用 `User.objects.select_for_update()` 锁用户行，并在外层再加一把带过期时间的 Redis 用户锁（`order:lock:{user_id}`），把“事务提交 + 清理购物车”整段流程串行化。数据库行锁只持续到事务结束，单独使用无法阻止新标签页在提交后、购物车清空前进场。
- 加锁按主键升序一次锁完整批 SKU（`select_for_update().filter(id__in=...).order_by("id")`），避免两笔订单交叉加锁产生死锁；遇到 InnoDB 1213/1205 自动重试，最多 3 次。
- 订单总额在锁内按 `sku.price` 重算，和 `OrderGoods` 快照同源；若与购物车展示金额不一致（期间被调价），直接拒绝并提示回购物车重新确认，不会静默改价。
- 确认页提交商品件数、商品金额和购物车内容指纹；提交时在锁内重新计算并比对，防止“总件数/总金额恰好相同但商品组成已变化”的旧页面静默下单。
- 订单号 `yyyyMMddHHmmss + uuid4 前 6 位`，唯一键冲突时重新生成重试；`order_id` 同时作为购物车清理幂等标记。
- 确认页下发一次性提交令牌（Redis `order:submit:{user_id}`，10 分钟，每个用户只保留最新一张确认页的令牌）；提交时用 Lua 比较并删除，双击/重复 POST 只有一个请求能通过，重新打开确认页会作废旧标签页的令牌。提交按钮点击后立即禁用，作为第一道前端拦截。
- 校验失败抛 `ValueError` → 整个事务回滚（已扣库存、已写明细一并还原）→ 提示后回购物车；锁冲突重试耗尽或订单号冲突则给出友好提示，不返回 500。
- 收货地址在拿到用户行锁后会重新确认仍存在；地址管理、下单和默认地址切换共用同一把用户行锁，避免并发留下多个默认地址或给刚删除的地址下单。
- 被历史订单引用的地址删除时直接做软删除，保留 `OrderInfo.addr` 外键，历史订单仍可展示且不会触发 `ProtectedError` 500。
- 下单、确认页和购物车 AJAX 接口捕获 Redis 故障并返回可读提示/503，不再把缓存抖动直接暴露成 500。
- 购物车清理放在事务提交之后、Redis 用户锁释放之前执行：Redis 不参与数据库事务，避免提交阶段回滚时订单不存在但购物车已被清空；清理失败写入 `CartCleanupTask` 待办，后续访问自动重试。
- 同一用户的下单请求等待锁超过 5 秒会返回 `OrderBusy`，页面提示“当前下单人数较多，请稍后重试”，不会重复扣库存。
- 未支付订单默认 30 分钟后由 Celery Beat 调用 `order.cancel_expired_orders` 自动取消；取消事务锁订单与整批 SKU，按商品明细回补库存、扣回销量，并以 `pay_status` 作为幂等闸门，重复/并发取消只回补一次。
- 超时时长、扫描间隔和批量大小可通过 `ORDER_UNPAID_TIMEOUT_SECONDS`、`ORDER_CANCEL_CHECK_INTERVAL`、`ORDER_CANCEL_BATCH_SIZE` 调整；也可用 `python manage.py cancel_expired_orders --dry-run` 预检查候选订单。
- 已取消订单禁止再次发起支付；若真实渠道在取消后才送达支付通知，会记录 critical 日志并确认通知，需通过后续退款/对账流程人工处理，不能把已回补库存的订单直接改回已支付。
- 订单详情 `order:detail` 按 `OrderInfo.objects.for_user(request.user)` 取单，展示商品、金额、收货信息和支付信息；用户中心“最近订单”和订单列表的订单号都指向详情页。
- `OrderInfo` 保存收件人/电话/地址快照，`OrderGoods` 保存 `sku_name` 快照；展示优先用快照，旧数据由迁移 `order.0004` 回填，后续修改地址或商品名不会改变历史订单。
- 订单列表支持 `?status=unpaid|paid` 筛选，分页链接会保留筛选参数；查询使用 `select_related("addr")` 合并地址查询，避免每单一次 Address SQL。

**订单评论**

- `OrderComment` 与 `OrderGoods` 一对一，每条订单商品最多一条评论；仅订单所有者且订单已支付可提交，表单、视图和服务层三重校验。
- 评论默认“待审核”，后台 `OrderCommentAdmin` 支持通过/驳回并记录审核人、时间和备注；只有已通过评论展示在商品详情页。
- 订单详情展示本人评论及状态，驳回后可修改重提；审核通过后不允许修改。评论长度 1–500 字符，空白、超长、重复和越权提交均被拒绝。
- 商品页用户名脱敏，评论正文使用 Django 自动转义；后台禁止新增/删除评论以保留审计链。
- 旧 `OrderGoods.comment` 列本版本兼容保留，历史非空评论已由 `order.0007` 迁入新表；下一版本确认稳定后再删除旧列。
- 紧急关闭提交入口：设置 `ORDER_COMMENT_ENABLED=0` 并重启，已审核评论仍展示。完整上线、监控和回滚步骤见 `docs/order-comment-runbook.md`。

**支付结果获取**

- 默认 `PAYMENT_PROVIDER=mock`：`order:pay` 跳转到本地模拟收银台，点击“模拟支付成功”后调用与真实通知相同的 `payment.mark_paid()`，可完整走通支付状态、轮询和对账链路。
- `payment.mark_paid()` 用 `OrderInfo.objects.select_for_update()` 锁订单行，按“未支付 → 已支付”幂等更新；重复通知只接受相同 `trade_no`，不同流水号记为冲突，不会重复扣库存或累加销量。
- 支付通知和同步返回会把渠道映射到订单 `pay_method` 后再落库，支付宝通知不能把微信/银联订单改成已支付。
- 异步通知 `order:pay_notify`（`/order/pay/notify/<channel>/`）免登录、`csrf_exempt`，但必须先验签；mock 渠道用 HMAC-SHA256，支付宝渠道用 SDK 验签并校验 `app_id`、`seller_id`、`out_trade_no`、`total_amount` 和 `trade_status`。
- 主动查询 `order:pay_status`（`/order/pay/status/<order_id>/`）只允许订单所属用户访问；状态页轮询会按 `PAYMENT_QUERY_THROTTLE_SECONDS` 节流，未支付时调用渠道查询作为通知丢失的兜底。
- 同步返回 `order:pay_return` 只做展示：支付宝返回参数先验签，验不过再主动查询，绝不直接依据 URL 参数改支付状态。
- 未支付订单由 Celery 任务 `order.query_unpaid_orders` 定时对账；`config/celery.py` 已自动发现 `order.tasks`。mock 渠道下任务直接跳过，不消耗外部查询额度。
- 切换真实支付宝：安装 `python-alipay-sdk`，在 `.env` 配置 `PAYMENT_PROVIDER=alipay`、APP_ID、应用私钥、支付宝公钥及 notify/return 地址。微信/银联暂未接入，订单选择其他支付方式时会给出明确提示。
- `mock` 收银台只用于本地开发和验收，生产环境必须切换到真实支付渠道，避免任何人通过模拟按钮把订单改成已支付。mock 收银台仅在 `DEBUG=1` 且 `PAYMENT_PROVIDER=mock` 时可用；`DEBUG=0` 仍配置 mock 时 `manage.py check` 会报 `order.E001`，直接阻止启动。

**支付宝沙盒接入**

1. 安装依赖：`pip install "python-alipay-sdk>=3.3.0"`。项目代码从 `alipay` 包导入 `AliPay`，PyPI 包名是 `python-alipay-sdk`，不是 `alipay-sdk-python`。
2. 在支付宝开放平台进入沙盒应用，获取沙箱 `APPID`、应用私钥、支付宝公钥；卖家账号可用于沙箱买家付款，`seller_id` 可选但建议填写。
3. 在项目根目录 `.env` 中配置：

```env
PAYMENT_PROVIDER=alipay
PAYMENT_ALIPAY_APP_ID=沙箱应用APPID
PAYMENT_ALIPAY_SELLER_ID=沙箱卖家ID
PAYMENT_ALIPAY_PRIVATE_KEY=应用私钥
PAYMENT_ALIPAY_PUBLIC_KEY=支付宝公钥
PAYMENT_ALIPAY_DEBUG=1
PAYMENT_ALIPAY_GATEWAY=
PAYMENT_ALIPAY_RETURN_URL=http://127.0.0.1:8000/order/pay/return/alipay/
PAYMENT_ALIPAY_NOTIFY_URL=http://127.0.0.1:8000/order/pay/notify/alipay/
```

`PAYMENT_ALIPAY_DEBUG=1` 且 `PAYMENT_ALIPAY_GATEWAY` 留空时，项目自动使用沙盒网关；不要把 `DEBUG=1` 与正式网关混用。

4. 本地开发时 `return_url` 可以回到 `127.0.0.1`；`notify_url` 是支付宝服务器发起的异步回调，本地地址无法从公网访问。要完整验证异步通知，使用 ngrok/Cloudflare Tunnel 等临时公网地址，并把隧道域名加入 `.env`：

```env
DJANGO_ALLOWED_HOSTS=127.0.0.1,localhost,xxxx.trycloudflare.com
CSRF_TRUSTED_ORIGINS=https://xxxx.trycloudflare.com
TRUST_X_FORWARDED_PROTO=1
```

否则 Django 会因 `DisallowedHost` 或 HTTPS 代理协议识别错误拒绝请求。
5. 用沙箱买家账号完成付款，确认订单从“未支付”变为“已支付”，再检查订单详情、支付时间和支付流水号。

生成指定订单的沙盒支付链接：

```bash
python manage.py alipay_sandbox_link <订单号> --public-base https://your-public-host.example.com
```

也可以分别指定两个公网地址：

```bash
python manage.py alipay_sandbox_link <订单号> \
  --notify-url https://your-public-host.example.com/order/pay/notify/alipay/ \
  --return-url https://your-public-host.example.com/order/pay/return/alipay/ \
  --url-only
```

命令会拒绝 `localhost`、内网 IP、非 HTTP(S) 地址和非沙盒网关；`--url-only` 只输出可复制的支付链接。

## 四、已完成 / 未完成

| 阶段 | 内容 | 状态 |
|---|---|---|
| P0 环境复位 | 依赖、虚拟环境、requirements.txt | ✅ |
| P1 骨架修复 | settings/urls 六处阻断缺陷、四个 AppConfig | ✅ |
| P2 数据层 | 12 个 Model + 全局软删除过滤 + AUTH_USER_MODEL + 迁移 | ✅ 已落库 |
| P3 用户模块 | 注册/邮件激活/登录/退出/个人中心/地址 CRUD | ✅ |
| P4 商品模块 | 首页楼层/分类列表/详情（规格切换）/全文搜索/浏览历史 | ✅（首页/分类列表 Redis 片段缓存 + 匿名静态页） |
| P5 购物车 | Redis Hash、库存/上架校验、滑动 TTL、幂等清理与补偿、游客购物车与登录合并 | ✅ |
| P6 订单模块 | 确认页/事务下单/升序加锁/死锁重试/用户级串行化/提交幂等/扣库存/订单列表/订单详情/快照/支付结果/超时取消与库存回补/订单评论提交与审核 | ✅（mock 收银台；真实支付宝需安装 SDK 并配置商户密钥） |
| P7 后台与静态化 | admin 注册、富文本、Celery 首页/分类列表静态化 | ✅ |
| P8 文件存储 | FastDFS + Nginx + Django 存储后端 | ✅（Docker Desktop 运行时生效） |
| P9 联调验收 | 冒烟脚本 32 项（含订单详情与模拟支付链路） | ✅ |
| P10 促销交易接入 | 统一算价、订单优惠快照、免运费、周末折扣、新客券；邀请活动已下线 | ✅ |

## 五、验收自查

```bash
python manage.py check                        # 无报错
python manage.py makemigrations --check --dry-run   # No changes detected
python manage.py build_static_pages           # 生成首页/分类列表静态页并输出耗时
python manage.py test goods.tests -v 2        # 静态化生成/命中/回退/边界/并发保护
python manage.py test order.tests.OrderCommentTests -v 2   # 评论提交/审核/越权/展示
python manage.py test -v 2                    # 单元测试 + HTTP 加购下单链路
python manage.py promotion_status --strict    # 促销配置与金额快照巡检
python manage.py runserver 127.0.0.1:8000
python scripts/smoke_test.py                  # 全链路冒烟，结果以脚本输出为准
python scripts/check_fastdfs.py               # FastDFS 上传/下载/删除
python manage.py retry_cart_cleanup           # 重试 Redis 清理失败的购物车待办
```

## 六、几个坑（已踩过，别再踩）

1. **自定义用户模型必须先于首次 migrate**：`AUTH_USER_MODEL="user.User"` 已设置，
   一旦先建了 `auth_user` 再切换，只能清库重来。
2. **Windows 版 Redis 5.0 不支持 RESP3**：`redis-py` 6+ 默认发 `HELLO` 会报
   `unknown command HELLO`。`requirements.txt` 已约束 `redis>=5.0,<6` 使用 RESP2，
   同时 Django cache 在 `CONNECTION_POOL_KWARGS` 中显式设置 `protocol=2`。
   Celery 也必须使用该依赖范围，否则任务投递阶段就会失败。
3. **Celery 在 Windows 必须 `--pool=solo`**：`celery -A config.celery worker -l info --pool=solo`。
4. **`apps/` 包名与 INSTALLED_APPS 写法二选一**：当前用 `sys.path.insert` + 裸 app 名
   （`user` / `goods` / `cart` / `order`），`AppConfig.name` 必须同步为裸名，不可混用 `apps.user`。
5. **OrderInfo.user 是 PROTECT**：删测试用户前必须先删订单，直接删会抛 `ProtectedError`。
6. **163 SMTP 报 550 User has no permission**：不能使用网页登录密码，需在 163 邮箱设置中
   开启 SMTP 服务并生成“客户端授权码”，填入 `EMAIL_HOST_PASSWORD`；若 25 端口受限，
   可改用 `EMAIL_PORT=465`、`EMAIL_USE_SSL=1`、`EMAIL_USE_TLS=0`。
7. **订单评论旧列不要急着删**：`OrderGoods.comment` 本版本保留用于回滚，历史数据已迁移到
   `df_order_comment`。确认线上稳定并完成备份后再走下一版删除列，避免回滚时旧代码找不到字段。
8. **静态页目录不要提交**：`static_pages/` 已被 `.gitignore` 忽略；页面里的图片地址取构建时的
   `FDFS_URL`，公开仓库或生产构建前先确认媒体域名是对外地址。Celery Beat 只启动一个实例，
   构建锁只是兜底，不是多实例调度的替代方案。
