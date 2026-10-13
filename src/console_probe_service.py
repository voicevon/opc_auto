"""
OPC Auto 控制台自检探针服务 (Console Probe Service)
核心职责：
ConsoleProbeService: 对 Web 控制台 (web/index.html + api_handler.py) 执行双通道健康探针，
产出与电商采集同构的 platform="console" Trace 档案，纳入既有自愈分流管道 (SelfHealingEngine)。

- 通道 A (HTTP 静态探针，必执行)：首页 HTML 关键容器断言 + 核心 API 存活检查，零浏览器依赖；
- 通道 B (CDP 渲染探针，机会执行)：复用宿主 9222 Chrome 真实渲染控制台，
  校验动态 DOM 与台账渲染，宿主离线时自动跳过 (通道 A 已覆盖静态检查)。

探针监控目标 (容器选择器 / API 清单 / 站点_origin) 单源消费规则库 console 平台基线，
与电商采集选择器同库演化，杜绝双轨硬编码。
"""

import re
import json
import time
import urllib.request
from typing import Dict, Any, List, Optional, Tuple

from .browser_harvest_service import AgentTraceLogger, HumanoidBrowserHarvester, RawCDPClient
from .self_healing_engine import RuleEvolutionManager


def _slugify(raw: str) -> str:
    """将选择器/API 路径转译为稳定的 selector_health 字段名后缀"""
    return re.sub(r"[^a-z0-9]+", "_", raw.lower()).strip("_")


class ConsoleProbeService:
    """Web 控制台双通道健康探针 (HTTP 静态 + CDP 真实渲染)"""

    def __init__(self):
        self.rule = RuleEvolutionManager().get_platform_rule("console") or {}
        self.origin: str = self.rule.get("web_origin", "http://localhost:8088")
        self.container_selectors: List[str] = self.rule.get("index_container_selectors", [])
        self.api_paths: List[str] = self.rule.get("core_api_paths", [])
        self.tab_url_keyword: str = self.rule.get("cdp_tab_url_keyword", "localhost:8088")
        self.harvester = HumanoidBrowserHarvester()
        self._static_hit_total = 0
        self._api_hit_total = 0

    # ------------------------------------------------------------------
    # 对外唯一入口
    # ------------------------------------------------------------------
    def run_full_probe(self) -> Dict[str, Any]:
        """执行完整双通道探针并落盘 console Trace，返回面向控制台的摘要"""
        trace = AgentTraceLogger(platform="console")
        issues: List[str] = []
        t0 = time.time()

        page_ok, page_title = self._probe_http_index(trace, issues)
        api_ok = self._probe_core_apis(trace, issues)
        render_hit, render_total = self._probe_cdp_render(trace, issues, skip_because_down=not page_ok)

        all_ok = page_ok and api_ok and (render_hit is None or render_hit == render_total)
        if all_ok:
            trace.payload["status"] = "SUCCESS"
        elif trace.payload.get("failure_context") is None:
            trace.record_failure_context("run_full_probe", "；".join(issues) or "探针存在异常项")
        trace.finish([], success=all_ok)

        parts = [f"静态DOM {self._static_hit_total}/{len(self.container_selectors)}",
                 f"核心API {self._api_hit_total}/{len(self.api_paths)}"]
        if render_hit is not None:
            parts.append(f"渲染DOM {render_hit}/{render_total}")
        else:
            parts.append("渲染探针跳过(宿主离线)")
        icon = "🟢" if all_ok else "🔴"
        message = f"{icon} 控制台探针完成：{'，'.join(parts)}"
        if issues:
            message += f"，发现 {len(issues)} 项异常"

        return {
            "trace_id": trace.trace_id,
            "ok": all_ok,
            "message": message,
            "issues": issues,
            "duration_sec": round(time.time() - t0, 2)
        }

    # ------------------------------------------------------------------
    # 通道 A：HTTP 静态探针
    # ------------------------------------------------------------------
    def _probe_http_index(self, trace: AgentTraceLogger, issues: List[str]) -> Tuple[bool, str]:
        """断言首页可达、标题正确且关键容器存在于静态 HTML"""
        t0 = time.time()
        try:
            req = urllib.request.Request(self.origin + "/", headers={"User-Agent": "OPC-Auto-Probe"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                html = resp.read().decode("utf-8", errors="replace")
        except Exception as conn_err:
            trace.log_step("probe_http_index", "HTTP_GET_FAILED",
                           {"origin": self.origin, "error": str(conn_err)},
                           duration_ms=int((time.time() - t0) * 1000))
            trace.record_failure_context("probe_http_index", f"控制台首页不可达: {conn_err}",
                                         suspected_cause="Web 服务 (api_handler.py) 未启动或端口变更")
            for sel in self.container_selectors:
                trace.record_selector_metric(f"dom_static_{_slugify(sel)}", sel, 0)
            self._static_hit_total = 0
            issues.append(f"控制台首页不可达: {conn_err}")
            return False, ""

        title_match = re.search(r"<title>(.*?)</title>", html, re.IGNORECASE)
        page_title = title_match.group(1).strip() if title_match else ""
        title_ok = "OPC Auto" in page_title

        hit_total = 0
        for sel in self.container_selectors:
            hit = self._count_static_hits(html, sel)
            hit_total += 1 if hit > 0 else 0
            trace.record_selector_metric(f"dom_static_{_slugify(sel)}", sel, hit,
                                         sample_text=page_title)
            if hit == 0:
                issues.append(f"首页静态 HTML 缺失关键容器: `{sel}`")
        self._static_hit_total = hit_total

        if not title_ok:
            issues.append(f"首页标题异常，缺少 OPC Auto 标识: `{page_title}`")

        trace.log_step("probe_http_index", "STATIC_DOM_ASSERT",
                       {"origin": self.origin, "page_title": page_title,
                        "containers_hit": f"{hit_total}/{len(self.container_selectors)}"},
                       duration_ms=int((time.time() - t0) * 1000))
        return title_ok and hit_total == len(self.container_selectors), page_title

    @staticmethod
    def _count_static_hits(html: str, selector: str) -> int:
        """在静态 HTML 文本中统计选择器命中数 (#id / .class 两类)"""
        if selector.startswith("#"):
            return len(re.findall(rf'id="[^"]*\b{re.escape(selector[1:])}\b[^"]*"', html))
        if selector.startswith("."):
            return len(re.findall(rf'class="[^"]*\b{re.escape(selector[1:])}\b[^"]*"', html))
        return html.count(selector)

    def _probe_core_apis(self, trace: AgentTraceLogger, issues: List[str]) -> bool:
        """逐个探活核心 API，要求 200 且响应可解析为 JSON"""
        all_ok = True
        for path in self.api_paths:
            t0 = time.time()
            ok, sample = self._http_get_json(self.origin + path)
            trace.record_selector_metric(f"api_{_slugify(path)}", f"GET {path}", 1 if ok else 0,
                                         sample_text=sample)
            if not ok:
                all_ok = False
                issues.append(f"核心 API 异常: GET {path}")
            trace.log_step("probe_core_api", "API_PING",
                           {"path": path, "ok": ok},
                           duration_ms=int((time.time() - t0) * 1000))
        self._api_hit_total = sum(1 for p in self.api_paths
                                  if (trace.payload["selector_health"].get(f"api_{_slugify(p)}", {}) or {}).get("hit_count") == 1)
        return all_ok

    def _http_get_json(self, url: str) -> Tuple[bool, str]:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "OPC-Auto-Probe"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                json.loads(body)
                return True, body[:120]
        except Exception as err:
            return False, str(err)[:120]

    # ------------------------------------------------------------------
    # 通道 B：CDP 真实渲染探针 (机会执行)
    # ------------------------------------------------------------------
    def _probe_cdp_render(self, trace: AgentTraceLogger, issues: List[str],
                          skip_because_down: bool = False) -> Tuple[Optional[int], Optional[int]]:
        """
        复用宿主 9222 Chrome 真实渲染控制台并校验动态 DOM。
        返回 (containers_hit, containers_total)；跳过 (宿主离线或服务已确认宕机) 返回 (None, None)。
        """
        t0 = time.time()
        if skip_because_down:
            trace.log_step("probe_cdp_render", "SKIPPED",
                           {"reason": "首页 HTTP 探针已失败 (服务不可达)，渲染探针跳过以避免冗余噪音"})
            return None, None
        if not self.harvester.is_browser_ready():
            trace.log_step("probe_cdp_render", "SKIPPED",
                           {"reason": "宿主浏览器 9222 未开放，渲染探针跳过 (通道 A 已覆盖静态检查)"})
            return None, None

        opened_tab_id: Optional[str] = None
        client: Optional[RawCDPClient] = None
        try:
            tab = next((t for t in self.harvester.list_tabs()
                        if t.get("type") == "page" and self.tab_url_keyword in t.get("url", "")), None)
            if not tab:
                req = urllib.request.Request(
                    f"{self.harvester.cdp_http_url}/json/new?{self.origin}",
                    method="PUT", headers={"User-Agent": "OPC-Auto-Probe"})
                with urllib.request.urlopen(req, timeout=6) as resp:
                    tab = json.loads(resp.read().decode("utf-8"))
                opened_tab_id = tab.get("id")
                time.sleep(2.0)  # 等待首次渲染与 app.js 数据加载

            ws_url = (tab or {}).get("webSocketDebuggerUrl")
            if not ws_url:
                trace.record_failure_context("probe_cdp_render",
                                             "控制台标签页缺少 webSocketDebuggerUrl，无法执行渲染探针")
                issues.append("CDP 渲染探针：控制台标签页不可挂接")
                return 0, len(self.container_selectors)

            client = RawCDPClient(ws_url)
            result = self._evaluate_render_check(client)
            client.close()
            client = None

            hit_total = 0
            for sel, hit in (result.get("found") or {}).items():
                trace.record_selector_metric(f"dom_render_{_slugify(sel)}", sel, hit,
                                             sample_text=str(result.get("title", "")))
                hit_total += 1 if hit > 0 else 0
                if hit == 0:
                    issues.append(f"渲染后 DOM 缺失关键容器: `{sel}`")

            trace.log_step("probe_cdp_render", "RENDER_DOM_CHECK",
                           {"tab_title": result.get("title"),
                            "ledger_rows": result.get("ledger_rows"),
                            "containers_hit": f"{hit_total}/{len(self.container_selectors)}"},
                           duration_ms=int((time.time() - t0) * 1000))
            return hit_total, len(self.container_selectors)
        except Exception as render_err:
            trace.record_failure_context("probe_cdp_render", f"CDP 渲染探针异常: {render_err}",
                                         suspected_cause="宿主 Chrome 标签页被关闭或 CDP 连接中断")
            issues.append(f"CDP 渲染探针异常: {render_err}")
            return 0, len(self.container_selectors)
        finally:
            if client:
                client.close()
            if opened_tab_id:
                try:
                    urllib.request.urlopen(
                        f"{self.harvester.cdp_http_url}/json/close/{opened_tab_id}", timeout=4).read()
                except Exception:
                    pass

    def _evaluate_render_check(self, client: RawCDPClient) -> Dict[str, Any]:
        sels_json = json.dumps(self.container_selectors, ensure_ascii=False)
        js = f"""
        (() => {{
            const sels = {sels_json};
            const found = {{}};
            for (const s of sels) {{ found[s] = document.querySelectorAll(s).length; }}
            return JSON.stringify({{
                title: document.title,
                found: found,
                ledger_rows: document.querySelectorAll('#invoice-table-body tr').length
            }});
        }})()
        """
        raw = client.evaluate(js)
        return json.loads(raw) if raw else {}
