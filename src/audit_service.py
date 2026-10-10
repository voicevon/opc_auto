"""
OPC Auto 真实巡检与总结报告服务 (Audit & Daily Report Generator)
"""

from datetime import datetime
from .database import get_db_connection
from .reconciliation_engine import run_reconciliation

def execute_daily_audit(trigger_type="manual"):
    """
    执行一次真实的自动化对账巡检，并生成当日 Markdown 总结报告
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    # 1. 记录巡检启动日志
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    today_str = datetime.now().strftime("%Y-%m-%d")

    cursor.execute("""
    INSERT INTO audit_logs (trigger_type, status, message, details)
    VALUES (?, 'running', '开始执行发票全量交叉对账巡检', ?)
    """, (trigger_type, f"触发时间: {now_str}"))
    conn.commit()
    log_id = cursor.lastrowid

    # 2. 运行核心对账计算
    stats = run_reconciliation()

    # 3. 构造真实的总结报告 Markdown 内容
    md_report = f"""# 📑 OPC Auto 每日发票对账与异常总结报告 ({today_str})

### 一、 核心对账指标总览
- **巡检执行时间**：{now_str} ({'用户手动触发' if trigger_type == 'manual' else '系统定时任务触发'})
- **纳入对账采购订单**：{stats['total_orders']} 笔
- **税局进项有效发票**：{stats['total_invoices']} 笔
- **闭环匹配成功率**：{round(stats['matched']['count'] / stats['total_orders'] * 100, 1) if stats['total_orders'] > 0 else 0.0}%

### 二、 四大业务分类详报
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

### 三、 处置建议与后续待办
"""
    if stats['warning']['count'] > 0:
        md_report += f"- ⚠ 请财务专员优先针对状态③中的 {stats['warning']['count']} 笔超期订单联系商家催开发票。\n"
    if stats['pending']['count'] > 0:
        md_report += f"- 📌 针对状态②中的 {stats['pending']['count']} 笔未申请订单，建议使用一键提报开票申请。\n"
    if stats['total_orders'] == 0 and stats['total_invoices'] == 0:
        md_report += "- ℹ 当前数据库中暂无采购订单与进项发票记录，请先在工作台录入或导入真实业务数据。\n"

    # 4. 将报告落库
    cursor.execute("""
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

    # 5. 更新巡检日志状态
    finish_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute("""
    UPDATE audit_logs SET status = 'success', message = '巡检完成并生成日报',
           details = ? WHERE id = ?
    """, (f"耗时对账完成，耗时完成于 {finish_str}，订单: {stats['total_orders']}，发票: {stats['total_invoices']}", log_id))

    conn.commit()
    conn.close()

    return {
        "status": "success",
        "executed_at": finish_str,
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
