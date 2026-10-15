"""
OPC Auto RESTful API 路由处理层
完全基于真实数据库驱动，拒绝任何写死假数据
"""

import json
from urllib.parse import parse_qs, urlparse
from .database import get_db_connection, DB_PATH
from .reconciliation_engine import run_reconciliation
from .audit_service import execute_daily_audit, get_latest_report

def handle_api_request(method, path, body_bytes):
    parsed = urlparse(path)
    route = parsed.path
    query = parse_qs(parsed.query)

    body = {}
    if body_bytes:
        try:
            body = json.loads(body_bytes.decode("utf-8"))
        except Exception:
            pass

    # 1. 系统真实状态 (包含数据库指标与宿主浏览器 CDP 9222 状态)
    if route == "/api/status" and method == "GET":
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT count(*) as c FROM procurement_orders")
        order_count = cursor.fetchone()["c"]
        cursor.execute("SELECT count(*) as c FROM tax_inbound_invoices")
        invoice_count = cursor.fetchone()["c"]
        cursor.execute("SELECT executed_at FROM audit_logs WHERE status='success' ORDER BY id DESC LIMIT 1")
        last_audit = cursor.fetchone()
        conn.close()

        from .browser_harvest_service import HumanoidBrowserHarvester, AgentTraceLogger
        harvester = HumanoidBrowserHarvester()
        browser_ready = harvester.is_browser_ready()
        recent_traces = AgentTraceLogger.get_latest_traces(limit=1)
        latest_trace = recent_traces[0] if recent_traces else None

        return 200, {
            "status": "online",
            "mode": "real_production",
            "db_path": DB_PATH,
            "browser_ready": browser_ready,
            "browser_port": 9222,
            "total_orders": order_count,
            "total_invoices": invoice_count,
            "last_audit_time": last_audit["executed_at"] if last_audit else "尚未执行过巡检",
            "latest_trace": latest_trace
        }

    # 1.1 读取最近 AI 执行 Trace 档案列表 (面向中间过程可观测性)
    if route == "/api/traces" and method == "GET":
        from .browser_harvest_service import AgentTraceLogger
        limit_val = int(query.get("limit", [5])[0])
        traces = AgentTraceLogger.get_latest_traces(limit=limit_val)
        return 200, {"traces": traces, "total": len(traces)}

    # 2. 真实四状态指标数据
    if route == "/api/stats" and method == "GET":
        stats = run_reconciliation()
        return 200, stats

    # 3. 真实台账明细查询 (支持按分类状态与搜索词过滤)
    if route == "/api/orders" and method == "GET":
        category_filter = query.get("category", ["all"])[0]
        search_kw = query.get("search", [""])[0].strip().lower()

        conn = get_db_connection()
        cursor = conn.cursor()

        # 联合查询采购订单、税局发票与对账结果
        sql = """
        SELECT 
            r.id as rec_id,
            r.status_category,
            r.status_text,
            r.urge_count,
            r.last_urge_time,
            o.id as order_id,
            o.channel,
            o.channel_name,
            o.order_no,
            o.order_time,
            o.item_desc,
            o.amount as order_amount,
            o.seller_name as order_seller,
            o.invoice_status,
            o.invoice_no as order_invoice_no,
            i.id as invoice_id,
            i.invoice_number as tax_invoice_number,
            i.seller_name as tax_seller_name,
            i.total_amount as tax_total_amount,
            i.issue_date as tax_issue_date
        FROM reconciliation_records r
        LEFT JOIN procurement_orders o ON r.order_id = o.id
        LEFT JOIN tax_inbound_invoices i ON r.invoice_id = i.id
        WHERE 1=1
        """
        params = []
        if category_filter != "all":
            sql += " AND r.status_category = ?"
            params.append(category_filter)

        sql += " ORDER BY r.id DESC"
        cursor.execute(sql, params)
        rows = [dict(row) for row in cursor.fetchall()]
        conn.close()

        # 内存中执行关键字二次搜索 (订单号、商户名、摘要、发票号)
        results = []
        for r in rows:
            channel_name = r["channel_name"] or "线下/税局受票"
            order_no = r["order_no"] or (f"TAX-{r['tax_invoice_number']}" if r["tax_invoice_number"] else "-")
            item_desc = r["item_desc"] or "线下增值税发票支出"
            amount = r["order_amount"] if r["order_amount"] is not None else r["tax_total_amount"]
            seller = r["tax_seller_name"] or r["order_seller"] or "未知企业"
            invoice_no = r["tax_invoice_number"] or r["order_invoice_no"] or "-"
            time_val = r["order_time"] or r["tax_issue_date"] or "-"

            item_obj = {
                "rec_id": r["rec_id"],
                "status": r["status_category"],
                "status_text": r["status_text"],
                "channel": r["channel"] or "offline",
                "channel_name": channel_name,
                "order_id": r["order_id"],
                "order_no": order_no,
                "desc": item_desc,
                "amount": f"{amount:,.2f}" if amount is not None else "0.00",
                "raw_amount": amount,
                "seller": seller,
                "invoice_no": invoice_no,
                "time": time_val,
                "urge_count": r["urge_count"]
            }

            if search_kw:
                match = (search_kw in item_obj["order_no"].lower() or 
                         search_kw in item_obj["desc"].lower() or 
                         search_kw in item_obj["seller"].lower() or 
                         search_kw in item_obj["invoice_no"].lower())
                if not match:
                    continue

            results.append(item_obj)

        return 200, {"total": len(results), "items": results}

    # 4. 真正触发执行一次对账巡检
    if route == "/api/audit/run" and method == "POST":
        res = execute_daily_audit(trigger_type=body.get("trigger", "manual"))
        return 200, res

    # 5. 获取最新生成的真实日报
    if route == "/api/reports/latest" and method == "GET":
        rep = get_latest_report()
        if rep:
            return 200, rep
        return 200, {"message": "当前尚未生成任何日报，请先执行巡检任务", "markdown_content": "# 暂无日报\n尚未执行巡检或数据库中无数据。"}

    # 5.5 电子税务局进项底账手动导入 (Excel/CSV 过渡方案)
    if route == "/api/invoices/import" and method == "POST":
        from .invoice_import_service import import_invoice_file
        filename = str(body.get("filename", "")).strip()
        content_b64 = str(body.get("content_base64", "")).strip()
        if not filename or not content_b64:
            return 400, {"error": "缺少 filename 或 content_base64 参数"}
        result = import_invoice_file(filename, content_b64)
        return (200 if result.get("success") else 400), result

    # 5.6 电子税务局进项发票手动同步 (首跑登录/调试用；cron 触发走 cron_daily 周一/周三门控)
    if route == "/api/etax/sync" and method == "POST":
        from .etax_sync_service import run_etax_sync
        res = run_etax_sync(trigger_type="manual")
        return (200 if res.get("status") in ("success", "skipped") else 502), res

    # 8. 真实触发订单开票申请状态变更
    if route == "/api/orders/apply-invoice" and method == "POST":
        order_no = body.get("order_no")
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
        UPDATE procurement_orders 
        SET invoice_status = 'applying', apply_time = CURRENT_TIMESTAMP 
        WHERE order_no = ?
        """, (order_no,))
        conn.commit()
        conn.close()
        run_reconciliation()
        return 200, {"success": True, "message": f"订单 [{order_no}] 已变更为申请开票中"}

    # 9. 真实记录催票指令
    if route == "/api/orders/urge" and method == "POST":
        rec_id = body.get("rec_id")
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
        UPDATE reconciliation_records 
        SET urge_count = urge_count + 1, last_urge_time = CURRENT_TIMESTAMP 
        WHERE id = ?
        """, (rec_id,))
        conn.commit()
        conn.close()
        return 200, {"success": True, "message": "催票记录已更新"}

    # 10. 读取/保存企业开票抬头配置
    if route == "/api/config/billing":
        conn = get_db_connection()
        cursor = conn.cursor()
        if method == "GET":
            cursor.execute("SELECT * FROM company_billing_profile ORDER BY id DESC LIMIT 1")
            row = cursor.fetchone()
            conn.close()
            return 200, dict(row) if row else {}
        elif method == "POST":
            cursor.execute("""
            INSERT INTO company_billing_profile (
                company_name, tax_id, address, phone, bank_name, bank_account, invoice_email
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                body.get("company_name", ""),
                body.get("tax_id", ""),
                body.get("address", ""),
                body.get("phone", ""),
                body.get("bank_name", ""),
                body.get("bank_account", ""),
                body.get("invoice_email", "")
            ))
            conn.commit()
            conn.close()
            return 200, {"success": True, "message": "企业开票信息更新成功"}

    # 12. 读取最近巡检日志
    if route == "/api/logs" and method == "GET":
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM audit_logs ORDER BY id DESC LIMIT 20")
        rows = [dict(r) for r in cursor.fetchall()]
        conn.close()
        return 200, {"logs": rows}

    # 13. 读取当前规则库自进化状态与最近 Trace 档案
    if route == "/api/evolution/status" and method == "GET":
        from .self_healing_engine import RuleEvolutionManager
        from .browser_harvest_service import AgentTraceLogger
        rule_mgr = RuleEvolutionManager()
        rules = rule_mgr.load_rules()
        recent_traces = AgentTraceLogger.get_latest_traces(limit=3)
        return 200, {
            "version": rules.get("version", "1.0.0"),
            "last_evolved_at": rules.get("last_evolved_at"),
            "history": rules.get("evolution_history", []),
            "platforms": rules.get("platforms", {}),
            "recent_traces": recent_traces
        }

    # 14. 手动触发一次 AI 自诊断与自愈演进复盘
    if route == "/api/evolution/trigger" and method == "POST":
        from .self_healing_engine import SelfHealingEngine
        healer = SelfHealingEngine()
        report = healer.analyze_and_heal()
        return 200, {
            "success": True,
            "report": report
        }

    # 15. 读取决策简报列表 (人机在环决策事项)
    if route == "/api/decisions" and method == "GET":
        from .diagnostic_router import DecisionBriefManager
        status_filter = query.get("status", [None])[0]
        mgr = DecisionBriefManager()
        briefs = mgr.list_briefs(status=status_filter)
        return 200, {"briefs": briefs, "total": len(briefs)}

    # 16. 人类操作员提交决策定夺
    if route == "/api/decisions/resolve" and method == "POST":
        from .diagnostic_router import DecisionBriefManager
        brief_id = body.get("brief_id")
        chosen_option_id = body.get("chosen_option_id")
        operator_note = body.get("operator_note", "")

        if not brief_id or not chosen_option_id:
            return 400, {"error": "缺少 brief_id 或 chosen_option_id 参数"}

        mgr = DecisionBriefManager()
        updated = mgr.resolve_brief(brief_id, chosen_option_id, operator_note)
        if not updated:
            return 404, {"error": "未找到指定简报"}
        return 200, {"success": True, "message": "决策指令已成功记录生效", "brief": updated}

    # 17. 获取 Layer 1 双轨分流态势总览
    if route == "/api/triage/overview" and method == "GET":
        from .self_healing_engine import RuleEvolutionManager
        from .diagnostic_router import DecisionBriefManager
        rule_mgr = RuleEvolutionManager()
        rules = rule_mgr.load_rules()
        brief_mgr = DecisionBriefManager()
        all_briefs = brief_mgr.list_briefs()
        pending_briefs = [b for b in all_briefs if b.get("status") == "PENDING"]

        return 200, {
            "path_1_llm_healing": {
                "rule_version": rules.get("version", "1.0.0"),
                "total_evolutions": len(rules.get("evolution_history", [])),
                "last_evolved_at": rules.get("last_evolved_at")
            },
            "path_2_human_decisions": {
                "pending_count": len(pending_briefs),
                "resolved_count": len(all_briefs) - len(pending_briefs),
                "pending_list": pending_briefs
            }
        }

    # 18. Trae 积分签到插件：读取签到状态
    if route == "/api/trae_score" and method == "GET":
        from trae_score.runner import get_status
        return 200, get_status()

    # 19. Trae 积分签到插件：手动触发一次签到 (幂等)
    if route == "/api/trae_score/claim" and method == "POST":
        from trae_score.runner import run_daily_claim
        return 200, run_daily_claim(trigger_type=body.get("trigger", "manual"))

    return 404, {"error": "Not Found", "route": route}

