"""
OPC Auto 真实交叉对账核心算法引擎 (Reconciliation Engine)
严格遵循：无任何仿真伪造数据，依据真实数据库记录执行四分类对账。
"""

from datetime import datetime, timedelta
from .database import get_db_connection

def run_reconciliation():
    """
    执行真实的进项发票交叉勾稽对账
    返回对账统计结果 dict
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    # 1. 读取所有真实采购订单
    cursor.execute("SELECT * FROM procurement_orders")
    orders = [dict(row) for row in cursor.fetchall()]

    # 2. 读取所有真实税局进项底账发票
    cursor.execute("SELECT * FROM tax_inbound_invoices WHERE invoice_state = 'normal'")
    invoices = [dict(row) for row in cursor.fetchall()]

    # 清空上一轮对账结果，重新根据最新库中真实数据核算
    cursor.execute("DELETE FROM reconciliation_records")

    matched_invoices_ids = set()
    matched_orders_ids = set()

    # 统计指标
    stats = {
        "total_orders": len(orders),
        "total_invoices": len(invoices),
        "matched": {"count": 0, "amount": 0.0},
        "pending": {"count": 0, "amount": 0.0},
        "warning": {"count": 0, "amount": 0.0},
        "offline": {"count": 0, "amount": 0.0}
    }

    # =========================================================================
    # 第一步：精准与特征匹配（匹配成功的归入【状态④ 配对成功】）
    # =========================================================================
    for order in orders:
        matched_invoice = None

        # 规则 A: 若订单已记录发票号码，直接按票号强匹配
        if order.get("invoice_no"):
            for inv in invoices:
                if inv["invoice_number"] == order["invoice_no"] and inv["id"] not in matched_invoices_ids:
                    matched_invoice = inv
                    break

        # 规则 B: 启发式特征匹配（金额精确一致 + 商家全称或简称模糊匹配）
        if not matched_invoice:
            for inv in invoices:
                if inv["id"] in matched_invoices_ids:
                    continue
                # 金额严格相等 (容差 0.01 元以内)
                if abs(inv["total_amount"] - order["amount"]) < 0.01:
                    # 销方名称关联性（包含或被包含）
                    order_seller = order["seller_name"].strip()
                    inv_seller = inv["seller_name"].strip()
                    if (order_seller in inv_seller or inv_seller in order_seller or 
                        (order.get("seller_tax_id") and order["seller_tax_id"] == inv["seller_tax_id"])):
                        matched_invoice = inv
                        break

        if matched_invoice:
            matched_orders_ids.add(order["id"])
            matched_invoices_ids.add(matched_invoice["id"])
            stats["matched"]["count"] += 1
            stats["matched"]["amount"] += order["amount"]

            cursor.execute("""
            INSERT INTO reconciliation_records (order_id, invoice_id, status_category, status_text, diff_amount)
            VALUES (?, ?, 'matched', '配对成功', 0.0)
            """, (order["id"], matched_invoice["id"]))

            # 更新订单的实际发票号状态
            cursor.execute("UPDATE procurement_orders SET invoice_status = 'invoiced', invoice_no = ? WHERE id = ?",
                           (matched_invoice["invoice_number"], order["id"]))

    # =========================================================================
    # 第二步：未匹配订单的分类（【状态② 待申请】 与 【状态③ 催票预警】）
    # =========================================================================
    for order in orders:
        if order["id"] in matched_orders_ids:
            continue

        inv_status = order.get("invoice_status", "unapplied")
        if inv_status == "unapplied":
            # 状态②：电商采购待申请发票
            stats["pending"]["count"] += 1
            stats["pending"]["amount"] += order["amount"]
            cursor.execute("""
            INSERT INTO reconciliation_records (order_id, invoice_id, status_category, status_text)
            VALUES (?, NULL, 'pending', '待申请发票')
            """, (order["id"],))
        else:
            # 状态③：已提交申请，但税局底账库中未查到对应发票 (卖家未开票/超期催促)
            stats["warning"]["count"] += 1
            stats["warning"]["amount"] += order["amount"]
            cursor.execute("""
            INSERT INTO reconciliation_records (order_id, invoice_id, status_category, status_text)
            VALUES (?, NULL, 'warning', '已申请/卖家未开具')
            """, (order["id"],))

    # =========================================================================
    # 第三步：未匹配的税局进项发票（【状态① 线下/非电商渠道票据】）
    # =========================================================================
    for inv in invoices:
        if inv["id"] in matched_invoices_ids:
            continue

        # 税局有票，但无任何电商订单对应（如中石化加油、线下餐饮、住宿、租金等）
        stats["offline"]["count"] += 1
        stats["offline"]["amount"] += inv["total_amount"]
        cursor.execute("""
        INSERT INTO reconciliation_records (order_id, invoice_id, status_category, status_text)
        VALUES (NULL, ?, 'offline', '非电商/线下票据')
        """, (inv["id"],))

    conn.commit()
    conn.close()
    return stats
