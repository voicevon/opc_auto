# Trae 每日积分签到插件：需求分析与技术路线总结

> 对应插件：`trae_score/`（2026-10-11 立项并上线）。本文沉淀立项前的需求调研、路线取舍与关键侦查结论，作为后续维护与扩展（如 Work 专属积分）的决策依据。

---

## 1. 需求本质

无人值守的每日定时任务：自动完成 Trae 积分签到领取，失败可留痕、可呈报，绝不死循环重试。

## 2. 关键调研结论：签到有两条独立途径（易混淆点）

| 途径 | 入口位置 | 积分类型 | 额度 | 用途 |
| :--- | :--- | :--- | :--- | :--- |
| TRAE IDE 客户端内 | IDE 里点头像 → "签到"按钮 | IDE 积分 | 免费 100/天，会员 200/天 | IDE 模型调用 |
| Trae Work 网页端 | work.trae.cn（需登录态） | Work 专属积分 | 200/天 | 仅 TraeWork 办公 |

**官方 FAQ 确认**：两套积分互不通用；IDE 积分签到入口只在 IDE 客户端内，浏览器上没有；Work 专属积分只能在 Trae Work 端领。积分有效期均为 31 个自然日，断签即浪费。

**用户拍板**：仅做 IDE 签到（`trae_score` 插件范围）。

## 3. 技术路线岔路口与取舍

| 候选路线 | 原理 | 结论 |
| :--- | :--- | :--- |
| A. CDP 附着浏览器 | 附着宿主 Chrome 模拟点击签到页 | 仅适用于 Web 签到；IDE 签到入口在客户端原生 UI 内，不适用 |
| B. 本地解密登录态 + 直连官方 API ✅ | 读取客户端本地加密登录态，解出 token 直调 `api.trae.cn` | **最终采用**。社区已验证可行，无需打开客户端、无需模拟点击 |
| C. AI Agent 每日接管 | Schedule 派发子代理模拟人工 | 成本高、不可审计，放弃 |

**关键认知转折**：最初设想 CDP/UI 自动化路线，但侦查发现签到入口在 IDE 客户端内而非网页；进一步调研发现社区已有成熟的「本地登录态解密 + API 重放」方案，直接采用最轻路线，CDP 方案废弃。

## 4. 侦查发现（本机实证）

1. **登录态载体**：`%APPDATA%\Trae CN\User\globalStorage\storage.json`，键 `iCubeAuthInfo://icube.cloudide`，AES-128-CBC 加密（b64 存储）。
2. **解密机制**：头部 6 字节 + 32 字节随机密钥；密钥派生 `SHA-512(SHA-512(key) XOR(URE⊕DRE))`，取派生值前 16 字节为 AES key、后 16 字节为 IV；明文跳过 64 字节 HMAC 后为 JSON。
3. **设备 ID**：优先从 storage.json 的 `iCubeAuthInfo://icube-dc:<device_id>` 键提取，兜底 `ahanet\tt_net_config.config` 正则 `device_id&#\*(\d+)`。
4. **官方签到 API**：`https://api.trae.cn/trae/api/v2/ug/checkin_credits/{status|claim}`，POST `{"req_source": 1}`（1=IDE，2=SOLO，积分互通取 1），鉴权头 `Authorization: Cloud-IDE-JWT {token}` + `x-device-id`。
5. **多客户端互通**：Trae CN 与 TRAE SOLO CN 同账号积分互通，扫描顺序 `["Trae CN", "TRAE SOLO CN"]`。

## 5. 落地架构

```
trae_score/
├── claimer.py    # 核心签到器：解密登录态 → 设备ID → status/claim API（幂等）
├── runner.py     # 运行入口：编排签到 + 状态落盘 data/trae_score_status.json（保留30天）
└── data/         # 签到结果状态真理源（插件内自治）
```

- **调度**：并入 `src/cron_daily.py` 每日定时执行链（由 Trae 外部自动化托管，采集→自愈→签到单时钟合并）
- **API**：`GET /api/trae_score`（查状态）、`POST /api/trae_score/claim`（手动触发）
- **安全**：令牌仅在内存中使用，绝不写日志、不落盘；依赖 `requests` + `pycryptodome`
- **幂等**：`status.checked_in=true` 自动跳过，同日多次运行安全

## 6. 运维与风险边界（人机分流）

| 场景 | 处置 |
| :--- | :--- |
| 登录态过期（HTTP 401/403） | 状态标记 `needs_login`，提示打开客户端重新登录一次即恢复；**绝不重试**，属路径二人工事项 |
| API 改版/页面变更 | 结果落盘可查，属确定性技术退化，走路径一代码自愈 |
| 依赖环境 | `pycryptodome` 缺失会导致解密失败，重装即可 |

**二阶段候选**（未立项）：Work 专属积分签到（work.trae.cn，需 Web 路线或其独立 API）、IDE 签到与积分余额的 Web 门户可视化。
