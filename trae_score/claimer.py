"""
Trae 每日积分自动领取插件 - 核心签到模块
原理：只读解密本地登录态 (%APPDATA%\\Trae CN\\User\\globalStorage\\storage.json)，
直连官方签到 API (api.trae.cn)，无需打开客户端、无需模拟点击。
幂等设计：今日已签到自动跳过；令牌仅在内存中使用，绝不落日志。
"""

import base64
import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

# ---- Trae 客户端协议常量 (单一真理源) ----
AUTH_KEY = "iCubeAuthInfo://icube.cloudide"
API_BASE = "https://api.trae.cn/trae/api/v2/ug/checkin_credits"
REQ_SOURCE = 1  # 1=Trae CN IDE; 2=SOLO/企业版 (同账号积分互通，取 1)

# iCube 登录态解密常量 (与 Trae 客户端加密格式同源)
_HDR_LEN = 6
_KEY_LEN = 32
_HMAC_LEN = 64
_URE = bytes([82, 9, 106, 213, 48, 54, 165, 56, 191, 64, 163, 158, 129, 243, 215, 251,
              124, 227, 57, 130, 155, 47, 255, 135, 52, 142, 67, 68, 196, 222, 233, 203,
              84, 123, 148, 50, 166, 194, 35, 61, 238, 76, 149, 11, 66, 250, 195, 78,
              8, 46, 161, 102, 40, 217, 36, 178, 118, 91, 162, 73, 109, 139, 209, 37])
_DRE = bytes([31, 221, 168, 51, 136, 7, 199, 49, 177, 18, 16, 89, 39, 128, 236, 95,
              96, 81, 127, 169, 25, 181, 74, 13, 45, 229, 122, 159, 147, 201, 156, 239,
              160, 224, 59, 77, 174, 42, 245, 176, 200, 235, 187, 60, 131, 83, 153, 97,
              23, 43, 4, 126, 186, 119, 214, 38, 225, 105, 20, 99, 85, 33, 12, 125])

# 候选客户端数据目录 (扫描顺序即优先级)
APP_DIRS = ["Trae CN", "TRAE SOLO CN"]


class TraeScoreClaimer:
    """Trae IDE 每日积分签到器：解密登录态 → 查询状态 → 领取积分"""

    def __init__(self):
        self.account_name = ""
        self.token = ""
        self.region = ""
        self.device_id = ""

    # ---------- 登录态解密 ----------

    @staticmethod
    def _decrypt_auth(b64_text: str) -> dict:
        """解密 iCubeAuthInfo 加密串，返回含 token / userRegion 的 dict"""
        raw = base64.b64decode(b64_text)
        key = raw[_HDR_LEN:_HDR_LEN + _KEY_LEN]
        xor = bytes(a ^ b for a, b in zip(_URE, _DRE))
        derived = hashlib.sha512(hashlib.sha512(key).digest() + xor).digest()
        aes_key, iv = derived[:16], derived[16:32]
        cipher_text = raw[_HDR_LEN + _KEY_LEN:]
        plain = unpad(AES.new(aes_key, AES.MODE_CBC, iv).decrypt(cipher_text), AES.block_size)
        return json.loads(plain[_HMAC_LEN:].decode("utf-8"))

    def load_login_state(self) -> bool:
        """扫描本地客户端登录态，解密 token。成功返回 True"""
        appdata = os.environ.get("APPDATA", "")
        for name in APP_DIRS:
            storage_path = Path(appdata) / name / "User" / "globalStorage" / "storage.json"
            if not storage_path.exists():
                continue
            try:
                storage = json.loads(storage_path.read_text(encoding="utf-8"))
                encrypted = storage.get(AUTH_KEY)
                if not encrypted:
                    continue
                auth = self._decrypt_auth(encrypted)
                token = auth.get("token")
                if not token:
                    continue
                self.account_name = name
                self.token = token
                self.region = (auth.get("userRegion") or {}).get("region") or ""
                return True
            except Exception:
                continue
        return False

    def load_device_id(self) -> bool:
        """设备 ID：storage.json 的 icube-dc 键 → ahanet 网络配置 兜底"""
        appdata = os.environ.get("APPDATA", "")
        for name in APP_DIRS:
            storage_path = Path(appdata) / name / "User" / "globalStorage" / "storage.json"
            if not storage_path.exists():
                continue
            try:
                storage = json.loads(storage_path.read_text(encoding="utf-8"))
                for key in storage:
                    match = re.match(r"iCubeAuthInfo://icube-dc:(\d+)$", key)
                    if match:
                        self.device_id = match.group(1)
                        return True
            except Exception:
                continue
        for name in APP_DIRS:
            config_path = Path(appdata) / name / "ahanet" / "tt_net_config.config"
            try:
                content = config_path.read_text(encoding="utf-8", errors="ignore")
                match = re.search(r"device_id&#\*(\d+)", content)
                if match:
                    self.device_id = match.group(1)
                    return True
            except OSError:
                continue
        return False

    # ---------- 官方签到 API ----------

    def _post(self, action: str) -> dict:
        headers = {
            "Authorization": f"Cloud-IDE-JWT {self.token}",
            "Content-Type": "application/json",
            "x-device-id": self.device_id,
        }
        if self.region:
            headers["X-User-Region"] = self.region
        response = requests.post(
            f"{API_BASE}/{action}",
            headers=headers,
            json={"req_source": REQ_SOURCE},
            timeout=30,
        )
        response.raise_for_status()
        return response.json()

    @staticmethod
    def _unwrap(resp: dict) -> dict:
        """兼容 {checked_in:...} 与 {code:0, data:{checked_in:...}} 两种返回结构"""
        if "checked_in" not in resp and isinstance(resp.get("data"), dict) and "checked_in" in resp["data"]:
            return resp["data"]
        return resp

    def query_status(self) -> dict:
        """查询今日签到状态与当前积分余额"""
        return self._unwrap(self._post("status"))

    def claim(self) -> dict:
        """领取今日积分"""
        return self._post("claim")

    # ---------- 高层编排 ----------

    def run(self) -> dict:
        """执行一次幂等签到，返回结构化结果 (绝不包含令牌)"""
        result = {
            "ok": False,
            "claimed": False,
            "credits": None,
            "account": "",
            "message": "",
            "checked_at": datetime.now().isoformat(),
        }

        if not self.load_login_state():
            result["message"] = "无可用登录态，请打开 Trae CN 客户端并登录一次"
            result["needs_login"] = True
            return result
        if not self.load_device_id():
            result["message"] = "未能提取设备 ID"
            return result

        result["account"] = self.account_name

        try:
            status = self.query_status()
        except requests.HTTPError as err:
            code = err.response.status_code if err.response is not None else "?"
            result["message"] = f"查询签到状态失败 (HTTP {code})，登录态可能过期，请打开客户端重新登录"
            result["needs_login"] = True
            return result
        except Exception as err:
            result["message"] = f"网络错误：{err}"
            return result

        if status.get("checked_in"):
            result["ok"] = True
            result["credits"] = status.get("credits")
            result["message"] = "今日已签到"
            return result
        if not status.get("enable"):
            result["message"] = "签到功能不可用：" + json.dumps(status, ensure_ascii=False)[:200]
            return result

        try:
            claim = self.claim()
        except Exception as err:
            result["message"] = f"领取积分失败：{err}"
            return result

        if claim.get("code") == 0:
            try:
                result["credits"] = self.query_status().get("credits")
            except Exception:
                pass
            result["ok"] = True
            result["claimed"] = True
            result["message"] = "签到成功"
            return result

        result["message"] = "签到失败：" + str(claim.get("message") or json.dumps(claim, ensure_ascii=False)[:200])
        return result
