"""
OPC Auto 拟人化浏览器接管与数据采集服务 (Humanoid Browser Harvest Service)
核心职责：
1. AgentTraceLogger: 面向 AI Agent 的专属结构化执行追踪与 DOM 变迁诊断留痕器
2. HumanoidBrowserHarvester: 基于原生 Chrome DevTools Protocol (CDP) 接管宿主真实浏览器，
   复用本地已登录 Cookie/Session，模拟真人浏览交互提取采购与发票数据。
"""

import os
import sys
import json
import time
import socket
import struct
import base64
import random
import urllib.request
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple

# 集中隔离的诊断留痕目录 (遵循 AGENTS.md 准则)
TRACE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "temp", "browser_traces"))


# ==============================================================================
# 一、 面向 AI Agent 的专属诊断留痕器 (Agent-Centric Trace Logger)
# ==============================================================================

class AgentTraceLogger:
    """
    面向 AI 智能体的深度执行追踪与现场切片记录器。
    
    【核心设计意图】：
    本记录器输出的机器级 Trace 档案受众首要是 AI Agent，而非人类业务员。
    用于在深夜演化时钟中由 AI 分析页面改版、选择器漂移与异常现场，驱动代码自进化。
    """
    def __init__(self, platform: str = "taobao", trace_id: Optional[str] = None):
        self.platform = platform
        self.trace_id = trace_id or f"trace_{platform}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        self.start_time = datetime.now()
        
        # 确保诊断留痕目录就绪
        os.makedirs(TRACE_DIR, exist_ok=True)
        self.trace_file = os.path.join(TRACE_DIR, f"{self.trace_id}.json")

        self.payload: Dict[str, Any] = {
            "trace_id": self.trace_id,
            "trace_version": "1.0",
            "platform": platform,
            "created_at": self.start_time.isoformat(),
            "status": "RUNNING",
            "browser_info": {},
            "steps": [],
            "selector_health": {},
            "dom_fingerprint": {},
            "extracted_summary": {
                "total_items_found": 0,
                "valid_records_extracted": 0,
                "field_completeness_rate": 0.0
            },
            "failure_context": None
        }

    def record_browser_info(self, cdp_endpoint: str, user_agent: str, viewport: Dict[str, int]):
        """记录宿主浏览器环境指纹"""
        self.payload["browser_info"] = {
            "cdp_endpoint": cdp_endpoint,
            "user_agent": user_agent,
            "viewport": viewport
        }

    def log_step(self, step_name: str, action: str, details: Optional[Dict[str, Any]] = None, duration_ms: int = 0):
        """记录拟人化操作单步时序"""
        step_entry = {
            "step_index": len(self.payload["steps"]) + 1,
            "step_name": step_name,
            "action": action,
            "timestamp": datetime.now().isoformat(),
            "duration_ms": duration_ms,
            "details": details or {}
        }
        self.payload["steps"].append(step_entry)

    def record_selector_metric(self, field_name: str, selector: str, hit_count: int, sample_text: str = ""):
        """
        记录选择器健康度指标。
        用于深夜演化时钟中快速识别哪些字段的选择器已漂移失效。
        """
        status = "healthy"
        if hit_count == 0:
            status = "FAILED_ZERO_HIT"
        elif hit_count > 50:
            status = "AMBIGUOUS_OVER_MATCH"

        self.payload["selector_health"][field_name] = {
            "selector": selector,
            "hit_count": hit_count,
            "status": status,
            "sample_snippet": sample_text[:100] if sample_text else ""
        }

    def record_failure_context(self, failed_step: str, error_msg: str, dom_html_snippet: str = "", suspected_cause: str = ""):
        """记录失败现场切片（供 AI 诊断并重构代码）"""
        self.payload["status"] = "FAILED"
        self.payload["failure_context"] = {
            "failed_step": failed_step,
            "error_message": error_msg,
            "suspected_cause": suspected_cause,
            "snapshot_html_snippet": dom_html_snippet[:4000] if dom_html_snippet else "",
            "snapshot_timestamp": datetime.now().isoformat()
        }

    def finish(self, extracted_items: List[Dict[str, Any]], success: bool = True):
        """完成采集会话，计算完整度并持久化写入 Trace 档案"""
        end_time = datetime.now()
        total_duration = (end_time - self.start_time).total_seconds()
        
        self.payload["status"] = "SUCCESS" if success else self.payload.get("status", "FAILED")
        self.payload["finished_at"] = end_time.isoformat()
        self.payload["total_duration_sec"] = round(total_duration, 2)
        
        # 统计字段完整度
        total = len(extracted_items)
        valid = 0
        if total > 0:
            # 校验核心字段健全度 (order_id, pay_amount, store_name)
            valid = sum(1 for item in extracted_items if item.get("order_id") and item.get("pay_amount"))
            rate = round(valid / total, 2)
        else:
            rate = 0.0

        self.payload["extracted_summary"] = {
            "total_items_found": total,
            "valid_records_extracted": valid,
            "field_completeness_rate": rate
        }

        # 持久化写入磁盘
        with open(self.trace_file, "w", encoding="utf-8") as f:
            json.dump(self.payload, f, ensure_ascii=False, indent=2)
        
        return self.trace_file

    @classmethod
    def get_latest_traces(cls, limit: int = 5) -> List[Dict[str, Any]]:
        """获取最近生成的机器 Trace 档案列表 (供 AI 自诊断读取)"""
        if not os.path.exists(TRACE_DIR):
            return []
        files = [os.path.join(TRACE_DIR, f) for f in os.listdir(TRACE_DIR) if f.endswith(".json")]
        files.sort(key=lambda x: os.path.getmtime(x), reverse=True)
        results = []
        for fp in files[:limit]:
            try:
                with open(fp, "r", encoding="utf-8") as f:
                    results.append(json.load(f))
            except Exception:
                continue
        return results


# ==============================================================================
# 二、 原生极轻量 CDP 通信协议栈 (Zero External Dependencies)
# ==============================================================================

class RawCDPClient:
    """基于 Python 标准库构建的原生 WebSocket CDP 协议客户端，无任何外部库依赖"""
    def __init__(self, ws_url: str, timeout: float = 12.0):
        self.ws_url = ws_url
        self.timeout = timeout
        self.sock = None
        self._msg_id = 0
        self._connect()

    def _connect(self):
        url = self.ws_url
        if url.startswith("ws://"):
            url = url[5:]
        host_port, path = url.split("/", 1)
        path = "/" + path
        if ":" in host_port:
            host, port = host_port.split(":")
            port = int(port)
        else:
            host, port = host_port, 80

        self.sock = socket.create_connection((host, port), timeout=self.timeout)
        key = base64.b64encode(os.urandom(16)).decode('ascii')
        handshake = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host_port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(handshake.encode('utf-8'))
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("WebSocket handshake failed: socket closed")
            response += chunk

        if b"101 Switching Protocols" not in response:
            raise ConnectionError(f"WebSocket handshake rejected: {response.decode('utf-8', errors='ignore')}")

    def send_frame(self, text: str):
        payload = text.encode('utf-8')
        length = len(payload)
        mask_key = os.urandom(4)
        header = bytearray([0x81])
        if length <= 125:
            header.append(0x80 | length)
        elif length <= 65535:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        header.extend(mask_key)
        masked_payload = bytearray(b ^ mask_key[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(header + masked_payload)

    def recv_frame(self) -> str:
        def read_exact(n):
            buf = bytearray()
            while len(buf) < n:
                chunk = self.sock.recv(n - len(buf))
                if not chunk:
                    raise ConnectionError("Socket closed during frame read")
                buf.extend(chunk)
            return buf

        b1, b2 = read_exact(2)
        opcode = b1 & 0x0F
        has_mask = bool(b2 & 0x80)
        length = b2 & 0x7F

        if length == 126:
            length = struct.unpack("!H", read_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", read_exact(8))[0]

        mask = read_exact(4) if has_mask else None
        data = read_exact(length)

        if has_mask:
            data = bytearray(b ^ mask[i % 4] for i, b in enumerate(data))
        return data.decode('utf-8', errors='replace')

    def call(self, method: str, params: Optional[Dict[str, Any]] = None, timeout: float = 8.0) -> Dict[str, Any]:
        self._msg_id += 1
        req_id = self._msg_id
        payload = {"id": req_id, "method": method, "params": params or {}}
        self.send_frame(json.dumps(payload))
        
        self.sock.settimeout(timeout)
        while True:
            raw = self.recv_frame()
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            if msg.get("id") == req_id:
                if "error" in msg:
                    raise RuntimeError(f"CDP error: {msg['error']}")
                return msg.get("result", {})

    def evaluate(self, expression: str, timeout: float = 8.0) -> Any:
        """在页面上下文中直接执行 JavaScript 表达式并取回结果"""
        res = self.call("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True
        }, timeout=timeout)
        return res.get("result", {}).get("value")

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass


# ==============================================================================
# 三、 拟人化真实浏览器接管驱动器 (Humanoid Browser Harvester)
# ==============================================================================

class HumanoidBrowserHarvester:
    """
    拟人化宿主浏览器接管与数据收割器。
    
    【核心特性】：
    1. 通过 9222 调试端口接管已登录的真实 Chrome/Edge，复用本地 Cookie/会话；
    2. 无任何 WebDriver 指纹泄露，天然规避滑块与设备风控；
    3. 全链路对接 AgentTraceLogger，自动留存诊断切片与健康度指标。
    """
    def __init__(self, cdp_port: int = 9222):
        self.cdp_port = cdp_port
        self.cdp_http_url = f"http://127.0.0.1:{cdp_port}"
        self.cdp_client: Optional[RawCDPClient] = None
        self.active_tab: Optional[Dict[str, Any]] = None

    def is_browser_ready(self) -> bool:
        """检测宿主浏览器的调试端口是否处于存活监听状态"""
        try:
            req = urllib.request.Request(f"{self.cdp_http_url}/json/version", headers={"User-Agent": "OPC-Auto-Agent"})
            with urllib.request.urlopen(req, timeout=1.5) as resp:
                return resp.status == 200
        except Exception:
            return False

    def list_tabs(self) -> List[Dict[str, Any]]:
        """获取宿主浏览器中当前已打开的所有页面/标签页"""
        try:
            req = urllib.request.Request(f"{self.cdp_http_url}/json/list", headers={"User-Agent": "OPC-Auto-Agent"})
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                return json.loads(resp.read().decode('utf-8'))
        except Exception:
            return []

    def attach_to_page(self, url_keyword: str = "", trace_logger: Optional[AgentTraceLogger] = None) -> bool:
        """
        挂接至匹配指定 URL 关键词的标签页，若不存在则挂接首个活跃页。
        """
        tabs = self.list_tabs()
        if not tabs:
            if trace_logger:
                trace_logger.record_failure_context(
                    failed_step="attach_to_page",
                    error_msg="未检测到任何可用的 CDP 标签页",
                    suspected_cause="宿主浏览器未开启 9222 调试端口或窗口未打开"
                )
            return False

        target_tab = None
        for t in tabs:
            if t.get("type") == "page":
                if url_keyword and url_keyword.lower() in t.get("url", "").lower():
                    target_tab = t
                    break
                if not target_tab:
                    target_tab = t

        if not target_tab or "webSocketDebuggerUrl" not in target_tab:
            return False

        self.active_tab = target_tab
        ws_url = target_tab["webSocketDebuggerUrl"]
        self.cdp_client = RawCDPClient(ws_url)

        if trace_logger:
            trace_logger.record_browser_info(
                cdp_endpoint=self.cdp_http_url,
                user_agent=self.cdp_client.evaluate("navigator.userAgent") or "unknown",
                viewport={
                    "width": self.cdp_client.evaluate("window.innerWidth") or 0,
                    "height": self.cdp_client.evaluate("window.innerHeight") or 0
                }
            )
            trace_logger.log_step(
                step_name="attach_browser_page",
                action="ATTACH_CDP",
                details={"tab_title": target_tab.get("title"), "url": target_tab.get("url")}
            )
        return True

    def humanoid_scroll_and_wait(self, min_wait: float = 1.0, max_wait: float = 2.0, trace_logger: Optional[AgentTraceLogger] = None):
        """
        模拟人类自然浏览动作：平滑小幅度滚动并引入高斯随机抖动，触发异步列表渲染。
        """
        if not self.cdp_client:
            return
        
        t0 = time.time()
        # 模拟真人分段滚动视口
        scroll_script = """
        (() => {
            const currentY = window.scrollY;
            const targetY = currentY + (350 + Math.floor(Math.random() * 250));
            window.scrollTo({top: targetY, behavior: 'smooth'});
            return targetY;
        })()
        """
        self.cdp_client.evaluate(scroll_script)
        
        # 高斯随机延迟，模拟真人停顿阅读时间
        delay = random.uniform(min_wait, max_wait)
        time.sleep(delay)
        
        duration_ms = int((time.time() - t0) * 1000)
        if trace_logger:
            trace_logger.log_step(
                step_name="humanoid_scroll",
                action="SCROLL_AND_WAIT",
                details={"delay_applied": round(delay, 2)},
                duration_ms=duration_ms
            )

    def harvest_orders(self, platform: str = "taobao", trace_logger: Optional[AgentTraceLogger] = None) -> List[Dict[str, Any]]:
        """
        执行订单与发票状态拟人化抓取。
        动态读取 RuleEvolutionManager 当前最新演化规则，全流程向 trace_logger 上报选择器健康度。
        """
        if not self.cdp_client:
            raise RuntimeError("CDP 客户端尚未连接！请先调用 attach_to_page()")

        # 动态获取自愈演进后的最新规则 (实现程序自迭代)
        try:
            from .self_healing_engine import RuleEvolutionManager
            rule_mgr = RuleEvolutionManager()
            plat_rule = rule_mgr.get_platform_rule(platform)
        except Exception:
            plat_rule = {}

        card_selectors = plat_rule.get("order_card_selectors", ["div[data-id]", ".js-order-item", ".bought-table-mod__tbody"])
        invoice_selectors = plat_rule.get("invoice_btn_selectors", ["button.invoice-action", "a[href*='invoice']", ".invoice-btn"])
        card_sel_str = ", ".join(card_selectors)
        inv_sel_str = ", ".join(invoice_selectors)

        t_start = time.time()
        # 动态将最新演进规则注入至前端 JS 执行
        extract_js = f"""
        (() => {{
            const results = [];
            // 使用当前演进生效的卡片选择器
            const cardSelectorStr = "{card_sel_str}";
            const orderCards = document.querySelectorAll(cardSelectorStr);
            const cardCount = orderCards.length;

            orderCards.forEach((card, idx) => {{
                const textContent = card.innerText || "";
                
                // 订单号提取 (自适应正则匹配)
                const orderIdMatch = textContent.match(/订单号[:：]?\\s*([A-Za-z0-9_-]+)/);
                const orderId = orderIdMatch ? orderIdMatch[1] : (card.getAttribute("data-id") || "");

                // 实付金额提取
                const amountMatch = textContent.match(/￥?\\s*([0-9]+\\.[0-9]{{2}})/);
                const payAmount = amountMatch ? parseFloat(amountMatch[1]) : 0.0;

                // 店铺/商户提取
                let storeName = "";
                const storeEl = card.querySelector(".seller-mod__name, a[title*='店铺'], .shop-name");
                if (storeEl) {{
                    storeName = storeEl.innerText.trim();
                }}

                // 发票状态探测 (使用动态演化选择器)
                let invoiceStatus = "电商未申请";
                let invoiceActionText = "";
                const invSelectorStr = "{inv_sel_str}";
                const invoiceEl = card.querySelector(invSelectorStr);
                
                if (textContent.includes("查看发票") || textContent.includes("发票详情")) {{
                    invoiceStatus = "已开票";
                    invoiceActionText = "查看发票";
                }} else if (textContent.includes("申请开票") || textContent.includes("补开发票")) {{
                    invoiceStatus = "电商待申请发票";
                    invoiceActionText = "申请开票";
                }} else if (invoiceEl) {{
                    invoiceStatus = "已申请/待开具";
                    invoiceActionText = invoiceEl.innerText.trim();
                }}

                if (orderId) {{
                    results.push({{
                        order_id: orderId,
                        pay_amount: payAmount,
                        store_name: storeName,
                        invoice_status: invoiceStatus,
                        invoice_action_text: invoiceActionText,
                        raw_snippet: textContent.substring(0, 80)
                    }});
                }}
            }});

            return {{
                card_count: cardCount,
                results: results,
                sample_html: orderCards.length > 0 ? orderCards[0].outerHTML.substring(0, 1500) : (document.body ? document.body.innerHTML.substring(0, 2000) : "")
        }})()
        """
        
        extracted = self.cdp_client.evaluate(extract_js) or {}
        card_count = extracted.get("card_count", 0)
        items = extracted.get("results", [])
        sample_html = extracted.get("sample_html", "")

        # 向 Trace Logger 上报指标
        if trace_logger:
            trace_logger.record_selector_metric(
                field_name="order_card_container",
                selector="div[data-id], .js-order-item, .bought-table-mod__tbody",
                hit_count=card_count,
                sample_text=items[0].get("raw_snippet", "") if items else ""
            )
            trace_logger.record_selector_metric(
                field_name="order_id",
                selector="regex:/订单号[:：]?\\s*([A-Za-z0-9_-]+)/",
                hit_count=len(items)
            )

            if card_count == 0:
                trace_logger.record_failure_context(
                    failed_step="harvest_orders",
                    error_msg="页面中未定位到任何订单容器卡片",
                    dom_html_snippet=sample_html,
                    suspected_cause="页面未完全加载、处于非订单页面，或电商前端类名已深度重构"
                )
            else:
                trace_logger.log_step(
                    step_name="harvest_orders",
                    action="EXTRACT_DOM",
                    details={"cards_detected": card_count, "items_extracted": len(items)},
                    duration_ms=int((time.time() - t_start) * 1000)
                )

        return items

    def close(self):
        """释放 CDP 客户端连接"""
        if self.cdp_client:
            self.cdp_client.close()
            self.cdp_client = None


# ==============================================================================
# 四、 辅助快捷入口与环境自检工具
# ==============================================================================

def get_browser_launch_command(port: int = 9222) -> str:
    """生成带 CDP 调试端口拉起本机真实 Chrome 的推荐命令"""
    default_chrome_path = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    default_edge_path = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
    
    path = default_chrome_path if os.path.exists(default_chrome_path) else default_edge_path
    cmd = f'"{path}" --remote-debugging-port={port} --restore-last-session'
    return cmd
