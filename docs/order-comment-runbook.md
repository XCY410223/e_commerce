# 订单评论功能上线与运维手册

## 1. 需求口径

- 评论对象是订单商品行（`OrderGoods`），每条订单商品最多一条评论。
- 只有订单所有者、且订单状态为已支付时可以提交评论。
- 评论提交后状态为“待审核”；后台审核通过后才展示在商品详情页。
- 订单详情页始终展示当前用户自己的评论和审核状态；驳回后可修改并重新提交。
- 审核通过后不允许用户修改；后台可重新审核，但保留审核人和审核时间。
- `ORDER_COMMENT_ENABLED=0` 可立即关闭提交入口，已审核评论继续展示。

## 2. 代码入口

| 关注点 | 位置 |
|---|---|
| 评论模型、状态、脱敏用户名 | `apps/order/models.py` |
| 提交、审核、商品页查询 | `apps/order/services.py` |
| 表单校验 | `apps/order/forms.py` |
| POST 视图与订单详情上下文 | `apps/order/views.py` |
| 路由 `order:comment` | `apps/order/urls.py` |
| 订单详情评论区 | `templates/order/detail.html` |
| 商品详情评价区 | `templates/goods/detail.html` |
| 后台审核 | `apps/order/admin.py` |
| 初始化迁移 | `apps/order/migrations/0007_ordercomment.py` |

## 3. 边界条件

- 未支付订单：服务层拒绝，不写评论。
- 非订单所有者：视图返回 404，服务层再次按 `order__user` 过滤。
- 空白内容：表单与服务层双重拒绝。
- 超过 500 字符：表单与服务层双重拒绝。
- 已存在待审核或已通过评论：拒绝重复提交。
- 已驳回评论：允许修改重提，状态回到“待审核”，清空原审核信息。
- 评论表软删除记录：重提时恢复同一条记录，避免一对一唯一键冲突。
- 数据库并发：提交事务对订单商品行加 `select_for_update()`，同一商品行串行提交。
- 审核备注超过 200 字符：审核失败并返回错误，不落状态。

## 4. 安全审查

- 权限：视图按当前用户过滤订单商品，服务层再次校验所有者，防止越权。
- 状态：服务层校验 `pay_status=已支付`，不能通过构造请求评论未支付订单。
- CSRF：提交入口只接受 POST，模板包含 `{% csrf_token %}`。
- XSS：评论内容使用 Django 模板自动转义，`linebreaksbr` 不关闭转义。
- 隐私：商品详情页用户名脱敏，不展示完整账号。
- 审计：保存审核人、审核时间、审核备注；后台禁止新增和删除评论。
- 滥用：当前未接入敏感词、频率限制和图片审核；上线前需由安全和运营确认是否必须补齐。

## 5. 性能与资源

- `OrderComment.order_goods` 是一对一唯一键，数据库层面保证一商品一评论。
- `(status, -create_time)` 索引服务后台筛选和商品页最近评价查询。
- 订单详情使用 `prefetch_related("skus__review")`，避免每条商品单独查询评论。
- 商品详情使用 `select_related("order_goods__order__user")`，避免展示用户名时 N+1。
- 商品页最多展示最近 20 条已审核评论，避免单页无限增长；如需“查看全部”，下一版本增加分页页。
- 单条评论上限 500 字符；按 100 万条估算，正文约 0.5 GB，另加索引和审核字段，需要纳入备份容量评估。

## 6. 可维护性

- 表单、视图、服务、模型分层，HTTP 校验与业务校验分离。
- 评论长度、审核备注长度统一定义在 `OrderComment` 常量中。
- 审核动作统一走 `audit_order_comment()`，后台批量动作不会绕过审计字段。
- 旧 `OrderGoods.comment` 列本版本保留，历史非空数据由迁移写入新表；确认稳定后的下一版本再删除旧列。
- 当前商品页固定上限 20 条，若后续需要评分、图片、回复和分页，应扩展 `OrderComment` 而不是继续堆叠 `OrderGoods` 字段。

## 7. 测试

```powershell
.\.venv\Scripts\python.exe manage.py test order.tests.OrderCommentTests -v 2
.\.venv\Scripts\python.exe manage.py test order -v 1
.\.venv\Scripts\python.exe manage.py test -v 1
```

评论测试覆盖：正常提交、未支付拒绝、越权 404、重复提交、驳回重提、空白/超长、审核权限、商品页只展示已通过、订单详情状态展示、后台通过/驳回、紧急开关。

## 8. 监控

- 提交成功日志：`订单评论已提交`，字段包含 `comment_id`、`order_id`、`line_id`、`created`。
- 审核日志：`订单评论已审核`，字段包含 `comment_id`、`status`、`auditor_id`。
- 关注待审核积压：后台按“待审核”筛选；建议待审核超过 100 条或最早一条超过 24 小时告警。
- 关注商品详情页 P95 延迟和 SQL 数；评论查询异常会随商品详情 5xx 暴露。
- 关注提交接口 4xx/5xx 比例；越权请求应表现为 404，不应出现 500。
- 关注数据库磁盘增长和备份耗时，评论正文与索引纳入容量评估。

可用以下命令巡检状态分布：

```powershell
.\.venv\Scripts\python.exe manage.py shell -c "from order.models import OrderComment; from django.db.models import Count; print(list(OrderComment.objects.values('status').annotate(total=Count('id'))))"
```

## 9. 发布与回滚

发布前：

1. 备份数据库，确认 `df_order_comment` 创建权限和磁盘空间。
2. 执行 `manage.py check`、`makemigrations --check --dry-run`、订单测试和全量测试。
3. 在预发环境验证提交、审核通过、商品页展示、驳回重提和开关关闭。

发布顺序：

1. 先执行 `python manage.py migrate order`，新增评论表和索引；旧列保留。
2. 再发布应用代码。
3. 验证后逐步放量；审核队列由运营接管。

回滚：

1. 首选设置 `ORDER_COMMENT_ENABLED=0` 并重启应用，立即停止新增评论。
2. 应用代码可回滚到旧版本，因为旧 `OrderGoods.comment` 列仍保留。
3. 保留 `df_order_comment` 表，等待修复后前滚；不要直接反向迁移。
4. 只有确认评论数据可丢弃且已有备份时，才允许回退到 `order.0006`；反向迁移会删除评论表。
5. 回滚后检查商品详情页、订单详情页和后台评论列表是否正常，恢复后再打开开关。

## 10. 责任边界

| 角色 | 责任 |
|---|---|
| 产品 | 确认评论规则、审核 SLA、是否允许修改和展示范围 |
| 开发 | 代码、迁移、测试、日志、缺陷修复和前滚方案 |
| 测试 | 权限、状态、边界、XSS、并发和回滚回归 |
| 运维/DBA | 备份、迁移执行、监控告警、容量和回滚操作 |
| 运营/客服 | 审核队列、驳回原因、用户申诉处理 |
| 安全 | 敏感词、频率限制、内容合规和隐私复核 |

## 11. 上线验收清单

- [ ] 已支付订单所有者能提交，未支付和非所有者不能提交。
- [ ] 待审核评论不在商品详情页出现，审核通过后出现。
- [ ] 驳回后订单详情显示原因并可重提。
- [ ] 后台审核动作记录审核人、时间和备注。
- [ ] `ORDER_COMMENT_ENABLED=0` 后提交入口关闭，已有评论仍展示。
- [ ] 订单模块和全量测试通过，监控日志可检索。
