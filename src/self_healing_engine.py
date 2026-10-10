"""
OPC Auto 自愈与自进化引擎 (Self-Healing & Evolution Engine)
核心职责：
1. RuleEvolutionManager: 动态采集规则库版本化管理，支持多候选回退、热加载与自愈升级
2. SelfHealingEngine: 读取面向 AI 的 Trace 档案，分析 DOM 变异现场切片，
   自动修复漂移选择器并完成规则与代码热升级，实现“今天运行的程序与明天运行的程序不一样”。
"""

import os
import re
import json
import logging
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 规则库持久化存储路径
RULES_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data", "rules_evolution.json"))
TRACE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "temp", "browser_traces"))


# ==============================================================================
# 一、 动态可演化规则库管理器 (Rule Evolution Manager)
# ==============================================================================

class RuleEvolutionManager:
    """
    采购平台采集规则库版本化管理器。
    将易变的前端 DOM 选择器、提取正则与动作链路从业务代码中解耦，支持夜间自愈热升级。
    """
    
    # 内置出厂基线规则 (Initial Baseline)
    DEFAULT_RULES = {
        "version": "1.0.0",
        "last_evolved_at": "2026-10-10T00:00:00",
        "platforms": {
            "taobao": {
                "order_card_selectors": [
                    "div[data-id]",
                    ".js-order-item",
                    ".bought-table-mod__tbody",
                    "div.order-list-item"
                ],
                "order_id_patterns": [
                    "订单号[:：]?\\s*([A-Za-z0-9_-]+)",
                    "data-id=\"([A-Za-z0-9_-]+)\""
                ],
                "pay_amount_patterns": [
                    "￥?\\s*([0-9]+\\.[0-9]{2})",
                    "实付款[:：]?\\s*([0-9]+\\.[0-9]{2})"
                ],
                "store_name_selectors": [
                    ".seller-mod__name",
                    "a[title*='店铺']",
                    ".shop-name",
                    "a.shop-title"
                ],
                "invoice_btn_selectors": [
                    "button.invoice-action",
                    "a[href*='invoice']",
                    "a:contains('发票')",
                    ".invoice-btn"
                ],
                "invoice_keywords": ["查看发票", "申请开票", "发票详情", "补开发票"]
            },
            "jd": {
                "order_card_selectors": [
                    "tbody[id*='parent-']",
                    "tr.tr-th",
                    ".order-tb"
                ],
                "order_id_patterns": [
                    "订单号[:：]?\\s*([0-9]+)"
                ],
                "pay_amount_patterns": [
                    "¥([0-9]+\\.[0-9]{2})"
                ],
                "store_name_selectors": [
                    ".shop-txt",
                    "a.name"
                ],
                "invoice_btn_selectors": [
                    "a[href*='my_invoice']",
                    "a:contains('发票')"
                ],
                "invoice_keywords": ["我的发票", "查看发票", "补开"]
            }
        },
        "evolution_history": []
    }

    def __init__(self, storage_path: str = RULES_PATH):
        self.storage_path = storage_path
        self._ensure_storage()

    def _ensure_storage(self):
        """确保规则文件存在"""
        os.makedirs(os.path.dirname(self.storage_path), exist_ok=True)
        if not os.path.exists(self.storage_path):
            self.save_rules(self.DEFAULT_RULES)

    def load_rules(self) -> Dict[str, Any]:
        """加载当前生效的最新规则"""
        try:
            with open(self.storage_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return self.DEFAULT_RULES

    def save_rules(self, rules: Dict[str, Any]):
        """持久化保存规则"""
        with open(self.storage_path, "w", encoding="utf-8") as f:
            json.dump(rules, f, ensure_ascii=False, indent=2)

    def get_platform_rule(self, platform: str) -> Dict[str, Any]:
        """获取指定平台的当前最新采集规则"""
        rules = self.load_rules()
        return rules.get("platforms", {}).get(platform, self.DEFAULT_RULES["platforms"].get(platform, {}))

    def update_rule_with_evolution(self, platform: str, updated_field: str, new_selector_or_pattern: str, reason: str) -> str:
        """
        演化更新特定平台的某个选择器或正则规则，并记录版本与日志
        """
        rules = self.load_rules()
        plat_rules = rules.setdefault("platforms", {}).setdefault(platform, {})
        target_list = plat_rules.setdefault(updated_field, [])

        # 若新规则已存在且在首位，则无需重复演变
        if target_list and target_list[0] == new_selector_or_pattern:
            return rules["version"]

        # 将新推导的自愈规则插入至优先级首位 (Failover Candidate Chain)
        if new_selector_or_pattern in target_list:
            target_list.remove(new_selector_or_pattern)
        target_list.insert(0, new_selector_or_pattern)

        # 递增补丁版本号
        v_parts = rules.get("version", "1.0.0").split(".")
        new_patch = int(v_parts[-1]) + 1
        new_version = f"{v_parts[0]}.{v_parts[1]}.{new_patch}"
        now_str = datetime.now().isoformat()

        rules["version"] = new_version
        rules["last_evolved_at"] = now_str
        
        # 记录演化日志
        evolution_entry = {
            "version": new_version,
            "timestamp": now_str,
            "platform": platform,
            "field": updated_field,
            "new_rule": new_selector_or_pattern,
            "reason": reason
        }
        rules.setdefault("evolution_history", []).append(evolution_entry)

        self.save_rules(rules)
        return new_version


# ==============================================================================
# 二、 深夜 AI 自诊断与自愈演进引擎 (Self-Healing Engine)
# ==============================================================================

class SelfHealingEngine:
    """
    负责在深夜演化时钟中分析机器级 Trace 档案，
    诊断前端改版特征，自动修复失效选择器并促成代码与规则闭环自进化。
    """
    def __init__(self, rule_manager: Optional[RuleEvolutionManager] = None):
        self.rule_manager = rule_manager or RuleEvolutionManager()

    def find_latest_trace(self, platform: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """在 temp/browser_traces/ 中检索最新的 Trace 诊断档案"""
        if not os.path.exists(TRACE_DIR):
            return None
        files = [os.path.join(TRACE_DIR, f) for f in os.listdir(TRACE_DIR) if f.endswith(".json")]
        if not files:
            return None
        files.sort(key=lambda x: os.path.getmtime(x), reverse=True)
        
        for fp in files:
            try:
                with open(fp, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if not platform or data.get("platform") == platform:
                        return data
            except Exception:
                continue
        return None

    def analyze_and_heal(self, trace_data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        核心自愈诊断入口：
        1. 检查各选择器健康度 (selector_health)；
        2. 若发现零命中 (FAILED_ZERO_HIT)，解析 failure_context 中的局部 DOM 快照；
        3. 利用启发式语义锚定，逆向重构有效的新选择器；
        4. 自动升级规则库，生成自愈演进报告。
        """
        if not trace_data:
            trace_data = self.find_latest_trace()

        report = {
            "engine": "SelfHealingEngine",
            "diagnosed_at": datetime.now().isoformat(),
            "trace_analyzed": trace_data.get("trace_id") if trace_data else "None",
            "platform": trace_data.get("platform", "unknown") if trace_data else "unknown",
            "is_healed": False,
            "diagnoses": [],
            "actions_taken": [],
            "new_rule_version": None
        }

        if not trace_data:
            report["diagnoses"].append("未检测到任何待分析的 Trace 档案")
            return report

        platform = trace_data.get("platform", "taobao")
        selector_health = trace_data.get("selector_health", {})
        failure_ctx = trace_data.get("failure_context") or {}
        html_snippet = failure_ctx.get("snapshot_html_snippet", "")

        # 遍历选择器健康度，探测失效点
        for field, health in selector_health.items():
            status = health.get("status", "")
            current_selector = health.get("selector", "")
            hit_count = health.get("hit_count", 0)

            if status == "FAILED_ZERO_HIT" or hit_count == 0:
                diag_msg = f"检测到字段 [{field}] 原选择器 `{current_selector}` 命中率为 0，判定发生前端变迁或类名混淆。"
                report["diagnoses"].append(diag_msg)

                # 启动启发式自愈推导
                healed_selector = self._infer_new_selector(field, html_snippet)
                if healed_selector:
                    reason = f"自愈引擎根据 DOM 切片推导新选择器，替代失效规则 `{current_selector}`"
                    field_target = "invoice_btn_selectors" if "invoice" in field else "order_card_selectors"
                    
                    new_version = self.rule_manager.update_rule_with_evolution(
                        platform=platform,
                        updated_field=field_target,
                        new_selector_or_pattern=healed_selector,
                        reason=reason
                    )
                    
                    action_msg = f"字段 [{field}] 自愈成功：新优先选择器 `{healed_selector}` 已生效，规则库演进至 v{new_version}"
                    report["actions_taken"].append(action_msg)
                    report["is_healed"] = True
                    report["new_rule_version"] = new_version
                else:
                    report["actions_taken"].append(f"字段 [{field}] 未能从现有切片中推导有效特征，已保留现场切片供更深层代码演化")

        # 若未发现任何零命中选择器
        if not report["diagnoses"]:
            report["diagnoses"].append("所有受监控的选择器健康度处于基线范围 (healthy)，无需触发规则重构")

        return report

    def _infer_new_selector(self, field: str, html_snippet: str) -> Optional[str]:
        """
        基于 HTML 片段进行语义特征逆向工程，推导自愈选择器
        """
        if not html_snippet:
            return None

        # 针对发票相关按钮/入口的自愈识别
        if "invoice" in field:
            # 搜索包含发票语义的标签: <span class="xxx">查看发票</span> 或 <a ...>发票详情</a>
            pattern = r'<([a-zA-Z0-9]+)\s+([^>]*?(?:class=["\']([^"\']+)["\'])?[^>]*?)>\s*([^<]*?(?:发票|开票)[^<]*?)\s*</\1>'
            match = re.search(pattern, html_snippet, re.IGNORECASE)
            if match:
                tag = match.group(1).lower()
                classes = match.group(3)
                if classes:
                    # 选取最具代表性的类名 (避开高频混淆类)
                    class_list = [c for c in classes.split() if not re.match(r'^[a-z0-9]{5,10}$', c)]
                    if class_list:
                        return f"{tag}.{class_list[0]}"
                return f"{tag}:contains('发票')"

        # 针对订单卡片容器的自愈识别
        if "order_card" in field or "container" in field:
            # 查找包裹了订单号的父级容器
            card_pattern = r'<div\s+([^>]*?class=["\']([^"\']*(?:order|bought|trade)[^"\']*)["\'][^>]*)>'
            match = re.search(card_pattern, html_snippet, re.IGNORECASE)
            if match:
                classes = match.group(2)
                first_cls = classes.split()[0] if classes.split() else ""
                if first_cls:
                    return f"div.{first_cls}"

        return None
