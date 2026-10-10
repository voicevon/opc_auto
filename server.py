"""
OPC Auto 生产级本地 Web 服务与 REST API 服务器
完全杜绝一切伪造与仿真数据，纯真实 SQLite 数据库驱动
"""

import http.server
import socketserver
import os
import sys
import json
from src.database import init_db, get_db_connection
from src.api_handler import handle_api_request

# 保证在 Windows GBK 控制台下的编码兼容
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

PORT = 8088
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

class RealOpcServerHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=WEB_DIR, **kwargs)

    def do_GET(self):
        if self.path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        if self.path.startswith("/api/"):
            status_code, response_data = handle_api_request("GET", self.path, None)
            self._send_json(status_code, response_data)
        else:
            super().do_GET()

    def do_POST(self):
        if self.path.startswith("/api/"):
            content_length = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(content_length) if content_length > 0 else b""
            status_code, response_data = handle_api_request("POST", self.path, body_bytes)
            self._send_json(status_code, response_data)
        else:
            self.send_response(405)
            self.end_headers()

    def _send_json(self, status_code, data):
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def log_message(self, format, *args):
        # 仅输出 API 访问日志
        msg = format % args
        if "/api/" in msg:
            sys.stdout.write(f"[API] {msg}\n")
            sys.stdout.flush()

def run():
    init_db()
    # 自动将用户在 Wiki 确认的真实开票信息预置入库 (若库中为空)
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT count(*) as count FROM company_billing_profile")
    if c.fetchone()["count"] == 0:
        c.execute("""
        INSERT INTO company_billing_profile (
            company_name, tax_id, address, phone, bank_name, bank_account, invoice_email
        ) VALUES (
            '山东卷积分大数据服务有限责任公司',
            '91370103MA94BUQC2D',
            '山东省济南市市中区万寿路2号 B座501',
            '13306400990',
            '中国工商银行济南历下支行',
            '1602003009200501238',
            '13306400990@163.com'
        )
        """)
        conn.commit()
    conn.close()

    try:
        httpd = socketserver.TCPServer(("", PORT), RealOpcServerHandler)
    except OSError as e:
        if getattr(e, "winerror", None) in (10048, 10013) or e.errno in (13, 48, 98):
            print("\n" + "=" * 55)
            print(f"[提示] 端口 {PORT} 已被占用，OPC Auto 服务已在后台正常运行中！")
            print(f"-> 您可以直接在浏览器中访问: http://localhost:{PORT}")
            print(f"-> 如需重启服务，请先在终端运行 stop.bat，然后再运行 run.bat。")
            print("=" * 55 + "\n")
            sys.stdout.flush()
            return
        raise

    # 成功绑定端口后，启动 24/7 守护定时调度器
    from src.scheduler_service import scheduler_manager
    scheduler_manager.start()

    with httpd:
        url = f"http://localhost:{PORT}"
        print(f"\n=======================================================")
        print(f"[OPC Auto] 真实生产后端与 Web 门户已启动 (24小时服务)！")
        print(f"数据模式: 100% 真实数据 (已绑定真实企业抬头)")
        print(f"后台调度: 已启动守护线程 (可动态配置定时时间)")
        print(f"访问地址: {url}")
        print(f"数据库: opc_auto.db")
        print(f"=======================================================\n")
        sys.stdout.flush()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n服务已停止。")
        finally:
            scheduler_manager.stop()

if __name__ == "__main__":
    run()
