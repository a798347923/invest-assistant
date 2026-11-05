# 参与贡献 / Contributing

感谢关注本项目。请在提交 Issue / Pull Request 前阅读以下约定。

## 开发环境

- Windows 10/11，Python 3.11+（当前开发环境 3.14）
- 安装依赖：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

- 密钥只放在 `.env`（已被 gitignore）；模板见 `.env.example`。**不要**在 Issue、日志、测试或提交中粘贴任何真实密钥、账户或交易记录。

## 提交前检查

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check src tests scripts
```

- 默认测试不访问真实网络；`-m live` 才会运行数据源冒烟测试。
- 新功能请附测试；修 bug 优先先写复现测试。
- 提交信息使用 Conventional Commits（`feat:` / `fix:` / `docs:` / `chore:` …），一个逻辑变更一个提交。

## 策略与实盘改动

- 策略实验必须遵循 [`AGENTS.md`](AGENTS.md) 中的窗口分离、样本外验证与组合级模拟协议；负结果也要记录到 `docs/rule-experiments.md`。
- 实盘相关代码默认 `live.enabled=false`、`live.dry_run=true`；任何自动化改动必须保持 fail-closed（定位/回读/确认失败即零提交），并补充对应测试。
- 不要在 PR 中要求维护者提供真实账户、券商凭证或实盘授权。

## 数据与隐私

- `data/`、`logs/`、`*.db`、`.env`、`config/secrets.*` 均不会进入仓库。
- 请勿提交包含个人持仓、账户号、姓名、邮箱、手机号或本地绝对路径的截图/日志；如需示例，使用脱敏后的虚构数据。

## 许可证

提交即表示你同意以本项目的 [MIT License](LICENSE) 授权你的贡献。
