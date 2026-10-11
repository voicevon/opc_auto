"""
Trae 每日积分自动领取插件 - 运行入口
职责：编排一次签到任务并将结果落盘至 trae_score/data/trae_score_status.json (单一真理源)，
供调度器第三时钟 (score_trigger_time) 与 Web 门户共同消费。
"""

import json
import os
import sys
from datetime import datetime

if __package__ in (None, ""):
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
    from trae_score.claimer import TraeScoreClaimer
else:
    from .claimer import TraeScoreClaimer

STATUS_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "data", "trae_score_status.json"))


def _load_status() -> dict:
    if os.path.exists(STATUS_PATH):
        try:
            with open(STATUS_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"history": []}


def _save_status(status: dict):
    os.makedirs(os.path.dirname(STATUS_PATH), exist_ok=True)
    with open(STATUS_PATH, "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=2)


def run_daily_claim(trigger_type: str = "manual") -> dict:
    """执行一次签到并把结果写入状态真理源。幂等：同日重复运行安全。"""
    claimer = TraeScoreClaimer()
    result = claimer.run()
    result["trigger_type"] = trigger_type
    result["today"] = datetime.now().strftime("%Y-%m-%d")

    status = _load_status()
    history = status.get("history", [])
    if history and history[-1].get("today") == result["today"] and not result["claimed"]:
        # 今日已有记录且本次未新领积分 (已签到/失败)，仅更新最新状态，不重复堆叠历史
        history[-1] = result
    else:
        history.append(result)
    status["history"] = history[-30:]  # 保留最近 30 天
    status["last_result"] = result
    _save_status(status)
    return result


def get_status() -> dict:
    """读取签到状态真理源 (供 API 层与 Web 门户消费)"""
    return _load_status()


if __name__ == "__main__":
    outcome = run_daily_claim(trigger_type="manual")
    print(f"[trae_score] {outcome['message']}"
          + (f"，当前积分：{outcome['credits']}" if outcome.get("credits") is not None else ""))
    sys.exit(0 if outcome["ok"] else 1)
