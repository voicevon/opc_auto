"""
OPC Auto 后台定时调度器 (24/7 Built-in Background Scheduler)
支持动态配置执行时间 (如 19:00, 21:00)，独立守护线程运行
"""

import os
import json
import threading
import time
from datetime import datetime, timedelta
from typing import Optional
from .audit_service import execute_daily_audit

# 单一真理源 (SSOT) 配置文件路径
SCHEDULER_CONFIG_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data", "scheduler_config.json"))

class SchedulerManager:
    """
    后台定时双时钟调度管理器。
    彻底摒弃散落硬编码，完全基于 data/scheduler_config.json 单一真理源驱动。
    """
    def __init__(self, config_path: str = SCHEDULER_CONFIG_PATH):
        self.lock = threading.Lock()
        self.config_path = config_path
        self.is_running = False
        self.worker_thread = None
        self._ensure_config_exists()

    def _ensure_config_exists(self):
        """确保真理源文件就绪"""
        if not os.path.exists(self.config_path):
            default_cfg = {
                "enabled": True,
                "harvest_trigger_time": "01:00",
                "heal_trigger_time": "05:00",
                "preset_options": [
                    {"label": "凌晨 01:00 (推荐低峰)", "time": "01:00"},
                    {"label": "晚上 20:00 (8点)", "time": "20:00"},
                    {"label": "晚上 21:00 (9点)", "time": "21:00"}
                ],
                "last_harvest_date": "",
                "last_heal_date": "",
                "updated_at": datetime.now().isoformat()
            }
            os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(default_cfg, f, ensure_ascii=False, indent=2)

    def get_config(self):
        """从真理源读取配置并动态计算下次触发时序"""
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            self._ensure_config_exists()
            with open(self.config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)

        # 动态计算下次业务采集触发时间
        harvest_time = cfg.get("harvest_trigger_time", "01:00")
        now = datetime.now()
        target_h, target_m = map(int, harvest_time.split(":"))
        target_dt = now.replace(hour=target_h, minute=target_m, second=0, microsecond=0)
        if target_dt <= now:
            target_dt += timedelta(days=1)
        cfg["next_run_full"] = target_dt.strftime("%Y-%m-%d %H:%M:%S")
        cfg["trigger_time"] = harvest_time  # 向前兼容旧前端字段
        return cfg

    def update_config(self, enabled: bool, harvest_time: str, heal_time: Optional[str] = None):
        """更新真理源配置"""
        with self.lock:
            cfg = self.get_config()
            cfg["enabled"] = bool(enabled)
            cfg["harvest_trigger_time"] = harvest_time.strip()
            if heal_time:
                cfg["heal_trigger_time"] = heal_time.strip()
            cfg["updated_at"] = datetime.now().isoformat()

            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            return self.get_config()

    def record_run(self, run_type: str, date_str: str):
        """记录最后执行时间至真理源"""
        with self.lock:
            cfg = self.get_config()
            if run_type == "harvest":
                cfg["last_harvest_date"] = date_str
            elif run_type == "heal":
                cfg["last_heal_date"] = date_str
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)

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
        """24小时不间断常驻轮询检测：完全依照真理源时钟设定驱动"""
        while self.is_running:
            try:
                cfg = self.get_config()
                if cfg.get("enabled", True):
                    now = datetime.now()
                    now_hm = now.strftime("%H:%M")
                    today_str = now.strftime("%Y-%m-%d")

                    harvest_target = cfg.get("harvest_trigger_time", "01:00")
                    heal_target = cfg.get("heal_trigger_time", "05:00")

                    # 1. 业务时钟：执行日常采集与对账巡检 (具备休眠与启动补偿能力)
                    if (now_hm >= harvest_target) and (cfg.get("last_harvest_date") != today_str):
                        print(f"\n[Scheduler] 触发真理源采集任务 (设定: {harvest_target}, 当前: {now_hm})，开始自动执行业务采集与巡检...")
                        execute_daily_audit(trigger_type="cron")
                        self.record_run("harvest", today_str)
                        print(f"[Scheduler] 业务采集与巡检执行完毕，已输出面向 AI 的 Trace 档案。")

                    # 2. 自愈时钟：复盘分析 Trace，驱动代码与规则自进化
                    if (now_hm >= heal_target) and (cfg.get("last_heal_date") != today_str):
                        print(f"\n[Scheduler] 触发真理源自愈任务 (设定: {heal_target}, 当前: {now_hm})，AI 自愈引擎开始复盘分析上一次运行的 Trace 档案...")
                        try:
                            from .self_healing_engine import SelfHealingEngine
                            healer = SelfHealingEngine()
                            heal_report = healer.analyze_and_heal()
                            triage_path = heal_report.get("triage_path")

                            if heal_report.get("is_healed"):
                                print(f"[Scheduler] 🚀 路径一【LLM 代码自愈成功】！规则库演进至新版本: {heal_report.get('new_rule_version')}")
                                for action in heal_report.get("actions_taken", []):
                                    print(f"   -> {action}")
                            elif triage_path == "PATH_2_HUMAN_DECISION":
                                brief = heal_report.get("decision_brief") or {}
                                print(f"[Scheduler] ⚠️ 路径二【人类决策阻断】：检测到重大业务/风控事件，已自动生成《决策简报》[ID: {brief.get('id')}]，请管理员登录控制台定夺方向！")
                            else:
                                print(f"[Scheduler] 前端选择器健康，基线正常。")
                        except Exception as heal_err:
                            print(f"[Scheduler] 自愈演化分析异常: {heal_err}")

                        self.record_run("heal", today_str)
            except Exception as e:
                print(f"[Scheduler] 调度器轮询异常: {e}")
            
            # 每隔 15 秒低消耗检测一次
            time.sleep(15)

scheduler_manager = SchedulerManager()
