"""
OPC Auto 真实巡检与总结报告服务 (Audit & Daily Report Generator)
"""

from datetime import datetime
from typing import Dict, Any, List, Optional
from .database import get_db_connection
from .reconciliation_engine import run_reconciliation

def finalize_reconciliation_and_report(header_md: str, tips: Optional[List[str]] = None) -> Dict[str, Any]:
    """
    运行全量交叉对账并生成/刷新当日 Markdown 日报的唯一出口 (DRY)。
    业务采集链 (execute_daily_audit) 与税局同步链 (etax_sync_service) 共用本函数。
    header_md: 各链路自述报告首节 (含触发方式与现场状态)；tips: 追加至处置建议的链路提示。
    """
    today_str = datetime.now().strftime("%Y-%m-%d")

    # 1. 运行核心交叉对账计算
    stats = run_reconciliation()

    # 2. 构造公共对账指标 Markdown 报告
    md_report = header_md + f"""
### 二、 核心交叉对账指标总览
- **纳入对账采购订单**：{stats['total_orders']} 笔
- **税局进项有效发票**：{stats['total_invoices']} 笔
- **闭环匹配成功率**：{round(stats['matched']['count'] / stats['total_orders'] * 100, 1) if stats['total_orders'] > 0 else 0.0}%

### 三、 四大业务分类详报
1. 🟢 **状态④ 核对配对成功 (闭环归档)**：
   - 笔数：{stats['matched']['count']} 笔
   - 价税合计总额：￥{stats['matched']['amount']:,.2f} 元
2. 🟡 **状态② 电商待申请发票 (待办催办)**：
   - 笔数：{stats['pending']['count']} 笔
   - 待申请总额：￥{stats['pending']['amount']:,.2f} 元
3. 🔴 **状态③ 已申请未到票 (超期预警)**：
   - 笔数：{stats['warning']['count']} 笔
   - 预警总额：￥{stats['warning']['amount']:,.2f} 元
4. 🟣 **状态① 非电商/线下渠道票据 (加油/差旅等)**：
   - 笔数：{stats['offline']['count']} 笔
   - 线下受票总额：￥{stats['offline']['amount']:,.2f} 元

### 四、 处置建议与后续待办
"""
    if stats['warning']['count'] > 0:
        md_report += f"- ⚠ 请财务专员优先针对状态③中的 {stats['warning']['count']} 笔超期订单联系商家催开发票。\n"
    if stats['pending']['count'] > 0:
        md_report += f"- 📌 针对状态②中的 {stats['pending']['count']} 笔未申请订单，建议使用一键提报开票申请。\n"
    for tip in (tips or []):
        md_report += f"- {tip}\n"

    # 3. 报告落库 (同日重跑幂等刷新)
    conn_rep = get_db_connection()
    cur_rep = conn_rep.cursor()
    cur_rep.execute("""
    INSERT INTO daily_reports (
        report_date, total_orders, total_invoices,
        matched_count, matched_amount,
        pending_count, pending_amount,
        warning_count, warning_amount,
        offline_count, offline_amount,
        markdown_content
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(report_date) DO UPDATE SET
        total_orders=excluded.total_orders,
        total_invoices=excluded.total_invoices,
        matched_count=excluded.matched_count,
        matched_amount=excluded.matched_amount,
        pending_count=excluded.pending_count,
        pending_amount=excluded.pending_amount,
        warning_count=excluded.warning_count,
        warning_amount=excluded.warning_amount,
        offline_count=excluded.offline_count,
        offline_amount=excluded.offline_amount,
        markdown_content=excluded.markdown_content,
        generated_at=CURRENT_TIMESTAMP
    """, (
        today_str, stats['total_orders'], stats['total_invoices'],
        stats['matched']['count'], stats['matched']['amount'],
        stats['pending']['count'], stats['pending']['amount'],
        stats['warning']['count'], stats['warning']['amount'],
        stats['offline']['count'], stats['offline']['amount'],
        md_report
    ))
    conn_rep.commit()
    conn_rep.close()

    return {"stats": stats, "report_markdown": md_report}


def execute_daily_audit(trigger_type="manual"):
    """
    执行真实的端到端全流程巡检：
    1. 接管宿主 Chrome CDP (9222) 拟人化采集电商采购订单，生成面向 AI 的 Trace 档案；
    2. 将采集订单落库 procurement_orders；
    3. 执行 run_reconciliation() 全量交叉对账；
    4. 生成详细的 Markdown 每日总结报告落库。
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    today_str = datetime.now().strftime("%Y-%m-%d")

    # 1. 记录巡检启动日志
    cursor.execute("""
    INSERT INTO audit_logs (trigger_type, status, message, details)
    VALUES (?, 'running', '开始执行真实浏览器采集与发票全量交叉对账巡检', ?)
    """, (trigger_type, f"触发时间: {now_str}"))
    conn.commit()
    log_id = cursor.lastrowid
    conn.close()

    # 2. 真实浏览器 CDP 拟人化采集与 Trace 记录
    from .browser_harvest_service import HumanoidBrowserHarvester, AgentTraceLogger
    trace_logger = AgentTraceLogger(platform="taobao")
    harvester = HumanoidBrowserHarvester()

    harvest_status_summary = {
        "cdp_connected": False,
        "orders_extracted": 0,
        "trace_id": trace_logger.trace_id,
        "trace_file": trace_logger.trace_file,
        "message": ""
    }

    if not harvester.is_browser_ready():
        trace_logger.record_failure_context(
            failed_step="check_cdp_port",
            error_msg="宿主浏览器 9222 调试端口未开放 (127.0.0.1:9222 无法连接)",
            suspected_cause="Chrome 未携带 --remote-debugging-port=9222 启动。请双击运行 start_chrome_debug.bat 启动浏览器"
        )
        trace_logger.finish(extracted_items=[], success=False)
        harvest_status_summary["message"] = "Chrome 9222 调试端口未开放，未能挂接已登录会话；已生成诊断 Trace 档案供自愈分析"
    else:
        try:
            # 仅挂接淘宝订单页专属标签页，严禁兜底挂接任意活跃页 (避免在 chrome://newtab 等非目标页误采集)
            attached = harvester.attach_to_page(url_keyword="taobao", trace_logger=trace_logger)

            if not attached:
                # 自愈式开页：按规则库 start_url 新开淘宝订单页 tab (探完即关)，再重试严格挂接
                from .self_healing_engine import RuleEvolutionManager
                start_url = RuleEvolutionManager().get_platform_rule("taobao").get("start_url", "")
                if start_url and harvester.open_new_tab(start_url, wait_render_s=6.0, trace_logger=trace_logger):
                    trace_logger.log_step(
                        step_name="ensure_target_tab",
                        action="AUTO_OPEN_TAB",
                        details={"url": start_url, "reason": "未找到淘宝订单页标签，按规则库 start_url 自动新开"}
                    )
                    attached = harvester.reattach_to_tab("taobao")

            if attached:
                harvest_status_summary["cdp_connected"] = True
                # 拟人化平滑滚动页面，触发懒加载并记录过程时序
                harvester.humanoid_scroll_and_wait(min_wait=0.8, max_wait=1.8, trace_logger=trace_logger)
                extracted_items = harvester.harvest_orders(platform="taobao", trace_logger=trace_logger)
                trace_logger.finish(extracted_items=extracted_items, success=(len(extracted_items) > 0))

                harvest_status_summary["orders_extracted"] = len(extracted_items)
                harvest_status_summary["message"] = f"拟人化采集成功，获取 {len(extracted_items)} 笔电商采购订单"

                # 将真实采集订单清洗落入 procurement_orders
                if extracted_items:
                    conn_orders = get_db_connection()
                    cur_orders = conn_orders.cursor()
                    for it in extracted_items:
                        order_no = str(it.get("order_id", "")).strip()
                        if not order_no:
                            continue
                        pay_amt = float(it.get("pay_amount", 0.0))
                        seller = it.get("store_name", "") or "淘宝精选店铺"
                        raw_status = it.get("invoice_status", "")
                        inv_status = "invoiced" if "已开票" in raw_status else ("applying" if "已申请" in raw_status else "unapplied")
                        item_desc = it.get("raw_snippet", "")[:120] or f"淘宝采购订单 {order_no}"

                        cur_orders.execute("""
                        INSERT INTO procurement_orders (
                            channel, channel_name, order_no, order_time, item_desc, amount, seller_name, invoice_status, notes
                        ) VALUES ('taobao', '淘宝企业购', ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(order_no) DO UPDATE SET
                            amount=excluded.amount,
                            seller_name=excluded.seller_name,
                            invoice_status=excluded.invoice_status,
                            notes=excluded.notes
                        """, (order_no, now_str, item_desc, pay_amt, seller, inv_status, f"CDP采集状态: {raw_status}"))
                    conn_orders.commit()
                    conn_orders.close()

                    # 2.1 全自动点击申请开票 (已确认决策：无需人工确认)
                    cursor_profile = get_db_connection()
                    prof_row = cursor_profile.execute(
                        "SELECT * FROM company_billing_profile ORDER BY id DESC LIMIT 1"
                    ).fetchone()
                    cursor_profile.close()
                    billing_profile = dict(prof_row) if prof_row else {}

                    try:
                        apply_result = harvester.auto_apply_invoices(
                            platform="taobao", billing_profile=billing_profile, trace_logger=trace_logger
                        )
                        harvest_status_summary["auto_apply"] = apply_result
                        if apply_result.get("applied"):
                            conn_apply = get_db_connection()
                            cur_apply = conn_apply.cursor()
                            for oid in apply_result["applied_order_ids"]:
                                cur_apply.execute("""
                                UPDATE procurement_orders
                                SET invoice_status = 'applying', apply_time = ?
                                WHERE order_no = ? AND invoice_status = 'unapplied'
                                """, (now_str, oid))
                            conn_apply.commit()
                            conn_apply.close()
                            harvest_status_summary["message"] += f"；已全自动提交开票申请 {apply_result['applied']} 笔"
                    except Exception as apply_err:
                        trace_logger.record_failure_context(
                            failed_step="auto_apply_invoices",
                            error_msg=str(apply_err),
                            suspected_cause="全自动开票申请执行期间异常"
                        )
            else:
                trace_logger.record_failure_context(
                    failed_step="attach_to_page",
                    error_msg="未在浏览器中找到淘宝『已买到的宝贝』订单页标签",
                    suspected_cause="宿主环境未就绪：请运行 start_chrome_debug.bat 打开订单页 (脚本会自动导航)",
                    failed_url="TARGET_TAB_NOT_FOUND"
                )
                trace_logger.finish(extracted_items=[], success=False)
                harvest_status_summary["message"] = "未在 Chrome 中定位到淘宝订单页标签页 (环境未就绪，非前端漂移)"
        except Exception as e:
            trace_logger.record_failure_context(
                failed_step="harvest_execution",
                error_msg=str(e),
                suspected_cause="拟人化数据收割执行期间异常"
            )
            trace_logger.finish(extracted_items=[], success=False)
            harvest_status_summary["message"] = f"采集过程发生异常: {e}"
        finally:
            harvester.close()

    # 3-5. 全量交叉对账 + 生成/刷新当日日报 (与税局同步链共用唯一出口)
    cdp_status_icon = "🟢" if harvest_status_summary["cdp_connected"] else "🔴"
    header_md = f"""# 📑 OPC Auto 每日全流程采购采集与发票对账报告 ({today_str})

### 一、 拟人化浏览器采集流水线状态
- **执行触发时间**：{now_str} ({'用户手动触发' if trigger_type == 'manual' else '系统定时任务触发'})
- **Chrome CDP 宿主连接**：{cdp_status_icon} {'9222 调试会话已挂接' if harvest_status_summary["cdp_connected"] else '9222 端口未开放 (离线模式)'}
- **本轮采集采购订单**：{harvest_status_summary["orders_extracted"]} 笔
- **AI 现场 Trace 档案**：`{harvest_status_summary["trace_id"]}`
- **采集摘要说明**：{harvest_status_summary["message"]}
"""
    report_tips = []
    if not harvest_status_summary["cdp_connected"]:
        report_tips.append("💡 **提示**：检测到浏览器 9222 调试端口未启动。若需自动采集淘宝/京东新订单，请双击运行 `start_chrome_debug.bat` 开启会话。")

    finalize_result = finalize_reconciliation_and_report(header_md, tips=report_tips)
    stats = finalize_result["stats"]
    md_report = finalize_result["report_markdown"]

    # 6. 更新巡检日志状态
    finish_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn_log = get_db_connection()
    cur_log = conn_log.cursor()
    cur_log.execute("""
    UPDATE audit_logs SET status = 'success', message = '采集与巡检完成并生成日报',
           details = ? WHERE id = ?
    """, (f"采集订单: {harvest_status_summary['orders_extracted']}, 库内订单: {stats['total_orders']}, 进项发票: {stats['total_invoices']}, Trace: {harvest_status_summary['trace_id']}", log_id))
    conn_log.commit()
    conn_log.close()

    return {
        "status": "success",
        "executed_at": finish_str,
        "harvest": harvest_status_summary,
        "stats": stats,
        "report_markdown": md_report
    }

def get_latest_report():
    """获取最新一份真实生成的总结报告"""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM daily_reports ORDER BY id DESC LIMIT 1")
    row = cursor.fetchone()
    conn.close()
    if row:
        return dict(row)
    return None
