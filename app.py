# -*- coding: utf-8 -*-
"""二維碼生成器 — 網頁伺服器（電腦 + 手機共用）"""
import base64
import hashlib
import hmac
import io
import json
import os
import socket
import struct
import time
from pathlib import Path

import psycopg2
import qrcode
import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image
from pydantic import BaseModel

try:
    from pillow_heif import register_heif_opener
    register_heif_opener()  # 支援 iPhone HEIC 照片
except ImportError:
    pass

BASE_DIR = Path(__file__).parent
CONFIG = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))
DB = CONFIG["db"]
PORT = CONFIG["server"]["port"]
REMOTE = CONFIG.get("remote", {})
PIN = str(REMOTE.get("pin", "")).strip()
COOKIE_VAL = hashlib.sha256(f"qr_tool:{PIN}".encode()).hexdigest() if PIN else ""
PORTMAP = {"wan_ip": None, "ok": False, "err": None}

# ---------- 防暴力破解 ----------
LOCK_SECONDS = int(os.environ.get("QR_LOCK_SECONDS", "300"))  # 測試時可用環境變數縮短
SEC_FILE = BASE_DIR / "security.json"
SEC = {
    "fail_count": 0,
    "lock_until": 0.0,
    "remote_blocked": False,
    "last_fail_ip": None,
    "blocked_at": None,
}
try:
    _saved = json.loads(SEC_FILE.read_text(encoding="utf-8"))
    SEC["remote_blocked"] = bool(_saved.get("remote_blocked"))
    SEC["blocked_at"] = _saved.get("blocked_at")
    SEC["last_fail_ip"] = _saved.get("last_fail_ip")
except FileNotFoundError:
    pass
except Exception as e:
    print(f"security.json 讀取失敗: {e}")


def _save_sec():
    try:
        SEC_FILE.write_text(
            json.dumps(
                {
                    "remote_blocked": SEC["remote_blocked"],
                    "blocked_at": SEC["blocked_at"],
                    "last_fail_ip": SEC["last_fail_ip"],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except Exception as e:
        print(f"security.json 寫入失敗: {e}")

app = FastAPI(title="二維碼生成器")

UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

DB_ERROR = None  # 資料庫連線失敗時的錯誤訊息


def get_conn(dbname=None):
    return psycopg2.connect(
        host=DB["host"],
        port=DB["port"],
        dbname=dbname or DB["dbname"],
        user=DB["user"],
        password=DB["password"],
        connect_timeout=5,
    )


def init_db():
    """建立資料庫與資料表（不存在才建立）"""
    global DB_ERROR
    try:
        try:
            conn = get_conn()
        except psycopg2.OperationalError as e:
            if 'database "%s" does not exist' % DB["dbname"] in str(e) or "不存在" in str(e):
                tmp = get_conn("postgres")
                tmp.autocommit = True
                with tmp.cursor() as cur:
                    cur.execute('CREATE DATABASE "%s"' % DB["dbname"])
                tmp.close()
                conn = get_conn()
            else:
                raise
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS qr_records (
                    id SERIAL PRIMARY KEY,
                    content TEXT NOT NULL UNIQUE,
                    times INTEGER NOT NULL DEFAULT 1,
                    is_favorite BOOLEAN NOT NULL DEFAULT FALSE,
                    location TEXT NOT NULL DEFAULT '',
                    stack_base INTEGER NOT NULL DEFAULT 0,
                    stack_height INTEGER NOT NULL DEFAULT 0,
                    pieces_per_box INTEGER NOT NULL DEFAULT 0,
                    qr_type TEXT NOT NULL DEFAULT 'goods',
                    created_at TIMESTAMP NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'qr_records'"
            )
            qr_cols = {r[0] for r in cur.fetchall()}
            if "is_favorite" not in qr_cols:
                cur.execute(
                    "ALTER TABLE qr_records "
                    "ADD COLUMN is_favorite BOOLEAN NOT NULL DEFAULT FALSE"
                )
            if qr_cols and "location" not in qr_cols:
                cur.execute(
                    "ALTER TABLE qr_records "
                    "ADD COLUMN location TEXT NOT NULL DEFAULT ''"
                )
            if qr_cols and "stack_base" not in qr_cols:  # 堆疊：底 × 高
                cur.execute(
                    "ALTER TABLE qr_records "
                    "ADD COLUMN stack_base INTEGER NOT NULL DEFAULT 0, "
                    "ADD COLUMN stack_height INTEGER NOT NULL DEFAULT 0"
                )
            if qr_cols and "qr_type" not in qr_cols:  # 貨物 / 位置 二維碼
                cur.execute(
                    "ALTER TABLE qr_records "
                    "ADD COLUMN qr_type TEXT NOT NULL DEFAULT 'goods'"
                )
            if qr_cols and "pieces_per_box" not in qr_cols:  # 件/箱
                cur.execute(
                    "ALTER TABLE qr_records "
                    "ADD COLUMN pieces_per_box INTEGER NOT NULL DEFAULT 0"
                )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS qr_images (
                    id SERIAL PRIMARY KEY,
                    record_id INTEGER NOT NULL
                        REFERENCES qr_records(id) ON DELETE CASCADE,
                    filename TEXT NOT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS return_records (
                    id SERIAL PRIMARY KEY,
                    content TEXT NOT NULL,
                    qty_ctn NUMERIC(12,2) NOT NULL DEFAULT 0,
                    qty_pcs NUMERIC(12,2) NOT NULL DEFAULT 0,
                    pcs_unit TEXT NOT NULL DEFAULT 'PCS',
                    qty_kg NUMERIC(12,2) NOT NULL DEFAULT 0,
                    has_rt BOOLEAN NOT NULL DEFAULT FALSE,
                    location TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMP NOT NULL DEFAULT NOW()
                )
                """
            )
            # 舊版結構自動遷移
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'return_records'"
            )
            cols = {r[0] for r in cur.fetchall()}
            if "qty_pkt" in cols:  # PKT 改名 PCS（數值保留）
                cur.execute(
                    "ALTER TABLE return_records RENAME COLUMN qty_pkt TO qty_pcs"
                )
            if cols and "pcs_unit" not in cols:  # 中間欄位單位可自訂
                cur.execute(
                    "ALTER TABLE return_records "
                    "ADD COLUMN pcs_unit TEXT NOT NULL DEFAULT 'PCS'"
                )
            if cols and "has_rt" not in cols:  # RT 勾選 + 回倉位置
                cur.execute(
                    "ALTER TABLE return_records "
                    "ADD COLUMN has_rt BOOLEAN NOT NULL DEFAULT FALSE, "
                    "ADD COLUMN location TEXT NOT NULL DEFAULT ''"
                )
            if "qty_ctn" not in cols:
                cur.execute(
                    "ALTER TABLE return_records "
                    "ADD COLUMN qty_ctn NUMERIC(12,2) NOT NULL DEFAULT 0, "
                    "ADD COLUMN qty_pcs NUMERIC(12,2) NOT NULL DEFAULT 0, "
                    "ADD COLUMN qty_kg NUMERIC(12,2) NOT NULL DEFAULT 0"
                )
            if "unit" in cols:
                cur.execute(
                    "UPDATE return_records SET "
                    "qty_ctn = CASE WHEN unit='CTN' THEN qty ELSE 0 END, "
                    "qty_pcs = CASE WHEN unit IN ('PKT','PCS') THEN qty ELSE 0 END, "
                    "qty_kg  = CASE WHEN unit='KG'  THEN qty ELSE 0 END"
                )
                cur.execute(
                    "ALTER TABLE return_records DROP COLUMN qty, DROP COLUMN unit"
                )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS return_images (
                    id SERIAL PRIMARY KEY,
                    record_id INTEGER NOT NULL
                        REFERENCES return_records(id) ON DELETE CASCADE,
                    filename TEXT NOT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS return_day_images (
                    id SERIAL PRIMARY KEY,
                    day DATE NOT NULL,
                    filename TEXT NOT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS account_records (
                    id SERIAL PRIMARY KEY,
                    label TEXT NOT NULL DEFAULT '',
                    username TEXT NOT NULL,
                    password TEXT NOT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS sameday_images (
                    id SERIAL PRIMARY KEY,
                    day DATE NOT NULL,
                    filename TEXT NOT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS route_records (
                    id SERIAL PRIMARY KEY,
                    name TEXT NOT NULL,
                    content TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMP NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS route_images (
                    id SERIAL PRIMARY KEY,
                    record_id INTEGER NOT NULL
                        REFERENCES route_records(id) ON DELETE CASCADE,
                    filename TEXT NOT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT NOW()
                )
                """
            )
        conn.close()
        DB_ERROR = None
    except Exception as e:
        DB_ERROR = str(e)


def make_qr_png(text: str) -> bytes:
    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=2,
    )
    qr.add_data(text)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def get_ips() -> dict:
    """偵測各網卡 IP。回傳 {"lan": 區網IP, "ts": Tailscale IP}
    跳過 VPN(Surfshark 10.14.x)、link-local(169.254)，區網優先 192.168.x"""
    ips = set()
    try:
        for a in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(a[4][0])
    except Exception:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:
        pass

    lan_candidates = {"192.168.": [], "172.": [], "10.": []}
    ts = None
    for ip in sorted(ips):
        if ip.startswith("127.") or ip.startswith("169.254."):
            continue
        parts = ip.split(".")
        if ip.startswith("100.") and 64 <= int(parts[1]) <= 127:
            ts = ip  # Tailscale (CGNAT 100.64-127.x)
        elif ip.startswith("192.168."):
            lan_candidates["192.168."].append(ip)
        elif ip.startswith("172.") and 16 <= int(parts[1]) <= 31:
            lan_candidates["172."].append(ip)
        elif ip.startswith("10."):
            lan_candidates["10."].append(ip)

    lan = None
    for prefix in ("192.168.", "172.", "10."):
        if lan_candidates[prefix]:
            lan = lan_candidates[prefix][0]
            break
    return {"lan": lan or "127.0.0.1", "ts": ts}


def _gateway_ip() -> str:
    gw = REMOTE.get("gateway", "").strip()
    if gw:
        return gw
    return get_ips()["lan"].rsplit(".", 1)[0] + ".1"


def natpmp_map():
    """用 NAT-PMP 請路由器開 8100 埠，成功回傳公網 IP"""
    gw = _gateway_ip()

    def req(payload, sz):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(3)
        try:
            s.sendto(payload, (gw, 5351))
            return s.recvfrom(sz + 16)[0]
        finally:
            s.close()

    try:
        d = req(struct.pack("!BB", 0, 0), 12)
        if struct.unpack("!BBHI", d[:8])[2] != 0:
            raise RuntimeError("公網IP查詢失敗")
        wan = ".".join(str(b) for b in d[8:12])
        d = req(struct.pack("!BBHHHI", 0, 2, 0, PORT, PORT, 7200), 16)
        result = struct.unpack("!BBHIHHI", d[:16])[2]
        if result != 0:
            raise RuntimeError(f"路由器拒絕開門 (result={result})")
        PORTMAP.update(wan_ip=wan, ok=True, err=None)
        return wan
    except Exception as e:
        PORTMAP.update(ok=False, err=str(e))
        return None


def _is_private(ip: str) -> bool:
    if ip in ("127.0.0.1", "::1", "localhost", ""):
        return True
    if ip.startswith(("192.168.", "10.")):
        return True
    if ip.startswith("172."):
        parts = ip.split(".")
        return len(parts) > 1 and parts[1].isdigit() and 16 <= int(parts[1]) <= 31
    return False


LOGIN_HTML = """<!DOCTYPE html>
<html lang="zh-Hant"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>二維碼生成器 — 登入</title>
<style>
body{font-family:"Microsoft JhengHei","Segoe UI",sans-serif;background:#f4f6f9;
display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}
.box{background:#fff;border:1px solid #e5e7eb;border-radius:16px;padding:36px 30px;
text-align:center;box-shadow:0 4px 12px rgba(0,0,0,.08);max-width:320px;width:88%}
h1{font-size:1.2rem;color:#1f2937;margin:0 0 20px}
input{width:100%;box-sizing:border-box;font-size:1.5rem;text-align:center;letter-spacing:8px;
padding:12px;border:2px solid #e5e7eb;border-radius:10px;margin-bottom:16px}
input:focus{outline:none;border-color:#2563eb}
button{width:100%;background:#2563eb;color:#fff;border:none;border-radius:10px;
padding:13px;font-size:1.05rem;cursor:pointer}
button:hover{background:#1d4ed8}
#msg{color:#dc2626;font-size:.85rem;min-height:20px;margin-top:10px}
</style></head><body>
<div class="box">
<h1>🔒 二維碼生成器</h1>
<input id="pin" type="password" inputmode="numeric" placeholder="PIN 碼" autofocus>
<button onclick="go()">登入</button>
<div id="msg"></div>
</div>
<script>
async function go(){
  const pin=document.getElementById('pin').value.trim();
  if(!pin)return;
  const r=await fetch('/api/login',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({pin})});
  if(r.ok){location.reload();}
  else{document.getElementById('msg').textContent='PIN 碼錯誤';
    document.getElementById('pin').value='';}
}
document.getElementById('pin').addEventListener('keydown',e=>{if(e.key==='Enter')go();});
</script></body></html>"""


BLOCKED_HTML = """<!DOCTYPE html>
<html lang="zh-Hant"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>遠端連線已封鎖</title>
<style>body{font-family:"Microsoft JhengHei",sans-serif;background:#f4f6f9;display:flex;
align-items:center;justify-content:center;min-height:100vh;margin:0}
.box{background:#fff;border:1px solid #fca5a5;border-radius:16px;padding:36px 30px;
text-align:center;max-width:340px;width:88%}
h1{font-size:1.1rem;color:#b91c1c;margin:0 0 12px}p{color:#6b7280;font-size:.9rem}</style>
</head><body><div class="box"><h1>🚫 遠端連線已封鎖</h1>
<p>因 PIN 碼連續輸入錯誤次數過多，遠端連線已被封鎖。<br>請在家中電腦上解除封鎖。</p>
</div></body></html>"""


@app.middleware("http")
async def pin_guard(request: Request, call_next):
    if PIN:
        client_ip = request.client.host if request.client else ""
        if not _is_private(client_ip):
            if SEC["remote_blocked"]:
                if request.url.path.startswith("/api/"):
                    return JSONResponse(
                        {"detail": "遠端連線已封鎖，請在電腦上解除"}, status_code=403
                    )
                return HTMLResponse(BLOCKED_HTML, status_code=403)
            if request.url.path != "/api/login":
                cookie = request.cookies.get("qr_auth", "")
                if not hmac.compare_digest(cookie, COOKIE_VAL):
                    if request.url.path.startswith("/api/"):
                        return JSONResponse({"detail": "需要登入"}, status_code=401)
                    return HTMLResponse(LOGIN_HTML)
    return await call_next(request)


class ClientLog(BaseModel):
    msg: str


@app.post("/api/clientlog")
def client_log(body: ClientLog, request: Request):
    """前端錯誤/診斷回報，寫入伺服器日誌"""
    ip = request.client.host if request.client else "?"
    print(f"[前端 {ip}] {body.msg[:500]}", flush=True)
    return {"ok": True}


class GenerateBody(BaseModel):
    text: str
    location: str = ""
    base: int = 0
    height: int = 0
    ppb: int = 0  # 件/箱
    qr_type: str = "goods"


def _valid_qr_type(v: str) -> str:
    v = (v or "goods").strip().lower()
    if v not in ("goods", "location"):
        raise HTTPException(400, "類型必須是 goods 或 location")
    return v


def _clamp_stack(v: int, name: str) -> int:
    if not (0 <= v <= 99999):
        raise HTTPException(400, f"{name} 數值超出範圍")
    return v


class LoginBody(BaseModel):
    pin: str


@app.post("/api/login")
def login(body: LoginBody, request: Request):
    now = time.time()
    if SEC["remote_blocked"]:
        raise HTTPException(403, "遠端連線已封鎖，請在電腦上解除")
    if now < SEC["lock_until"]:
        remain = int(SEC["lock_until"] - now)
        raise HTTPException(429, f"錯誤次數過多，請於 {remain} 秒後再試")

    if PIN and hmac.compare_digest(body.pin.strip(), PIN):
        SEC["fail_count"] = 0
        SEC["lock_until"] = 0.0
        resp = JSONResponse({"ok": True})
        resp.set_cookie(
            "qr_auth", COOKIE_VAL,
            max_age=90 * 24 * 3600, httponly=True, samesite="lax",
        )
        return resp

    # 答錯 — 只統計外部來源（家中網路不會觸發封鎖）
    client_ip = request.client.host if request.client else ""
    if _is_private(client_ip):
        raise HTTPException(401, "PIN 碼錯誤")
    SEC["fail_count"] += 1
    SEC["last_fail_ip"] = client_ip
    if SEC["fail_count"] >= 10:
        SEC["remote_blocked"] = True
        SEC["blocked_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _save_sec()
        raise HTTPException(403, "錯誤次數過多，遠端連線已封鎖，請在電腦上解除")
    if SEC["fail_count"] == 5:
        SEC["lock_until"] = now + LOCK_SECONDS
        raise HTTPException(429, f"已連續錯誤 5 次，暫停 {LOCK_SECONDS // 60} 分鐘")
    remain = (10 if SEC["fail_count"] > 5 else 5) - SEC["fail_count"]
    raise HTTPException(401, f"PIN 碼錯誤（剩 {remain} 次機會）")


@app.post("/api/unlock_remote")
def unlock_remote(request: Request):
    client_ip = request.client.host if request.client else ""
    if not _is_private(client_ip):
        raise HTTPException(403, "只能在家中電腦或網路上解除")
    SEC.update(remote_blocked=False, fail_count=0, lock_until=0.0, blocked_at=None)
    _save_sec()
    return {"ok": True}


NO_CACHE = {"Cache-Control": "no-cache, must-revalidate"}


@app.get("/")
def index():
    return FileResponse(BASE_DIR / "static" / "index.html", headers=NO_CACHE)


@app.get("/api/status")
def status():
    init_db() if DB_ERROR else None  # 之前失敗的話重試一次
    ips = get_ips()
    return {
        "db_ok": DB_ERROR is None,
        "db_error": DB_ERROR,
        "lan_ip": ips["lan"],
        "remote_url": (
            f"http://{PORTMAP['wan_ip']}:{PORT}" if PORTMAP["ok"] else None
        ),
        "remote_blocked": SEC["remote_blocked"],
        "blocked_at": SEC["blocked_at"],
        "last_fail_ip": SEC["last_fail_ip"],
        "port": PORT,
    }


@app.post("/api/generate")
def generate(body: GenerateBody):
    content = body.text.strip().upper()
    if not content:
        raise HTTPException(400, "內容不能為空")

    loc = body.location.strip().upper()[:50]
    base = _clamp_stack(body.base, "底")
    height = _clamp_stack(body.height, "高")
    ppb = _clamp_stack(body.ppb, "件/箱")
    qr_type = _valid_qr_type(body.qr_type)
    png = make_qr_png(content)
    record = {
        "id": None, "content": content, "times": None, "created_at": None,
        "is_favorite": False, "location": loc,
        "stack_base": base, "stack_height": height,
        "pieces_per_box": ppb, "qr_type": qr_type, "images": [],
    }

    if DB_ERROR is None:
        try:
            conn = get_conn()
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO qr_records
                        (content, location, stack_base, stack_height,
                         pieces_per_box, qr_type)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (content) DO UPDATE
                        SET created_at = NOW(), times = qr_records.times + 1,
                            location = CASE WHEN EXCLUDED.location <> ''
                                THEN EXCLUDED.location
                                ELSE qr_records.location END,
                            stack_base = CASE WHEN EXCLUDED.stack_base > 0
                                THEN EXCLUDED.stack_base
                                ELSE qr_records.stack_base END,
                            stack_height = CASE WHEN EXCLUDED.stack_height > 0
                                THEN EXCLUDED.stack_height
                                ELSE qr_records.stack_height END,
                            pieces_per_box = CASE WHEN EXCLUDED.pieces_per_box > 0
                                THEN EXCLUDED.pieces_per_box
                                ELSE qr_records.pieces_per_box END
                    RETURNING id, content, times, created_at, is_favorite,
                              location, stack_base, stack_height,
                              pieces_per_box, qr_type
                    """,
                    (content, loc, base, height, ppb, qr_type),
                )
                row = cur.fetchone()
                cur.execute(
                    "SELECT filename FROM qr_images WHERE record_id = %s ORDER BY id",
                    (row[0],),
                )
                imgs = [r[0] for r in cur.fetchall()]
                record = {
                    "id": row[0],
                    "content": row[1],
                    "times": row[2],
                    "created_at": row[3].strftime("%Y-%m-%d %H:%M:%S"),
                    "is_favorite": row[4],
                    "location": row[5],
                    "stack_base": row[6],
                    "stack_height": row[7],
                    "pieces_per_box": row[8],
                    "qr_type": row[9],
                    "images": imgs,
                }
            conn.close()
        except Exception as e:
            record["db_error"] = str(e)

    record["qr"] = "data:image/png;base64," + base64.b64encode(png).decode()
    return record


QR_SELECT = """
    SELECT id, content, times, created_at, is_favorite, location,
           stack_base, stack_height, pieces_per_box, qr_type,
           COALESCE(
               (SELECT array_agg(i.filename ORDER BY i.id)
                FROM qr_images i WHERE i.record_id = qr_records.id),
               '{}'
           )
    FROM qr_records
"""


def _like_pattern(q: str) -> str:
    """跳脫 ILIKE 萬用字元，讓含 _ / % / \\ 的貨號能精確搜尋"""
    q = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return "%" + q.upper() + "%"


def _fuzzy_pattern(q: str) -> str:
    """字元序模糊搜尋：S198 / SEA198 都能匹配 SEAFZN0198"""
    parts = []
    for ch in q.upper():
        if ch in ("\\", "%", "_"):
            ch = "\\" + ch
        parts.append(ch)
    return "%" + "%".join(parts) + "%"


def _qr_row_dict(r):
    return {
        "id": r[0],
        "content": r[1],
        "times": r[2],
        "created_at": r[3].strftime("%Y-%m-%d %H:%M:%S"),
        "is_favorite": r[4],
        "location": r[5],
        "stack_base": r[6],
        "stack_height": r[7],
        "pieces_per_box": r[8],
        "qr_type": r[9],
        "images": list(r[10]),
    }


@app.get("/api/records")
def records(q: str = "", fav: bool = False):
    if DB_ERROR is not None:
        return {"db_ok": False, "records": [], "returns": []}
    conn = get_conn()
    returns_matches = []
    with conn.cursor() as cur:
        where = []
        params = []
        if q:
            # 精確子字串 + 字元序模糊（S198 → SEAFZN0198）
            where.append("(content ILIKE %s OR content ILIKE %s)")
            params += [_like_pattern(q), _fuzzy_pattern(q)]
        if fav:
            where.append("is_favorite")
        sql = QR_SELECT
        if where:
            sql += " WHERE " + " AND ".join(where)
        if q:
            sql += " ORDER BY (content ILIKE %s) DESC, created_at DESC LIMIT 300"
            params.append(_like_pattern(q))
        else:
            sql += " ORDER BY created_at DESC LIMIT 300"
        cur.execute(sql, params)
        rows = cur.fetchall()
        if q and not fav:
            # 同時搜尋退貨記錄
            cur.execute(
                """
                SELECT id, content, qty_ctn, qty_pcs, pcs_unit, qty_kg, created_at
                FROM return_records
                WHERE content ILIKE %s OR content ILIKE %s
                ORDER BY (content ILIKE %s) DESC, created_at DESC LIMIT 100
                """,
                (_like_pattern(q), _fuzzy_pattern(q), _like_pattern(q)),
            )
            returns_matches = [
                {
                    "id": r[0],
                    "content": r[1],
                    "ctn": float(r[2]),
                    "pcs": float(r[3]),
                    "pcs_unit": r[4],
                    "kg": float(r[5]),
                    "date": r[6].strftime("%Y-%m-%d"),
                    "created_at": r[6].strftime("%Y-%m-%d %H:%M:%S"),
                }
                for r in cur.fetchall()
            ]
    conn.close()
    return {
        "db_ok": True,
        "records": [_qr_row_dict(r) for r in rows],
        "returns": returns_matches,
    }


@app.get("/api/records/lookup")
def lookup_record(content: str = ""):
    """精確查詢主頁二維碼（退貨輸入時即時顯示舊位置用）"""
    if DB_ERROR is not None:
        return {"found": False}
    c = content.strip().upper()
    if not c:
        return {"found": False}
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT location, stack_base, stack_height, pieces_per_box "
            "FROM qr_records WHERE content = %s",
            (c,),
        )
        row = cur.fetchone()
    conn.close()
    if not row:
        return {"found": False}
    return {
        "found": True,
        "location": row[0],
        "base": row[1],
        "height": row[2],
        "ppb": row[3],
    }


class FavBody(BaseModel):
    on: bool


class LocBody(BaseModel):
    location: str = ""
    base: int = 0
    height: int = 0
    ppb: int = 0
    qr_type: str = "goods"


@app.post("/api/records/{record_id}/location")
def set_location(record_id: int, body: LocBody):
    """事後補加／修改二維碼的位置、堆疊、件/箱與類型（留空／0 即清除）"""
    if DB_ERROR is not None:
        raise HTTPException(503, "資料庫未連線")
    loc = body.location.strip().upper()[:50]
    base = _clamp_stack(body.base, "底")
    height = _clamp_stack(body.height, "高")
    ppb = _clamp_stack(body.ppb, "件/箱")
    qr_type = _valid_qr_type(body.qr_type)
    conn = get_conn()
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE qr_records SET location = %s, stack_base = %s, "
            "stack_height = %s, pieces_per_box = %s, qr_type = %s WHERE id = %s",
            (loc, base, height, ppb, qr_type, record_id),
        )
        updated = cur.rowcount
    conn.close()
    if not updated:
        raise HTTPException(404, "找不到記錄")
    return {
        "ok": True, "location": loc, "base": base,
        "height": height, "ppb": ppb, "qr_type": qr_type,
    }


@app.post("/api/records/{record_id}/favorite")
def set_favorite(record_id: int, body: FavBody):
    if DB_ERROR is not None:
        raise HTTPException(503, "資料庫未連線")
    conn = get_conn()
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE qr_records SET is_favorite = %s WHERE id = %s",
            (body.on, record_id),
        )
        updated = cur.rowcount
    conn.close()
    if not updated:
        raise HTTPException(404, "找不到記錄")
    return {"ok": True, "is_favorite": body.on}


@app.post("/api/records/{record_id}/images")
def add_qr_images(record_id: int, files: list[UploadFile] = File(...)):
    """為二維碼記錄加上貨物相片（交易式：失敗整批回滾並清檔）"""
    if DB_ERROR is not None:
        raise HTTPException(503, "資料庫未連線")
    conn = get_conn()
    images = []
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM qr_records WHERE id = %s", (record_id,))
            if not cur.fetchone():
                raise HTTPException(404, "找不到記錄")
            for i, f in enumerate(files):
                if not f.filename:
                    continue
                rel = _save_upload(f"q{record_id}", i, f)
                images.append(rel)
                cur.execute(
                    "INSERT INTO qr_images (record_id, filename) VALUES (%s, %s)",
                    (record_id, rel),
                )
            cur.execute(
                "SELECT filename FROM qr_images WHERE record_id = %s ORDER BY id",
                (record_id,),
            )
            all_images = [r[0] for r in cur.fetchall()]
        conn.commit()
    except Exception:
        conn.rollback()
        for rel in images:
            try:
                (UPLOAD_DIR / rel).unlink(missing_ok=True)
            except OSError:
                pass
        raise
    finally:
        conn.close()
    return {"ok": True, "images": all_images}


@app.delete("/api/qrimg")
def delete_qr_image(record_id: int, filename: str):
    if DB_ERROR is not None:
        raise HTTPException(503, "資料庫未連線")
    conn = get_conn()
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM qr_images WHERE record_id = %s AND filename = %s",
            (record_id, filename),
        )
        deleted = cur.rowcount
    conn.close()
    if not deleted:
        raise HTTPException(404, "找不到相片")
    try:
        (UPLOAD_DIR / filename).unlink(missing_ok=True)
    except OSError as e:
        print(f"刪除相片檔案失敗 {filename}: {e}")
    return {"ok": True}


@app.get("/api/records/{record_id}/qr")
def record_qr(record_id: int, download: bool = False):
    if DB_ERROR is not None:
        raise HTTPException(503, "資料庫未連線")
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT content FROM qr_records WHERE id = %s", (record_id,))
        row = cur.fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "找不到記錄")
    png = make_qr_png(row[0])
    headers = {}
    if download:
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in row[0])[:50]
        headers["Content-Disposition"] = f'attachment; filename="{safe}.png"'
    return Response(png, media_type="image/png", headers=headers)


@app.delete("/api/records/{record_id}")
def delete_record(record_id: int):
    if DB_ERROR is not None:
        raise HTTPException(503, "資料庫未連線")
    conn = get_conn()
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(
            "SELECT filename FROM qr_images WHERE record_id = %s", (record_id,)
        )
        for (rel,) in cur.fetchall():
            try:
                (UPLOAD_DIR / rel).unlink(missing_ok=True)
            except OSError as e:
                print(f"刪除相片失敗 {rel}: {e}")
        cur.execute("DELETE FROM qr_records WHERE id = %s", (record_id,))
        deleted = cur.rowcount
    conn.close()
    if not deleted:
        raise HTTPException(404, "找不到記錄")
    return {"ok": True}


def _sync_qr_record(cur, content: str, location: str):
    """退貨記錄的二維碼同步到主頁歷史記錄（位置以最新為準，未填則保留原位置）"""
    cur.execute(
        """
        INSERT INTO qr_records (content, location) VALUES (%s, %s)
        ON CONFLICT (content) DO UPDATE
            SET created_at = NOW(), times = qr_records.times + 1,
                location = CASE WHEN EXCLUDED.location <> ''
                    THEN EXCLUDED.location
                    ELSE qr_records.location END
        """,
        (content, location),
    )


def _parse_qty(value: str, name: str) -> float:
    value = (value or "").strip()
    if not value:
        return 0.0
    try:
        v = round(float(value), 2)
        # 上限對齊 NUMERIC(12,2)；NaN 的比較為 False 也會在這裡被擋下
        if not (0 <= v <= 9_999_999_999.99):
            raise ValueError
        return v
    except ValueError:
        raise HTTPException(400, f"{name} 數量格式錯誤或超出範圍")


def _save_upload(prefix: str, index: int, upload: UploadFile) -> str:
    """壓縮並儲存上傳照片，回傳相對路徑"""
    subdir = time.strftime("%Y%m")
    (UPLOAD_DIR / subdir).mkdir(exist_ok=True)
    name = f"{prefix}_{index}_{int(time.time() * 1000)}.jpg"
    dest = UPLOAD_DIR / subdir / name
    raw = upload.file.read()
    try:
        img = Image.open(io.BytesIO(raw))
        img = img.convert("RGB")
        img.thumbnail((1600, 1600))
        img.save(dest, "JPEG", quality=85)
    except Exception:
        # 無法解析的格式直接存原檔
        ext = Path(upload.filename or "x.jpg").suffix or ".jpg"
        dest = dest.with_suffix(ext)
        dest.write_bytes(raw)
    return f"{subdir}/{dest.name}"


@app.get("/returns")
def returns_page():
    return FileResponse(BASE_DIR / "static" / "returns.html", headers=NO_CACHE)


@app.get("/favorites")
def favorites_page():
    return FileResponse(BASE_DIR / "static" / "favorites.html", headers=NO_CACHE)


@app.get("/accounts")
def accounts_page():
    return FileResponse(BASE_DIR / "static" / "accounts.html", headers=NO_CACHE)


@app.get("/routes")
def routes_page():
    return FileResponse(BASE_DIR / "static" / "routes.html", headers=NO_CACHE)


@app.get("/sameday")
def sameday_page():
    return FileResponse(BASE_DIR / "static" / "sameday.html", headers=NO_CACHE)


# ---------- Same Day：每日早上出貨相片 ----------
@app.get("/api/sameday")
def list_sameday():
    if DB_ERROR is not None:
        return {"db_ok": False, "days": []}
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT day, id, filename FROM sameday_images
            WHERE day >= (CURRENT_DATE - INTERVAL '90 days')
            ORDER BY day DESC, id
            """
        )
        day_map = {}
        for day, iid, fn in cur.fetchall():
            key = day.strftime("%Y-%m-%d")
            day_map.setdefault(key, []).append({"id": iid, "filename": fn})
    conn.close()
    return {
        "db_ok": True,
        "days": [{"date": d, "images": imgs} for d, imgs in day_map.items()],
    }


@app.post("/api/sameday")
def add_sameday_images(date: str = Form(""), files: list[UploadFile] = File(...)):
    if DB_ERROR is not None:
        raise HTTPException(503, "資料庫未連線")
    day = date.strip() or time.strftime("%Y-%m-%d")
    try:
        time.strptime(day, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, "日期格式錯誤")
    conn = get_conn()
    images = []
    try:
        with conn.cursor() as cur:
            for i, f in enumerate(files):
                if not f.filename:
                    continue
                rel = _save_upload(f"sd{day.replace('-', '')}", i, f)
                images.append(rel)
                cur.execute(
                    "INSERT INTO sameday_images (day, filename) VALUES (%s, %s)",
                    (day, rel),
                )
        conn.commit()
    except Exception:
        conn.rollback()
        for rel in images:
            try:
                (UPLOAD_DIR / rel).unlink(missing_ok=True)
            except OSError:
                pass
        raise
    finally:
        conn.close()
    return {"ok": True}


@app.delete("/api/sameday/{img_id}")
def delete_sameday_image(img_id: int):
    if DB_ERROR is not None:
        raise HTTPException(503, "資料庫未連線")
    conn = get_conn()
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM sameday_images WHERE id = %s RETURNING filename",
            (img_id,),
        )
        row = cur.fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "找不到相片")
    try:
        (UPLOAD_DIR / row[0]).unlink(missing_ok=True)
    except OSError as e:
        print(f"刪除相片檔案失敗 {row[0]}: {e}")
    return {"ok": True}


# ---------- 車線速查（純查找，不生成二維碼、不進主頁歷史） ----------
ROUTE_SELECT = """
    SELECT r.id, r.name, r.content, r.created_at,
           COALESCE(
               (SELECT array_agg(i.filename ORDER BY i.id)
                FROM route_images i WHERE i.record_id = r.id),
               '{}'
           )
    FROM route_records r
"""


def _route_row(r):
    return {
        "id": r[0],
        "name": r[1],
        "content": r[2],
        "created_at": r[3].strftime("%Y-%m-%d %H:%M:%S"),
        "images": list(r[4]),
    }


@app.get("/api/routes")
def list_routes(q: str = ""):
    if DB_ERROR is not None:
        return {"db_ok": False, "routes": []}
    conn = get_conn()
    with conn.cursor() as cur:
        if q:
            # 只搜尋名稱左邊的號碼（「002 ↔ 68」只匹配 002 那邊）
            cur.execute(
                ROUTE_SELECT + """
                WHERE split_part(r.name, '↔', 1) ILIKE %s
                ORDER BY (split_part(r.name, '↔', 1) ILIKE %s) DESC, r.name
                LIMIT 200
                """,
                (_like_pattern(q), q.upper() + "%"),
            )
        else:
            cur.execute(ROUTE_SELECT + " ORDER BY r.name LIMIT 200")
        rows = cur.fetchall()
    conn.close()
    return {"db_ok": True, "routes": [_route_row(r) for r in rows]}


@app.post("/api/routes")
def create_route(
    name: str = Form(...),
    content: str = Form(""),
    files: list[UploadFile] = File(default=[]),
):
    if DB_ERROR is not None:
        raise HTTPException(503, "資料庫未連線")
    name = name.strip()[:100]
    content = content.strip()[:5000]
    if not name:
        raise HTTPException(400, "車線名稱不能為空")
    conn = get_conn()
    images = []
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO route_records (name, content) "
                "VALUES (%s, %s) RETURNING id",
                (name, content),
            )
            rid = cur.fetchone()[0]
            for i, f in enumerate(files):
                if not f.filename:
                    continue
                rel = _save_upload(f"rt{rid}", i, f)
                images.append(rel)
                cur.execute(
                    "INSERT INTO route_images (record_id, filename) VALUES (%s, %s)",
                    (rid, rel),
                )
        conn.commit()
    except Exception:
        conn.rollback()
        for rel in images:
            try:
                (UPLOAD_DIR / rel).unlink(missing_ok=True)
            except OSError:
                pass
        raise
    finally:
        conn.close()
    return {"id": rid, "name": name, "content": content, "images": images}


class RouteUpdate(BaseModel):
    name: str
    content: str = ""


@app.put("/api/routes/{record_id}")
def update_route(record_id: int, body: RouteUpdate):
    if DB_ERROR is not None:
        raise HTTPException(503, "資料庫未連線")
    name = body.name.strip()[:100]
    content = body.content.strip()[:5000]
    if not name:
        raise HTTPException(400, "車線名稱不能為空")
    conn = get_conn()
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE route_records SET name = %s, content = %s WHERE id = %s",
            (name, content, record_id),
        )
        updated = cur.rowcount
    conn.close()
    if not updated:
        raise HTTPException(404, "找不到記錄")
    return {"ok": True}


@app.delete("/api/routes/{record_id}")
def delete_route(record_id: int):
    if DB_ERROR is not None:
        raise HTTPException(503, "資料庫未連線")
    conn = get_conn()
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(
            "SELECT filename FROM route_images WHERE record_id = %s", (record_id,)
        )
        for (rel,) in cur.fetchall():
            try:
                (UPLOAD_DIR / rel).unlink(missing_ok=True)
            except OSError as e:
                print(f"刪除相片失敗 {rel}: {e}")
        cur.execute("DELETE FROM route_records WHERE id = %s", (record_id,))
        deleted = cur.rowcount
    conn.close()
    if not deleted:
        raise HTTPException(404, "找不到記錄")
    return {"ok": True}


@app.post("/api/routes/{record_id}/images")
def add_route_images(record_id: int, files: list[UploadFile] = File(...)):
    if DB_ERROR is not None:
        raise HTTPException(503, "資料庫未連線")
    conn = get_conn()
    images = []
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM route_records WHERE id = %s", (record_id,))
            if not cur.fetchone():
                raise HTTPException(404, "找不到記錄")
            for i, f in enumerate(files):
                if not f.filename:
                    continue
                rel = _save_upload(f"rt{record_id}", i, f)
                images.append(rel)
                cur.execute(
                    "INSERT INTO route_images (record_id, filename) VALUES (%s, %s)",
                    (record_id, rel),
                )
        conn.commit()
    except Exception:
        conn.rollback()
        for rel in images:
            try:
                (UPLOAD_DIR / rel).unlink(missing_ok=True)
            except OSError:
                pass
        raise
    finally:
        conn.close()
    return {"ok": True}


@app.delete("/api/routeimg")
def delete_route_image(record_id: int, filename: str):
    if DB_ERROR is not None:
        raise HTTPException(503, "資料庫未連線")
    conn = get_conn()
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM route_images WHERE record_id = %s AND filename = %s",
            (record_id, filename),
        )
        deleted = cur.rowcount
    conn.close()
    if not deleted:
        raise HTTPException(404, "找不到相片")
    try:
        (UPLOAD_DIR / filename).unlink(missing_ok=True)
    except OSError as e:
        print(f"刪除相片檔案失敗 {filename}: {e}")
    return {"ok": True}


# ---------- 帳號密碼二維碼（獨立功能，不進主頁歷史） ----------
class AccountBody(BaseModel):
    label: str = ""
    username: str
    password: str


def _clean_account(body: AccountBody):
    label = body.label.strip()[:50]
    username = body.username.strip()  # 帳號密碼不轉大寫（有大小寫之分）
    password = body.password.strip()
    if not username or not password:
        raise HTTPException(400, "帳號和密碼不能為空")
    return label, username, password


@app.get("/api/accounts")
def list_accounts():
    if DB_ERROR is not None:
        return {"db_ok": False, "accounts": []}
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, label, username, password, created_at "
            "FROM account_records ORDER BY id"
        )
        rows = cur.fetchall()
    conn.close()
    return {
        "db_ok": True,
        "accounts": [
            {
                "id": r[0],
                "label": r[1],
                "username": r[2],
                "password": r[3],
                "created_at": r[4].strftime("%Y-%m-%d %H:%M:%S"),
            }
            for r in rows
        ],
    }


@app.post("/api/accounts")
def create_account(body: AccountBody):
    if DB_ERROR is not None:
        raise HTTPException(503, "資料庫未連線")
    label, username, password = _clean_account(body)
    conn = get_conn()
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO account_records (label, username, password) "
            "VALUES (%s, %s, %s) RETURNING id",
            (label, username, password),
        )
        aid = cur.fetchone()[0]
    conn.close()
    return {"id": aid, "label": label, "username": username, "password": password}


@app.put("/api/accounts/{account_id}")
def update_account(account_id: int, body: AccountBody):
    if DB_ERROR is not None:
        raise HTTPException(503, "資料庫未連線")
    label, username, password = _clean_account(body)
    conn = get_conn()
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE account_records SET label = %s, username = %s, "
            "password = %s WHERE id = %s",
            (label, username, password, account_id),
        )
        updated = cur.rowcount
    conn.close()
    if not updated:
        raise HTTPException(404, "找不到記錄")
    return {"ok": True}


@app.delete("/api/accounts/{account_id}")
def delete_account(account_id: int):
    if DB_ERROR is not None:
        raise HTTPException(503, "資料庫未連線")
    conn = get_conn()
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DELETE FROM account_records WHERE id = %s", (account_id,))
        deleted = cur.rowcount
    conn.close()
    if not deleted:
        raise HTTPException(404, "找不到記錄")
    return {"ok": True}


@app.get("/api/accounts/{account_id}/qr")
def account_qr(account_id: int, field: str = "username"):
    if field not in ("username", "password"):
        raise HTTPException(400, "field 必須是 username 或 password")
    if DB_ERROR is not None:
        raise HTTPException(503, "資料庫未連線")
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {field} FROM account_records WHERE id = %s", (account_id,)
        )
        row = cur.fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "找不到記錄")
    return Response(make_qr_png(row[0]), media_type="image/png")


@app.post("/api/returns")
def create_return(
    content: str = Form(...),
    ctn: str = Form("0"),
    pcs: str = Form("0"),
    kg: str = Form("0"),
    pcs_unit: str = Form("PCS"),
    rt: str = Form("false"),
    location: str = Form(""),
    files: list[UploadFile] = File(default=[]),
):
    if DB_ERROR is not None:
        raise HTTPException(503, "資料庫未連線")
    content = content.strip().upper()
    if not content:
        raise HTTPException(400, "內容不能為空")
    q_ctn = _parse_qty(ctn, "CTN")
    q_pcs = _parse_qty(pcs, "PCS")
    q_kg = _parse_qty(kg, "KG")
    unit_label = pcs_unit.strip().upper()[:12] or "PCS"
    has_rt = rt.strip().lower() in ("true", "1", "on", "yes")
    loc = location.strip().upper()[:50]
    if q_ctn == 0 and q_pcs == 0 and q_kg == 0:
        raise HTTPException(400, "至少輸入一項數量")

    # 用交易包住：任何一張照片失敗就整筆回滾，避免半成品記錄 + 重送變重複
    conn = get_conn()
    images = []
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO return_records "
                "(content, qty_ctn, qty_pcs, pcs_unit, qty_kg, has_rt, location) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id, created_at",
                (content, q_ctn, q_pcs, unit_label, q_kg, has_rt, loc),
            )
            rid, created = cur.fetchone()
            _sync_qr_record(cur, content, loc)  # 同步到主頁歷史記錄
            for i, f in enumerate(files):
                if not f.filename:
                    continue
                rel = _save_upload(f"r{rid}", i, f)
                images.append(rel)  # 先記下，INSERT 失敗時清理才找得到這個檔
                cur.execute(
                    "INSERT INTO return_images (record_id, filename) VALUES (%s, %s)",
                    (rid, rel),
                )
        conn.commit()
    except Exception:
        conn.rollback()
        for rel in images:
            try:
                (UPLOAD_DIR / rel).unlink(missing_ok=True)
            except OSError:
                pass
        raise
    finally:
        conn.close()
    return {
        "id": rid,
        "content": content,
        "ctn": q_ctn,
        "pcs": q_pcs,
        "pcs_unit": unit_label,
        "kg": q_kg,
        "has_rt": has_rt,
        "location": loc,
        "created_at": created.strftime("%Y-%m-%d %H:%M:%S"),
        "images": images,
    }


@app.post("/api/returns/dayimages")
def add_day_images(date: str = Form(""), files: list[UploadFile] = File(...)):
    """上傳「當日貨物相片」（附屬於日期，不屬於單筆記錄）"""
    if DB_ERROR is not None:
        raise HTTPException(503, "資料庫未連線")
    day = date.strip() or time.strftime("%Y-%m-%d")
    try:
        time.strptime(day, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, "日期格式錯誤")
    conn = get_conn()
    images = []
    try:
        with conn.cursor() as cur:
            for i, f in enumerate(files):
                if not f.filename:
                    continue
                rel = _save_upload(f"d{day.replace('-', '')}", i, f)
                images.append(rel)
                cur.execute(
                    "INSERT INTO return_day_images (day, filename) VALUES (%s, %s)",
                    (day, rel),
                )
        conn.commit()
    except Exception:
        conn.rollback()
        for rel in images:
            try:
                (UPLOAD_DIR / rel).unlink(missing_ok=True)
            except OSError:
                pass
        raise
    finally:
        conn.close()
    return {"ok": True}


@app.delete("/api/dayimages/{img_id}")
def delete_day_image(img_id: int):
    if DB_ERROR is not None:
        raise HTTPException(503, "資料庫未連線")
    conn = get_conn()
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM return_day_images WHERE id = %s RETURNING filename",
            (img_id,),
        )
        row = cur.fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "找不到相片")
    try:
        (UPLOAD_DIR / row[0]).unlink(missing_ok=True)
    except OSError as e:
        print(f"刪除相片檔案失敗 {row[0]}: {e}")
    return {"ok": True}


@app.get("/api/returns/days")
def list_return_days():
    """日期總覽：每天的筆數與三種單位合計"""
    if DB_ERROR is not None:
        return {"db_ok": False, "days": []}
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT created_at::date AS d, COUNT(*),
                   COALESCE(SUM(qty_ctn), 0), COALESCE(SUM(qty_kg), 0)
            FROM return_records
            GROUP BY d ORDER BY d DESC LIMIT 180
            """
        )
        rows = cur.fetchall()
        cur.execute(
            """
            SELECT created_at::date AS d, pcs_unit, SUM(qty_pcs)
            FROM return_records WHERE qty_pcs > 0
            GROUP BY d, pcs_unit ORDER BY pcs_unit
            """
        )
        unit_map = {}
        for d, unit, qty in cur.fetchall():
            unit_map.setdefault(d, []).append({"unit": unit, "qty": float(qty)})
    conn.close()
    return {
        "db_ok": True,
        "days": [
            {
                "date": r[0].strftime("%Y-%m-%d"),
                "count": r[1],
                "ctn": float(r[2]),
                "kg": float(r[3]),
                "units": unit_map.get(r[0], []),
            }
            for r in rows
        ],
    }


@app.get("/api/returns")
def list_returns(date: str = ""):
    if DB_ERROR is not None:
        return {"db_ok": False, "records": [], "totals": {}}
    day = date.strip() or time.strftime("%Y-%m-%d")
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.id, r.content, r.qty_ctn, r.qty_pcs, r.pcs_unit,
                   r.qty_kg, r.has_rt, r.location, r.created_at,
                   COALESCE(
                       (SELECT array_agg(i.filename ORDER BY i.id)
                        FROM return_images i WHERE i.record_id = r.id),
                       '{}'
                   )
            FROM return_records r
            WHERE r.created_at::date = %s::date
            ORDER BY r.created_at DESC
            """,
            (day,),
        )
        rows = cur.fetchall()
        cur.execute(
            """
            SELECT COALESCE(SUM(qty_ctn), 0), COALESCE(SUM(qty_kg), 0)
            FROM return_records WHERE created_at::date = %s::date
            """,
            (day,),
        )
        t_ctn, t_kg = cur.fetchone()
        cur.execute(
            """
            SELECT pcs_unit, SUM(qty_pcs)
            FROM return_records
            WHERE created_at::date = %s::date AND qty_pcs > 0
            GROUP BY pcs_unit ORDER BY pcs_unit
            """,
            (day,),
        )
        units = [{"unit": u, "qty": float(q)} for u, q in cur.fetchall()]
        cur.execute(
            "SELECT id, filename FROM return_day_images "
            "WHERE day = %s::date ORDER BY id",
            (day,),
        )
        day_images = [{"id": r[0], "filename": r[1]} for r in cur.fetchall()]
    conn.close()
    return {
        "db_ok": True,
        "date": day,
        "totals": {"CTN": float(t_ctn), "KG": float(t_kg), "units": units},
        "day_images": day_images,
        "records": [
            {
                "id": r[0],
                "content": r[1],
                "ctn": float(r[2]),
                "pcs": float(r[3]),
                "pcs_unit": r[4],
                "kg": float(r[5]),
                "has_rt": r[6],
                "location": r[7],
                "created_at": r[8].strftime("%Y-%m-%d %H:%M:%S"),
                "images": list(r[9]),
            }
            for r in rows
        ],
    }


class ReturnUpdate(BaseModel):
    content: str
    ctn: str = "0"
    pcs: str = "0"
    kg: str = "0"
    pcs_unit: str = "PCS"
    rt: bool = False
    location: str = ""


@app.put("/api/returns/{record_id}")
def update_return(record_id: int, body: ReturnUpdate):
    if DB_ERROR is not None:
        raise HTTPException(503, "資料庫未連線")
    content = body.content.strip().upper()
    if not content:
        raise HTTPException(400, "內容不能為空")
    q_ctn = _parse_qty(body.ctn, "CTN")
    q_pcs = _parse_qty(body.pcs, "PCS")
    q_kg = _parse_qty(body.kg, "KG")
    unit_label = body.pcs_unit.strip().upper()[:12] or "PCS"
    loc = body.location.strip().upper()[:50]
    if q_ctn == 0 and q_pcs == 0 and q_kg == 0:
        raise HTTPException(400, "至少輸入一項數量")
    conn = get_conn()
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(
            "SELECT content FROM return_records WHERE id = %s", (record_id,)
        )
        row = cur.fetchone()
        if not row:
            conn.close()
            raise HTTPException(404, "找不到記錄")
        old_content = row[0]
        cur.execute(
            "UPDATE return_records SET content = %s, qty_ctn = %s, "
            "qty_pcs = %s, pcs_unit = %s, qty_kg = %s, has_rt = %s, "
            "location = %s WHERE id = %s",
            (content, q_ctn, q_pcs, unit_label, q_kg, body.rt, loc, record_id),
        )
        updated = cur.rowcount
        if updated:
            if old_content != content:
                # 改錯字：舊內容當時自動同步到主頁、而且沒被用過的話，順手清掉
                cur.execute(
                    """
                    DELETE FROM qr_records q
                    WHERE q.content = %s
                      AND q.times <= 1
                      AND NOT q.is_favorite
                      AND q.qr_type = 'goods'
                      AND q.stack_base = 0 AND q.stack_height = 0
                      AND q.pieces_per_box = 0
                      AND NOT EXISTS (
                          SELECT 1 FROM qr_images i WHERE i.record_id = q.id
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM return_records r
                          WHERE r.content = %s AND r.id <> %s
                      )
                    """,
                    (old_content, old_content, record_id),
                )
            _sync_qr_record(cur, content, loc)  # 同步到主頁歷史（位置以最新為準）
    conn.close()
    return {"ok": True}


@app.post("/api/returns/{record_id}/images")
def add_return_images(record_id: int, files: list[UploadFile] = File(...)):
    """為既有退貨記錄補加照片（交易式：失敗整批回滾並清檔）"""
    if DB_ERROR is not None:
        raise HTTPException(503, "資料庫未連線")
    conn = get_conn()
    images = []
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM return_records WHERE id = %s", (record_id,))
            if not cur.fetchone():
                raise HTTPException(404, "找不到記錄")
            for i, f in enumerate(files):
                if not f.filename:
                    continue
                rel = _save_upload(f"r{record_id}", i, f)
                images.append(rel)
                cur.execute(
                    "INSERT INTO return_images (record_id, filename) "
                    "VALUES (%s, %s)",
                    (record_id, rel),
                )
        conn.commit()
    except Exception:
        conn.rollback()
        for rel in images:
            try:
                (UPLOAD_DIR / rel).unlink(missing_ok=True)
            except OSError:
                pass
        raise
    finally:
        conn.close()
    return {"ok": True}


@app.delete("/api/retimg")
def delete_return_image(record_id: int, filename: str):
    if DB_ERROR is not None:
        raise HTTPException(503, "資料庫未連線")
    conn = get_conn()
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM return_images WHERE record_id = %s AND filename = %s",
            (record_id, filename),
        )
        deleted = cur.rowcount
    conn.close()
    if not deleted:
        raise HTTPException(404, "找不到照片")
    try:
        (UPLOAD_DIR / filename).unlink(missing_ok=True)
    except OSError as e:
        print(f"刪除照片檔案失敗 {filename}: {e}")
    return {"ok": True}


@app.get("/api/returns/{record_id}/qr")
def return_record_qr(record_id: int):
    """記錄的二維碼（由退貨內容自動生成）"""
    if DB_ERROR is not None:
        raise HTTPException(503, "資料庫未連線")
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT content FROM return_records WHERE id = %s", (record_id,))
        row = cur.fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "找不到記錄")
    return Response(make_qr_png(row[0]), media_type="image/png")


@app.delete("/api/returns/{record_id}")
def delete_return(record_id: int):
    if DB_ERROR is not None:
        raise HTTPException(503, "資料庫未連線")
    conn = get_conn()
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(
            "SELECT filename FROM return_images WHERE record_id = %s", (record_id,)
        )
        for (rel,) in cur.fetchall():
            try:
                (UPLOAD_DIR / rel).unlink(missing_ok=True)
            except Exception as e:
                print(f"刪除照片失敗 {rel}: {e}")
        cur.execute("DELETE FROM return_records WHERE id = %s", (record_id,))
        deleted = cur.rowcount
    conn.close()
    if not deleted:
        raise HTTPException(404, "找不到記錄")
    return {"ok": True}


app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


@app.get("/api/urlqr")
def url_qr(target: str = "lan"):
    """產生「手機掃描開啟本站」的二維碼（target: lan=同網路, remote=公網）"""
    if target == "remote" and PORTMAP["ok"]:
        url = f"http://{PORTMAP['wan_ip']}:{PORT}/"
    else:
        url = f"http://{get_ips()['lan']}:{PORT}/"
    return Response(make_qr_png(url), media_type="image/png")


if __name__ == "__main__":
    import threading
    import webbrowser

    # 連接埠已被占用 → 程式多半已經在執行中，直接開瀏覽器就好
    test = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    test.settimeout(1)
    port_in_use = test.connect_ex(("127.0.0.1", PORT)) == 0
    test.close()
    if port_in_use:
        print(f"程式已經在執行中，直接為你開啟瀏覽器 http://localhost:{PORT}/")
        webbrowser.open(f"http://localhost:{PORT}/")
        raise SystemExit

    init_db()
    ips = get_ips()
    print("=" * 46)
    print("  二維碼生成器已啟動")
    print(f"  電腦開啟:        http://localhost:{PORT}/")
    print(f"  手機開啟(同網路): http://{ips['lan']}:{PORT}/")
    if REMOTE.get("mode") == "portforward":
        wan = natpmp_map()
        if wan:
            print(f"  遠端(公司/4G):    http://{wan}:{PORT}/  (需輸入PIN碼)")

            def _renew_loop():
                while True:
                    time.sleep(3000)  # 每50分鐘續約開門（路由器2小時到期）
                    natpmp_map()

            threading.Thread(target=_renew_loop, daemon=True).start()
        else:
            print(f"  [警告] 路由器開門失敗: {PORTMAP['err']}")
    if DB_ERROR:
        print(f"  [警告] 資料庫未連線: {DB_ERROR.strip()}")
        print("  請編輯 config.json 填入正確的資料庫密碼")
    print("=" * 46)
    if os.environ.get("QR_NO_BROWSER") != "1":  # 啟動器/開機自啟時不自動開瀏覽器
        threading.Timer(1.0, lambda: webbrowser.open(f"http://localhost:{PORT}/")).start()
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
