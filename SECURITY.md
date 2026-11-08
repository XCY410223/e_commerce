# 安全策略

## 支持范围

仅维护 `main` 分支的最新代码。历史版本不单独提供安全修复。

## 私下报告漏洞

请不要通过公开 Issue 披露安全问题。优先使用仓库的 **Security → Advisories → Report a vulnerability** 私下报告，并尽量包含以下信息：

- 受影响的页面、接口或提交版本；
- 复现步骤和必要的请求样例；
- 影响范围（如越权、支付篡改、密钥泄露）；
- 已知的缓解方式或修复建议。

提交前请删除真实密码、Cookie、支付密钥、用户地址和手机号。维护者会在确认问题后给出处理计划；在修复发布前，请不要公开漏洞细节或利用代码。

## 仓库边界

- 仓库只应包含演示占位数据；真实用户数据、生产数据库导出和业务日志不得提交。
- `.env`、支付宝私钥、SMTP 授权码、SSH 私钥和部署凭据必须由运行环境注入。
- 演示账号 `demo / demo1234` 仅用于本地体验，禁止用于任何公网或生产部署。
- FastDFS 上传入口必须使用强随机令牌，并且只对内网或受鉴权网关保护的环境开放。

## 部署前检查

1. 将 `DJANGO_DEBUG` 设为 `0`，配置正确的 `DJANGO_ALLOWED_HOSTS` 和 `CSRF_TRUSTED_ORIGINS`。
2. 更换 `DJANGO_SECRET_KEY`、数据库密码、`PAYMENT_MOCK_SECRET` 和 `FDFS_UPLOAD_TOKEN`。
3. 使用真实支付渠道时，密钥只保存在服务器环境变量或密钥管理服务中；不要启用 mock 收银台。
4. 对公网启用 HTTPS，限制数据库、Redis、FastDFS 和 SSH 的访问来源。
5. 定期备份数据库和媒体文件，并在发布前验证回滚流程。

## 公开仓库维护建议

- 开启 GitHub Secret scanning、Push protection 和 Dependabot alerts。
- 为 `main` 开启分支保护，要求 CI 通过后再合并。
- 定期检查 Collaborators、Deploy keys、Actions secrets 和第三方应用授权。
- 如果凭据曾进入过 Git 历史，仅删除当前文件不够，必须立即轮换凭据并清理历史。
