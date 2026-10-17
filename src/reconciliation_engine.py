"""
OPC Auto 真实交叉对账核心算法引擎 (Reconciliation Engine)
严格遵循：无任何仿真伪造数据，依据真实数据库记录执行双侧五态对账。

状态模型 (2026-10-17 用户定稿)：
- 订单侧: unapplied(未申请发票) → uninvoiced(未开发票，催办)
- 票侧:   pending_review(待审核新到票) → offline(人工批量勾选确认线下，可退回)
- 共同:   matched(真开票·配对成功，三单一致闭环)
"""

from datetime import datetime
import threading
from .database import get_db_connection

# 对账全量重建为 DELETE+INSERT 写操作，必须全局互斥，防止并发请求读到中间态或撞 SQLite 写锁。
# RLock 可重入：批量确认/退回接口在持锁完成 UPDATE 后调用 run_reconciliation 不会自死锁。
RECON_LOCK = threading.RLock()

def run_reconciliation():
    """
    执行真实的进项发票交叉勾稽对账
    返回对账统计结果 dict
    """
    with RECON_LOCK:
        return _run_reconciliation_locked()

def _run_reconciliation_locked():
    conn = get_db_connection()
    cursor = conn.cursor()

    # 1. 读取所有真实采购订单
    cursor.execute("SELECT * FROM procurement_orders")
    orders = [dict(row) for row in cursor.fetchall()]

    # 2. 读取所有真实税局进项底账发票
    cursor.execute("SELECT * FROM tax_inbound_invoices WHERE invoice_state = 'normal'")
    invoices = [dict(row) for row in cursor.fetchall()]

    # 3. 保留人工审核留痕与催票记录 (对账全量重建时人工动作不可丢失)
    cursor.execute("""
    SELECT invoice_id, confirmed_at, urge_count, last_urge_time
    FROM reconciliation_records
    WHERE status_category = 'offline' AND invoice_id IS NOT NULL
    """)
    confirmed_map = {r["invoice_id"]: dict(r) for r in cursor.fetchall()}

    cursor.execute("""
    SELECT order_id, urge_count, last_urge_time FROM reconciliation_records
    WHERE order_id IS NOT NULL AND urge_count > 0
    """)
    urged_map = {r["order_id"]: (r["urge_count"], r["last_urge_time"]) for r in cursor.fetchall()}

    # 清空上一轮对账结果，重新根据最新库中真实数据核算
    cursor.execute("DELETE FROM reconciliation_records")

    matched_invoices_ids = set()
    matched_orders_ids = set()

    # 统计指标 (双侧五态)
    stats = {
        "total_orders": len(orders),
        "total_invoices": len(invoices),
        "unapplied": {"count": 0, "amount": 0.0},
        "uninvoiced": {"count": 0, "amount": 0.0},
        "matched": {"count": 0, "amount": 0.0},
        "pending_review": {"count": 0, "amount": 0.0},
        "offline": {"count": 0, "amount": 0.0}
    }

    # =========================================================================
    # 第一步：精准与特征匹配（匹配成功归入【真开票 · 配对成功】闭环）
    # =========================================================================
    for order in orders:
        matched_invoice = None

        # 规则 A: 若订单已记录发票号码，直接按票号强匹配
        if order.get("invoice_no"):
            for inv in invoices:
                if inv["invoice_number"] == order["invoice_no"] and inv["id"] not in matched_invoices_ids:
                    matched_invoice = inv
                    break

        # 规则 B: 启发式特征匹配（金额精确一致为基础，双重置信度校验）
        if not matched_invoice:
            for inv in invoices:
                if inv["id"] in matched_invoices_ids:
                    continue
                # 金额严格相等 (容差 0.01 元以内)
                if abs(inv["total_amount"] - order["amount"]) >= 0.01:
                    continue
                # 销方名称关联性（包含或被包含；空串包含恒真需先排除）
                order_seller = (order["seller_name"] or "").strip()
                inv_seller = (inv["seller_name"] or "").strip()
                name_linked = bool(order_seller and inv_seller and
                                   (order_seller in inv_seller or inv_seller in order_seller))
                tax_linked = bool(order.get("seller_tax_id") and order["seller_tax_id"] == inv["seller_tax_id"])
                if name_linked or tax_linked:
                    matched_invoice = inv
                    break
                # 淘宝店铺名与开票主体企业名常无字面关联，退守三重防误配校验：
                # 同金额订单侧唯一 + 同金额未配发票侧唯一 + 开票日不早于真实下单日
                order_time = (order.get("order_time") or "")[:10]
                issue_date = (inv.get("issue_date") or "")[:10]
                if not order_time or not issue_date or issue_date < order_time:
                    continue
                same_amt_orders = [o for o in orders if abs(o["amount"] - order["amount"]) < 0.01]
                same_amt_invs = [v for v in invoices
                                 if v["id"] not in matched_invoices_ids
                                 and abs(v["total_amount"] - order["amount"]) < 0.01]
                if len(same_amt_orders) == 1 and len(same_amt_invs) == 1:
                    matched_invoice = inv
                    break

        if matched_invoice:
            matched_orders_ids.add(order["id"])
            matched_invoices_ids.add(matched_invoice["id"])
            stats["matched"]["count"] += 1
            stats["matched"]["amount"] += order["amount"]

            urge = urged_map.get(order["id"], (0, ""))
            cursor.execute("""
            INSERT INTO reconciliation_records (order_id, invoice_id, status_category, status_text, diff_amount, urge_count, last_urge_time)
            VALUES (?, ?, 'matched', '真开票 · 配对成功', 0.0, ?, ?)
            """, (order["id"], matched_invoice["id"], urge[0], urge[1]))

            # 更新订单的实际发票号状态 (口径: 配上税局底账 = 真开票)
            cursor.execute("UPDATE procurement_orders SET invoice_status = 'invoiced', invoice_no = ? WHERE id = ?",
                           (matched_invoice["invoice_number"], order["id"]))

    # =========================================================================
    # 第二步：未匹配订单分类（【未申请发票】 与 【未开发票·催办】）
    # =========================================================================
    for order in orders:
        if order["id"] in matched_orders_ids:
            continue

        urge = urged_map.get(order["id"], (0, ""))
        inv_status = order.get("invoice_status", "unapplied")
        if inv_status == "unapplied":
            stats["unapplied"]["count"] += 1
            stats["unapplied"]["amount"] += order["amount"]
            cursor.execute("""
            INSERT INTO reconciliation_records (order_id, invoice_id, status_category, status_text, urge_count, last_urge_time)
            VALUES (?, NULL, 'unapplied', '未申请发票', ?, ?)
            """, (order["id"], urge[0], urge[1]))
        else:
            stats["uninvoiced"]["count"] += 1
            stats["uninvoiced"]["amount"] += order["amount"]
            cursor.execute("""
            INSERT INTO reconciliation_records (order_id, invoice_id, status_category, status_text, urge_count, last_urge_time)
            VALUES (?, NULL, 'uninvoiced', '已申请发票 · 税局暂无票', ?, ?)
            """, (order["id"], urge[0], urge[1]))

    # =========================================================================
    # 第三步：未匹配税局票分类（【待审核新到票】 与 【人工确认线下】）
    # =========================================================================
    for inv in invoices:
        if inv["id"] in matched_invoices_ids:
            continue

        if inv["id"] in confirmed_map:
            # 人工批量勾选确认过的线下支出 (退回操作会清空 confirmed_at)
            c = confirmed_map[inv["id"]]
            stats["offline"]["count"] += 1
            stats["offline"]["amount"] += inv["total_amount"]
            cursor.execute("""
            INSERT INTO reconciliation_records (order_id, invoice_id, status_category, status_text, confirmed_at)
            VALUES (NULL, ?, 'offline', '已确认线下支出', ?)
            """, (inv["id"], c["confirmed_at"]))
        else:
            # 刚同步/导入的税局票，未配上任何订单，等待人工审核
            stats["pending_review"]["count"] += 1
            stats["pending_review"]["amount"] += inv["total_amount"]
            cursor.execute("""
            INSERT INTO reconciliation_records (order_id, invoice_id, status_category, status_text)
            VALUES (NULL, ?, 'pending_review', '待审核 · 未配上订单')
            """, (inv["id"],))

    conn.commit()
    conn.close()
    return stats
