# -*- coding: utf-8 -*-
"""二維碼生成器 啟動器
- 開啟啟動器 = 啟動伺服器；關閉啟動器 = 停止伺服器
- 可勾選「開機自動啟動」（寫入 HKCU Run，不需系統管理員）
"""
import json
import os
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.request
import webbrowser
import winreg
from pathlib import Path

BASE_DIR = Path(__file__).parent
PYTHON = sys.executable.replace("pythonw.exe", "python.exe")
PYTHONW = PYTHON.replace("python.exe", "pythonw.exe")
PORT = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))["server"]["port"]
LOG_FILE = BASE_DIR / "qr_server.log"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "QRTool"
AUTOSTART_MODE = "--autostart" in sys.argv

server_proc = None


def port_in_use() -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(1)
    try:
        return s.connect_ex(("127.0.0.1", PORT)) == 0
    finally:
        s.close()


def kill_port_owner():
    """接管：把占用連接埠的舊伺服器關掉（本機唯一用這個埠的就是本程式）"""
    try:
        out = subprocess.check_output(
            ["netstat", "-ano", "-p", "tcp"], text=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[1].endswith(f":{PORT}") and parts[3] == "LISTENING":
                subprocess.run(
                    ["taskkill", "/F", "/PID", parts[4]], capture_output=True,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
        time.sleep(1)
    except Exception:
        pass


def start_server():
    global server_proc
    if port_in_use():
        kill_port_owner()
    env = {**os.environ, "QR_NO_BROWSER": "1"}
    log = open(LOG_FILE, "w", encoding="utf-8")
    server_proc = subprocess.Popen(
        [PYTHON, str(BASE_DIR / "app.py")],
        cwd=BASE_DIR, env=env,
        stdout=log, stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )


def stop_server():
    global server_proc
    if server_proc and server_proc.poll() is None:
        server_proc.terminate()
        try:
            server_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server_proc.kill()
    server_proc = None


def get_status():
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/status", timeout=3) as r:
            return json.loads(r.read())
    except Exception:
        return None


def get_autostart() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, RUN_NAME)
        return True
    except FileNotFoundError:
        return False


def set_autostart(on: bool):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            cmd = f'"{PYTHONW}" "{BASE_DIR / "launcher.py"}" --autostart'
            winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, cmd)
        else:
            try:
                winreg.DeleteValue(k, RUN_NAME)
            except FileNotFoundError:
                pass


# ---------- GUI ----------
BG = "#111827"
CARD = "#1f2937"
TEXT = "#f3f4f6"
DIM = "#9ca3af"
BLUE = "#3b82f6"
GREEN = "#22c55e"
RED = "#f87171"

root = tk.Tk()
root.title("二維碼生成器 啟動器")
root.geometry("430x330")
root.resizable(False, False)
root.configure(bg=BG)

tk.Label(root, text="📦 二維碼生成器", font=("Microsoft JhengHei", 15, "bold"),
         bg=BG, fg=TEXT).pack(pady=(18, 4))

status_var = tk.StringVar(value="啟動中…")
status_lbl = tk.Label(root, textvariable=status_var, font=("Microsoft JhengHei", 11),
                      bg=BG, fg=DIM)
status_lbl.pack()

url_frame = tk.Frame(root, bg=CARD)
url_frame.pack(fill="x", padx=24, pady=12)
lan_var = tk.StringVar(value="")
remote_var = tk.StringVar(value="")
tk.Label(url_frame, textvariable=lan_var, font=("Consolas", 10), bg=CARD, fg=TEXT,
         anchor="w").pack(fill="x", padx=12, pady=(10, 2))
tk.Label(url_frame, textvariable=remote_var, font=("Consolas", 10), bg=CARD, fg=TEXT,
         anchor="w").pack(fill="x", padx=12, pady=(0, 10))

btn_frame = tk.Frame(root, bg=BG)
btn_frame.pack(pady=6)


def open_web():
    webbrowser.open(f"http://localhost:{PORT}/")


tk.Button(btn_frame, text="🌐 開啟網頁", font=("Microsoft JhengHei", 11, "bold"),
          bg=BLUE, fg="white", relief="flat", padx=18, pady=6,
          cursor="hand2", command=open_web).pack(side="left", padx=6)


def restart_server():
    status_var.set("重新啟動中…")
    status_lbl.config(fg=DIM)
    stop_server()
    start_server()


tk.Button(btn_frame, text="↻ 重新啟動", font=("Microsoft JhengHei", 11),
          bg=CARD, fg=TEXT, relief="flat", padx=18, pady=6,
          cursor="hand2", command=restart_server).pack(side="left", padx=6)

auto_var = tk.BooleanVar(value=get_autostart())


def toggle_autostart():
    try:
        set_autostart(auto_var.get())
    except Exception as e:
        status_var.set(f"設定失敗: {e}")


tk.Checkbutton(root, text="開機自動啟動", variable=auto_var, command=toggle_autostart,
               font=("Microsoft JhengHei", 11), bg=BG, fg=TEXT,
               activebackground=BG, activeforeground=TEXT,
               selectcolor=CARD, cursor="hand2").pack(pady=6)

tk.Label(root, text="關閉此視窗 = 停止程式（手機也會連不上）",
         font=("Microsoft JhengHei", 9), bg=BG, fg=DIM).pack(pady=(2, 0))


def poll():
    """每 2 秒更新狀態"""
    if server_proc is None or server_proc.poll() is not None:
        status_var.set("⛔ 伺服器已停止（按「重新啟動」）")
        status_lbl.config(fg=RED)
        lan_var.set("")
        remote_var.set("")
    else:
        s = get_status()
        if s:
            status_var.set("✅ 執行中" + ("　⚠ 資料庫未連線" if not s.get("db_ok") else ""))
            status_lbl.config(fg=GREEN if s.get("db_ok") else RED)
            lan_var.set(f"手機(同網路)  http://{s['lan_ip']}:{s['port']}/")
            remote_var.set(f"遠端(公司/4G) {s['remote_url'] or '未開通'}")
        else:
            status_var.set("啟動中…")
            status_lbl.config(fg=DIM)
    root.after(2000, poll)


def on_close():
    stop_server()
    root.destroy()


root.protocol("WM_DELETE_WINDOW", on_close)

start_server()
if not AUTOSTART_MODE:
    threading.Timer(2.0, open_web).start()  # 手動開啟時自動開網頁；開機自啟不開
poll()
root.mainloop()
