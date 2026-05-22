"""uvicorn 起動ロジック。CLI の `accounting serve` から呼ばれる。"""
from __future__ import annotations

import io
import socket
import webbrowser

import qrcode
import uvicorn
from rich.console import Console
from rich.panel import Panel

from accounting.config import settings
from accounting.web.app import create_app
from accounting.web.auth import generate_token

console = Console()


def _detect_lan_ip() -> str:
    """LAN IP を検出する。

    macOS では `gethostbyname(gethostname())` が 127.0.0.1 を返す事があるため、
    UDP socket を外部宛に「接続」して `getsockname()` から取る方法を採る。
    通信は発生しない（UDP は connect だけでは送信しない）。
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "127.0.0.1"


def _qr_ascii(url: str) -> str:
    qr = qrcode.QRCode(border=1)
    qr.add_data(url)
    qr.make(fit=True)
    buf = io.StringIO()
    qr.print_ascii(out=buf, invert=True)
    return buf.getvalue()


def _print_banner(host: str, port: int, token: str) -> tuple[str, str]:
    local_url = f"http://localhost:{port}/?token={token}"
    if host == "0.0.0.0":
        lan_ip = _detect_lan_ip()
        lan_url = f"http://{lan_ip}:{port}/?token={token}"
    else:
        lan_url = f"http://{host}:{port}/?token={token}"

    body = (
        f"[bold]Mac:[/bold]    {local_url}\n"
        f"[bold]iPhone:[/bold] {lan_url}\n\n"
        "Ctrl+C で停止"
    )
    console.print(
        Panel(
            body,
            title="🌐 さとやまコーヒー 月次決算ハブを起動しました",
            border_style="green",
        )
    )
    console.print(_qr_ascii(lan_url))
    return local_url, lan_url


def start_server(host: str, port: int, open_browser: bool) -> None:
    # `.env` の ACCOUNTING_WEB_TOKEN が設定されていれば固定トークンを使う。
    # 設定されていなければ起動毎にランダム生成（従来挙動）。
    token = settings.web_token.strip() or generate_token()
    local_url, _ = _print_banner(host, port, token)

    if open_browser:
        try:
            webbrowser.open(local_url)
        except Exception:
            pass

    app = create_app(auth_token=token)
    uvicorn.run(app, host=host, port=port, log_level="info")
