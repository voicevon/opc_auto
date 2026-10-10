"""
OPC Auto 诊断分流路由器与人机在环决策引擎 (Diagnostic Router & Human-in-the-Loop Engine)
架构定位：Layer 1 (上层管控与决策分流层)

核心职责：
1. DiagnosticRouter: 消费运行日志与 temp/browser_traces/ 档案，依据 Dual-Path Triage 判定矩阵：
   - 路径一 (PATH_1_LLM_HEALING): 确定性技术退化 (DOM 漂移/选择器 0 命中) ➔ 分流给 LLM 自主修改代码/规则
   - 路径二 (PATH_2_HUMAN_DECISION): 业务变革/风控阻断/架构抉择 ➔ 阻断重试，提炼《决策简报》呈报人类定夺
2. DecisionBriefManager: 《决策简报》持久化与生命周期管理 (包含 3~5 个备选方案及利弊分析)
"""

import os
import json
import logging
from datetime import datetime
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)

# 数据真理源路径
DECISION_BRIEFS_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data", "decision_briefs.json"))


class TriagePath:
    LLM_HEALING = "PATH_1_LLM_HEALING"          # 路径一：确定性技术退化 (交由 LLM 自主修补)
    HUMAN_DECISION = "PATH_2_HUMAN_DECISION"    # 路径二：业务/风控重大决策 (提炼简报呈报人类)
    HEALTHY_BASELINE = "PATH_HEALTHY"           # 基线健康，无需干预


class DecisionBriefManager:
    """决策简报数据管理器 (单源真理，持久化至 data/decision_briefs.json)"""
    def __init__(self, storage_path: str = DECISION_BRIEFS_PATH):
        self.storage_path = storage_path
        self._ensure_storage()

    def _ensure_storage(self):
        os.makedirs(os.path.dirname(self.storage_path), exist_ok=True)
        if not os.path.exists(self.storage_path):
            with open(self.storage_path, "w", encoding="utf-8") as f:
                json.dump([], f, ensure_ascii=False, indent=2)

    def list_briefs(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        try:
            with open(self.storage_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = []
        if status:
            return [b for b in data if b.get("status") == status]
        return data

    def save_brief(self, brief: Dict[str, Any]) -> Dict[str, Any]:
        briefs = self.list_briefs()
        # 避免同一 Trace 相同成因的简报重复堆积
        existing = next((b for b in briefs if b.get("trace_id") == brief.get("trace_id") and b.get("category") == brief.get("category")), None)
        if existing:
            return existing

        briefs.insert(0, brief)
        with open(self.storage_path, "w", encoding="utf-8") as f:
            json.dump(briefs, f, ensure_ascii=False, indent=2)
        return brief

    def resolve_brief(self, brief_id: str, chosen_option_id: str, operator_note: str = "") -> Optional[Dict[str, Any]]:
        briefs = self.list_briefs()
        target = None
        for b in briefs:
            if b.get("id") == brief_id:
                b["status"] = "RESOLVED"
                b["resolved_at"] = datetime.now().isoformat()
                b["chosen_option_id"] = chosen_option_id
                b["operator_note"] = operator_note
                target = b
                break
        if target:
            with open(self.storage_path, "w", encoding="utf-8") as f:
                json.dump(briefs, f, ensure_ascii=False, indent=2)
        return target


class DiagnosticRouter:
    """
    Layer 1 诊断分流路由器 (SSOT & Dual-Path Triage Engine)
    严密遵循 AGENTS.md《人机分工与自愈分流准则》进行路径分流。
    """
    
    # 路径二（人类定夺）关键字模式库
    RISK_CONTROL_KEYWORDS = ["验证码", "滑动验证", "滑块", "安全验证", "异常访问", "风控", "请通过验证", "login_risk", "punish", "sec.taobao.com", "passport.jd.com"]
    LOGIN_EXPIRED_KEYWORDS = ["请先登录", "登录超时", "重新登录", "会话已过期", "扫码登录", "login_expired"]
    BUSINESS_MUTATION_KEYWORDS = ["线下开票", "暂不支持开票", "联系商家客服", "开票额度不足", "专票需提交线下资料", "订单未开具发票通道已关闭"]

    def __init__(self, brief_manager: Optional[DecisionBriefManager] = None):
        self.brief_manager = brief_manager or DecisionBriefManager()

    def evaluate(self, trace_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        核心分流决策算法：
        入参：trace_data 现场切片档案
        输出：分流评估报告 (包含 target_path, reason, decision_brief 等)
        """
        result = {
            "trace_id": trace_data.get("trace_id", "unknown"),
            "platform": trace_data.get("platform", "unknown"),
            "evaluated_at": datetime.now().isoformat(),
            "target_path": TriagePath.HEALTHY_BASELINE,
            "category": "NORMAL",
            "reason": "运行基线指标正常，无须干预",
            "decision_brief": None
        }

        failure_ctx = trace_data.get("failure_context") or {}
        error_msg = (failure_ctx.get("error_message") or "").lower()
        html_snippet = (failure_ctx.get("snapshot_html_snippet") or "").lower()
        suspected_cause = (failure_ctx.get("suspected_cause") or "").lower()
        selector_health = trace_data.get("selector_health") or {}

        # ----------------------------------------------------------------------
        # 1. 优先判定路径二：强风控阻断 / 登录态失效 / 需人类扫码介入
        # ----------------------------------------------------------------------
        combined_text = f"{error_msg} {html_snippet} {suspected_cause}"
        
        is_risk_blocked = any(kw.lower() in combined_text for kw in self.RISK_CONTROL_KEYWORDS)
        is_login_expired = any(kw.lower() in combined_text for kw in self.LOGIN_EXPIRED_KEYWORDS)

        if is_risk_blocked or is_login_expired:
            result["target_path"] = TriagePath.HUMAN_DECISION
            result["category"] = "RISK_OR_AUTH_BLOCKED"
            category_name = "安全风控与人机验证阻断" if is_risk_blocked else "宿主登录会话失效"
            result["reason"] = f"底层检测到平台触发【{category_name}】，严禁自动脚本重试以避免封号，必须交由人类接管定夺。"
            
            # 生成结构化决策简报 (3~4 个备选方案)
            brief = self._create_risk_control_brief(trace_data, category_name)
            result["decision_brief"] = self.brief_manager.save_brief(brief)
            return result

        # ----------------------------------------------------------------------
        # 2. 判定路径二：平台业务流程根本性改变 (无法通过简单改选择器解决)
        # ----------------------------------------------------------------------
        is_biz_mutated = any(kw.lower() in combined_text for kw in self.BUSINESS_MUTATION_KEYWORDS)
        if is_biz_mutated:
            result["target_path"] = TriagePath.HUMAN_DECISION
            result["category"] = "BUSINESS_FLOW_MUTATION"
            result["reason"] = "检测到平台开票业务流程发生模式级变迁（如转为线下客服审批），超出代码自愈范畴，需人类战略定夺。"

            brief = self._create_business_mutation_brief(trace_data)
            result["decision_brief"] = self.brief_manager.save_brief(brief)
            return result

        # ----------------------------------------------------------------------
        # 3. 判定路径一：确定性技术退化 (选择器零命中 / 类名哈希漂移 / DOM 改版)
        # ----------------------------------------------------------------------
        zero_hit_fields = [
            f for f, h in selector_health.items()
            if h.get("status") == "FAILED_ZERO_HIT" or h.get("hit_count") == 0
        ]
        
        if zero_hit_fields or failure_ctx.get("error_type") in ("SelectorNotFound", "DOMDrift"):
            result["target_path"] = TriagePath.LLM_HEALING
            result["category"] = "TECHNICAL_DOM_DRIFT"
            fields_str = ", ".join(zero_hit_fields) if zero_hit_fields else "DOM元素"
            result["reason"] = f"检测到确定性前端技术退化：字段 [{fields_str}] 原选择器失效，符合自愈边界，交由 LLM 代码自愈闭环处理。"
            return result

        # 4. 默认健康
        return result

    def _create_risk_control_brief(self, trace_data: Dict[str, Any], block_type: str) -> Dict[str, Any]:
        """提炼风控/鉴权阻断《决策简报》"""
        trace_id = trace_data.get("trace_id", "trace_unknown")
        platform = trace_data.get("platform", "电商平台")
        now_str = datetime.now().isoformat()
        
        return {
            "id": f"BRIEF-{int(datetime.now().timestamp() * 1000)}",
            "title": f"【风控阻断警报】{platform.upper()} 平台触发{block_type}",
            "category": "RISK_CONTROL_BLOCK",
            "status": "PENDING",
            "trace_id": trace_id,
            "platform": platform,
            "created_at": now_str,
            "phenomenon": f"自动化采集在访问目标页面时遭遇反爬防御或登录凭证过期，页面重定向至验证/登录屏障。",
            "root_cause": "电商风控系统识别到设备指纹异常或会话 Cookie 超出有效生存期（TTL）。",
            "impact": "当前批次采购订单及发票数据采集暂时挂起，该平台当日对账任务延后。",
            "options": [
                {
                    "option_id": "opt_manual_login",
                    "title": "方案 A：人工打开宿主真实浏览器完成扫码/滑块验证",
                    "description": "操作员在本机 Chrome 打开对应平台完成交互，系统随后重新附着复用最新登录态。",
                    "pros": "最安全、零封号风险、直接沿用真人信任链。",
                    "cons": "需要人工在几分钟内协同操作一次。",
                    "is_recommended": True
                },
                {
                    "option_id": "opt_switch_export",
                    "title": "方案 B：切换为财务后台手工导出 Excel 导入模式",
                    "description": "跳过本次拟人化爬取，由财务登录后台导出账单后上传本系统完成核对。",
                    "pros": "不触发任何风控检测，数据完整度高。",
                    "cons": "脱离了全自动采集流程，依赖人工周期性导出。",
                    "is_recommended": False
                },
                {
                    "option_id": "opt_postpone_retry",
                    "title": "方案 C：随机静默延迟 2 小时后低频重试",
                    "description": "暂时休眠，避开高峰期后以更拟人化的高斯抖动延迟再次探活。",
                    "pros": "无需人工干预。",
                    "cons": "若属于永久凭证失效，重试仍可能失败甚至加重风控惩罚。",
                    "is_recommended": False
                }
            ],
            "chosen_option_id": None,
            "resolved_at": None,
            "operator_note": ""
        }

    def _create_business_mutation_brief(self, trace_data: Dict[str, Any]) -> Dict[str, Any]:
        """提炼平台业务流程变迁《决策简报》"""
        trace_id = trace_data.get("trace_id", "trace_unknown")
        platform = trace_data.get("platform", "电商平台")
        now_str = datetime.now().isoformat()

        return {
            "id": f"BRIEF-{int(datetime.now().timestamp() * 1000)}",
            "title": f"【业务变迁提示】{platform.upper()} 发票申请流程发生根本性改变",
            "category": "BUSINESS_FLOW_MUTATION",
            "status": "PENDING",
            "trace_id": trace_id,
            "platform": platform,
            "created_at": now_str,
            "phenomenon": "页面提示线上开票渠道已关闭或变更，原有的‘申请开票’按钮被客服工单或线下流程替代。",
            "root_cause": "平台规则改版，部分类目或商户停止支持线上直开电子发票，转为线下审批或邮件开票。",
            "impact": "无法通过自动化选择器点击直接完成开票申请，原发票催开状态机闭环受阻。",
            "options": [
                {
                    "option_id": "opt_reconcile_by_tax",
                    "title": "方案 A：降级为【纯电税局底账逆向匹配】模式",
                    "description": "平台端停止催票动作，直接依靠国家电子税局每日同步的进项发票与采购订单金额反向交叉对账。",
                    "pros": "完全摆脱电商前端业务流程改版的影响，以税局权威数据为准。",
                    "cons": "若商家延迟开票，无法在平台端自动发催促消息。",
                    "is_recommended": True
                },
                {
                    "option_id": "opt_im_chat_urge",
                    "title": "方案 B：研发平台旺旺/客服 IM 自动催票子流程",
                    "description": "改造采集器，点击‘联系卖家’唤起网页聊天窗口，发送标准化催票开票话术与企业抬头税号。",
                    "pros": "能够覆盖必须人工找商家客服的订单。",
                    "cons": "开发成本高，聊天窗口交互复杂度与风控风险较高。",
                    "is_recommended": False
                },
                {
                    "option_id": "opt_mark_manual_todo",
                    "title": "方案 C：打标为【待人工线下开票】异常待办",
                    "description": "在前端对账仪表盘中生成异常待办任务，由财务人员手动联系商户索要发票。",
                    "pros": "业务处理最稳妥，责任划分清晰。",
                    "cons": "增加了少量人工待办工作量。",
                    "is_recommended": False
                }
            ],
            "chosen_option_id": None,
            "resolved_at": None,
            "operator_note": ""
        }
