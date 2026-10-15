"""
OPC Auto 真实数据持久化模块 (SQLite 引擎)
严格遵循：无任何 mock/仿真数据，真实生产级结构
"""

import sqlite3
import os
import json
from datetime import datetime

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
DB_PATH = os.path.join(DATA_DIR, "opc_auto.db")

def get_db_connection():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """初始化真实数据库表结构，确保全新启动时为干净、真实的空表"""
    conn = get_db_connection()
    cursor = conn.cursor()

    # 1. 公司法定真实开票信息表 (企业抬头配置)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS company_billing_profile (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_name TEXT NOT NULL DEFAULT '',
        tax_id TEXT NOT NULL DEFAULT '',
        address TEXT NOT NULL DEFAULT '',
        phone TEXT NOT NULL DEFAULT '',
        bank_name TEXT NOT NULL DEFAULT '',
        bank_account TEXT NOT NULL DEFAULT '',
        invoice_email TEXT NOT NULL DEFAULT '',
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # 2. 真实采购订单表 (覆盖淘宝、京东、拼多多、线下采购)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS procurement_orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        channel TEXT NOT NULL,                -- taobao / jd / pdd / offline
        channel_name TEXT NOT NULL,           -- 显示名称如 "淘宝企业购"
        order_no TEXT NOT NULL UNIQUE,        -- 真实业务订单号
        order_time TEXT NOT NULL,             -- 下单时间 (YYYY-MM-DD HH:MM:SS)
        item_desc TEXT NOT NULL,              -- 商品/项目明细摘要
        amount REAL NOT NULL,                 -- 交易实付金额 (元)
        seller_name TEXT NOT NULL,            -- 平台店铺名称 / 商户名称
        seller_tax_id TEXT DEFAULT '',        -- 商家税号 (若已知)
        invoice_status TEXT DEFAULT 'unapplied', -- unapplied(未申请) / applying(申请中) / invoiced(已开票)
        invoice_no TEXT DEFAULT '',           -- 关联开出的发票号码
        apply_time TEXT DEFAULT '',           -- 提交开票申请时间
        notes TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # 3. 真实电子税务局进项发票底账表 (数电发票)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS tax_inbound_invoices (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        invoice_code TEXT DEFAULT '',         -- 发票代码 (数电票可能为空或20位票号)
        invoice_number TEXT NOT NULL UNIQUE,  -- 真实发票号码 (全网唯一)
        issue_date TEXT NOT NULL,             -- 开票日期 (YYYY-MM-DD)
        seller_tax_id TEXT NOT NULL,          -- 销方纳税人识别号
        seller_name TEXT NOT NULL,            -- 销方企业全称 (法定工商名)
        amount_without_tax REAL NOT NULL,     -- 不含税金额
        tax_amount REAL NOT NULL,             -- 税额
        total_amount REAL NOT NULL,           -- 价税合计金额
        invoice_type TEXT DEFAULT '数电专票', -- 数电专票 / 数电普票 / 传统专票
        invoice_state TEXT DEFAULT 'normal',  -- normal(正常) / red(已红冲) / cancelled(作废)
        raw_source TEXT DEFAULT 'etax_sync',  -- etax_sync(税局同步) / manual_import(导入)
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # 4. 真实智能对账核验记录表 (四状态分流)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS reconciliation_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER,                     -- 外键，可为 NULL (线下票据无订单)
        invoice_id INTEGER,                   -- 外键，可为 NULL (未开票订单无发票)
        status_category TEXT NOT NULL,        -- matched(状态④) / pending(状态②) / warning(状态③) / offline(状态①)
        status_text TEXT NOT NULL,            -- 中文描述
        diff_amount REAL DEFAULT 0.0,         -- 金额差异
        urge_count INTEGER DEFAULT 0,         -- 催票次数
        last_urge_time TEXT DEFAULT '',       -- 最近一次催票时间
        reconciled_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (order_id) REFERENCES procurement_orders(id),
        FOREIGN KEY (invoice_id) REFERENCES tax_inbound_invoices(id)
    )
    """)

    # 5. 真实每日巡检日志与总结报告表
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS daily_reports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        report_date TEXT NOT NULL UNIQUE,     -- 报告日期 YYYY-MM-DD
        total_orders INTEGER DEFAULT 0,
        total_invoices INTEGER DEFAULT 0,
        matched_count INTEGER DEFAULT 0,
        matched_amount REAL DEFAULT 0.0,
        pending_count INTEGER DEFAULT 0,
        pending_amount REAL DEFAULT 0.0,
        warning_count INTEGER DEFAULT 0,
        warning_amount REAL DEFAULT 0.0,
        offline_count INTEGER DEFAULT 0,
        offline_amount REAL DEFAULT 0.0,
        markdown_content TEXT NOT NULL,       -- 生成的完整 Markdown 日报
        generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # 6. 系统自动化执行日志表
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS audit_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        trigger_type TEXT NOT NULL,           -- manual(手动) / cron(定时)
        status TEXT NOT NULL,                 -- running / success / failed
        message TEXT NOT NULL,
        details TEXT DEFAULT '',
        executed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # 7. 平台同步水位线表 (单一真理源：各采集平台最近一次成功同步日期)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS sync_watermarks (
        platform TEXT PRIMARY KEY,            -- etax(电子税务局) / taobao 等
        last_sync_date TEXT NOT NULL,         -- 最近一次成功同步的业务日期 (YYYY-MM-DD)
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    conn.commit()
    conn.close()

if __name__ == "__main__":
    init_db()
    print("真实数据库初始化完成，路径:", DB_PATH)
