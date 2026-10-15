"""
OPC Auto 电子税务局进项发票自动同步服务 (Etax Sync Service)
核心职责：
1. 每周一/周三 (cron 门控，手动触发随时可跑) 接管宿主 Chrome 9222，开新 tab 访问税局；
2. 校验登录态 —— 以 localStorage 身份信息 (navUserinfo/userInfo) 与 tpass 会话令牌为权威信号；
   短信验证码无法自动化，会话失效时产出《决策简报》并立即中止，严禁重试循环 (人机分流准则路径二)；
3. 同 tab 导航至数电平台【全量发票查询】页 (dppt 域)，按同步水位线 [last_sync_date+1, today] 分页拉取进项列表；
4. 清洗入库 tax_inbound_invoices (raw_source='etax_sync'，票号幂等)，逐票下载 PDF/OFD 至 data/tax_invoices/；
5. 调用 finalize_reconciliation_and_report() 重算对账并刷新当日日报；
6. 全程产出 platform="tax" Trace 档案，纳入自愈引擎每日复盘与选择器自愈演化。
"""

import os
import re
import json
import time
import urllib.request
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional

from .database import get_db_connection, DATA_DIR
from .browser_harvest_service import HumanoidBrowserHarvester, AgentTraceLogger, RawCDPClient
from .invoice_import_service import _to_float, _normalize_state
from .audit_service import finalize_reconciliation_and_report
from .diagnostic_router import DiagnosticRouter

# 下载文件统一存放目录 (git 忽略，不入库)
TAX_DOWNLOAD_ROOT = os.path.join(DATA_DIR, "tax_invoices")
# 每周自动同步门控日 (周一=0, 周三=2)；仅对 cron 触发生效门控，手动触发不受限
WEEKLY_SYNC_WEEKDAYS = (0, 2)
# 水位线平台标识 (sync_watermarks 表)
WATERMARK_PLATFORM = "etax"
# 首次同步 (无水位线) 的默认回溯天数 (查询页默认区间约近 16 天，历史更早发票走手动导入)
DEFAULT_LOOKBACK_DAYS = 15
# 分页拉取的最大页数保险丝
MAX_PAGES = 30


def _tax_rule() -> Dict[str, Any]:
    """读取规则库 tax 平台段 (单一真理源；规则文件缺段时由 DEFAULT_RULES 兜底)"""
    try:
        from .self_healing_engine import RuleEvolutionManager
        return RuleEvolutionManager().get_platform_rule("tax")
    except Exception:
        return {}


class EtaxSyncService:
    """电子税务局进项数电票自动同步器 (开 tab 探完即关，不干扰电商采集活跃页)"""

    def __init__(self):
        self.harvester = HumanoidBrowserHarvester()

    # ------------------------------------------------------------------
    # 同步水位线 (单一真理源：sync_watermarks 表)
    # ------------------------------------------------------------------
    def _get_watermark(self) -> Optional[str]:
        conn = get_db_connection()
        row = conn.execute(
            "SELECT last_sync_date FROM sync_watermarks WHERE platform = ?", (WATERMARK_PLATFORM,)
        ).fetchone()
        conn.close()
        return row["last_sync_date"] if row else None

    def _set_watermark(self, date_str: str):
        conn = get_db_connection()
        conn.execute("""
        INSERT INTO sync_watermarks (platform, last_sync_date, updated_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(platform) DO UPDATE SET
            last_sync_date=excluded.last_sync_date,
            updated_at=CURRENT_TIMESTAMP
        """, (WATERMARK_PLATFORM, date_str))
        conn.commit()
        conn.close()

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------
    def run_sync(self, trigger_type: str = "manual") -> Dict[str, Any]:
        """执行一次税局进项发票同步 (cron 触发受周一/周三门控，manual 随时可跑)"""
        if trigger_type == "cron" and datetime.now().weekday() not in WEEKLY_SYNC_WEEKDAYS:
            return {"status": "skipped", "message": "今日非周一/周三同步窗口，跳过税局同步"}

        rule = _tax_rule()
        trace = AgentTraceLogger(platform="tax")
        summary: Dict[str, Any] = {
            "status": "failed",
            "trace_id": trace.trace_id,
            "trace_file": trace.trace_file,
            "requires_human_login": False,
            "message": ""
        }

        if not self.harvester.is_browser_ready():
            trace.record_failure_context(
                failed_step="check_cdp_port",
                error_msg="宿主浏览器 9222 调试端口未开放 (127.0.0.1:9222 无法连接)",
                suspected_cause="Chrome 未携带 --remote-debugging-port=9222 启动。请双击运行 start_chrome_debug.bat 启动浏览器"
            )
            trace.finish(extracted_items=[], success=False)
            summary["message"] = "Chrome 9222 调试端口未开放，税局同步中止 (环境未就绪)"
            return summary

        client: Optional[RawCDPClient] = None
        try:
            # 1. 开 tab 至税局门户 (etax 域，登录态身份信息存储于此 origin)
            portal_origin = rule.get("web_origin", "https://etax.shandong.chinatax.gov.cn")
            if not self.harvester.open_new_tab(portal_origin, trace_logger=trace):
                trace.finish(extracted_items=[], success=False)
                summary["message"] = "新建税局标签页失败，税局同步中止"
                return summary
            client = self.harvester.cdp_client

            # 2. 登录态探测 (短信验证码无法自动化，失效即人工在环)
            login_state = self._check_login(client)
            trace.log_step("check_login_state", "LOGIN_PROBE", login_state)
            if not login_state.get("logged_in"):
                trace.record_failure_context(
                    failed_step="check_login_state",
                    error_msg="电子税务局登录态失效或未登录 (身份存储缺失且命中登录屏障)",
                    suspected_cause="请先登录：税局会话超出有效生存期，需账号密码+短信验证码人工重新登录 (Cookie 持久化后二次免登录)",
                    failed_url=portal_origin
                )
                trace.finish(extracted_items=[], success=False)
                # 复用统一分流路由：命中登录失效关键词 → 路径二决策简报
                brief = DiagnosticRouter().evaluate(trace.payload).get("decision_brief")
                summary["requires_human_login"] = True
                summary["decision_brief_id"] = (brief or {}).get("id")
                summary["message"] = "税局登录态失效，已产出《决策简报》，请在宿主 Chrome 中人工完成登录后重试"
                return summary

            # 3. 菜单点击流导航至数电平台【全量发票查询】页 (dppt 域)
            # 直接深链会被 OAuth 弹回登录页 —— 复现真人路径: 门户 → 税务数字账户 → 发票查询统计 → 全量发票查询
            # (优先复用浏览器中已存在的 dppt 业务 tab；被弹回 tpass 登录页即判定 SSO 失效)
            reached, login_bounced = self._navigate_to_query_page(trace)
            client = self.harvester.cdp_client  # reattach 可能已更换底层连接
            if login_bounced:
                trace.record_failure_context(
                    failed_step="navigate_query_page",
                    error_msg="数字账户 SSO 会话失效，从门户进入业务页被弹回多合一登录页",
                    suspected_cause="请先登录：tpass 单点登录会话超出有效生存期，需账号密码+短信验证码人工重新登录",
                    failed_url=(client.evaluate("location.href") or "")[:300]
                )
                trace.finish(extracted_items=[], success=False)
                brief = DiagnosticRouter().evaluate(trace.payload).get("decision_brief")
                summary["requires_human_login"] = True
                summary["decision_brief_id"] = (brief or {}).get("id")
                summary["message"] = "税局 SSO 会话失效，已产出《决策简报》，请在宿主 Chrome 人工登录后重试同步"
                return summary
            if not reached:
                trace.record_failure_context(
                    failed_step="navigate_query_page",
                    error_msg="未能通过门户菜单导航至全量发票查询页 (点击链路中断或菜单文案改版)",
                    suspected_cause="税务数字账户入口/菜单层级/菜单文案发生变化，或数字账户会话未建立",
                    failed_url=(client.evaluate("location.href") or "")[:300]
                )
                trace.finish(extracted_items=[], success=False)
                summary["message"] = "导航全量发票查询页失败，已留存现场 Trace 供自愈分析"
                return summary

            # 4. 计算同步区间 [水位线+1, today] 并设定下载目录
            today_str = datetime.now().strftime("%Y-%m-%d")
            watermark = self._get_watermark()
            if watermark:
                start_date = (datetime.strptime(watermark, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
            else:
                start_date = (datetime.now() - timedelta(days=DEFAULT_LOOKBACK_DAYS)).strftime("%Y-%m-%d")

            month_dir = os.path.join(TAX_DOWNLOAD_ROOT, datetime.now().strftime("%Y%m"))
            client.call("Browser.setDownloadBehavior", {
                "behavior": "allow", "downloadPath": month_dir, "eventsEnabled": False
            })
            trace.log_step("setup_download_dir", "SET_DOWNLOAD_BEHAVIOR",
                           {"dir": month_dir, "range": f"{start_date} ~ {today_str}"})

            # 5. 分页拉取进项列表 + 本页 DOM 存活时逐票下载 (按水位线下界过滤)
            invoices, downloaded_files = self._fetch_invoice_rows(client, start_date, month_dir, trace)
            trace.log_step("parse_invoice_rows", "HARVEST_DONE",
                           {"in_range": len(invoices), "range_from": start_date,
                            "downloaded": len(downloaded_files)})

            # 6. 清洗入库 (票号幂等, raw_source='etax_sync')
            imported = self._import_invoices(invoices)
            trace.log_step("import_to_db", "DB_UPSERT", {"imported": imported, "total": len(invoices)})

            # 6.5 推进水位线 —— 仅在真实抓到发票时推进；0 行可能是页面/选择器异常，保留旧水位线以便下轮重查
            if invoices:
                self._set_watermark(today_str)

            # 7. 重算对账并刷新当日日报 (与采集链共用唯一出口)
            finalize_result = self._finalize_report(trigger_type, start_date, today_str,
                                                    len(invoices), imported, len(downloaded_files))
            trace.finish(extracted_items=invoices, success=True)

            summary.update({
                "status": "success",
                "range": f"{start_date} ~ {today_str}",
                "found": len(invoices),
                "imported": imported,
                "downloaded_files": len(downloaded_files),
                "stats": finalize_result["stats"],
                "message": f"税局同步完成：区间 {start_date} ~ {today_str}，"
                           f"抓取 {len(invoices)} 张，新增入库 {imported} 张，下载文件 {len(downloaded_files)} 个"
            })
            return summary
        except Exception as sync_err:
            trace.record_failure_context(
                failed_step="sync_execution",
                error_msg=str(sync_err),
                suspected_cause="税局同步执行期间异常 (页面改版/接口变动/网络中断)"
            )
            trace.finish(extracted_items=[], success=False)
            summary["message"] = f"税局同步异常: {sync_err}"
            return summary
        finally:
            if client:
                client.close()
                self.harvester.cdp_client = None
            # 成功收尾: 仅关闭本 run 打开的门户 tab，保留 dppt 查询 tab 维持会话供下轮复用；
            # 失败收尾: 查询 tab 状态未知，一并关闭。
            self.harvester.close_opened_tab()  # 本 run 打开的门户 tab (opened_tab_id)
            if summary.get("status") != "success":
                query_tab_id = (self.harvester.active_tab or {}).get("id")
                if query_tab_id:
                    self.harvester.close_opened_tab(query_tab_id)
            self.harvester.close()

    # ------------------------------------------------------------------
    # 菜单点击流导航 (门户 → 税务数字账户 → 发票查询统计 → 全量发票查询)
    # ------------------------------------------------------------------
    def _current_url(self, client: RawCDPClient) -> str:
        try:
            return client.evaluate("location.href") or ""
        except Exception:
            return ""

    def _maybe_reattach(self, client: RawCDPClient, trace: AgentTraceLogger):
        """业务页若在新 tab 打开 (dppt/szzhzz 域)，切换 CDP 挂接至该 tab (遗留 tab 绝不自动关闭)"""
        cur = self._current_url(client)
        if "dppt" in cur or "szzhzz" in cur:
            return
        old_tab_id = (self.harvester.active_tab or {}).get("id")
        for kw in ("dppt", "szzhzz"):
            for t in self.harvester.list_tabs():
                if (t.get("type") == "page" and kw in t.get("url", "")
                        and t.get("webSocketDebuggerUrl") and t.get("id") != old_tab_id):
                    if self.harvester.reattach_to_tab(kw):
                        trace.log_step("reattach_tab", "SWITCH_TAB",
                                       {"url": t.get("url", "")[:200]})
                        return

    def _click_text(self, client: RawCDPClient, text: str, trace: AgentTraceLogger,
                    step_name: str) -> bool:
        """
        按可见文本锚点点击。优先 CDP Input.dispatchMouseEvent 派发真实受信鼠标事件
        (合成 el.click() isTrusted=false 会被 window.open 弹窗拦截静默丢弃)；
        失败时降级合成点击与同 href 链接兜底跳转。
        """
        pos_raw = client.evaluate(f"""
        (() => {{
            const target = {json.dumps(text)};
            const els = Array.from(document.querySelectorAll("a, button, span, div, li, p"))
                .filter(el => el.offsetParent !== null);
            const label = (el) => (el.innerText || "").trim();
            let hit = els.find(el => label(el) === target);
            if (!hit) hit = els.find(el => label(el).includes(target));
            if (!hit) return null;
            const leaves = Array.from(hit.querySelectorAll("*"))
                .filter(el => label(el) === label(hit) && label(el));
            if (leaves.length > 0) hit = leaves[leaves.length - 1];
            hit.scrollIntoView({{block: "center"}});
            const r = hit.getBoundingClientRect();
            if (r.width <= 0 || r.height <= 0) return null;
            return JSON.stringify({{x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2)}});
        }})()
        """)
        if pos_raw:
            try:
                pos = json.loads(pos_raw)
                client.call("Input.dispatchMouseEvent", {
                    "type": "mousePressed", "x": pos["x"], "y": pos["y"],
                    "button": "left", "clickCount": 1
                })
                client.call("Input.dispatchMouseEvent", {
                    "type": "mouseReleased", "x": pos["x"], "y": pos["y"],
                    "button": "left", "clickCount": 1
                })
                trace.log_step(step_name, "REAL_CLICK", {"text": text, "x": pos["x"], "y": pos["y"]})
                return True
            except Exception as click_err:
                trace.log_step(step_name, "REAL_CLICK_FAILED", {"text": text, "error": str(click_err)[:120]})

        # 降级一：合成点击 (最内层叶子节点)
        clicked_label = client.evaluate(f"""
        (() => {{
            const target = {json.dumps(text)};
            const els = Array.from(document.querySelectorAll("a, button, span, div, li, p"))
                .filter(el => el.offsetParent !== null);
            const label = (el) => (el.innerText || "").trim();
            let hit = els.find(el => label(el) === target);
            if (!hit) hit = els.find(el => label(el).includes(target));
            if (hit) {{
                const leaves = Array.from(hit.querySelectorAll("*"))
                    .filter(el => el.offsetParent !== null && label(el) === label(hit));
                if (leaves.length > 0) hit = leaves[leaves.length - 1];
                hit.click();
                return label(hit).substring(0, 30);
            }}
            return null;
        }})()
        """)
        if clicked_label:
            trace.log_step(step_name, "SYNTH_CLICK", {"text": text, "clicked_label": clicked_label})
            return True

        # 降级二：命中同文本的 <a href> 直接在当前 tab 跳转
        navigated = client.evaluate(f"""
        (() => {{
            const target = {json.dumps(text)};
            const a = Array.from(document.querySelectorAll("a"))
                .filter(el => el.offsetParent !== null)
                .find(el => (el.innerText || "").trim().includes(target) && el.href);
            if (a) {{ location.assign(a.href); return a.href.substring(0, 150); }}
            return null;
        }})()
        """)
        if navigated:
            trace.log_step(step_name, "NAVIGATE_BY_HREF", {"text": text, "href": navigated})
            return True

        trace.record_selector_metric(f"menu_text_{step_name}", f"text='{text}'", 0)
        return False

    def _close_invoice_dialog(self, client: RawCDPClient):
        """关闭发票简要信息弹窗 (验证无残留，重试 + Escape 兜底)"""
        for _ in range(2):
            visible = client.evaluate("""
            (() => {
                const d = document.querySelector(".t-dialog__wrap, .t-dialog__position");
                return (d && d.getBoundingClientRect().width > 0) ? "1" : null;
            })()
            """)
            if not visible:
                return
            client.evaluate("""
            (() => {
                const closeBtn = document.querySelector(".t-dialog__close");
                if (closeBtn && closeBtn.getBoundingClientRect().width > 0) { closeBtn.click(); return; }
                const btn = Array.from(document.querySelectorAll("button, span, div"))
                    .filter(el => el.getBoundingClientRect().width > 0)
                    .find(el => ["收起", "关闭", "取消", "取 消"].includes((el.innerText || "").trim()));
                if (btn) btn.click();
            })()
            """)
            time.sleep(0.8)
        client.call("Input.dispatchKeyEvent",
                    {"type": "keyDown", "key": "Escape", "code": "Escape", "windowsVirtualKeyCode": 27})
        client.call("Input.dispatchKeyEvent",
                    {"type": "keyUp", "key": "Escape", "code": "Escape", "windowsVirtualKeyCode": 27})

    def _switch_invoice_source(self, client: RawCDPClient, target_text: str,
                               trace: AgentTraceLogger) -> bool:
        """
        切换"发票来源"下拉至目标项 (如 取得发票)。
        TDesign Select: 选中值存于内层 input.value (wrap.innerText 为空)，真实点击展开 popup → 点击目标 option。
        """
        locator_js = """
        (() => {
            const wrap = Array.from(document.querySelectorAll(".t-select__wrap"))
                .filter(el => el.getBoundingClientRect().width > 0)
                .find(el => ["开具发票", "取得发票", "全部"].includes(
                    ((el.querySelector("input") || {}).value || "").trim()));
            if (!wrap) return null;
            return JSON.stringify({
                val: ((wrap.querySelector("input") || {}).value || "").trim(),
                x: Math.round(wrap.getBoundingClientRect().x + wrap.getBoundingClientRect().width / 2),
                y: Math.round(wrap.getBoundingClientRect().y + wrap.getBoundingClientRect().height / 2)
            });
        })()
        """
        current_raw = client.evaluate(locator_js)
        if not current_raw:
            trace.record_selector_metric("invoice_source_select", ".t-select__wrap", 0)
            return False
        current = json.loads(current_raw)
        if current["val"] == target_text:
            return True

        # 展开下拉 (真实受信点击)
        for t in ("mousePressed", "mouseReleased"):
            client.call("Input.dispatchMouseEvent",
                        {"type": t, "x": current["x"], "y": current["y"], "button": "left", "clickCount": 1})
        time.sleep(1.0)

        # 点击目标 option (popup 挂在 body 下；TDesign option 的 innerText 可能为空，用 textContent 匹配)
        opt = client.evaluate(f"""
        (() => {{
            const target = {json.dumps(target_text)};
            const opts = Array.from(document.querySelectorAll(".t-select__dropdown .t-select-option, .t-select__list li"))
                .filter(el => el.getBoundingClientRect().width > 0);
            if (!opts.length) return null;
            let hit = opts.find(el => (el.textContent || "").trim() === target)
                || opts.find(el => (el.textContent || "").trim().includes(target));
            if (!hit) hit = opts.find(el => !el.className.includes("selected"));  // 兜底: 点击未选中项
            if (!hit) return null;
            const r = hit.getBoundingClientRect();
            return JSON.stringify({{x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2)}});
        }})()
        """)
        if not opt:
            trace.record_selector_metric("invoice_source_option", f"option='{target_text}'", 0)
            client.call("Input.dispatchKeyEvent",
                        {"type": "keyDown", "key": "Escape", "code": "Escape", "windowsVirtualKeyCode": 27})
            client.call("Input.dispatchKeyEvent",
                        {"type": "keyUp", "key": "Escape", "code": "Escape", "windowsVirtualKeyCode": 27})
            return False
        d = json.loads(opt)
        for t in ("mousePressed", "mouseReleased"):
            client.call("Input.dispatchMouseEvent",
                        {"type": t, "x": d["x"], "y": d["y"], "button": "left", "clickCount": 1})
        time.sleep(0.8)
        return True

    def _is_login_page(self, client: RawCDPClient) -> bool:
        """页面文本特征判定多合一登录页 (URL 含 redirect_uri 时关键字判断不可靠)"""
        try:
            return bool(client.evaluate(
            "/多合一|统一社会信用代码|扫码登录/.test(document.body ? document.body.innerText : \"\")"))
        except Exception:
            return False

    def _navigate_to_query_page(self, trace: AgentTraceLogger) -> tuple:
        """
        进入全量发票查询页，返回 (reached, login_bounced)。
        三级策略：
        ① 复用已存在的 invoice-query 业务 tab；② 直达 query_page_url (dppt 会话 cookie 持久时一步到位)；
        ③ 门户菜单点击流 (首次 OAuth 建会话)。被弹回多合一登录页即判定需人工短信登录。
        注意: reattach 会更换底层 CDP 客户端，方法内一律实时取 self.harvester.cdp_client。
        """
        rule = _tax_rule()
        portal_origin = rule.get("web_origin", "https://etax.shandong.chinatax.gov.cn:8443/loginb/")
        query_url = rule.get(
            "query_page_url", "https://dppt.shandong.chinatax.gov.cn:8443/invoice-query/invoice-query")
        account_text = rule.get("account_entry_text", "税务数字账户")
        path_texts = rule.get("query_menu_path_texts", ["发票业务", "发票查询统计", "全量发票查询"])

        def _form_ready(c: RawCDPClient) -> bool:
            try:
                return bool(c.evaluate("!!document.querySelector(\"input[placeholder='开票日期起']\")"))
            except Exception:
                return False

        # ① 复用已存在的全量发票查询 tab (跳过一切导航，绝不关闭该 tab)
        old_tab_id = (self.harvester.active_tab or {}).get("id")
        for t in self.harvester.list_tabs():
            if (t.get("type") == "page" and "invoice-query" in t.get("url", "")
                    and t.get("webSocketDebuggerUrl") and t.get("id") != old_tab_id):
                if self.harvester.reattach_to_tab("invoice-query"):
                    client = self.harvester.cdp_client
                    trace.log_step("reuse_existing_query_tab", "REUSE_TAB",
                                   {"url": t.get("url", "")[:200]})
                    for _ in range(3):
                        if _form_ready(client):
                            return True, False
                        time.sleep(4.0)  # SPA 未渲染完，等待后复查
                    return False, False

        client = self.harvester.cdp_client

        # ② 直达查询页 (dppt 会话 cookie 存活时一步到位)
        try:
            client.evaluate(f"location.href = {json.dumps(query_url)}")
        except Exception:
            pass
        for _ in range(4):
            time.sleep(4.0)
            client = self.harvester.cdp_client
            if self._is_login_page(client):
                trace.log_step("direct_nav_bounced", "LOGIN_BOUNCE",
                               {"url": self._current_url(client)[:250]})
                break
            if _form_ready(client):
                trace.log_step("direct_nav_reached", "DIRECT_NAV", {"url": self._current_url(client)[:250]})
                return True, False

        # ③ 门户菜单点击流 (重建数字账户会话；真实受信点击驱动 OAuth/window.open)
        client = self.harvester.cdp_client
        client.evaluate(f"location.href = {json.dumps(portal_origin)}")
        time.sleep(5.0)
        client = self.harvester.cdp_client
        self._click_text(client, account_text, trace, "click_account_entry")
        attached = False
        for _ in range(6):
            time.sleep(2.0)
            self._maybe_reattach(client, trace)
            client = self.harvester.cdp_client
            if self._is_login_page(client):
                trace.log_step("sso_bounce_detected", "LOGIN_BOUNCE",
                               {"url": self._current_url(client)[:250]})
                return False, True
            url_now = self._current_url(client)
            if "dppt" in url_now or "szzhzz" in url_now:
                attached = True
                break
        if not attached and self._is_login_page(client):
            return False, True

        # 依次点击菜单路径 (每步重试 2 次，SPA 渲染慢时留足等待)
        for text in path_texts:
            clicked = False
            for attempt in range(2):
                if self._click_text(client, text, trace, f"click_menu_{text[:8]}"):
                    clicked = True
                    break
                time.sleep(3.0)  # 等待菜单渲染
            if clicked:
                time.sleep(2.5)
                self._maybe_reattach(client, trace)
                client = self.harvester.cdp_client
                if self._is_login_page(client):
                    trace.log_step("sso_bounce_detected", "LOGIN_BOUNCE",
                                   {"url": self._current_url(client)[:250]})
                    return False, True

        # 到达校验
        url = self._current_url(client)
        if self._is_login_page(client):
            return False, True
        reached = "invoice-query" in url and _form_ready(client)
        trace.log_step("navigate_query_page", "NAVIGATE_RESULT",
                       {"url": url[:250], "reached": reached})
        return reached, False

    # ------------------------------------------------------------------
    # 登录态探测
    # ------------------------------------------------------------------
    def _check_login(self, client: RawCDPClient) -> Dict[str, Any]:
        """
        探测税局登录态：以 localStorage 身份信息 (navUserinfo/userInfo) 与 tpass 会话令牌为权威信号。
        页面文本不可靠 —— 门户页登录后仍展示"登录"入口且不含办税菜单文案。
        """
        rule = _tax_rule()
        identity_keys = rule.get("login_identity_storage_keys", ["navUserinfo", "userInfo"])
        fail_keywords = rule.get("login_fail_keywords", ["请先登录", "统一身份认证", "短信验证码", "扫码登录"])

        try:
            raw = client.evaluate(f"""
            (() => {{
                const parse = (k) => {{ try {{ return JSON.parse(localStorage.getItem(k) || "null"); }} catch (e) {{ return null; }} }};
                const identity = {{}};
                {json.dumps(identity_keys)}.forEach(k => {{
                    const v = parse(k);
                    if (v) Object.assign(identity, v);
                }});
                return JSON.stringify({{
                    identity: identity,
                    has_tpass_token: !!document.cookie.match(/tpass_[^=]+=/),
                    body_text: (document.body ? document.body.innerText : "").substring(0, 3000)
                }});
            }})()
            """)
            data = json.loads(raw) if raw else {}
        except Exception as eval_err:
            return {"logged_in": False, "error": str(eval_err)[:200]}

        identity = data.get("identity") or {}
        text = data.get("body_text", "")
        hit_fail = next((kw for kw in fail_keywords if kw in text), None)
        full_name = identity.get("full_name", "")
        company_name = identity.get("company_name", "")
        # 身份信息存在即已登录 (权威信号)；身份缺失时以 tpass 令牌作降级信号
        logged_in = bool(full_name or company_name) or (data.get("has_tpass_token") and not hit_fail)
        return {
            "logged_in": logged_in,
            "full_name": full_name,
            "company_name": company_name,
            "competent_tax_bureau": identity.get("competent_tax_bureau_name", ""),
            "hit_fail": hit_fail
        }

    # ------------------------------------------------------------------
    # 列表拉取 (表头映射 + 分页)
    # ------------------------------------------------------------------
    def _fetch_invoice_rows(self, client: RawCDPClient, start_date: str, month_dir: str,
                            trace: AgentTraceLogger) -> tuple:
        """
        切换发票来源为【取得发票】(默认"开具发票"作为销方恒为 0 条) → 查询 →
        分页采集 (每页 DOM 存活时逐票下载) → 返回 (发票列表, 下载文件列表)。
        查询区间使用页面默认区间 (约近 16 天，恒覆盖每周一/周三同步窗口)；
        水位线下界由本方法按 issue_date 过滤 (API 请求体加密无法直改)。
        """
        rule = _tax_rule()
        search_text = rule.get("search_btn_text", "查询")
        source_text = rule.get("invoice_source_text", "取得发票")
        row_sels = rule.get("list_row_selectors", ["table tbody tr"])
        next_sels = rule.get("next_page_selectors", ["button.btn-next", "[title='下一页']"])

        # 1. 切换发票来源下拉 (真实受信点击驱动 TDesign Select)
        source_switched = self._switch_invoice_source(client, source_text, trace)
        trace.log_step("switch_invoice_source", "SELECT_SOURCE",
                       {"target": source_text, "switched": source_switched})

        # 2. 点击查询
        qb = client.evaluate(f"""
        (() => {{
            const btn = Array.from(document.querySelectorAll("button"))
                .filter(el => el.offsetParent !== null)
                .find(el => (el.innerText || "").trim() === {json.dumps(search_text)});
            if (!btn) return null;
            const r = btn.getBoundingClientRect();
            return JSON.stringify({{x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2)}});
        }})()
        """)
        if qb:
            p = json.loads(qb)
            for t in ("mousePressed", "mouseReleased"):
                client.call("Input.dispatchMouseEvent",
                            {"type": t, "x": p["x"], "y": p["y"], "button": "left", "clickCount": 1})
        trace.log_step("click_search", "CLICK_SEARCH", {"clicked": bool(qb)})
        time.sleep(4.0)  # 等待查询结果渲染

        # 复用 tab 时 TDesign 会保持上次页码 —— 强制回到第 1 页
        client.evaluate("""
        (() => {
            const num1 = Array.from(document.querySelectorAll(".t-pagination__number"))
                .filter(el => el.getBoundingClientRect().width > 0)
                .find(el => (el.innerText || "").trim() === "1" && !el.className.includes("current"));
            if (num1) num1.click();
        })()
        """)
        time.sleep(2.0)

        headers = client.evaluate(self._headers_js()) or []
        all_invoices: List[Dict[str, Any]] = []
        downloaded_files: List[str] = []
        seen_numbers = set()

        for page in range(MAX_PAGES):
            page_rows = client.evaluate(self._rows_js(row_sels)) or []
            page_invoices = []
            for cells in page_rows:
                # 以数电发票号码 (或发票号码) 列去重 + 水位线下界过滤
                key = self._row_invoice_key(headers, cells)
                if not key or key in seen_numbers:
                    continue
                inv = self._normalize_invoice_rows(headers, [cells])
                if not inv or inv[0].get("issue_date", "") < start_date:
                    continue
                seen_numbers.add(key)
                page_invoices.append(inv[0])
            trace.log_step("collect_page", "PAGE_ROWS",
                           {"page": page + 1, "rows_on_page": len(page_rows),
                            "in_range": len(page_invoices)})

            # 逐票下载必须在本页 DOM 存活时进行 (翻页后行节点销毁)
            for inv in page_invoices:
                all_invoices.append(inv)
                files = self._download_invoice_file(client, inv["invoice_number"], month_dir, trace)
                downloaded_files.extend(files)

            # 翻页 (真实受信点击；先确保下载弹窗已关闭，避免遮挡)
            self._close_invoice_dialog(client)
            has_next = client.evaluate(f"""
            (() => {{
                const sels = {json.dumps(next_sels)};
                for (const sel of sels) {{
                    const btn = document.querySelector(sel);
                    if (btn && btn.getBoundingClientRect().width > 0 &&
                        !btn.disabled && !btn.className.includes("disabled")) {{
                        const r = btn.getBoundingClientRect();
                        return JSON.stringify({{x: Math.round(r.x + r.width / 2),
                            y: Math.round(r.y + r.height / 2)}});
                    }}
                }}
                return null;
            }})()
            """)
            if not has_next:
                break
            p = json.loads(has_next)
            for t in ("mousePressed", "mouseReleased"):
                client.call("Input.dispatchMouseEvent",
                            {"type": t, "x": p["x"], "y": p["y"], "button": "left", "clickCount": 1})
            time.sleep(2.5)  # 等待下一页渲染

        # 选择器健康度上报 (自愈输入；字段名带选择器索引避免互相覆盖)
        for i, sel in enumerate(row_sels):
            hit = client.evaluate(f"document.querySelectorAll({json.dumps(sel)}).length") or 0
            trace.record_selector_metric(f"invoice_list_row_{i}", sel, hit,
                                         sample_text=all_invoices[0]["invoice_number"] if all_invoices else "")
        return all_invoices, downloaded_files

    @staticmethod
    def _headers_js() -> str:
        return """
        (() => {
            // 不允许过滤空 th (勾选列占位) —— 必须与行 td 索引严格对齐
            const ths = Array.from(document.querySelectorAll(".el-table__header th, thead th"))
                .map(th => (th.innerText || "").trim().replace(/\\s+/g, ""));
            return ths;
        })()
        """

    @staticmethod
    def _rows_js(row_sels: List[str]) -> str:
        return f"""
        (() => {{
            const selStr = {json.dumps(row_sels)}.join(", ");
            const rows = [];
            document.querySelectorAll(selStr).forEach(tr => {{
                const cells = Array.from(tr.querySelectorAll("td")).map(td => (td.innerText || "").trim());
                if (cells.length > 0) rows.push(cells);
            }});
            return rows;
        }})()
        """

    # 表头 → 规范字段映射 (单一真理源，按数电平台真实列名)
    HEADER_FIELD_MAP = {
        "数电发票号码": "invoice_number",
        "发票号码": "invoice_number_fallback",
        "开票日期": "issue_date",
        "购/销方名称": "seller_name",
        "购销方名称": "seller_name",
        "销方名称": "seller_name",
        "购/销方识别号": "seller_tax_id",
        "购销方识别号": "seller_tax_id",
        "金额": "amount_without_tax",
        "税额": "tax_amount",
        "价税合计": "total_amount",
        "票种": "invoice_type",
        "发票状态": "invoice_state",
    }

    def _header_index_map(self, headers: List[str]) -> Dict[str, int]:
        mapping: Dict[str, int] = {}
        for idx, h in enumerate(headers):
            field = self.HEADER_FIELD_MAP.get(h)
            if field and field not in mapping:
                mapping[field] = idx
        return mapping

    def _row_invoice_key(self, headers: List[str], cells: List[str]) -> Optional[str]:
        m = self._header_index_map(headers)
        idx = m.get("invoice_number", m.get("invoice_number_fallback", -1))
        if idx is None or idx < 0 or idx >= len(cells):
            return None
        num = re.sub(r"\D", "", cells[idx])
        return num or None

    def _normalize_invoice_rows(self, headers: List[str], rows: List[List[str]]) -> List[Dict[str, Any]]:
        """按表头映射将行矩阵归一化为发票字段字典 (无表头或关键列缺失的行丢弃)"""
        m = self._header_index_map(headers)
        if "invoice_number" not in m and "invoice_number_fallback" not in m:
            return []
        no_idx = m.get("invoice_number", m.get("invoice_number_fallback"))

        invoices = []
        for cells in rows:
            def cell(field):
                idx = m.get(field)
                return cells[idx] if idx is not None and idx < len(cells) else ""

            number = re.sub(r"\D", "", cell("invoice_number") or cell("invoice_number_fallback"))
            total = _to_float(cell("total_amount"))
            issue_date = cell("issue_date")
            if not number or total is None or not issue_date:
                continue

            without = _to_float(cell("amount_without_tax"))
            tax = _to_float(cell("tax_amount"))
            if without is None:
                without = round(total - (tax or 0.0), 2)
            if tax is None:
                tax = round(total - without, 2)

            invoices.append({
                "invoice_number": number,
                "issue_date": issue_date[:10],
                "seller_name": cell("seller_name") or "未知销方",
                "seller_tax_id": cell("seller_tax_id"),
                "amount_without_tax": without,
                "tax_amount": tax,
                "total_amount": total,
                "invoice_type": cell("invoice_type") or "数电普票",
                "invoice_state": _normalize_state(cell("invoice_state")),
            })
        return invoices

    # ------------------------------------------------------------------
    # 入库 / 下载
    # ------------------------------------------------------------------
    def _import_invoices(self, invoices: List[Dict[str, Any]]) -> int:
        """清洗字段并幂等入库 (票号 UNIQUE, raw_source='etax_sync')"""
        conn = get_db_connection()
        cursor = conn.cursor()
        imported = 0
        for inv in invoices:
            total = inv.get("total_amount")
            if not inv.get("invoice_number") or not inv.get("issue_date") or total is None:
                continue
            without = inv.get("amount_without_tax")
            tax = inv.get("tax_amount")
            if without is None:
                without = round(total - (tax or 0.0), 2)
            if tax is None:
                tax = round(total - without, 2)
            cursor.execute("""
            INSERT OR IGNORE INTO tax_inbound_invoices (
                invoice_code, invoice_number, issue_date, seller_tax_id, seller_name,
                amount_without_tax, tax_amount, total_amount, invoice_type, invoice_state, raw_source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'etax_sync')
            """, (
                "", inv["invoice_number"], inv["issue_date"], inv.get("seller_tax_id", ""),
                inv.get("seller_name", "未知销方"), without, tax, total,
                inv.get("invoice_type", "数电普票"), inv.get("invoice_state", "normal")
            ))
            imported += cursor.rowcount
        conn.commit()
        conn.close()
        return imported

    def _download_invoice_file(self, client: RawCDPClient, invoice_number: str,
                               month_dir: str, trace: AgentTraceLogger) -> List[str]:
        """定位发票所在行 → 点击下载 (含格式选择弹窗两阶段) → 以发票号重命名；已存在文件幂等跳过"""
        for ext in (".zip", ".pdf", ".ofd", ".xml"):
            if os.path.exists(os.path.join(month_dir, f"{invoice_number}{ext}")):
                trace.log_step("skip_existing_download", "SKIP", {"invoice": invoice_number, "ext": ext})
                return []
        rule = _tax_rule()
        row_sels = rule.get("list_row_selectors", ["table tbody tr"])
        keywords = rule.get("download_keywords", ["下载"])
        formats = rule.get("download_format_priority", ["PDF", "OFD"])

        clicked = client.evaluate(f"""
        (() => {{
            const selStr = {json.dumps(row_sels)}.join(", ");
            const keywords = {json.dumps(keywords, ensure_ascii=False)};
            for (const row of document.querySelectorAll(selStr)) {{
                const t = row.innerText || "";
                if (!t.includes("{invoice_number}")) continue;
                const hit = Array.from(row.querySelectorAll("a, button, span, i, div"))
                    .filter(el => el.offsetParent !== null)
                    .find(el => keywords.some(k => (el.innerText || el.title || "").includes(k)));
                if (hit) {{ hit.click(); return true; }}
            }}
            return false;
        }})()
        """)
        if not clicked:
            trace.record_selector_metric(f"download_btn", "download_keywords", 0, sample_text=invoice_number)
            return []

        time.sleep(1.5)
        files = self.harvester.wait_download_complete(month_dir, timeout_s=15)
        if not files:
            # 阶段二：发票简要信息弹窗 (含"发票下载PDF/发票下载OFD/下载为XML"按钮，contains 匹配)
            picked = client.evaluate(f"""
            (() => {{
                const formats = {json.dumps(formats, ensure_ascii=False)};
                const cands = Array.from(document.querySelectorAll("button, a, li, span, div"))
                    .filter(el => el.getBoundingClientRect().width > 0);
                for (const fmt of formats) {{
                    const hit = cands.find(el => (el.innerText || "").trim().toUpperCase().includes(fmt)
                        && !(el.innerText || "").includes("二维码"));
                    if (hit) {{
                        const r = hit.getBoundingClientRect();
                        return JSON.stringify({{fmt: fmt, x: Math.round(r.x + r.width / 2),
                            y: Math.round(r.y + r.height / 2)}});
                    }}
                }}
                return null;
            }})()
            """)
            if picked:
                d = json.loads(picked)
                # 格式按钮需真实受信点击 (span 内层文本可能不响应合成 click)
                for t in ("mousePressed", "mouseReleased"):
                    client.call("Input.dispatchMouseEvent",
                                {"type": t, "x": d["x"], "y": d["y"], "button": "left", "clickCount": 1})
                trace.log_step("pick_download_format", "PICK_FORMAT",
                               {"invoice": invoice_number, "format": d["fmt"]})
                files = self.harvester.wait_download_complete(month_dir, timeout_s=40)
            # 关闭弹窗避免遮挡后续下载与翻页
            self._close_invoice_dialog(client)
            time.sleep(0.3)

        renamed = []
        for fp in files:
            target = self._rename_by_invoice_no(fp, invoice_number)
            if target:
                renamed.append(target)
        return renamed

    def _rename_by_invoice_no(self, file_path: str, invoice_number: str) -> Optional[str]:
        """以发票号重命名下载文件 (防止 Chrome 默认随机名覆盖)"""
        ext = os.path.splitext(file_path)[1].lower()
        target = os.path.join(os.path.dirname(file_path), f"{invoice_number}{ext}")
        if os.path.abspath(file_path) == os.path.abspath(target):
            return target
        try:
            if os.path.exists(target):
                os.remove(target)
            os.rename(file_path, target)
            return target
        except OSError:
            return file_path

    # ------------------------------------------------------------------
    # 对账 + 日报 (共用出口)
    # ------------------------------------------------------------------
    def _finalize_report(self, trigger_type: str, start_date: str, end_date: str,
                         found: int, imported: int, downloaded: int) -> Dict[str, Any]:
        """构造税局同步首节报告并调用唯一对账出口 (保留当日采集链摘要)"""
        today_str = datetime.now().strftime("%Y-%m-%d")
        conn = get_db_connection()
        audit_row = conn.execute("""
        SELECT status, message FROM audit_logs
        WHERE executed_at LIKE ? ORDER BY id DESC LIMIT 1
        """, (f"{today_str}%",)).fetchone()
        conn.close()
        audit_line = (f"{audit_row['status']} - {audit_row['message']}" if audit_row else "今日尚未执行业务采集链")

        header_md = f"""# 📑 OPC Auto 每日全流程采购采集与发票对账报告 ({today_str})

### 一、 电子税务局进项发票同步状态
- **执行触发**：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ({'用户手动触发' if trigger_type == 'manual' else '系统定时任务触发 (周一/周三窗口)'})
- **同步区间**：{start_date} ~ {end_date}
- **抓取/新增入库/下载文件**：{found} 张 / {imported} 张 / {downloaded} 个
- **今日采集链摘要**：{audit_line}
"""
        return finalize_reconciliation_and_report(header_md)


def run_etax_sync(trigger_type: str = "manual") -> Dict[str, Any]:
    """模块级快捷入口 (cron_daily 与 API 共用)"""
    return EtaxSyncService().run_sync(trigger_type=trigger_type)
