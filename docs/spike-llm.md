# S2 DeepSeek 原型验证报告

| 项 | 值 |
|---|---|
| 日期 | 2026-10-11 |
| 模型 | deepseek-chat（OpenAI 兼容 API） |
| 脚本 | `scripts/spike_llm.py`（3 标的分析卡）、`scripts/spike_llm_retry.py`（重试验证） |
| 原始数据 | `docs/spike-llm-raw.json` |

## 一、结论

1. **JSON 结构化输出稳定**：两轮共 6 次生成 + 1 次重试验证，解析成功率 100%；约定字段全部齐全（summary / technicals / news_sentiment / bull_points / bear_points / risks / action_bias / confidence / data_caveats）。
2. **延迟与成本低**：单次 1.5~3.5 秒；单次约 375~412 输入 / 221~463 输出 tokens。按当前用量模式（每日 10 只候选 + 触发解读 + 收盘报告），月成本在**个位数人民币量级**（以官网实际价格与账单为准）。
3. **防幻觉行为良好**：行情数据缺失时，模型未编造价格，而是明确标注「数据不足 / 获取失败」并压低置信度（0.3~0.4）；对提供的基金净值序列能正确计算（区间跌幅 12%）。
4. **上下文缓存生效**：usage 中出现 `prompt_cache_hit_tokens`（第二轮命中 128 tokens）——固定系统提示前缀的策略有效。
5. **网络存在偶发连接重置**：DeepSeek 连续快速调用第 4 次出现 RemoteDisconnected；重试验证脚本第 1 次重试即成功。**重试 + 退避是必需机制，不是可选优化。**

## 二、实测数据（两轮合并）

| 标的 | 延迟 | 输入 tokens | 输出 tokens | action_bias | confidence | JSON |
|---|---|---|---|---|---|---|
| 贵州茅台 600519 | 1.8s / 3.5s | 375 | 264 / 463 | 观察 | 0.3~0.4 | ✅ |
| 宁德时代 300750 | 2.4~2.5s | 387 | 310~350 | 观察 | 0.4 | ✅ |
| 场外基金 011186 | 1.5~2.2s | 412 | 221~314 | 观察 | 0.7 | ✅ |

bias 全部为「观察」——符合"数据有限时保守"的预期行为。

## 三、对设计/后续任务的影响

- **B2-04 LLM 客户端**：OpenAI 兼容客户端 + `response_format=json_object` + pydantic 校验 + tenacity 重试（指数退避）；usage 记录包含 cached tokens。
- **提示词策略**：固定系统前缀（利于上下文缓存），数据放 user 消息；提示词必须包含「JSON」字样与字段定义（DeepSeek 要求）。
- 触发解读/收盘报告复用同一客户端；`max_tokens ≈ 900` 足够（实测输出 200~470）。
- 免责声明固定附加在卡片与推送尾部；prompt 中已要求禁止收益承诺与「必涨必跌」表述。

## 四、遗留

- S2 期间东财 push2his 主机开始间歇性失败 → 已在 `docs/spike-datasource.md` 更新日线回退策略（先 EM 前复权、失败回落新浪）。
- 正式实现时补充：LLM 失败降级（模板消息兜底）、当日分析缓存复用。
