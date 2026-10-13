"""
OPC Auto 每日定时任务统一编排入口 (Trae 外部自动化唯一触发点)
由 Trae 定时自动化每日凌晨调用: python -m src.cron_daily
执行链: 业务采集与对账巡检 -> 控制台健康探针 -> AI 自愈复盘 -> Trae 积分签到
"""

from .audit_service import execute_daily_audit


def run_console_probe():
    """控制台双通道健康探针 (HTTP 静态 + CDP 渲染)，产出 console Trace 供自愈复盘"""
    try:
        from .console_probe_service import ConsoleProbeService
        summary = ConsoleProbeService().run_full_probe()
        print(f"[CronDaily] 控制台探针：{summary['message']}")
        for issue in summary.get("issues", []):
            print(f"   -> {issue}")
    except Exception as probe_err:
        print(f"[CronDaily] 控制台探针异常: {probe_err}")


def run_self_healing():
    """AI 自愈引擎：复盘今日全部 Trace (电商采集 + 控制台探针)，驱动代码与规则自进化"""
    try:
        from .self_healing_engine import SelfHealingEngine
        healer = SelfHealingEngine()
        reports = healer.analyze_all_and_heal()
        if not reports:
            print(f"[CronDaily] 🟡 今日无任何 Trace 诊断档案，自愈复盘无输入。")
            return

        for heal_report in reports:
            platform = heal_report.get("platform", "unknown")
            trace_id = heal_report.get("trace_analyzed")
            triage_path = heal_report.get("triage_path")

            if heal_report.get("is_healed"):
                print(f"[CronDaily] 🚀 [{platform}] 路径一【LLM 代码自愈成功】({trace_id})！规则库演进至新版本: {heal_report.get('new_rule_version')}")
                for action in heal_report.get("actions_taken", []):
                    print(f"   -> {action}")
            elif triage_path == "PATH_2_HUMAN_DECISION":
                brief = heal_report.get("decision_brief") or {}
                print(f"[CronDaily] ⚠️ [{platform}] 路径二【人类决策阻断】({trace_id})：检测到重大业务/风控事件，已自动生成《决策简报》[ID: {brief.get('id')}]，请管理员登录控制台定夺方向！")
            elif heal_report.get("health_status") in ("no_selector_data", "no_trace"):
                print(f"[CronDaily] 🟡 [{platform}] 本轮自愈无诊断输入（{heal_report.get('health_status')}）：{heal_report.get('diagnoses', [''])[0]}")
            elif heal_report.get("health_status") == "env_not_ready":
                print(f"[CronDaily] 🟡 [{platform}] 宿主环境未就绪 ({trace_id})，已阻止选择器自愈：请运行 start_chrome_debug.bat 打开淘宝『已买到的宝贝』页面后重试采集。")
            elif heal_report.get("actions_taken"):
                print(f"[CronDaily] 🔎 [{platform}] 探测到退化项已留存现场 ({trace_id})：")
                for action in heal_report.get("actions_taken", []):
                    print(f"   -> {action}")
            else:
                print(f"[CronDaily] ✅ [{platform}] 前端选择器健康，基线正常 ({trace_id})。")
    except Exception as heal_err:
        print(f"[CronDaily] 自愈演化分析异常: {heal_err}")


def run_score_claim():
    """Trae 每日积分签到 (trae_score 插件，幂等)"""
    try:
        from trae_score.runner import run_daily_claim
        result = run_daily_claim(trigger_type="cron")
        print(f"[CronDaily] Trae 积分签到结果：{result['message']}"
              + (f" (当前积分 {result['credits']})" if result.get("credits") is not None else ""))
    except Exception as score_err:
        print(f"[CronDaily] Trae 积分签到异常: {score_err}")


def main():
    print(f"\n[CronDaily] 触发每日定时任务：开始执行业务采集与巡检...")
    execute_daily_audit(trigger_type="cron")
    print(f"[CronDaily] 业务采集与巡检执行完毕，执行控制台健康探针...")
    run_console_probe()
    print(f"[CronDaily] 控制台探针执行完毕，立即执行 AI 自愈复盘...")
    run_self_healing()
    print(f"[CronDaily] 执行 Trae 每日积分签到...")
    run_score_claim()
    print(f"[CronDaily] 每日定时任务全部执行完毕。\n")


if __name__ == "__main__":
    main()
