"""
OPC Auto 电子税务局同步服务单元测试 (etax_sync_service)
隔离性：全部用例运行在临时 SQLite 库上，严禁污染真实 data/opc_auto.db
"""

import sys
import os
from datetime import datetime

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest

import src.database as database
from src.database import init_db, get_db_connection
from src.etax_sync_service import EtaxSyncService


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    """重定向数据库至临时目录并初始化空表"""
    db_path = tmp_path / "test_opc_auto.db"
    monkeypatch.setattr(database, "DB_PATH", str(db_path))
    init_db()
    yield str(db_path)


def _service():
    return EtaxSyncService()


# ------------------------------------------------------------------------------
# 列表行归一化 (表头映射，按数电平台真实 18 列布局)
# ------------------------------------------------------------------------------

REAL_HEADERS = ["", "序号", "票种", "发票状态", "数电发票号码", "发票代码", "发票号码", "发票风险等级",
                "购/销方名称", "购/销方识别号", "金额", "税额", "价税合计", "特定业务类型",
                "开票日期", "查询类型", "发票来源", "开票人", "操作"]


def _real_row(no="26372000004879425331", seller="济南有宽餐饮管理合伙企业（有限合伙）",
              amount="2358.49", tax="141.51", total="2500.00", state="正常",
              inv_type="数电发票（普通发票）", date="2026-10-02 16:29:43"):
    return ["", "1", inv_type, state, no, "--", "--", "低", seller, "91370100MA3xxxxx00",
            amount, tax, total, "", date, "取得发票", "电子发票服务平台", "张三", "详情预览下载"]


def test_normalize_row_with_real_layout(isolated_db):
    rows = [_real_row()]
    invoices = _service()._normalize_invoice_rows(REAL_HEADERS, rows)
    assert len(invoices) == 1
    inv = invoices[0]
    assert inv["invoice_number"] == "26372000004879425331"
    assert inv["issue_date"] == "2026-10-02"
    assert "餐饮" in inv["seller_name"]
    assert inv["amount_without_tax"] == 2358.49
    assert inv["tax_amount"] == 141.51
    assert inv["total_amount"] == 2500.00
    assert inv["invoice_type"] == "数电发票（普通发票）"
    assert inv["invoice_state"] == "normal"


def test_normalize_missing_tax_derives_from_total(isolated_db):
    # 税额列缺失时由 价税合计 - 不含税金额 推导
    row = _real_row(tax="")
    invoices = _service()._normalize_invoice_rows(REAL_HEADERS, [row])
    assert invoices[0]["amount_without_tax"] == 2358.49
    assert invoices[0]["tax_amount"] == 141.51
    assert invoices[0]["total_amount"] == 2500.00


def test_normalize_red_state_then_db_dedup(isolated_db):
    # 同票号红冲行归一化各保留，最终由入库层票号 UNIQUE 幂等去重
    rows = [_real_row(no="26372000004879425001", state="已红冲"),
            _real_row(no="26372000004879425001", state="已红冲")]
    invoices = _service()._normalize_invoice_rows(REAL_HEADERS, rows)
    assert len(invoices) == 2
    assert all(inv["invoice_state"] == "red" for inv in invoices)
    assert _service()._import_invoices(invoices) == 1


def test_normalize_drops_rows_without_key_columns(isolated_db):
    rows = [["", "1", "数电发票（普通发票）", "正常", "", "--", "--", "", "某销方", "", "", "", "", "", "", "", "", "", ""],
            _real_row(no="26372000004879425002")]
    invoices = _service()._normalize_invoice_rows(REAL_HEADERS, rows)
    assert len(invoices) == 1
    assert invoices[0]["invoice_number"] == "26372000004879425002"


def test_normalize_no_headers_returns_empty(isolated_db):
    assert _service()._normalize_invoice_rows([], [_real_row()]) == []


# ------------------------------------------------------------------------------
# 入库幂等 (raw_source=etax_sync)
# ------------------------------------------------------------------------------

def test_import_idempotent(isolated_db):
    svc = _service()
    invoices = [{
        "invoice_number": "26372000004879425331",
        "issue_date": "2026-10-02",
        "seller_name": "济南有宽餐饮管理合伙企业（有限合伙）",
        "seller_tax_id": "",
        "amount_without_tax": 2358.49,
        "tax_amount": 141.51,
        "total_amount": 2500.00,
        "invoice_type": "数电普票",
        "invoice_state": "normal",
    }]
    assert svc._import_invoices(invoices) == 1
    # 同票号重复入库必须幂等跳过
    assert svc._import_invoices(invoices) == 0

    conn = get_db_connection()
    row = conn.execute(
        "SELECT * FROM tax_inbound_invoices WHERE invoice_number = ?", ("26372000004879425331",)
    ).fetchone()
    conn.close()
    assert row["raw_source"] == "etax_sync"
    assert row["total_amount"] == 2500.00


# ------------------------------------------------------------------------------
# 同步水位线
# ------------------------------------------------------------------------------

def test_watermark_roundtrip(isolated_db):
    svc = _service()
    assert svc._get_watermark() is None
    svc._set_watermark("2026-10-13")
    assert svc._get_watermark() == "2026-10-13"
    svc._set_watermark("2026-10-14")
    assert svc._get_watermark() == "2026-10-14"
    conn = get_db_connection()
    count = conn.execute("SELECT COUNT(*) AS c FROM sync_watermarks WHERE platform='etax'").fetchone()["c"]
    conn.close()
    assert count == 1  # 单行覆盖，不堆积


# ------------------------------------------------------------------------------
# 调度门控与环境降级
# ------------------------------------------------------------------------------

def _fake_datetime(year, month, day):
    class FakeDatetime(datetime):
        @classmethod
        def now(cls):
            return cls(year, month, day)
    return FakeDatetime


def test_cron_gate_skips_non_sync_days(isolated_db, monkeypatch):
    # 2026-10-15 为周四：cron 触发必须跳过
    monkeypatch.setattr("src.etax_sync_service.datetime", _fake_datetime(2026, 10, 15))
    result = _service().run_sync(trigger_type="cron")
    assert result["status"] == "skipped"


def test_cron_gate_proceeds_on_monday(isolated_db, monkeypatch):
    # 2026-10-12 为周一：cron 触发应放行；强制宿主离线 → 降级为环境未就绪失败，而非 skipped
    monkeypatch.setattr("src.etax_sync_service.datetime", _fake_datetime(2026, 10, 12))
    monkeypatch.setattr(
        "src.browser_harvest_service.HumanoidBrowserHarvester.is_browser_ready", lambda self: False)
    result = _service().run_sync(trigger_type="cron")
    assert result["status"] == "failed"
    assert "9222" in result["message"]


def test_manual_trigger_ignores_gate(isolated_db, monkeypatch):
    # 手动触发不受周几门控限制 (周四也放行)；强制宿主离线验证降级路径
    monkeypatch.setattr("src.etax_sync_service.datetime", _fake_datetime(2026, 10, 15))
    monkeypatch.setattr(
        "src.browser_harvest_service.HumanoidBrowserHarvester.is_browser_ready", lambda self: False)
    result = _service().run_sync(trigger_type="manual")
    assert result["status"] == "failed"
    assert "9222" in result["message"]


# ------------------------------------------------------------------------------
# 登录态探测 (Fake CDP 客户端)
# ------------------------------------------------------------------------------

class FakeCDPClient:
    def __init__(self, page_text):
        self._text = page_text

    def evaluate(self, expression, timeout=8.0):
        return self._text


def _storage_payload(identity: dict, has_tpass: bool, body_text: str = ""):
    """模拟 _check_login 从页面 JS 取回的存储信号 JSON"""
    import json as _json
    return _json.dumps({"identity": identity, "has_tpass_token": has_tpass, "body_text": body_text})


def test_login_probe_logged_in(isolated_db):
    client = FakeCDPClient(_storage_payload(
        {"full_name": "冯旭明", "company_name": "山东卷积分大数据服务有限责任公司"}, True))
    state = _service()._check_login(client)
    assert state["logged_in"] is True
    assert state["company_name"] == "山东卷积分大数据服务有限责任公司"


def test_login_probe_expired(isolated_db):
    client = FakeCDPClient(_storage_payload({}, False, "统一身份认证 请先登录 短信验证码"))
    state = _service()._check_login(client)
    assert state["logged_in"] is False


def test_login_probe_tpass_token_fallback(isolated_db):
    # 身份键缺失但有 tpass 会话令牌且无失效文案 → 降级判定已登录
    client = FakeCDPClient(_storage_payload({}, True, "常用功能 税费缴纳"))
    state = _service()._check_login(client)
    assert state["logged_in"] is True


def test_login_probe_conflict_prefers_fail(isolated_db):
    # 身份缺失 + 页面出现失效文案 + 令牌存在 → 判定失效 (严禁误判已登录)
    client = FakeCDPClient(_storage_payload({}, True, "请先登录 请重新登录"))
    state = _service()._check_login(client)
    assert state["logged_in"] is False
