# 安全策略 / Security Policy

## 报告安全问题

请**不要**通过公开 Issue 报告以下问题：

- 密钥、令牌、账户或个人信息泄露；
- 认证/鉴权绕过、面板未授权访问；
- 实盘下单链路中的误单、重复提交或资金风险；
- 依赖供应链问题。

请使用 GitHub 的 **Private vulnerability reporting**（仓库页面 → Security → Report a vulnerability）或 Security Advisory 私下联系维护者。我们会尽快确认并回复。

## 使用边界

- 本项目是研究工具，不构成投资建议，也不承诺收益。
- 实盘功能默认关闭（`live.enabled=false`、`live.dry_run=true`）。请先用模拟盘 / dry-run 验证，不要用无法承受损失的资金测试。
- 不要在 Issue、日志或截图中提交真实 API Key、SMTP 密码、Server酱 SendKey、Tushare Token、券商账号或交易密码。
- 第三方数据源与券商 App 的使用需遵守各自服务条款；程序化交易可能涉及报备与合规要求，请自行确认。

## 支持版本

当前维护 `0.4.x`。旧版本不保证安全更新。
