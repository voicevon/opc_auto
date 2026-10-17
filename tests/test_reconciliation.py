"""
OPC Auto 交叉对账引擎单元测试 (reconciliation_engine 五态模型)
隔离性：全部用例运行在临时 SQLite 库上，严禁污染真实 data/opc_auto.db

五态口径 (2026-10-17 用户定稿)：
- 订单侧: unapplied(未申请发票) / uninvoiced(未开发票催办)
- 票侧:   pending_review(待审核) / offline(人工确认线下)
- 共同:   matched(真开票·配对成功闭环)
"""

import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest

import src.database as database
from src.database import init_db, get_db_connection
from src.reconciliation_engine import run_reconciliation


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    """重定向数据库至临时目录并初始化空表"""
    db_path = tmp_path / "test_opc_auto.db"
    monkeypatch.setattr(database, "DB_PATH", str(db_path))
    init_db()
    yield str(db_path)


def _add_order(order_no, amount, seller, tax_id="", invoice_status="unapplied", order_time="2026-10-01 10:00:00"):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO procurement_orders (channel, channel_name, order_no, order_time, item_desc, amount,
                                    seller_name, seller_tax_id, invoice_status)
    VALUES ('tb', '淘宝', ?, ?, '测试商品', ?, ?, ?, ?)
    """, (order_no, order_time, amount, seller, tax_id, invoice_status))
    oid = cur.lastrowid
    conn.commit()
    conn.close()
    return oid


def _add_invoice(invoice_number, amount, seller, tax_id="", issue_date="2026-10-05"):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO tax_inbound_invoices (invoice_number, invoice_code, invoice_type, invoice_state,
                                      seller_name, seller_tax_id, amount_without_tax, tax_amount,
                                      total_amount, issue_date, raw_source)
    VALUES (?, '--', '数电发票', 'normal', ?, ?, 0, 0, ?, ?, 'manual_import')
    """, (invoice_number, seller, tax_id, amount, issue_date))
    iid = cur.lastrowid
    conn.commit()
    conn.close()
    return iid


def _categories():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT status_category, COUNT(*) as c FROM reconciliation_records GROUP BY status_category")
    res = {r["status_category"]: r["c"] for r in cur.fetchall()}
    conn.close()
    return res


# ------------------------------------------------------------------------------
# 五态分流
# ------------------------------------------------------------------------------

def test_matched_by_name_and_amount(isolated_db):
    """销方名称关联 + 金额相等 → 真开票配对成功"""
    _add_order("TB001", 100.00, "尊脉旗舰店", invoice_status="applying")
    _add_invoice("INV001", 100.00, "上海尊脉贸易有限公司")
    stats = run_reconciliation()
    assert stats["matched"]["count"] == 1
    assert stats["uninvoiced"]["count"] == 0
    assert _categories().get("matched") == 1


def test_matched_by_sole_amount_pair(isolated_db):
    """店铺名与企业名无字面关联，但同金额双侧唯一 + 开票日不早于下单日 → 配对"""
    _add_order("TB002", 88.88, "天卓办公用品专营店", order_time="2026-10-02 09:00:00")
    _add_invoice("INV002", 88.88, "浙江天卓实业有限公司", issue_date="2026-10-06")
    stats = run_reconciliation()
    assert stats["matched"]["count"] == 1


def test_no_match_when_issue_date_before_order(isolated_db):
    """开票日早于真实下单日 → 禁止配对（防误配）"""
    _add_order("TB003", 66.00, "某旗舰店", invoice_status="applying", order_time="2026-10-10 09:00:00")
    _add_invoice("INV003", 66.00, "无关企业", issue_date="2026-10-01")
    stats = run_reconciliation()
    assert stats["matched"]["count"] == 0
    assert stats["pending_review"]["count"] == 1
    assert stats["uninvoiced"]["count"] == 1


def test_unapplied_vs_uninvoiced(isolated_db):
    """未申请 → unapplied；已申请未配上 → uninvoiced"""
    _add_order("TB004", 10.00, "店A", invoice_status="unapplied")
    _add_order("TB005", 20.00, "店B", invoice_status="applying")
    stats = run_reconciliation()
    assert stats["unapplied"]["count"] == 1
    assert stats["uninvoiced"]["count"] == 1


def test_pending_review_for_unmatched_invoice(isolated_db):
    """票未配上任何订单 → 待审核"""
    _add_invoice("INV004", 300.00, "中国联通")
    stats = run_reconciliation()
    assert stats["pending_review"]["count"] == 1
    assert stats["pending_review"]["amount"] == 300.00


def test_offline_confirmation_persists_and_revokes(isolated_db):
    """人工确认线下留痕保留；confirmed_at 清空后退回待审核"""
    iid = _add_invoice("INV005", 50.00, "山东高速")
    run_reconciliation()

    # 模拟控制台批量确认
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE reconciliation_records SET status_category='offline', status_text='已确认线下支出', confirmed_at='2026-10-17 10:00:00' WHERE invoice_id=?", (iid,))
    conn.commit()
    conn.close()

    stats = run_reconciliation()
    assert stats["offline"]["count"] == 1
    assert stats["pending_review"]["count"] == 0

    # 误勾退回：清空 confirmed_at
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE reconciliation_records SET status_category='pending_review', confirmed_at='' WHERE invoice_id=?", (iid,))
    conn.commit()
    conn.close()

    stats = run_reconciliation()
    assert stats["offline"]["count"] == 0
    assert stats["pending_review"]["count"] == 1


def test_match_updates_order_invoice_no(isolated_db):
    """配上后回写真实发票号与 invoiced 状态"""
    _add_order("TB006", 45.00, "姗艺旗舰店", invoice_status="applying")
    _add_invoice("INV006", 45.00, "义乌姗艺贸易有限公司")
    run_reconciliation()
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT invoice_status, invoice_no FROM procurement_orders WHERE order_no='TB006'")
    row = cur.fetchone()
    conn.close()
    assert row["invoice_status"] == "invoiced"
    assert row["invoice_no"] == "INV006"
