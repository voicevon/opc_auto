---
name: humanoid-browser-harvest
description: "OPC Auto 拟人化浏览器数据采集与技术自愈技能。用于接管本地真实浏览器会话复用 Cookie，拟人化抓取电商订单发票，输出标准 Trace 留痕并执行确定性选择器自愈。"
---

# Humanoid Browser Harvest (拟人化浏览器采集与战术自愈 SOP)

本技能专职负责系统 **Layer 2 (执行层)** 的宿主浏览器会话接管、拟人化采集与 Trace 遥测输出，服从 **Layer 1** 调度与分流判定。

---

## 一、 浏览器接管与拟人采集 SOP

### 1. CDP 接管本地真实会话 (复用免密 Cookie)
```python
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    # 优先附着本地真实浏览器 (调试端口 9222)
    browser = p.chromium.connect_over_cdp("http://localhost:9222")
    context = browser.contexts[0]
    page = context.pages[0] if context.pages else context.new_page()
    page.goto("https://buyertrade.taobao.com/trade/itemlist/list_bought_items.htm")
```

### 2. 拟人化交互准则
- **随机抖动**：翻页与操作间隔加入高斯随机延迟（`1.2s ± 0.4s`），严禁突发超高频请求；
- **事件等待**：优先监听 `networkidle` 与关键 DOM 容器出现，拒绝无意义硬编码长 sleep。

---

## 二、 面向 AI 的 Trace 诊断留痕契约 (Agent-Centric Telemetry)

采集异常或字段缺失时，必须在 `temp/browser_traces/` 留存单次诊断切片：`trace_{platform}_{timestamp}.json`，向上抛送至 Layer 1 分流器。

```json
{
  "trace_version": "1.0",
  "platform": "taobao",
  "target_url": "https://buyertrade.taobao.com/trade/itemlist/list_bought_items.htm",
  "dom_fingerprint": {
    "total_order_containers_detected": 15,
    "selector_health": {
      "order_card": {"selector": "div[data-id]", "hit_count": 15, "status": "healthy"},
      "invoice_btn": {"selector": "button.invoice-action", "hit_count": 0, "status": "DRIFT_SUSPECTED"}
    }
  },
  "failure_context": {
    "failed_step": "extract_invoice_status",
    "error_type": "SelectorNotFound",
    "snapshot_html_snippet": "<div class=\"order-actions\"><span class=\"ant-btn-link\">查看发票</span></div>"
  }
}
```

---

## 三、 路径一：确定性技术退化自愈战术 (Tactical Healing)

当 Layer 1 确认为技术退化（DOM 漂移/选择器失效）并触发修复时，按以下战术执行：

1. **提取切片**：读取 Trace 中的 `snapshot_html_snippet`，定位目标元素的新特征；
2. **弹性选择器升级**：禁用动态哈希类名，采用语义化与候选链：
   ```python
   INVOICE_SELECTORS = [
       "text='查看发票'",
       "text='申请开票'",
       "a:has-text('发票')",
       "[data-action*='invoice']"
   ]
   ```
3. **本地验证**：在离线快照或沙箱回放中验证通过后更新规则或代码，工作区变更就绪，等待用户指令。
