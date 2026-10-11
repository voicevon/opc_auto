"""Trae 每日积分自动领取插件 (Trae IDE 签到)"""

from .claimer import TraeScoreClaimer
from .runner import get_status, run_daily_claim

__all__ = ["TraeScoreClaimer", "get_status", "run_daily_claim"]
