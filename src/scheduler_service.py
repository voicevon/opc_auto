"""
OPC Auto 后台定时调度器 (24/7 Built-in Background Scheduler)
支持动态配置执行时间 (如 19:00, 21:00)，独立守护线程运行
"""

import threading
import time
from datetime import datetime, timedelta
from .database import get_db_connection
from .audit_service import execute_daily_audit

# 全局调度配置与状态
class SchedulerManager:
    def __init__(self):
        self.lock = threading.Lock()
        self.is_running = False
        self.worker_thread = None
        self._init_config_table()

    def _init_config_table(self):
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS scheduler_config (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            enabled INTEGER DEFAULT 1,
            trigger_time TEXT NOT NULL DEFAULT '21:00',
            last_run_date TEXT DEFAULT '',
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
        # 确保有一条默认记录
        cursor.execute("SELECT count(*) as c FROM scheduler_config")
        if cursor.fetchone()["c"] == 0:
            cursor.execute("INSERT INTO scheduler_config (enabled, trigger_time) VALUES (1, '21:00')")
        conn.commit()
        conn.close()

    def get_config(self):
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM scheduler_config ORDER BY id DESC LIMIT 1")
        row = cursor.fetchone()
        conn.close()
        if not row:
            return {"enabled": True, "trigger_time": "21:00", "last_run_date": "", "next_run": "21:00"}
        
        cfg = dict(row)
        cfg["enabled"] = bool(cfg["enabled"])
        
        # 计算下次执行时间
        now = datetime.now()
        target_h, target_m = map(int, cfg["trigger_time"].split(":"))
        target_dt = now.replace(hour=target_h, minute=target_m, second=0, microsecond=0)
        if target_dt <= now:
            target_dt += timedelta(days=1)
        cfg["next_run_full"] = target_dt.strftime("%Y-%m-%d %H:%M:%S")
        return cfg

    def update_config(self, enabled: bool, trigger_time: str):
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
        UPDATE scheduler_config 
        SET enabled = ?, trigger_time = ?, updated_at = CURRENT_TIMESTAMP
        WHERE id = (SELECT id FROM scheduler_config ORDER BY id DESC LIMIT 1)
        """, (1 if enabled else 0, trigger_time.strip()))
        conn.commit()
        conn.close()
        return self.get_config()

    def start(self):
        with self.lock:
            if self.is_running:
                return
            self.is_running = True
            self.worker_thread = threading.Thread(target=self._run_loop, daemon=True)
            self.worker_thread.start()

    def stop(self):
        with self.lock:
            self.is_running = False

    def _run_loop(self):
        """24小时不间断常驻轮询检测：支持业务采集钟与深夜自愈演化钟"""
        while self.is_running:
            try:
                cfg = self.get_config()
                if cfg["enabled"]:
                    now = datetime.now()
                    now_hm = now.strftime("%H:%M")
                    today_str = now.strftime("%Y-%m-%d")

                    # 1. 业务时钟：执行日常采集与对账巡检 (默认 20:00/21:00)
                    if now_hm == cfg["trigger_time"] and cfg.get("last_run_date") != today_str:
                        print(f"\n[Scheduler] 到达预定时间 {cfg['trigger_time']}，开始自动执行业务采集与巡检...")
                        execute_daily_audit(trigger_type="cron")
                        
                        conn = get_db_connection()
                        cursor = conn.cursor()
                        cursor.execute("""
                        UPDATE scheduler_config SET last_run_date = ? 
                        WHERE id = (SELECT id FROM scheduler_config ORDER BY id DESC LIMIT 1)
                        """, (today_str,))
                        conn.commit()
                        conn.close()
                        print(f"[Scheduler] 业务采集与巡检执行完毕，已输出面向 AI 的 Trace 档案。")

                    # 2. 自愈时钟：深夜 00:00 (午夜 12 点) 自动分析当晚 Trace，驱动代码与规则自进化
                    heal_time = "00:00"
                    if now_hm == heal_time and cfg.get("last_heal_date") != today_str:
                        print(f"\n[Scheduler] 到达深夜演化时间 {heal_time}，AI 自愈引擎开始复盘当晚 Trace 档案...")
                        try:
                            from .self_healing_engine import SelfHealingEngine
                            healer = SelfHealingEngine()
                            heal_report = healer.analyze_and_heal()
                            if heal_report.get("is_healed"):
                                print(f"[Scheduler] 🚀 自愈成功！规则库演进至新版本: {heal_report.get('new_rule_version')}")
                                for action in heal_report.get("actions_taken", []):
                                    print(f"   -> {action}")
                            else:
                                print(f"[Scheduler] 前端选择器健康，基线正常。")
                        except Exception as heal_err:
                            print(f"[Scheduler] 自愈演化分析异常: {heal_err}")

                        # 记录自愈时钟已执行
                        try:
                            conn = get_db_connection()
                            cursor = conn.cursor()
                            cursor.execute("ALTER TABLE scheduler_config ADD COLUMN last_heal_date TEXT DEFAULT ''")
                            conn.commit()
                        except Exception:
                            pass
                        
                        conn = get_db_connection()
                        cursor = conn.cursor()
                        cursor.execute("""
                        UPDATE scheduler_config SET last_heal_date = ?
                        WHERE id = (SELECT id FROM scheduler_config ORDER BY id DESC LIMIT 1)
                        """, (today_str,))
                        conn.commit()
                        conn.close()
            except Exception as e:
                print(f"[Scheduler] 调度器轮询异常: {e}")
            
            # 每隔 15 秒低消耗检测一次
            time.sleep(15)

scheduler_manager = SchedulerManager()
