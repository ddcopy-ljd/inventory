"""懿臻珠宝云 · 插件后端（可独立运行）。

独立启动：在本目录执行  python main.py
默认 http://127.0.0.1:8000  演示账号 admin / 123456
"""

from __future__ import annotations

import logging
import json
import os
import secrets
import socket
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, Field
import uvicorn

import db
import rfid_print

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("jewelry")

ROOT = Path(__file__).resolve().parent
FRONTEND_DIR = ROOT / "frontend"
LOGO_DIR = ROOT / "logo"

HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8002"))
# 平台启动时注入 TENANT_DB_DIR；独立运行时该变量为空 → 进入 STANDALONE 模式
MODE = "STANDALONE" if not os.environ.get("TENANT_DB_DIR") else "PLATFORM"

FEATURES = [
    "products", "inventory", "sales", "deposits", "loans", "customers",
    "repairs", "purchases", "outsourcings", "rfid", "labels", "site", "logs",
]

app = FastAPI(title="懿臻珠宝云", docs_url="/docs", redoc_url=None)
app.add_middleware(GZipMiddleware, minimum_size=500)

_sessions: dict[str, dict] = {}
_lock = threading.Lock()


def _safe_relative(f: Path, base: Path) -> bool:
    try:
        return f.is_relative_to(base)
    except AttributeError:
        pass
    try:
        f.relative_to(base)
        return True
    except ValueError:
        return False


def _tenant_of(request: Request) -> str:
    return request.headers.get("X-Resolved-Tenant-ID") or os.environ.get("TENANT_ID") or "tenant_trial"


def _bypass(request: Request) -> bool:
    return request.headers.get("X-System-Bypass-Auth") == "true"


def _operator_mode(request: Request) -> str:
    return (request.headers.get("X-Operator-Mode") or "NORMAL").upper()


def _header_operator(request: Request) -> str:
    return request.headers.get("X-Operator-ID") or "admin"


def _open(request: Request) -> sqlite3.Connection:
    tenant = _tenant_of(request)
    if MODE == "STANDALONE" or not os.environ.get("TENANT_DB_DIR"):
        base = os.environ.get("TENANT_DB_DIR") or str(ROOT / "dev_data")
        os.environ["TENANT_DB_DIR"] = base
        db.DB_DIR = Path(base)
        path = Path(base) / f"db_jewelry_{tenant}_v1.0.0.sqlite"
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        db.migrate_schema(conn)
        if conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 0:
            db.seed_demo(conn)
        db.ensure_new_seeds(conn)
        return conn
    conn = db.connect(tenant)
    db.ensure_new_seeds(conn)
    return conn


@contextmanager
def _db(request: Request):
    conn = _open(request)
    try:
        yield conn
    finally:
        conn.close()


def _require_auth(request: Request) -> dict:
    if _bypass(request):
        return {"username": _header_operator(request), "display_name": _header_operator(request), "role": "TENANT_ADMIN"}
    raw = request.headers.get("Authorization", "")
    token = raw.replace("Bearer ", "").strip()
    with _lock:
        sess = _sessions.get(token)
    if not sess:
        raise HTTPException(status_code=401, detail="未登录或登录已过期")
    return sess


def _log(conn: sqlite3.Connection, operator: str, action: str, target: str, result: str = "成功") -> None:
    try:
        conn.execute(
            "INSERT INTO operate_logs(operator,store,action,target,result) VALUES(?,?,?,?,?)",
            (operator, "总店", action, target, result),
        )
        conn.commit()
    except Exception:
        logger.exception("写操作日志失败")


def _inv(conn: sqlite3.Connection, product_id: int | None, epc: str, typ: str, operator: str, qty: int = 1) -> None:
    conn.execute(
        "INSERT INTO inventory_logs(product_id,epc,type,qty,operator) VALUES(?,?,?,?,?)",
        (product_id, epc or "", typ, qty, operator),
    )


def _touch_customer(conn: sqlite3.Connection, name: str, phone: str, amount: float, due: float) -> None:
    if not phone and not name:
        return
    cust = None
    if phone:
        cust = conn.execute("SELECT * FROM customers WHERE phone=?", (phone,)).fetchone()
    if cust:
        conn.execute(
            "UPDATE customers SET total_amount=total_amount+?, due_amount=due_amount+?, name=COALESCE(NULLIF(?,''),name) WHERE id=?",
            (amount, due, name, cust["id"]),
        )
    elif name or phone:
        conn.execute(
            "INSERT INTO customers(name,phone,level,total_amount,due_amount) VALUES(?,?,?,?,?)",
            (name or "散客", phone or "", "普通", amount, max(due, 0)),
        )


def _next_code(conn: sqlite3.Connection, prefix: str = "J") -> str:
    row = conn.execute("SELECT MAX(id) m FROM products").fetchone()
    return f"{prefix}{(row['m'] or 0) + 1:03d}"


def _next_bill_no(conn: sqlite3.Connection) -> str:
    d = date.today().strftime("%Y%m%d")
    row = conn.execute("SELECT MAX(id) m FROM sales").fetchone()
    seq = (row["m"] or 0) + 1
    return f"XS{d}{seq:03d}"


def _gen_epc(code: str) -> str:
    raw = secrets.token_hex(8).upper()
    return f"E280{raw}{code[-4:].upper().ljust(4, '0')}"[:28]


# ---------------------------------------------------------------- 前端 / 静态

@app.get("/api/health")
def health():
    return {"ok": True, "name": "懿臻珠宝云", "mode": MODE, "version": "1.0.0"}


@app.get("/")
def spa_index():
    f = FRONTEND_DIR / "index.html"
    if f.exists():
        return FileResponse(str(f), headers={"Cache-Control": "no-cache"})
    return JSONResponse({"detail": "前端未构建"}, status_code=503)


@app.get("/site")
def public_site(request: Request):
    with _db(request) as conn:
        prof = conn.execute("SELECT * FROM tenant_profiles ORDER BY id DESC LIMIT 1").fetchone()
        rows = conn.execute(
            """SELECT id, code, name, category, material, weight, size, price, cert, status,
                      origin, showcase_order, showcase_desc
                 FROM products
                WHERE showcase_public=1 AND status='在库'
                ORDER BY (showcase_order=0) ASC, showcase_order ASC, id DESC
                LIMIT 10"""
        ).fetchall()
    name = (prof["name"] if prof else "懿臻珠宝") or "懿臻珠宝"
    slogan = (prof["slogan"] if prof else "") or ""
    intro = (prof["intro"] if prof else "") or ""
    addr = (prof["address"] if prof else "") or ""
    phone = (prof["phone"] if prof else "") or ""
    hours = (prof["hours"] if prof else "") or ""
    sh_title = (prof["showcase_title"] if prof else "新品橱窗") or "新品橱窗"
    sh_sub = (prof["showcase_subtitle"] if prof else "本周臻品 · 限量发售") or "本周臻品 · 限量发售"

    items_html: list[str] = []
    for r in rows:
        desc = (r["showcase_desc"] or "").strip()
        if not desc:
            parts = []
            if r["origin"]: parts.append(f"产地：{r['origin']}")
            if r["material"]: parts.append(f"材质：{r['material']}")
            if r["weight"] and float(r["weight"]) > 0: parts.append(f"金重：{float(r['weight']):.2f}g")
            if r["size"]: parts.append(f"尺寸：{r['size']}")
            if r["price"]: parts.append(f"参考价：¥{float(r['price']):,.0f}")
            desc = "｜".join(parts)
        cat = r["category"] or "珠宝"
        tags_html = f"<span class='tag-cat'>{cat}</span>"
        if r["cert"]:
            tags_html += f"<span class='tag-cert'>附权威证书</span>"
        price_text = f"¥{float(r['price']):,.0f}" if r["price"] and float(r["price"]) > 0 else "<i>到店咨询</i>"
        # 货号行：便于客户到店时报货号描述商品
        code_html = f"<p class='sc-code'>货号 <b>{r['code']}</b></p>" if r["code"] else ""
        # 首字母占位的视觉 LOGO
        avatar_ch = (r["name"] or "臻")[:1]
        items_html.append(
            f"""<article class='sc-card'>
  <div class='sc-cover'>
    <div class='sc-avatar'>{avatar_ch}</div>
    <div class='sc-badges'>{tags_html}</div>
  </div>
  <div class='sc-body'>
    <h3>{r['name']}</h3>
    {code_html}
    <p class='sc-desc'>{desc}</p>
    <div class='sc-foot'>
      <span class='sc-price'>{price_text}</span>
      <button class='sc-book' onclick=\"focusBook('{r['name']}','{cat}','{r['code'] or ''}')\">预约看货</button>
    </div>
  </div>
</article>"""
        )
    cards_html = "\n".join(items_html) or (
        "<div class='sc-empty'><div class='sc-empty-ico'>💎</div>"
        "<p>橱窗正在整理中</p><span>敬请期待本季臻品</span></div>"
    )
    # 企业宣传 · 四大安心承诺
    promises = [
        ("🔍", "源头直采", "金料与裸石直选自深圳水贝、云南腾冲，省去中间环节，同品质价格更实在。"),
        ("📜", "一物一证", "每件成品均配 NGTC / GIA 权威证书，支持全国任意机构复检，假一赔十。"),
        ("🔨", "自有工坊", "驻店师傅平均从业 20 年，改圈、刻字、维修立等可取，高级定制最快 7 日交付。"),
        ("♾️", "终身养护", "所购首饰终身享免费清洗、抛光与牢固度检测，以旧换新按当日金价估价。"),
    ]
    promise_cards_html = "\n".join(
        f"<div class='p-card'><div class='p-ico'>{ico}</div><h3>{pt}</h3><p>{pd}</p></div>"
        for ico, pt, pd in promises
    )
    # 小图标
    icon_phone = "<svg width='14' height='14' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2'><path d='M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6 19.79 19.79 0 0 1-3.07-8.67A2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72 12.84 12.84 0 0 0 .7 2.81 2 2 0 0 1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45 12.84 12.84 0 0 0 2.81.7A2 2 0 0 1 22 16.92z'/></svg>"
    icon_loc = "<svg width='14' height='14' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2'><path d='M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z'/><circle cx='12' cy='10' r='3'/></svg>"
    icon_clock = "<svg width='14' height='14' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2'><circle cx='12' cy='12' r='10'/><polyline points='12 6 12 12 16 14'/></svg>"

    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{name} · 官方臻选</title>
<style>
* {{ box-sizing: border-box; }}
body {{ margin: 0; font-family: "PingFang SC","Microsoft YaHei","Hiragino Sans GB",sans-serif; background:#FBF6EC; color:#3D2B1F; line-height:1.6; }}
.hero {{
  position: relative; overflow: hidden;
  background: linear-gradient(150deg, #3D1522 0%, #5C2233 45%, #6E2A3F 100%);
  color: #F7EFE0; padding: 72px 24px 56px; text-align: center;
}}
.hero::before {{
  content: ''; position: absolute; top: -80px; right: -80px; width: 260px; height: 260px;
  background: radial-gradient(circle, rgba(201,169,97,.35) 0%, transparent 70%); border-radius: 50%;
}}
.hero::after {{
  content: ''; position: absolute; bottom: -120px; left: -60px; width: 220px; height: 220px;
  background: radial-gradient(circle, rgba(201,169,97,.22) 0%, transparent 70%); border-radius: 50%;
}}
.hero > * {{ position: relative; z-index: 1; }}
.brand-logo {{
  width: 72px; height: 72px; margin: 0 auto 18px; border-radius: 50%;
  background: linear-gradient(135deg, #F5E7B8, #E8D5A0 30%, #C9A961 60%, #9C7B3A);
  display: flex; align-items: center; justify-content: center; font-size: 30px; color: #5C2233;
  font-weight: 700; box-shadow: 0 8px 28px rgba(201,169,97,.45), inset 0 1px 1px rgba(255,255,255,.6);
  padding: 10px;
}}
.brand-logo img {{ width: 100%; height: 100%; object-fit: contain; }}
.hero h1 {{ margin: 0 0 8px; font-size: 32px; letter-spacing: 2px; }}
.hero .slogan {{ color: #EED8A1; font-size: 18px; margin: 6px 0 16px; letter-spacing: 1px; }}
.hero .intro {{ max-width: 640px; margin: 0 auto; opacity: .88; font-size: 14px; }}
.hero .meta {{
  display: flex; flex-wrap: wrap; justify-content: center; gap: 14px 22px;
  margin-top: 26px; font-size: 13px; opacity: .92;
}}
.hero .meta span {{ display: inline-flex; align-items: center; gap: 5px; }}
.wrap {{ max-width: 1160px; margin: 0 auto; padding: 48px 20px 64px; }}
.section-title {{
  display: flex; align-items: flex-end; justify-content: space-between; margin-bottom: 22px; flex-wrap: wrap; gap: 10px;
}}
.section-title h2 {{
  margin: 0; font-size: 26px; color: #4A1B2A; letter-spacing: 1px; position: relative; padding-left: 14px;
}}
.section-title h2::before {{
  content:''; position: absolute; left: 0; top: 8px; bottom: 8px; width: 4px; border-radius: 2px;
  background: linear-gradient(180deg, #C9A961, #8C6A1E);
}}
.section-title .sub {{ color: #8A6F58; font-size: 13px; padding-bottom: 4px; }}
.sc-grid {{
  display: grid; gap: 22px;
  grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
}}
.sc-card {{
  background: #fff; border-radius: 18px; overflow: hidden;
  box-shadow: 0 2px 10px rgba(92,34,51,.06), 0 8px 26px rgba(92,34,51,.05);
  border: 1px solid rgba(201,169,97,.22);
  transition: transform .25s ease, box-shadow .25s ease;
}}
.sc-card:hover {{ transform: translateY(-4px); box-shadow: 0 8px 20px rgba(92,34,51,.10), 0 18px 46px rgba(201,169,97,.18); }}
.sc-cover {{
  position: relative; height: 190px;
  background:
    linear-gradient(135deg, rgba(255,255,255,.14), rgba(255,255,255,.02)),
    linear-gradient(155deg, #8C2B46 0%, #5C2233 55%, #3D1522 100%);
  display: flex; align-items: center; justify-content: center; overflow: hidden;
}}
.sc-cover::after {{
  content: ''; position: absolute; inset: 0;
  background: radial-gradient(120% 80% at 20% 10%, rgba(245,231,184,.35) 0%, transparent 50%),
              radial-gradient(80% 60% at 100% 100%, rgba(201,169,97,.28) 0%, transparent 55%);
  pointer-events: none;
}}
.sc-avatar {{
  width: 110px; height: 110px; border-radius: 50%;
  background: linear-gradient(135deg, #F5E7B8 0%, #C9A961 55%, #9C7B3A 100%);
  color: #4A1B2A; display: flex; align-items: center; justify-content: center;
  font-size: 46px; font-weight: 700; letter-spacing: 1px;
  box-shadow: 0 6px 20px rgba(0,0,0,.22), inset 0 2px 4px rgba(255,255,255,.6);
  position: relative; z-index: 1;
  font-family: "PingFang SC","Microsoft YaHei",serif;
}}
.sc-badges {{
  position: absolute; top: 10px; left: 10px; right: 10px; display: flex; gap: 6px; flex-wrap: wrap; z-index: 2;
}}
.tag-cat, .tag-cert {{
  font-size: 11px; padding: 3px 9px; border-radius: 20px; backdrop-filter: blur(4px);
  background: rgba(255,255,255,.18); color: #F5E7B8; border: 1px solid rgba(245,231,184,.35);
}}
.tag-cert {{ background: rgba(201,169,97,.85); color: #3D2B1F; border-color: rgba(255,255,255,.3); }}
.sc-body {{ padding: 18px 18px 20px; }}
.sc-body h3 {{ margin: 0 0 8px; font-size: 17px; color: #3D2B1F; }}
.sc-code {{
  margin: 0 0 10px; display: inline-block; font-size: 12px; color: #6E5426;
  background: rgba(201,169,97,.14); border: 1px solid rgba(201,169,97,.4);
  border-radius: 6px; padding: 2px 9px; letter-spacing: .3px;
}}
.sc-code b {{ font-family: Consolas, Menlo, 'Courier New', monospace; font-weight: 700; letter-spacing: 1px; }}
.sc-desc {{
  margin: 0 0 16px; font-size: 12.5px; line-height: 1.7; color: #6A5546; min-height: 44px;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
}}
.sc-foot {{ display: flex; align-items: center; justify-content: space-between; gap: 10px; }}
.sc-price {{
  font-size: 20px; font-weight: 700; color: #5C2233; font-family: Georgia, 'Times New Roman', serif; letter-spacing: .5px;
}}
.sc-price i {{ color: #8A6F58; font-style: normal; font-weight: 500; font-size: 14px; font-family: inherit; }}
.sc-book {{
  border: none; padding: 8px 14px; border-radius: 20px; cursor: pointer; font-size: 12.5px;
  background: linear-gradient(135deg, #5C2233, #8C2B46); color: #F7EFE0;
  box-shadow: 0 2px 10px rgba(92,34,51,.25); transition: transform .15s ease;
}}
.sc-book:hover {{ transform: scale(1.03); }}
.sc-book:active {{ transform: scale(.98); }}
.sc-empty {{
  grid-column: 1 / -1; text-align: center; padding: 60px 20px; background: #fff; border-radius: 18px;
  border: 1px dashed rgba(201,169,97,.5); color: #8A6F58;
}}
.sc-empty-ico {{ font-size: 42px; margin-bottom: 10px; opacity: .7; }}
.sc-empty p {{ margin: 0 0 4px; color: #4A1B2A; font-weight: 600; }}
.form-section {{ margin-top: 58px; }}
.form {{
  background: #fff; border-radius: 18px; padding: 28px;
  border: 1px solid rgba(201,169,97,.22);
  box-shadow: 0 2px 12px rgba(92,34,51,.04);
}}
.form-grid {{
  display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 14px 18px;
}}
label {{ display: block; font-size: 12.5px; color: #6A5546; margin-bottom: 4px; }}
input, textarea {{
  width: 100%; padding: 10px 12px; border: 1px solid #E5D7BE; border-radius: 10px; box-sizing: border-box;
  font-family: inherit; font-size: 14px; background: #FEFBF5; color: #3D2B1F; outline: none; transition: border-color .15s;
}}
input:focus, textarea:focus {{ border-color: #C9A961; background: #fff; }}
textarea {{ resize: vertical; min-height: 72px; }}
.submit-row {{ margin-top: 20px; display: flex; align-items: center; justify-content: space-between; gap: 14px; flex-wrap: wrap; }}
.btn-submit {{
  border: none; padding: 12px 26px; border-radius: 28px; cursor: pointer; font-size: 14.5px; font-weight: 600;
  background: linear-gradient(135deg, #C9A961, #9C7B3A); color: #3D2B1F; letter-spacing: 1px;
  box-shadow: 0 4px 16px rgba(201,169,97,.35); transition: transform .15s ease, box-shadow .15s ease;
}}
.btn-submit:hover {{ transform: translateY(-1px); box-shadow: 0 6px 22px rgba(201,169,97,.45); }}
.msg-box {{ min-height: 22px; font-size: 13px; }}
.ok {{ color: #2E7D4F; font-weight: 600; }}
.err {{ color: #B33A3A; font-weight: 600; }}
.footer {{ text-align: center; color: #8A6F58; font-size: 12px; margin-top: 44px; padding-top: 18px; border-top: 1px dashed #E5D7BE; }}
.footer-certs {{ color: #12473D; font-weight: 600; font-size: 12.5px; letter-spacing: 1px; margin-bottom: 8px; }}
section[id] {{ scroll-margin-top: 64px; }}
/* ===== 吸顶锚点导航 ===== */
.site-nav {{
  position: sticky; top: 0; z-index: 50;
  background: rgba(251,246,236,.9); backdrop-filter: blur(10px);
  border-bottom: 1px solid rgba(21,72,61,.14);
}}
.site-nav-in {{ max-width: 1160px; margin: 0 auto; padding: 12px 20px; display: flex; align-items: center; justify-content: space-between; }}
.site-nav-brand {{ font-weight: 700; color: #12473D; letter-spacing: 2px; font-size: 15px; }}
.site-nav-links a {{ color: #12473D; text-decoration: none; font-size: 13.5px; margin-left: 24px; opacity: .75; transition: opacity .15s; }}
.site-nav-links a:hover {{ opacity: 1; text-decoration: underline; text-underline-offset: 4px; }}
/* ===== 品牌故事（墨玉绿，与酒红金品牌头区分） ===== */
.about {{
  position: relative; overflow: hidden; color: #EAF4EE;
  background: linear-gradient(155deg, #0B322A 0%, #11473C 48%, #18584A 100%);
}}
.about::before {{
  content: ''; position: absolute; top: -120px; right: -90px; width: 340px; height: 340px; border-radius: 50%;
  background: radial-gradient(circle, rgba(94,196,151,.20) 0%, transparent 70%);
}}
.about::after {{
  content: 'SINCE 2009'; position: absolute; right: 16px; bottom: -20px; font-size: 84px; font-weight: 800;
  color: rgba(255,255,255,.045); letter-spacing: 6px; pointer-events: none; white-space: nowrap;
}}
.about-wrap {{
  position: relative; z-index: 1; max-width: 1160px; margin: 0 auto; padding: 66px 20px 8px;
  display: grid; grid-template-columns: 1.25fr .9fr; gap: 50px; align-items: center;
}}
.about-kicker {{
  display: inline-block; font-size: 12px; letter-spacing: 3px; color: #8FE0BC;
  border: 1px solid rgba(143,224,188,.4); border-radius: 20px; padding: 4px 14px; margin-bottom: 20px;
}}
.about-text h2 {{ margin: 0 0 20px; font-size: 28px; line-height: 1.5; color: #F3F9F6; letter-spacing: 1px; }}
.about-text p {{ margin: 0 0 14px; font-size: 14.5px; line-height: 2; color: rgba(234,244,238,.86); }}
.about-cats {{ margin-top: 22px; font-size: 13px; color: #8FE0BC; letter-spacing: 2px; }}
.about-stats {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
.stat {{
  background: rgba(255,255,255,.06); border: 1px solid rgba(143,224,188,.18);
  border-radius: 16px; padding: 26px 18px; text-align: center;
}}
.stat b {{ display: block; font-size: 30px; color: #8FE0BC; font-family: Georgia, 'Times New Roman', serif; margin-bottom: 6px; }}
.stat span {{ font-size: 12.5px; color: rgba(234,244,238,.78); }}
/* ===== 四大承诺（同墨绿章节，玻璃卡） ===== */
.promise-inner {{ max-width: 1160px; margin: 0 auto; padding: 56px 20px 64px; }}
.promise-head {{ text-align: center; margin-bottom: 34px; }}
.promise-head h2 {{ margin: 0 0 8px; font-size: 25px; color: #F3F9F6; letter-spacing: 2px; }}
.promise-head p {{ margin: 0; font-size: 13.5px; color: rgba(234,244,238,.66); }}
.promise-grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 18px; }}
.p-card {{
  background: rgba(255,255,255,.05); border: 1px solid rgba(143,224,188,.16); border-radius: 18px;
  padding: 30px 22px; text-align: center;
  transition: transform .2s ease, background .2s ease, border-color .2s ease;
}}
.p-card:hover {{ transform: translateY(-5px); background: rgba(255,255,255,.09); border-color: rgba(143,224,188,.42); }}
.p-ico {{
  width: 58px; height: 58px; margin: 0 auto 16px; border-radius: 50%;
  background: rgba(143,224,188,.14); display: flex; align-items: center; justify-content: center; font-size: 26px;
}}
.p-card h3 {{ margin: 0 0 10px; font-size: 17px; color: #F3F9F6; }}
.p-card p {{ margin: 0; font-size: 12.5px; line-height: 1.85; color: rgba(234,244,238,.75); }}
@media (max-width: 900px) {{
  .about-wrap {{ grid-template-columns: 1fr; gap: 32px; padding: 48px 18px 0; }}
  .promise-grid {{ grid-template-columns: 1fr 1fr; }}
}}
@media (max-width: 640px) {{
  .hero {{ padding: 54px 16px 42px; }}
  .hero h1 {{ font-size: 24px; }}
  .wrap {{ padding: 32px 14px 48px; }}
  .section-title h2 {{ font-size: 20px; }}
  .sc-grid {{ gap: 16px; }}
  .form {{ padding: 20px; }}
  .site-nav-brand {{ display: none; }}
  .site-nav-in {{ justify-content: center; }}
  .site-nav-links a {{ margin: 0 10px; }}
  .about-text h2 {{ font-size: 22px; }}
  .about::after {{ font-size: 52px; bottom: -12px; }}
  .promise-grid {{ grid-template-columns: 1fr; }}
  .stat b {{ font-size: 24px; }}
}}
</style></head><body>
<section class="hero">
  <div class="brand-logo"><img src="/logo/logo.svg" alt="{name} logo" onerror="this.remove(); this.parentElement.innerHTML='臻';"></div>
  <h1>{name}</h1>
  <div class="slogan">{slogan}</div>
  <p class="intro">{intro}</p>
  <div class="meta">
    { (f'<span>{icon_loc}{addr}</span>' if addr else '') }
    { (f'<span>{icon_phone}{phone}</span>' if phone else '') }
    { (f'<span>{icon_clock}{hours}</span>' if hours else '') }
  </div>
</section>
<nav class="site-nav">
  <div class="site-nav-in">
    <span class="site-nav-brand">{name}</span>
    <span class="site-nav-links">
      <a href="#about">品牌故事</a><a href="#showcase">臻品橱窗</a><a href="#booking">预约到店</a>
    </span>
  </div>
</nav>
<section class="about" id="about">
  <div class="about-wrap">
    <div class="about-text">
      <span class="about-kicker">SINCE 2009 · 品牌故事</span>
      <h2>十七年只做一件事<br>让每件珠宝都经得起岁月</h2>
      <p>{name}创立于 2009 年，前身为老街上一间三十平米的打金铺。十七年来，我们坚持从深圳水贝、云南腾冲源头直选金料与裸石，每件成品均经 NGTC / GIA 权威检测，一物一证，支持全国复检。</p>
      <p>如今，懿臻已发展为集黄金、钻石、翡翠、彩宝销售与高级定制于一体的珠宝门店，驻店师傅平均从业 20 年以上。我们相信，好的珠宝从不只是商品——它陪人走过求婚、结婚、弥月、纪念等一生里最重要的时刻。</p>
      <div class="about-cats">黄金首饰 · 钻石婚戒 · 翡翠玉石 · 彩宝定制 · 投资金条</div>
    </div>
    <div class="about-stats">
      <div class="stat"><b>17年</b><span>匠心经营</span></div>
      <div class="stat"><b>10000+</b><span>客户的共同选择</span></div>
      <div class="stat"><b>100%</b><span>一物一证 · 支持复检</span></div>
      <div class="stat"><b>20年</b><span>驻店师傅平均工龄</span></div>
    </div>
  </div>
  <div class="promise-inner">
    <div class="promise-head">
      <h2>四大安心承诺</h2>
      <p>从选料到售后，每个环节都写进我们的店规</p>
    </div>
    <div class="promise-grid">{promise_cards_html}</div>
  </div>
</section>
<div class="wrap">
  <div class="section-title" id="showcase">
    <div><h2>{sh_title}</h2><span class="sub">{sh_sub}</span></div>
    <span class="sub">共 {len(rows)} 件臻品 · 官方直营 · 假一赔十</span>
  </div>
  <div class="sc-grid">{cards_html}</div>

  <section class="form-section" id="booking">
    <div class="section-title">
      <div><h2>预约到店</h2><span class="sub">专属顾问一对一 · VIP 私享鉴赏</span></div>
    </div>
    <div class="form">
      <form id="bookForm" onsubmit="return book(event)">
        <div class="form-grid">
          <div><label>您的称呼 *</label><input name="name" id="f-name" placeholder="请输入姓名" required></div>
          <div><label>联系方式 *</label><input name="contact" id="f-contact" placeholder="手机 / 微信" required></div>
          <div><label>意向品类</label><input name="category" id="f-category" placeholder="如 钻石、黄金、翡翠"></div>
          <div><label>目标商品（可填名称）</label><input name="product" id="f-product" placeholder="点击「预约看货」可自动填入"></div>
          <div><label>期望到店日期</label><input name="want_date" type="date"></div>
          <div><label>期望时段</label><input name="want_slot" placeholder="如 14:00 - 16:00"></div>
        </div>
        <label style="margin-top:14px">备注说明</label>
        <textarea name="remark" placeholder="可填写具体需求，如圈号、预算、送礼场合等"></textarea>
        <div class="submit-row">
          <button class="btn-submit" type="submit">✦ 提交 VIP 预约 ✦</button>
          <div id="msg" class="msg-box"></div>
        </div>
      </form>
    </div>
  </section>
  <div class="footer">
    <div class="footer-certs">NGTC / GIA 权威检测合作 · 假一赔十 · 终身免费养护</div>
    © {name} · 以臻金品质 铸一世珍藏
  </div>
</div>
<script>
function focusBook(name, cat, code) {{
  const fp = document.getElementById('f-product');
  const fc = document.getElementById('f-category');
  if (fp) fp.value = code ? ('货号' + code + ' ' + name) : name;
  if (fc && !fc.value) fc.value = cat;
  fp && fp.scrollIntoView({{ behavior:'smooth', block:'center' }});
  fp && fp.focus();
}}
function book(e) {{
  e.preventDefault();
  var f = e.target;
  var msg = document.getElementById('msg');
  msg.className = ''; msg.textContent = '正在提交…';
  var b = {{
    name: f.name.value.trim(),
    contact: f.contact.value.trim(),
    contact_type: 'phone',
    category: f.category.value.trim(),
    want_date: f.want_date.value,
    want_slot: f.want_slot.value.trim(),
    remark: (f.product.value.trim() ? ('意向商品：' + f.product.value + '\\n') : '') + f.remark.value
  }};
  fetch('/api/public/appointments', {{
    method: 'POST',
    headers: {{ 'Content-Type': 'application/json' }},
    body: JSON.stringify(b)
  }}).then(r=>r.json()).then(d=>{{
    msg.textContent = d.detail || '预约已提交，专属顾问将尽快与您联系';
    msg.className = 'ok';
    f.reset();
  }}).catch(()=>{{
    msg.textContent = '网络繁忙，请稍后重试或直接致电门店';
    msg.className = 'err';
  }});
  return false;
}}
</script>
</body></html>"""
    return HTMLResponse(html)


# ---------------------------------------------------------------- 登录

class LoginReq(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


@app.post("/api/auth/login")
def api_login(body: LoginReq, request: Request):
    with _db(request) as conn:
        user = conn.execute("SELECT * FROM users WHERE username=?", (body.username,)).fetchone()
        if not user or user["password"] != body.password:
            raise HTTPException(status_code=401, detail="用户名或密码错误")
        token = secrets.token_urlsafe(24)
        info = {"username": user["username"], "display_name": user["display_name"], "role": user["role"]}
        with _lock:
            _sessions[token] = info
        _log(conn, user["username"], "登录", user["username"])
        mode = _operator_mode(request)
        return {
            "token": token,
            "user": info,
            "features": FEATURES,
            "mode": mode,
            "tenant": _tenant_of(request),
        }


@app.post("/api/auth/logout")
def api_logout(request: Request):
    token = request.headers.get("Authorization", "").replace("Bearer ", "").strip()
    with _lock:
        _sessions.pop(token, None)
    return {"ok": True}


@app.get("/api/auth/me")
def api_me(request: Request):
    u = _require_auth(request)
    return {"user": u, "features": FEATURES, "mode": _operator_mode(request), "tenant": _tenant_of(request)}


# ---------------------------------------------------------------- 看板 / 店铺

@app.get("/api/dashboard/overview")
def dashboard_overview(request: Request):
    _require_auth(request)
    with _db(request) as conn:
        today = date.today().isoformat()
        stock = conn.execute("SELECT COUNT(*) n, COALESCE(SUM(cost),0) c FROM products WHERE status='在库'").fetchone()
        reserved = conn.execute("SELECT COUNT(*) n FROM products WHERE status='已定'").fetchone()
        loaned = conn.execute("SELECT COUNT(*) n FROM products WHERE status='借出'").fetchone()
        today_sales = conn.execute(
            "SELECT COALESCE(SUM(amount),0) a, COUNT(*) n FROM sales WHERE biz_date=? AND status!='已冲红'", (today,)
        ).fetchone()
        month = today[:7]
        month_sales = conn.execute(
            "SELECT COALESCE(SUM(amount),0) a FROM sales WHERE substr(biz_date,1,7)=? AND status!='已冲红'", (month,)
        ).fetchone()
        deposit = conn.execute("SELECT COALESCE(SUM(balance),0) a FROM deposits WHERE status='已定'").fetchone()
        due = conn.execute("SELECT COALESCE(SUM(due_amount),0) a FROM customers").fetchone()
        loans = conn.execute("SELECT COUNT(*) n FROM loans WHERE status IN ('借出中','借入中')").fetchone()
        repairs = conn.execute("SELECT COUNT(*) n FROM repairs WHERE status NOT IN ('已完成','已取走')").fetchone()
        recent = conn.execute(
            "SELECT bill_no, customer, amount, method, biz_date, status FROM sales ORDER BY id DESC LIMIT 8"
        ).fetchall()
        return {
            "tenant": _tenant_of(request),
            "date": today,
            "stock": {"count": stock["n"], "cost": round(stock["c"], 2), "reserved": reserved["n"], "loaned": loaned["n"]},
            "todaySales": {"amount": round(today_sales["a"], 2), "count": today_sales["n"]},
            "monthSales": {"amount": round(month_sales["a"], 2)},
            "depositPending": {"amount": round(deposit["a"], 2)},
            "customerDue": {"amount": round(due["a"], 2)},
            "loansActive": loans["n"],
            "repairsActive": repairs["n"],
            "recentSales": [dict(r) for r in recent],
        }


@app.get("/api/dashboard/trend")
def dashboard_trend(request: Request):
    _require_auth(request)
    with _db(request) as conn:
        rows = conn.execute("""
            SELECT substr(biz_date,1,7) m, COALESCE(SUM(amount),0) a
            FROM sales WHERE status!='已冲红' AND biz_date >= date('now','start of month','-5 months')
            GROUP BY m ORDER BY m
        """).fetchall()
        return {"months": [r["m"] for r in rows], "amounts": [round(r["a"], 2) for r in rows]}


@app.get("/api/dashboard/reminders")
def dashboard_reminders(request: Request):
    _require_auth(request)
    with _db(request) as conn:
        deposits_urgent = conn.execute(
            "SELECT * FROM deposits WHERE status='已定' AND promised_date <= date('now','+7 days') ORDER BY promised_date LIMIT 8"
        ).fetchall()
        loans_overdue = conn.execute(
            "SELECT * FROM loans WHERE status IN ('借出中','借入中') AND due_date < date('now') ORDER BY due_date LIMIT 8"
        ).fetchall()
        repairs_pending = conn.execute(
            "SELECT * FROM repairs WHERE status IN ('待维修','维修中') ORDER BY promised_date LIMIT 8"
        ).fetchall()
        return {
            "deposits": [dict(r) for r in deposits_urgent],
            "loansOverdue": [dict(r) for r in loans_overdue],
            "repairsPending": [dict(r) for r in repairs_pending],
        }


@app.get("/api/profile")
def profile_get(request: Request):
    _require_auth(request)
    with _db(request) as conn:
        row = conn.execute("SELECT * FROM tenant_profiles ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else {}


class ProfileIn(BaseModel):
    name: str = ""
    short_name: str = ""
    slogan: str = ""
    intro: str = ""
    contact: str = ""
    phone: str = ""
    address: str = ""
    hours: str = ""
    categories: str = ""
    published: int = 1
    showcase_title: str = "新品橱窗"
    showcase_subtitle: str = "本周臻品 · 限量发售"


@app.put("/api/profile")
def profile_put(body: ProfileIn, request: Request):
    op = _require_auth(request)
    with _db(request) as conn:
        row = conn.execute("SELECT id FROM tenant_profiles ORDER BY id DESC LIMIT 1").fetchone()
        vals = (body.name, body.short_name, body.slogan, body.intro, body.contact, body.phone, body.address,
                body.hours, body.categories, body.published, body.showcase_title, body.showcase_subtitle)
        if row:
            conn.execute(
                """UPDATE tenant_profiles SET name=?,short_name=?,slogan=?,intro=?,contact=?,phone=?,address=?,
                   hours=?,categories=?,published=?,showcase_title=?,showcase_subtitle=? WHERE id=?""",
                vals + (row["id"],),
            )
        else:
            conn.execute(
                """INSERT INTO tenant_profiles(tenant_id,name,short_name,slogan,intro,contact,phone,address,
                   hours,categories,published,showcase_title,showcase_subtitle)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (_tenant_of(request),) + vals,
            )
        conn.commit()
        _log(conn, op["username"], "更新店铺资料", body.name)
        return {"ok": True}


# ---------------------------------------------------------------- 商品

class ProductIn(BaseModel):
    code: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=80)
    category: str = "黄金"
    material: str = ""
    weight: float = 0
    size: str = ""
    cert: str = ""
    cost: float = 0
    price: float = 0
    status: str = "在库"
    store_id: int | None = 1
    rfid_epc: str = ""
    name_i18n: str = "{}"
    category_code: str = ""
    showcase_public: int = 0
    showcase_order: int = 0
    showcase_desc: str = ""
    origin: str = ""
    high_value: int = 0


# ==================== 语言 / 业务配置 / 分类 ====================

@app.get("/api/languages")
def language_list(request: Request):
    _require_auth(request)
    with _db(request) as conn:
        rows = conn.execute("SELECT code,name,is_default,sort_order FROM languages ORDER BY sort_order").fetchall()
        return [{"code": r[0], "name": r[1], "is_default": bool(r[2]), "sort_order": r[3]} for r in rows]


@app.get("/api/biz-config")
def biz_config_get(request: Request):
    _require_auth(request)
    with _db(request) as conn:
        r = conn.execute("SELECT epc_prefix,seq_bits FROM biz_config WHERE id=1").fetchone()
        if not r:
            conn.execute("INSERT OR IGNORE INTO biz_config(id,epc_prefix,seq_bits) VALUES(1,'E280',8)")
            conn.commit()
            r = ("E280", 8)
        return {"epc_prefix": r[0], "seq_bits": r[1]}


class BizConfigUpdate(BaseModel):
    epc_prefix: str
    seq_bits: int = 8


@app.put("/api/biz-config")
def biz_config_update(request: Request, body: BizConfigUpdate):
    _require_auth(request)
    with _db(request) as conn:
        conn.execute("UPDATE biz_config SET epc_prefix=?, seq_bits=? WHERE id=1",
                     (body.epc_prefix.strip().upper(), body.seq_bits))
        conn.commit()
    return {"ok": True}


@app.get("/api/categories")
def category_list(request: Request):
    _require_auth(request)
    with _db(request) as conn:
        rows = conn.execute("SELECT code,names,sort_order FROM categories ORDER BY sort_order").fetchall()
        return [{"code": r[0], "names": json.loads(r[1] or "{}"), "sort_order": r[2]} for r in rows]


class CategoryUpdate(BaseModel):
    code: str
    names: dict
    sort_order: int = 0


@app.post("/api/categories")
def category_create(request: Request, body: CategoryUpdate):
    _require_auth(request)
    with _db(request) as conn:
        conn.execute("INSERT INTO categories(code,names,sort_order) VALUES(?,?,?)",
                     (body.code, json.dumps(body.names, ensure_ascii=False), body.sort_order))
        conn.commit()
    return {"ok": True, "code": body.code}


@app.put("/api/categories/{code}")
def category_update(request: Request, code: str, body: CategoryUpdate):
    _require_auth(request)
    with _db(request) as conn:
        conn.execute("UPDATE categories SET names=?, sort_order=? WHERE code=?",
                     (json.dumps(body.names, ensure_ascii=False), body.sort_order, code))
        conn.commit()
    return {"ok": True}


@app.delete("/api/categories/{code}")
def category_delete(request: Request, code: str):
    _require_auth(request)
    with _db(request) as conn:
        conn.execute("DELETE FROM categories WHERE code=?", (code,))
        conn.commit()
    return {"ok": True}


@app.post("/api/products/{pid}/generate-epc")
def product_generate_epc(request: Request, pid: int):
    """按 EPC 前缀+分类码+序号 生成并回写 RFID EPC。"""
    _require_auth(request)
    with _db(request) as conn:
        cfg = conn.execute("SELECT epc_prefix,seq_bits FROM biz_config WHERE id=1").fetchone()
        prefix, seq_bits = (cfg[0], cfg[1]) if cfg else ("E280", 8)
        p = conn.execute("SELECT category_code, rfid_epc FROM products WHERE id=?", (pid,)).fetchone()
        if not p:
            raise HTTPException(404, "商品不存在")
        if p[1]:
            return {"ok": True, "epc": p[1], "reused": True}
        cat_code = p[0] or "00"
        # 当前分类最大序号+1
        like = f"{prefix}{cat_code}%"
        row = conn.execute("SELECT MAX(CAST(SUBSTR(rfid_epc, ?) AS INTEGER)) FROM products WHERE rfid_epc LIKE ?",
                           (len(prefix) + len(cat_code) + 1, like)).fetchone()
        seq = (row[0] or 0) + 1
        epc = f"{prefix}{cat_code}{seq:0{seq_bits}X}"
        conn.execute("UPDATE products SET rfid_epc=? WHERE id=?", (epc, pid))
        conn.commit()
        return {"ok": True, "epc": epc, "seq": seq}


@app.get("/api/products")
def product_list(request: Request, q: str = "", status: str = "", page: int = 1, size: int = 80):
    _require_auth(request)
    with _db(request) as conn:
        where, args = [], []
        if q:
            where.append("(name LIKE ? OR code LIKE ? OR rfid_epc LIKE ?)")
            args += [f"%{q}%", f"%{q}%", f"%{q}%"]
        if status:
            where.append("status=?")
            args.append(status)
        cond = ("WHERE " + " AND ".join(where)) if where else ""
        total = conn.execute(f"SELECT COUNT(*) n FROM products {cond}", args).fetchone()["n"]
        rows = conn.execute(
            f"SELECT * FROM products {cond} ORDER BY id DESC LIMIT ? OFFSET ?",
            args + [size, (page - 1) * size],
        ).fetchall()
        return {"total": total, "page": page, "size": size, "items": [dict(r) for r in rows]}


@app.get("/api/products/options")
def product_options(request: Request, status: str = "在库"):
    _require_auth(request)
    with _db(request) as conn:
        if status:
            rows = conn.execute("SELECT id, code, name, price, status, rfid_epc FROM products WHERE status=? ORDER BY id", (status,)).fetchall()
        else:
            rows = conn.execute("SELECT id, code, name, price, status, rfid_epc FROM products ORDER BY id").fetchall()
        return {"items": [dict(r) for r in rows]}


@app.get("/api/products/{pid}")
def product_detail(pid: int, request: Request):
    _require_auth(request)
    with _db(request) as conn:
        row = conn.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
        if not row:
            raise HTTPException(404, "商品不存在")
        return dict(row)


@app.post("/api/products")
def product_create(body: ProductIn, request: Request):
    op = _require_auth(request)
    with _db(request) as conn:
        _assert_stock_unfrozen(conn)
        if conn.execute("SELECT 1 FROM products WHERE code=?", (body.code,)).fetchone():
            raise HTTPException(400, "商品编码已存在")
        epc = body.rfid_epc or ""
        cur = conn.execute(
            """INSERT INTO products(code,name,name_i18n,category,category_code,material,weight,size,cert,cost,price,status,store_id,rfid_epc,
                                     showcase_public,showcase_order,showcase_desc,origin,high_value)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (body.code, body.name, body.name_i18n, body.category, body.category_code, body.material, body.weight, body.size, body.cert,
             body.cost, body.price, body.status, body.store_id, epc,
             body.showcase_public, body.showcase_order, body.showcase_desc, body.origin, body.high_value),
        )
        _inv(conn, cur.lastrowid, epc, "in", op["username"])
        conn.commit()
        _log(conn, op["username"], "新增商品", body.code)
        return {"id": cur.lastrowid}


@app.put("/api/products/{pid}")
def product_update(pid: int, body: ProductIn, request: Request):
    op = _require_auth(request)
    with _db(request) as conn:
        _assert_stock_unfrozen(conn)
        if not conn.execute("SELECT 1 FROM products WHERE id=?", (pid,)).fetchone():
            raise HTTPException(404, "商品不存在")
        if conn.execute("SELECT 1 FROM products WHERE code=? AND id<>?", (body.code, pid)).fetchone():
            raise HTTPException(400, "商品编码已存在")
        conn.execute(
            """UPDATE products SET code=?,name=?,name_i18n=?,category=?,category_code=?,material=?,weight=?,size=?,cert=?,
               cost=?,price=?,status=?,store_id=?,rfid_epc=?,showcase_public=?,
               showcase_order=?,showcase_desc=?,origin=?,high_value=? WHERE id=?""",
            (body.code, body.name, body.name_i18n, body.category, body.category_code, body.material, body.weight, body.size, body.cert,
             body.cost, body.price, body.status, body.store_id, body.rfid_epc, body.showcase_public,
             body.showcase_order, body.showcase_desc, body.origin, body.high_value, pid),
        )
        conn.commit()
        _log(conn, op["username"], "修改商品", f"#{pid} {body.code}")
        return {"ok": True}


@app.delete("/api/products/{pid}")
def product_delete(pid: int, request: Request):
    op = _require_auth(request)
    with _db(request) as conn:
        _assert_stock_unfrozen(conn)
        row = conn.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
        if not row:
            raise HTTPException(404, "商品不存在")
        if row["status"] not in ("在库",):
            raise HTTPException(400, "仅允许删除在库商品")
        conn.execute("DELETE FROM products WHERE id=?", (pid,))
        conn.commit()
        _log(conn, op["username"], "删除商品", f"#{pid}")
        return {"ok": True}


class PrintLabelIn(BaseModel):
    template: str = "fold"
    copies: int = 1


@app.post("/api/products/{pid}/print-label")
def product_print_label(pid: int, body: PrintLabelIn, request: Request):
    op = _require_auth(request)
    with _db(request) as conn:
        row = conn.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
        if not row:
            raise HTTPException(404, "商品不存在")
        epc = row["rfid_epc"] or _gen_epc(row["code"])
        conn.execute("UPDATE products SET rfid_epc=? WHERE id=?", (epc, pid))
        _inv(conn, pid, epc, "rfid", op["username"], body.copies)
        conn.commit()
        _log(conn, op["username"], "RFID标签打印", f"{row['code']} {epc}")
        return {"ok": True, "rfid_epc": epc, "product": dict(row) | {"rfid_epc": epc}, "copies": body.copies, "template": body.template}


# ---------------------------------------------------------------- 库存 / RFID

@app.get("/api/inventory/summary")
def inventory_summary(request: Request):
    _require_auth(request)
    with _db(request) as conn:
        def cnt(st):
            r = conn.execute("SELECT COUNT(*) n, COALESCE(SUM(cost),0) c FROM products WHERE status=?", (st,)).fetchone()
            return {"count": r["n"], "cost": round(r["c"], 2)}
        logs = conn.execute("SELECT * FROM inventory_logs ORDER BY id DESC LIMIT 30").fetchall()
        return {
            "inStock": cnt("在库"),
            "reserved": cnt("已定"),
            "loaned": cnt("借出"),
            "sold": cnt("已售"),
            "logs": [dict(r) for r in logs],
        }


class InboundIn(BaseModel):
    code: str = ""
    name: str
    category: str = "黄金"
    material: str = ""
    weight: float = 0
    size: str = ""
    cert: str = ""
    cost: float = 0
    price: float = 0
    rfid_epc: str = ""
    biz_date: str = ""


@app.post("/api/inventory/inbound")
def inventory_inbound(body: InboundIn, request: Request):
    op = _require_auth(request)
    with _db(request) as conn:
        _assert_stock_unfrozen(conn)
        code = body.code.strip() or _next_code(conn)
        if conn.execute("SELECT 1 FROM products WHERE code=?", (code,)).fetchone():
            raise HTTPException(400, "商品编码已存在")
        epc = body.rfid_epc or _gen_epc(code)
        cur = conn.execute(
            """INSERT INTO products(code,name,category,material,weight,size,cert,cost,price,status,store_id,rfid_epc,showcase_public)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0)""",
            (code, body.name, body.category, body.material, body.weight, body.size, body.cert, body.cost, body.price, "在库", 1, epc),
        )
        _inv(conn, cur.lastrowid, epc, "in", op["username"])
        conn.commit()
        _log(conn, op["username"], "入库登记", code)
        return {"id": cur.lastrowid, "code": code, "rfid_epc": epc}


@app.post("/api/inventory/copy/{pid}")
def inventory_copy(pid: int, request: Request):
    op = _require_auth(request)
    with _db(request) as conn:
        _assert_stock_unfrozen(conn)
        src = conn.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
        if not src:
            raise HTTPException(404, "源商品不存在")
        code = _next_code(conn)
        epc = _gen_epc(code)
        cur = conn.execute(
            """INSERT INTO products(code,name,category,material,weight,size,cert,cost,price,status,store_id,rfid_epc,showcase_public)
               VALUES(?,?,?,?,?,?,?,?,?,'在库',?, ?,0)""",
            (code, src["name"], src["category"], src["material"], src["weight"], src["size"], src["cert"], src["cost"], src["price"], src["store_id"], epc),
        )
        _inv(conn, cur.lastrowid, epc, "in", op["username"])
        conn.commit()
        _log(conn, op["username"], "复制入库", f"{src['code']}→{code}")
        return {"id": cur.lastrowid, "code": code}


class RfidScanIn(BaseModel):
    epcs: list[str] = Field(default_factory=list)
    simulate: bool = False


@app.post("/api/inventory/rfid-scan")
def rfid_scan(body: RfidScanIn, request: Request):
    op = _require_auth(request)
    with _db(request) as conn:
        book = conn.execute("SELECT * FROM products WHERE status IN ('在库','已定','借出') AND rfid_epc!=''").fetchall()
        book_map = {r["rfid_epc"]: dict(r) for r in book}
        epcs = [e.strip() for e in body.epcs if e and e.strip()]
        if body.simulate and not epcs:
            epcs = list(book_map.keys())
        scanned = []
        seen = set()
        surplus = []
        for epc in epcs:
            if epc in seen:
                continue
            seen.add(epc)
            _inv(conn, book_map.get(epc, {}).get("id"), epc, "scan", op["username"])
            if epc in book_map:
                p = book_map[epc]
                scanned.append({"epc": epc, "product": p["name"], "code": p["code"], "status": p["status"], "result": "账实相符"})
            else:
                surplus.append({"epc": epc, "product": "", "code": "", "status": "", "result": "盘盈"})
        shortage = []
        for epc, p in book_map.items():
            if epc not in seen:
                shortage.append({"epc": epc, "product": p["name"], "code": p["code"], "status": p["status"], "result": "盘亏"})
        conn.commit()
        _log(conn, op["username"], "RFID隔空盘点", f"感应{len(seen)} 盘盈{len(surplus)} 盘亏{len(shortage)}")
        return {
            "scannedCount": len(seen),
            "bookCount": len(book_map),
            "matched": scanned,
            "surplus": surplus,
            "shortage": shortage,
            "items": scanned + surplus + shortage,
        }


# ---------------------------------------------------------------- 盘点任务（手持机序列号方案）

@app.post("/api/inventory/tasks")
def inventory_task_create(request: Request):
    op = _require_auth(request)
    with _db(request) as conn:
        _assert_stock_unfrozen(conn)
        items = conn.execute(
            "SELECT id, code, name, rfid_epc, status FROM products "
            "WHERE status IN ('在库','已定','借出') AND rfid_epc!='' ORDER BY id"
        ).fetchall()
        seq_map = {}
        task_items = []
        for i, r in enumerate(items, 1):
            seq_map[r["rfid_epc"]] = i
            task_items.append({"seq": i, "epc": r["rfid_epc"]})
        task_no = f"PD{datetime.now().strftime('%Y%m%d%H%M%S')}"
        cur = conn.execute(
            "INSERT INTO inventory_tasks(task_no,status,operator,created_at) VALUES(?,?,?,?)",
            (task_no, "scanning", op["username"], datetime.now().isoformat()),
        )
        tid = cur.lastrowid
        for r in items:
            conn.execute(
                "INSERT INTO inventory_task_items(task_id,seq,product_id,code,name,epc,book_status) "
                "VALUES(?,?,?,?,?,?,?)",
                (tid, seq_map[r["rfid_epc"]], r["id"], r["code"], r["name"], r["rfid_epc"], r["status"]),
            )
        conn.commit()
        return {"task_id": tid, "task_no": task_no, "total": len(items), "items": task_items}


@app.get("/api/inventory/tasks/{tid}/items")
def inventory_task_items(tid: int, request: Request):
    _require_auth(request)
    with _db(request) as conn:
        rows = conn.execute(
            "SELECT seq, epc FROM inventory_task_items WHERE task_id=? ORDER BY seq", (tid,)
        ).fetchall()
        return {"items": [{"seq": r["seq"], "epc": r["epc"]} for r in rows]}


class TaskScanIn(BaseModel):
    seqs: list[int] = Field(default_factory=list)
    unknown_epcs: list[str] = Field(default_factory=list)


@app.post("/api/inventory/tasks/{tid}/scan")
def inventory_task_scan(tid: int, body: TaskScanIn, request: Request):
    _require_auth(request)
    with _db(request) as conn:
        for s in body.seqs:
            conn.execute(
                "UPDATE inventory_task_items SET scan_status='found', scanned_at=? WHERE task_id=? AND seq=?",
                (datetime.now().isoformat(), tid, s),
            )
        for epc in body.unknown_epcs:
            conn.execute(
                "INSERT OR IGNORE INTO inventory_task_unknown(task_id,epc) VALUES(?,?)",
                (tid, epc),
            )
        conn.commit()
        found = conn.execute(
            "SELECT COUNT(*) FROM inventory_task_items WHERE task_id=? AND scan_status='found'", (tid,)
        ).fetchone()[0]
        unknown = conn.execute(
            "SELECT COUNT(*) FROM inventory_task_unknown WHERE task_id=?", (tid,)
        ).fetchone()[0]
        return {"found": found, "unknown": unknown}


@app.post("/api/inventory/tasks/{tid}/finish")
def inventory_task_finish(tid: int, request: Request):
    op = _require_auth(request)
    with _db(request) as conn:
        surplus = conn.execute(
            "SELECT epc FROM inventory_task_unknown WHERE task_id=?", (tid,)
        ).fetchall()
        shortage = conn.execute(
            "SELECT code, name, epc FROM inventory_task_items "
            "WHERE task_id=? AND (scan_status IS NULL OR scan_status='pending') AND seq>0",
            (tid,),
        ).fetchall()
        conn.execute(
            "UPDATE inventory_tasks SET status='done', finished_at=? WHERE id=?",
            (datetime.now().isoformat(), tid),
        )
        conn.commit()
        _log(conn, op["username"], "盘点完成", f"盘盈{len(surplus)} 盘亏{len(shortage)}")
        return {
            "task_id": tid,
            "surplus": [{"epc": r["epc"]} for r in surplus],
            "shortage": [{"code": r["code"], "name": r["name"], "epc": r["epc"]} for r in shortage],
        }


@app.post("/api/inventory/tasks/{tid}/cancel")
def inventory_task_cancel(tid: int, request: Request):
    op = _require_auth(request)
    with _db(request) as conn:
        conn.execute("UPDATE inventory_tasks SET status='cancelled' WHERE id=?", (tid,))
        conn.commit()
        _log(conn, op["username"], "盘点取消", f"任务{tid}")
        return {"ok": True}


@app.get("/api/inventory/lock-status")
def inventory_lock_status(request: Request):
    _require_auth(request)
    with _db(request) as conn:
        row = conn.execute(
            "SELECT id, task_no FROM inventory_tasks WHERE status='scanning' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return {"locked": row is not None, "task": dict(row) if row else None}


# ---------------------------------------------------------------- 标签排版打印

@app.get("/api/print/printers")
def print_printers(request: Request):
    _require_auth(request)
    names = rfid_print.list_printers()
    return {"supported": bool(names), "printers": names}


class LabelPrintIn(BaseModel):
    product_ids: list[int] = Field(default_factory=list)
    printer: str = ""
    copies: int = 1
    fields: list[str] = Field(default_factory=lambda: sorted(rfid_print.DEFAULT_FIELDS))
    write_epc: bool = True
    font: str = rfid_print.DEFAULT_FONT
    simulate: bool = False  # True 时只生成 ZPL 不实际发送


@app.post("/api/print/labels")
def print_labels(body: LabelPrintIn, request: Request):
    op = _require_auth(request)
    if not body.product_ids:
        raise HTTPException(400, "请至少选择一件商品")
    fields = {f for f in body.fields if f in rfid_print.FIELD_OPTIONS}
    with _db(request) as conn:
        prof = conn.execute("SELECT name FROM tenant_profiles ORDER BY id DESC LIMIT 1").fetchone()
        store_name = prof["name"] if prof and prof["name"] else ""
        rows = conn.execute(
            f"SELECT * FROM products WHERE id IN ({','.join('?' * len(body.product_ids))})",
            body.product_ids,
        ).fetchall()
        if len(rows) != len(set(body.product_ids)):
            raise HTTPException(404, "部分商品不存在")
        products = [dict(r) for r in rows]
        jobs = []
        for p in products:
            epc = p.get("rfid_epc") or _gen_epc(p["code"])
            if not p.get("rfid_epc"):
                conn.execute("UPDATE products SET rfid_epc=? WHERE id=?", (epc, p["id"]))
            p["rfid_epc"] = epc
            _inv(conn, p["id"], epc, "rfid", op["username"], body.copies)
            jobs.append({"id": p["id"], "code": p["code"], "name": p["name"], "rfid_epc": epc})
        zpl = rfid_print.build_batch_zpl(
            products, fields=fields, store_name=store_name,
            font=(body.font or ""), write_epc=body.write_epc, copies=body.copies,
        )
        sent = False
        error = ""
        if not body.simulate:
            if not body.printer:
                raise HTTPException(400, "未选择打印机（无打印机时可勾选“仅生成指令”）")
            try:
                rfid_print.send_raw(body.printer, zpl, job_title=f"jewelry-labels-{len(jobs)}")
                sent = True
            except Exception as e:
                raise HTTPException(500, f"发送打印机失败：{e}")
        conn.commit()
        _log(conn, op["username"], "RFID标签排版打印",
             f"{len(jobs)}件×{body.copies}张 {body.printer or '仅生成指令'}")
        return {
            "ok": True, "sent": sent, "count": len(jobs) * body.copies,
            "printer": body.printer or "", "jobs": jobs, "zpl": zpl,
        }


# ---------------------------------------------------------------- RFID 手持机盘点

# 盘点令牌：token -> (租户, 过期时间戳, 上传接口URL)
_stocktake_tokens: dict[str, tuple[str, float, str]] = {}
STOCKTAKE_TOKEN_TTL = 12 * 3600


def _lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def _db_for_tenant(tenant: str):
    return _db(SimpleNamespace(headers={"X-Resolved-Tenant-ID": tenant}))


def _next_batch_no(conn: sqlite3.Connection) -> str:
    return "PD" + datetime.now().strftime("%Y%m%d%H%M%S") + secrets.token_hex(1).upper()


def _run_stocktake(conn: sqlite3.Connection, epcs: list[str], device: str, operator: str) -> dict:
    """核心盘点：扫到的 EPC 集合对比账面，落库批次与明细，返回结果。"""
    book_rows = conn.execute(
        "SELECT * FROM products WHERE status IN ('在库','已定','借出') AND rfid_epc!=''"
    ).fetchall()
    book_map = {r["rfid_epc"]: dict(r) for r in book_rows}
    all_rows = conn.execute("SELECT * FROM products WHERE rfid_epc!=''").fetchall()
    all_map = {r["rfid_epc"]: dict(r) for r in all_rows}

    # 重复读取计数
    read_count: dict[str, int] = {}
    for e in epcs:
        read_count[e] = read_count.get(e, 0) + 1
    seen = list(dict.fromkeys(epcs))

    matched, surplus, shortage, abnormal = [], [], [], []
    for epc in seen:
        dup = read_count[epc] - 1
        if epc in book_map:
            p = book_map[epc]
            matched.append({"epc": epc, "code": p["code"], "product": p["name"],
                            "book_status": p["status"], "result": "相符", "dup_count": dup})
        elif epc in all_map:
            p = all_map[epc]
            abnormal.append({"epc": epc, "code": p["code"], "product": p["name"],
                             "book_status": p["status"], "result": f"异常（{p['status']}仍出现）", "dup_count": dup})
        else:
            surplus.append({"epc": epc, "code": "", "product": "",
                            "book_status": "", "result": "盘盈（未登记标签）", "dup_count": dup})
    for epc, p in book_map.items():
        if epc not in seen:
            shortage.append({"epc": epc, "code": p["code"], "product": p["name"],
                             "book_status": p["status"], "result": "盘亏（未扫到）", "dup_count": 0})

    batch_no = _next_batch_no(conn)
    cur = conn.execute(
        """INSERT INTO stocktakes(batch_no,device,scanned_count,book_count,matched_count,
                                  surplus_count,shortage_count,abnormal_count,dup_count,operator)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (batch_no, device, len(seen), len(book_map), len(matched),
         len(surplus), len(shortage), len(abnormal),
         sum(v - 1 for v in read_count.values() if v > 1), operator),
    )
    sid = cur.lastrowid
    for it in matched + surplus + shortage + abnormal:
        conn.execute(
            """INSERT INTO stocktake_items(stocktake_id,result,epc,product_id,code,product,book_status,dup_count)
               VALUES(?,?,?,?,?,?,?,?)""",
            (sid, it["result"], it["epc"],
             (book_map.get(it["epc"]) or all_map.get(it["epc"]) or {}).get("id"),
             it["code"], it["product"], it["book_status"], it["dup_count"]),
        )
        if it["epc"] in book_map:
            _inv(conn, book_map[it["epc"]]["id"], it["epc"], "scan", operator)
    conn.commit()
    _log(conn, operator, "RFID批量盘点",
         f"{batch_no} 扫描{len(seen)} 盘盈{len(surplus)} 盘亏{len(shortage)} 异常{len(abnormal)}")
    items = matched + abnormal + surplus + shortage
    return {
        "id": sid, "batch_no": batch_no, "device": device,
        "scannedCount": len(seen), "bookCount": len(book_map),
        "matchedCount": len(matched), "surplusCount": len(surplus),
        "shortageCount": len(shortage), "abnormalCount": len(abnormal),
        "matched": matched, "surplus": surplus, "shortage": shortage, "abnormal": abnormal,
        "items": items,
    }


class StocktakeSetupIn(BaseModel):
    host: str = ""  # 可手工指定手持机能访问的主机（如 192.168.1.20:8002）


@app.post("/api/stocktake/setup")
def stocktake_setup(body: StocktakeSetupIn, request: Request):
    op = _require_auth(request)
    tenant = _tenant_of(request)
    token = secrets.token_urlsafe(18)
    _stocktake_tokens[token] = (tenant, time.time() + STOCKTAKE_TOKEN_TTL)
    # 清理过期令牌
    now = time.time()
    for k in [k for k, v in _stocktake_tokens.items() if v[1] < now]:
        _stocktake_tokens.pop(k, None)
    host = (body.host or "").strip().rstrip("/")
    if not host:
        host = f"{_lan_ip()}:{PORT}"
    if not host.startswith("http"):
        host = "http://" + host
    url = f"{host}/api/stocktake/upload?key={token}"
    _stocktake_tokens[token] = (tenant, time.time() + STOCKTAKE_TOKEN_TTL, url)
    return {"url": url, "token": token, "expires_in": STOCKTAKE_TOKEN_TTL,
            "page_url": f"{host}/stocktake/upload?key={token}", "operator": op["username"]}


@app.get("/api/stocktake/qr")
def stocktake_qr(token: str):
    """二维码内容即手持机上传接口地址（<img src> 直接加载，凭 token 访问）。"""
    rec = _stocktake_tokens.get(token)
    if not rec or rec[1] < time.time():
        raise HTTPException(403, "盘点二维码已过期，请重新生成")
    try:
        import io

        import qrcode
        from qrcode.image.svg import SvgImage
    except Exception:
        raise HTTPException(503, "服务端缺少 qrcode 依赖")
    img = qrcode.make(rec[2], image_factory=SvgImage, box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf)
    return Response(content=buf.getvalue(), media_type="image/svg+xml")


class StocktakeUploadIn(BaseModel):
    epcs: list[str] = Field(default_factory=list)
    device: str = ""


@app.post("/api/stocktake/upload")
def stocktake_upload(key: str, body: StocktakeUploadIn):
    """手持机调用：免登录，凭盘点 key 上传 EPC 列表。"""
    rec = _stocktake_tokens.get(key)
    if not rec:
        raise HTTPException(403, "盘点密钥无效，请在盘点页面重新获取二维码")
    if rec[1] < time.time():
        _stocktake_tokens.pop(key, None)
        raise HTTPException(403, "盘点密钥已过期，请重新获取二维码")
    tenant = rec[0]
    epcs = [e.strip().upper() for e in body.epcs if e and e.strip()]
    if not epcs:
        raise HTTPException(400, "未收到任何 EPC 数据")
    with _db_for_tenant(tenant) as conn:
        result = _run_stocktake(conn, epcs, body.device or "RFID手持机", "手持机")
    return {"ok": True, **result}


@app.get("/api/stocktake/list")
def stocktake_list(request: Request, limit: int = 10):
    _require_auth(request)
    with _db(request) as conn:
        rows = conn.execute(
            "SELECT * FROM stocktakes ORDER BY id DESC LIMIT ?", (max(1, min(limit, 50)),)
        ).fetchall()
        return {"list": [dict(r) for r in rows]}


@app.get("/api/stocktake/{sid}")
def stocktake_detail(sid: int, request: Request):
    _require_auth(request)
    with _db(request) as conn:
        head = conn.execute("SELECT * FROM stocktakes WHERE id=?", (sid,)).fetchone()
        if not head:
            raise HTTPException(404, "盘点批次不存在")
        items = conn.execute(
            "SELECT * FROM stocktake_items WHERE stocktake_id=? ORDER BY id", (sid,)
        ).fetchall()
        return {"head": dict(head), "items": [dict(r) for r in items]}


_UPLOAD_PAGE = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>RFID 盘点上传</title>
<style>
body{margin:0;font-family:"PingFang SC","Microsoft YaHei",sans-serif;background:#0F3D33;color:#eaf4ee;padding:18px}
h2{font-size:19px;margin:4px 0 2px} .sub{color:#8fe0bc;font-size:12.5px;margin-bottom:16px}
.card{background:rgba(255,255,255,.07);border:1px solid rgba(143,224,188,.25);border-radius:14px;padding:16px;margin-bottom:14px}
label{font-size:13px;display:block;margin:10px 0 6px}
input,textarea{width:100%;box-sizing:border-box;border-radius:9px;border:1px solid rgba(143,224,188,.35);
 background:rgba(0,0,0,.25);color:#fff;padding:10px;font-size:14px}
textarea{min-height:170px;line-height:1.7;letter-spacing:.5px}
button{width:100%;margin-top:14px;border:0;border-radius:11px;padding:13px;font-size:16px;font-weight:700;
 background:linear-gradient(135deg,#2fae82,#8fe0bc);color:#083025}
.r{margin-top:12px;font-size:13px;line-height:1.9} .ok{color:#8fe0bc}.err{color:#ffb4b4}
table{width:100%;border-collapse:collapse;margin-top:8px;font-size:12.5px}td,th{border-bottom:1px solid rgba(255,255,255,.12);padding:6px 4px;text-align:left}
</style></head><body>
<h2>📡 RFID 批量盘点上传</h2><div class="sub">密钥已通过扫码自动带入 · 懿臻珠宝云</div>
<div class="card">
 <label>手持机 / 设备编号（选填）</label><input id="dev" placeholder="如 PDA-01">
 <label>扫描到的 EPC（每行一个，或用空格/逗号分隔）</label>
 <textarea id="epcs" placeholder="E28011606000020999A1C14501&#10;E280116060000205B85A1234"></textarea>
 <button onclick="up()">上传并生成盘点结果</button>
 <div id="r" class="r"></div>
</div>
<script>
var KEY = new URLSearchParams(location.search).get('key') || '';
function up(){
  var raw = document.getElementById('epcs').value;
  var epcs = raw.split(/[\\s,;]+/).map(function(s){return s.trim();}).filter(Boolean);
  var box = document.getElementById('r');
  if(!epcs.length){ box.innerHTML='<span class="err">请先录入 EPC</span>'; return; }
  box.innerHTML='正在上传盘点…';
  fetch('/api/stocktake/upload?key=' + encodeURIComponent(KEY), {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({epcs:epcs, device:document.getElementById('dev').value})
  }).then(function(r){return r.json().then(function(j){return {ok:r.ok,j:j};});})
  .then(function(x){
    if(!x.ok){ box.innerHTML='<span class="err">'+(x.j.detail||'上传失败')+'</span>'; return; }
    var d=x.j, rows=d.items.map(function(it){
      return '<tr><td>'+it.result+'</td><td>'+(it.code||'')+'</td><td>'+(it.product||'')+'</td><td style="font-family:monospace;font-size:11px">'+it.epc+'</td></tr>';
    }).join('');
    box.innerHTML='<div class="ok">批次 '+d.batch_no+' 完成 ✔</div>'+
      '<div>扫描 <b>'+d.scannedCount+'</b> / 账面 <b>'+d.bookCount+'</b> · '+
      '相符 <b>'+d.matchedCount+'</b> · 盘盈 <b>'+d.surplusCount+'</b> · '+
      '盘亏 <b>'+d.shortageCount+'</b> · 异常 <b>'+d.abnormalCount+'</b></div>'+
      '<table><tr><th>结果</th><th>货号</th><th>商品</th><th>EPC</th></tr>'+rows+'</table>';
  }).catch(function(e){ box.innerHTML='<span class="err">网络错误：'+e+'</span>'; });
}
</script></body></html>"""


@app.get("/stocktake/upload", response_class=HTMLResponse)
def stocktake_upload_page(key: str):
    return HTMLResponse(_UPLOAD_PAGE)


# ------------------------------------------------- 多终端协同盘点（主管端，需登录）

def _next_task_no(conn: sqlite3.Connection) -> str:
    return "RW" + time.strftime("%Y%m%d%H%M%S")


def _active_session(conn: sqlite3.Connection):
    return conn.execute(
        "SELECT * FROM stocktake_sessions WHERE status IN ('进行中','待核对') ORDER BY id DESC LIMIT 1"
    ).fetchone()


def _assert_stock_unfrozen(conn: sqlite3.Connection):
    """协同盘点进行中/待核对期间冻结一切改变库存的操作（销售、出入库、借还、状态变更）。"""
    s = _active_session(conn)
    if s:
        raise HTTPException(409, f"盘点任务 {s['task_no']} {s['status']}，库存操作已暂停，主管核对确认后恢复")


class TaskStartIn(BaseModel):
    host: str = ""


@app.post("/api/stocktake/task/start")
def task_start(body: TaskStartIn, request: Request):
    """主管开启协同盘点任务：冻结销售/出入库，返回任务密钥与手持机接入地址。"""
    op = _require_auth(request)
    with _db(request) as conn:
        if _active_session(conn):
            raise HTTPException(409, "已有盘点任务进行中，请先结束当前任务")
        task_no = _next_task_no(conn)
        key = secrets.token_urlsafe(18)
        cur = conn.execute(
            "INSERT INTO stocktake_sessions(task_no,task_key,status,operator) VALUES(?,?, '进行中',?)",
            (task_no, key, op["username"]),
        )
        sid = cur.lastrowid
        conn.commit()
        _log(conn, op["username"], "开启协同盘点", task_no)
        host = (body.host or "").strip().rstrip("/")
        if not host:
            host = f"{_lan_ip()}:{PORT}"
        if not host.startswith("http"):
            host = "http://" + host
        row = conn.execute("SELECT * FROM stocktake_sessions WHERE id=?", (sid,)).fetchone()
        # 返回完整任务对象（含 devices/status/result 等），与 active 轮询结构一致，前端可直接渲染
        return {**_session_progress(conn, row),
                "key": key, "co_url": f"{host}/api/stocktake/co/join?key={key}"}


@app.get("/api/stocktake/task/active")
def task_active(request: Request):
    _require_auth(request)
    with _db(request) as conn:
        s = _active_session(conn)
        if not s:
            return {"active": False}
        return {"active": True, **_session_progress(conn, s)}


@app.post("/api/stocktake/task/{sid}/end")
def task_end(sid: int, request: Request):
    """结束扫描阶段：合并所有设备的 EPC，统一核对生成差异；销售仍冻结，等待主管确认。"""
    op = _require_auth(request)
    with _db(request) as conn:
        s = conn.execute("SELECT * FROM stocktake_sessions WHERE id=?", (sid,)).fetchone()
        if not s:
            raise HTTPException(404, "盘点任务不存在")
        if s["status"] != "进行中":
            raise HTTPException(409, "任务扫描阶段已结束")
        epcs = [r["epc"] for r in conn.execute(
            "SELECT epc FROM stocktake_scans WHERE session_id=? ORDER BY id", (sid,)).fetchall()]
        if not epcs:
            raise HTTPException(400, "没有任何扫描数据，无法核对。如手持机故障或误开任务，请点「强制终止任务」立即解除冻结")
        result = _run_stocktake(conn, epcs, "协同盘点", op["username"])
        conn.execute(
            "UPDATE stocktake_sessions SET status='待核对', ended=datetime('now','localtime'), result_id=? WHERE id=?",
            (result["id"], sid))
        conn.commit()
        _log(conn, op["username"], "结束协同盘点-待核对", s["task_no"])
        return {"ok": True, "result": result}


@app.post("/api/stocktake/task/{sid}/confirm")
def task_confirm(sid: int, request: Request):
    """主管核对确认：解除销售冻结，任务完成。"""
    op = _require_auth(request)
    with _db(request) as conn:
        s = conn.execute("SELECT * FROM stocktake_sessions WHERE id=?", (sid,)).fetchone()
        if not s:
            raise HTTPException(404, "盘点任务不存在")
        conn.execute("UPDATE stocktake_sessions SET status='已完成' WHERE id=?", (sid,))
        conn.commit()
        _log(conn, op["username"], "确认协同盘点完成", s["task_no"])
        return {"ok": True}


@app.post("/api/stocktake/task/{sid}/abort")
def task_abort(sid: int, request: Request):
    """主管强制终止任务：无论扫描进度如何立即解除冻结，不生成盘点结果。

    用于手持机故障、无法加入、误开任务等异常场景，避免业务被永久冻结。
    """
    op = _require_auth(request)
    with _db(request) as conn:
        s = conn.execute("SELECT * FROM stocktake_sessions WHERE id=?", (sid,)).fetchone()
        if not s:
            raise HTTPException(404, "盘点任务不存在")
        if s["status"] == "已完成":
            return {"ok": True, "already": True}
        conn.execute(
            "UPDATE stocktake_sessions SET status='已完成', ended=datetime('now','localtime') WHERE id=?",
            (sid,))
        conn.commit()
        _log(conn, op["username"], "强制终止协同盘点任务", s["task_no"])
        return {"ok": True}


@app.get("/api/stocktake/task/{sid}")
def task_detail(sid: int, request: Request):
    _require_auth(request)
    with _db(request) as conn:
        s = conn.execute("SELECT * FROM stocktake_sessions WHERE id=?", (sid,)).fetchone()
        if not s:
            raise HTTPException(404, "盘点任务不存在")
        return _session_progress(conn, s)


@app.get("/api/stocktake/task/{sid}/qr")
def task_qr(sid: int, request: Request):
    """协同任务二维码，内容为手持机 join 完整地址。"""
    _require_auth(request)
    with _db(request) as conn:
        s = conn.execute("SELECT task_key FROM stocktake_sessions WHERE id=?", (sid,)).fetchone()
        if not s:
            raise HTTPException(404, "盘点任务不存在")
        url = f"http://{_lan_ip()}:{PORT}/api/stocktake/co/join?key={s['task_key']}"
        try:
            import io
            import qrcode
            from qrcode.image.svg import SvgImage
        except Exception:
            raise HTTPException(503, "服务端缺少 qrcode 依赖")
        img = qrcode.make(url, image_factory=SvgImage, box_size=10, border=2)
        buf = io.BytesIO()
        img.save(buf)
        return Response(content=buf.getvalue(), media_type="image/svg+xml")


def _session_progress(conn: sqlite3.Connection, s) -> dict:
    devices = conn.execute(
        "SELECT device_no,name,last_seen,finished FROM stocktake_devices WHERE session_id=? ORDER BY device_no",
        (s["id"],)).fetchall()
    scanned = conn.execute(
        "SELECT COUNT(*) n FROM stocktake_scans WHERE session_id=?", (s["id"],)).fetchone()["n"]
    result = None
    if s["result_id"]:
        r = conn.execute("SELECT * FROM stocktakes WHERE id=?", (s["result_id"],)).fetchone()
        if r:
            result = {"id": r["id"], "scannedCount": r["scanned_count"], "bookCount": r["book_count"],
                      "matchedCount": r["matched_count"], "surplusCount": r["surplus_count"],
                      "shortageCount": r["shortage_count"], "abnormalCount": r["abnormal_count"]}
    return {
        "id": s["id"], "task_no": s["task_no"], "status": s["status"],
        "operator": s["operator"], "started": s["started"], "ended": s["ended"],
        "scanned_count": scanned,
        "co_url": f"http://{_lan_ip()}:{PORT}/api/stocktake/co/join?key={s['task_key']}",
        "devices": [dict(d) for d in devices],
        "result": result,
    }


# ------------------------------------------------- 多终端协同盘点（手持机端，凭 key 免登录）

def _session_by_key(key: str, conn: sqlite3.Connection):
    s = conn.execute("SELECT * FROM stocktake_sessions WHERE task_key=?", (key,)).fetchone()
    if not s:
        raise HTTPException(403, "盘点密钥无效，请重新扫描任务二维码")
    return s


def _device_heartbeat(conn: sqlite3.Connection, sid: int, device_key: str, name: str):
    """注册/续期设备并分配临时编号，返回 (device_no, finished)。"""
    row = conn.execute(
        "SELECT * FROM stocktake_devices WHERE session_id=? AND device_key=?",
        (sid, device_key)).fetchone()
    if row:
        conn.execute(
            "UPDATE stocktake_devices SET last_seen=datetime('now','localtime'), name=? WHERE id=?",
            (name or row["name"], row["id"]))
        return row["device_no"], row["finished"]
    no = conn.execute(
        "SELECT COALESCE(MAX(device_no),0)+1 n FROM stocktake_devices WHERE session_id=?", (sid,)
    ).fetchone()["n"]
    conn.execute(
        "INSERT INTO stocktake_devices(session_id,device_key,device_no,name) VALUES(?,?,?,?)",
        (sid, device_key, no, name or f"手持机{no}号"))
    return no, 0


class CoJoinIn(BaseModel):
    device: str = ""
    name: str = ""


@app.post("/api/stocktake/co/join")
def co_join(key: str, body: CoJoinIn):
    device_key = (body.device or "anon").strip()
    with _db_for_tenant_key(key) as conn:
        s = _session_by_key(key, conn)
        no, finished = _device_heartbeat(conn, s["id"], device_key, body.name)
        conn.commit()
        return {"active": s["status"] == "进行中", "status": s["status"],
                "task_no": s["task_no"], "device_no": no, "finished": bool(finished),
                "server_time": int(time.time())}


@app.get("/api/stocktake/co/snapshot")
def co_snapshot(key: str, device: str = ""):
    """下发本店在库商品全量快照，手持机据此本地实时比对。"""
    with _db_for_tenant_key(key) as conn:
        s = _session_by_key(key, conn)
        rows = conn.execute(
            "SELECT code,name,rfid_epc epc,status,COALESCE(high_value,0) high_value FROM products "
            "WHERE rfid_epc!='' AND status IN ('在库','已定','借出')"
        ).fetchall()
        items = [dict(r) for r in rows]
        return {"version": s["started"], "task_no": s["task_no"],
                "status": s["status"], "count": len(items), "items": items}


class CoScanTag(BaseModel):
    epc: str
    rssi: int = 0


class CoScanIn(BaseModel):
    device: str = ""
    name: str = ""
    tags: list[CoScanTag] = Field(default_factory=list)


@app.post("/api/stocktake/co/scan")
def co_scan(key: str, body: CoScanIn):
    """实时上报本批标签；服务端按 任务+EPC 全局去重，返回本机本次新登记的 EPC。"""
    device_key = (body.device or "anon").strip()
    accepted, seen = [], set()
    with _db_for_tenant_key(key) as conn:
        s = _session_by_key(key, conn)
        if s["status"] != "进行中":
            raise HTTPException(409, "盘点任务已结束，停止扫描")
        no, _ = _device_heartbeat(conn, s["id"], device_key, body.name)
        for t in body.tags:
            epc = (t.epc or "").strip().upper()
            if not epc or epc in seen:
                continue
            seen.add(epc)
            cur = conn.execute(
                "INSERT OR IGNORE INTO stocktake_scans(session_id,epc,device_key,device_no,rssi) "
                "VALUES(?,?,?,?,?)", (s["id"], epc, device_key, no, t.rssi))
            if cur.rowcount > 0:
                accepted.append(epc)
        total = conn.execute(
            "SELECT COUNT(*) n FROM stocktake_scans WHERE session_id=?", (s["id"],)).fetchone()["n"]
        conn.commit()
        return {"accepted": accepted, "device_no": no, "global_count": total}


@app.get("/api/stocktake/co/pull")
def co_pull(key: str, device: str = "", since_id: int = 0):
    """增量拉取其他设备新扫到的标签（id 大于 since_id）。"""
    device_key = (device or "anon").strip()
    with _db_for_tenant_key(key) as conn:
        s = _session_by_key(key, conn)
        conn.execute(
            "UPDATE stocktake_devices SET last_seen=datetime('now','localtime') "
            "WHERE session_id=? AND device_key=?", (s["id"], device_key))
        rows = conn.execute(
            "SELECT id,epc,device_no FROM stocktake_scans "
            "WHERE session_id=? AND id>? AND device_key!=? ORDER BY id",
            (s["id"], since_id, device_key)).fetchall()
        conn.commit()
        max_id = rows[-1]["id"] if rows else since_id
        return {"status": s["status"], "items": [dict(r) for r in rows], "max_id": max_id}


class CoFinishIn(BaseModel):
    device: str = ""
    name: str = ""


@app.post("/api/stocktake/co/finish")
def co_finish(key: str, body: CoFinishIn):
    device_key = (body.device or "anon").strip()
    with _db_for_tenant_key(key) as conn:
        s = _session_by_key(key, conn)
        _device_heartbeat(conn, s["id"], device_key, body.name)
        conn.execute(
            "UPDATE stocktake_devices SET finished=1, last_seen=datetime('now','localtime') "
            "WHERE session_id=? AND device_key=?", (s["id"], device_key))
        conn.commit()
        return {"ok": True, "status": s["status"]}


@app.get("/api/stocktake/co/task")
def co_task(key: str, device: str = ""):
    """手持机轮询任务状态（是否已被主管结束/确认）。"""
    device_key = (device or "anon").strip()
    with _db_for_tenant_key(key) as conn:
        s = _session_by_key(key, conn)
        row = conn.execute(
            "SELECT device_no,finished FROM stocktake_devices WHERE session_id=? AND device_key=?",
            (s["id"], device_key)).fetchone()
        return {"active": s["status"] == "进行中", "status": s["status"],
                "device_no": row["device_no"] if row else 0,
                "finished": bool(row["finished"]) if row else False}


@contextmanager
def _db_for_tenant_key(key: str):
    """凭任务 key 反查租户并打开其库（手持机免登录接口使用）。"""
    # key 不直接携带租户；扫描全部租户库定位任务
    base = os.environ.get("TENANT_DB_DIR") or str(ROOT / "dev_data")
    db_dir = Path(base)
    for path in db_dir.glob("db_jewelry_*_v*.sqlite"):
        conn = sqlite3.connect(str(path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        db.migrate_schema(conn)
        row = conn.execute(
            "SELECT id FROM stocktake_sessions WHERE task_key=?", (key,)).fetchone()
        if row:
            try:
                yield conn
            finally:
                conn.close()
            return
        conn.close()
    raise HTTPException(403, "盘点密钥无效，请重新扫描任务二维码")


# ---------------------------------------------------------------- 销售

class SaleReq(BaseModel):
    customer: str = ""
    phone: str = ""
    product: str = ""
    product_id: int | None = None
    amount: float = Field(ge=0)
    paid: float = Field(ge=0)
    method: str = "现金"
    biz_date: str = ""


@app.post("/api/sales")
def sale_create(body: SaleReq, request: Request):
    op = _require_auth(request)
    with _db(request) as conn:
        _assert_stock_unfrozen(conn)
        prod = None
        if body.product_id:
            prod = conn.execute("SELECT * FROM products WHERE id=?", (body.product_id,)).fetchone()
            if not prod:
                raise HTTPException(400, "商品不存在")
            if prod["status"] != "在库":
                raise HTTPException(400, f"商品当前状态为{prod['status']}，无法开单")
        bill = _next_bill_no(conn)
        biz = body.biz_date or date.today().isoformat()
        status = "已完成" if body.paid >= body.amount else "欠款"
        name = body.product or (prod["name"] if prod else "")
        cur = conn.execute(
            """INSERT INTO sales(bill_no,customer,phone,product,product_id,amount,paid,method,biz_date,type,status)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (bill, body.customer, body.phone, name, body.product_id, body.amount, body.paid, body.method, biz, "普通", status),
        )
        _touch_customer(conn, body.customer, body.phone, body.amount, body.amount - body.paid)
        if prod:
            conn.execute("UPDATE products SET status='已售' WHERE id=?", (prod["id"],))
            _inv(conn, prod["id"], prod["rfid_epc"], "out", op["username"])
        conn.commit()
        _log(conn, op["username"], "销售开单", bill)
        return {"bill_no": bill, "id": cur.lastrowid}


@app.get("/api/sales")
def sale_list(request: Request, q: str = "", page: int = 1, size: int = 50):
    _require_auth(request)
    with _db(request) as conn:
        where, args = [], []
        if q:
            where.append("(bill_no LIKE ? OR customer LIKE ? OR phone LIKE ? OR product LIKE ?)")
            args += [f"%{q}%"] * 4
        cond = ("WHERE " + " AND ".join(where)) if where else ""
        total = conn.execute(f"SELECT COUNT(*) n FROM sales {cond}", args).fetchone()["n"]
        rows = conn.execute(
            f"SELECT * FROM sales {cond} ORDER BY id DESC LIMIT ? OFFSET ?",
            args + [size, (page - 1) * size],
        ).fetchall()
        return {"total": total, "page": page, "size": size, "items": [dict(r) for r in rows]}


@app.post("/api/sales/{sid}/void")
def sale_void(sid: int, request: Request):
    op = _require_auth(request)
    with _db(request) as conn:
        _assert_stock_unfrozen(conn)
        row = conn.execute("SELECT * FROM sales WHERE id=?", (sid,)).fetchone()
        if not row:
            raise HTTPException(404, "单据不存在")
        if row["status"] == "已冲红":
            raise HTTPException(400, "单据已冲红")
        conn.execute("UPDATE sales SET status='已冲红' WHERE id=?", (sid,))
        if row["product_id"]:
            conn.execute("UPDATE products SET status='在库' WHERE id=?", (row["product_id"],))
            _inv(conn, row["product_id"], "", "in", op["username"])
        if row["phone"]:
            conn.execute(
                "UPDATE customers SET total_amount=MAX(total_amount-?,0), due_amount=MAX(due_amount-?,0) WHERE phone=?",
                (row["amount"], row["amount"] - row["paid"], row["phone"]),
            )
        conn.commit()
        _log(conn, op["username"], "销售冲红", row["bill_no"])
        return {"ok": True}


# ---------------------------------------------------------------- 定金

class DepositIn(BaseModel):
    customer: str = ""
    phone: str = ""
    product_id: int | None = None
    product: str = ""
    total: float = Field(ge=0)
    deposit: float = Field(ge=0)
    balance: float = Field(ge=0)
    promised_date: str = ""
    reminder_days: int = 7


@app.get("/api/deposits")
def deposit_list(request: Request, page: int = 1, size: int = 50):
    _require_auth(request)
    with _db(request) as conn:
        total = conn.execute("SELECT COUNT(*) n FROM deposits").fetchone()["n"]
        rows = conn.execute("SELECT * FROM deposits ORDER BY id DESC LIMIT ? OFFSET ?", (size, (page - 1) * size)).fetchall()
        return {"total": total, "page": page, "size": size, "items": [dict(r) for r in rows]}


@app.post("/api/deposits")
def deposit_create(body: DepositIn, request: Request):
    op = _require_auth(request)
    with _db(request) as conn:
        _assert_stock_unfrozen(conn)
        name = body.product
        if body.product_id:
            p = conn.execute("SELECT * FROM products WHERE id=?", (body.product_id,)).fetchone()
            if not p:
                raise HTTPException(400, "商品不存在")
            if p["status"] != "在库":
                raise HTTPException(400, "仅可锁定在库商品")
            conn.execute("UPDATE products SET status='已定' WHERE id=?", (p["id"],))
            name = name or p["name"]
        cur = conn.execute(
            """INSERT INTO deposits(customer,phone,product_id,product,total,deposit,balance,promised_date,reminder_days,status)
               VALUES(?,?,?,?,?,?,?,?,?,'已定')""",
            (body.customer, body.phone, body.product_id, name, body.total, body.deposit, body.balance, body.promised_date, body.reminder_days),
        )
        _touch_customer(conn, body.customer, body.phone, body.deposit, body.balance)
        conn.commit()
        _log(conn, op["username"], "收定金", body.customer)
        return {"id": cur.lastrowid}


@app.post("/api/deposits/{did}/pay")
def deposit_pay(did: int, request: Request):
    op = _require_auth(request)
    with _db(request) as conn:
        _assert_stock_unfrozen(conn)
        d = conn.execute("SELECT * FROM deposits WHERE id=?", (did,)).fetchone()
        if not d:
            raise HTTPException(404, "定金单不存在")
        if d["status"] != "已定":
            raise HTTPException(400, "单据不可收尾款")
        bill = _next_bill_no(conn)
        conn.execute(
            """INSERT INTO sales(bill_no,customer,phone,product,product_id,amount,paid,method,biz_date,type,status)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (bill, d["customer"], d["phone"], d["product"], d["product_id"], d["total"], d["total"], "尾款转单", date.today().isoformat(), "定金转单", "已完成"),
        )
        if d["product_id"]:
            p = conn.execute("SELECT * FROM products WHERE id=?", (d["product_id"],)).fetchone()
            conn.execute("UPDATE products SET status='已售' WHERE id=?", (d["product_id"],))
            _inv(conn, d["product_id"], (p["rfid_epc"] if p else ""), "out", op["username"])
        conn.execute("UPDATE deposits SET status='已完成', balance=0, deposit=? WHERE id=?", (d["total"], did))
        if d["phone"]:
            conn.execute("UPDATE customers SET due_amount=MAX(due_amount-?,0) WHERE phone=?", (d["balance"], d["phone"]))
            conn.execute("UPDATE customers SET total_amount=total_amount+? WHERE phone=?", (d["balance"], d["phone"]))
        conn.commit()
        _log(conn, op["username"], "尾款转正式单", bill)
        return {"ok": True, "bill_no": bill}


@app.post("/api/deposits/{did}/void")
def deposit_void(did: int, request: Request):
    op = _require_auth(request)
    with _db(request) as conn:
        _assert_stock_unfrozen(conn)
        d = conn.execute("SELECT * FROM deposits WHERE id=?", (did,)).fetchone()
        if not d:
            raise HTTPException(404, "定金单不存在")
        if d["status"] != "已定":
            raise HTTPException(400, "单据不可冲红")
        if d["product_id"]:
            conn.execute("UPDATE products SET status='在库' WHERE id=?", (d["product_id"],))
        conn.execute("UPDATE deposits SET status='已冲红' WHERE id=?", (did,))
        conn.commit()
        _log(conn, op["username"], "定金冲红", str(did))
        return {"ok": True}


# ---------------------------------------------------------------- 借货

class LoanIn(BaseModel):
    direction: str = "out"
    product: str = ""
    code: str = ""
    party: str = ""
    qty: int = 1
    loan_date: str = ""
    due_date: str = ""


@app.get("/api/loans")
def loan_list(request: Request, page: int = 1, size: int = 50):
    _require_auth(request)
    with _db(request) as conn:
        total = conn.execute("SELECT COUNT(*) n FROM loans").fetchone()["n"]
        rows = conn.execute("SELECT * FROM loans ORDER BY id DESC LIMIT ? OFFSET ?", (size, (page - 1) * size)).fetchall()
        return {"total": total, "page": page, "size": size, "items": [dict(r) for r in rows]}


@app.post("/api/loans")
def loan_create(body: LoanIn, request: Request):
    op = _require_auth(request)
    with _db(request) as conn:
        _assert_stock_unfrozen(conn)
        st = "借出中" if body.direction == "out" else "借入中"
        if body.direction == "out" and body.code:
            p = conn.execute("SELECT * FROM products WHERE code=?", (body.code,)).fetchone()
            if p:
                if p["status"] != "在库":
                    raise HTTPException(400, "仅可借出在库商品")
                conn.execute("UPDATE products SET status='借出' WHERE id=?", (p["id"],))
                _inv(conn, p["id"], p["rfid_epc"], "out", op["username"])
        cur = conn.execute(
            "INSERT INTO loans(direction,product,code,party,qty,loan_date,due_date,status) VALUES(?,?,?,?,?,?,?,?)",
            (body.direction, body.product, body.code, body.party, body.qty, body.loan_date or date.today().isoformat(), body.due_date, st),
        )
        conn.commit()
        _log(conn, op["username"], "借货", body.product)
        return {"id": cur.lastrowid}


@app.post("/api/loans/{lid}/return")
def loan_return(lid: int, request: Request):
    op = _require_auth(request)
    with _db(request) as conn:
        _assert_stock_unfrozen(conn)
        row = conn.execute("SELECT * FROM loans WHERE id=?", (lid,)).fetchone()
        if not row:
            raise HTTPException(404, "记录不存在")
        if row["status"] in ("已归还", "已核销"):
            raise HTTPException(400, "已归还")
        if row["direction"] == "out" and row["code"]:
            p = conn.execute("SELECT * FROM products WHERE code=?", (row["code"],)).fetchone()
            if p:
                conn.execute("UPDATE products SET status='在库' WHERE id=?", (p["id"],))
                _inv(conn, p["id"], p["rfid_epc"], "in", op["username"])
        st = "已归还" if row["direction"] == "out" else "已核销"
        conn.execute("UPDATE loans SET status=? WHERE id=?", (st, lid))
        conn.commit()
        _log(conn, op["username"], "借货归还", row["product"])
        return {"ok": True}


# ---------------------------------------------------------------- 客户

class CustomerIn(BaseModel):
    name: str
    phone: str = ""
    level: str = "普通"
    birthday: str = ""
    preference: str = ""


@app.get("/api/customers/list")
def customer_list(request: Request, page: int = 1, size: int = 50):
    _require_auth(request)
    with _db(request) as conn:
        total = conn.execute("SELECT COUNT(*) n FROM customers").fetchone()["n"]
        rows = conn.execute("SELECT * FROM customers ORDER BY total_amount DESC LIMIT ? OFFSET ?", (size, (page - 1) * size)).fetchall()
        return {"total": total, "page": page, "size": size, "items": [dict(r) for r in rows]}


@app.post("/api/customers")
def customer_create(body: CustomerIn, request: Request):
    op = _require_auth(request)
    with _db(request) as conn:
        cur = conn.execute(
            "INSERT INTO customers(name,phone,level,birthday,preference) VALUES(?,?,?,?,?)",
            (body.name, body.phone, body.level, body.birthday, body.preference),
        )
        conn.commit()
        _log(conn, op["username"], "新增客户", body.name)
        return {"id": cur.lastrowid}


@app.put("/api/customers/{cid}")
def customer_update(cid: int, body: CustomerIn, request: Request):
    op = _require_auth(request)
    with _db(request) as conn:
        conn.execute(
            "UPDATE customers SET name=?,phone=?,level=?,birthday=?,preference=? WHERE id=?",
            (body.name, body.phone, body.level, body.birthday, body.preference, cid),
        )
        conn.commit()
        _log(conn, op["username"], "修改客户", body.name)
        return {"ok": True}


# ---------------------------------------------------------------- 维修 / 采购 / 委外

class RepairIn(BaseModel):
    customer: str = ""
    phone: str = ""
    item: str = ""
    issue: str = ""
    est_fee: float = 0
    actual_fee: float = 0
    receive_date: str = ""
    promised_date: str = ""
    status: str = "待维修"
    technician: str = ""
    remark: str = ""


@app.get("/api/repairs")
def repair_list(request: Request, page: int = 1, size: int = 50):
    _require_auth(request)
    with _db(request) as conn:
        total = conn.execute("SELECT COUNT(*) n FROM repairs").fetchone()["n"]
        rows = conn.execute("SELECT * FROM repairs ORDER BY id DESC LIMIT ? OFFSET ?", (size, (page - 1) * size)).fetchall()
        return {"total": total, "page": page, "size": size, "items": [dict(r) for r in rows]}


@app.post("/api/repairs")
def repair_create(body: RepairIn, request: Request):
    op = _require_auth(request)
    with _db(request) as conn:
        cur = conn.execute(
            """INSERT INTO repairs(customer,phone,item,issue,est_fee,actual_fee,receive_date,promised_date,status,technician,remark)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (body.customer, body.phone, body.item, body.issue, body.est_fee, body.actual_fee,
             body.receive_date, body.promised_date, body.status, body.technician, body.remark),
        )
        conn.commit()
        _log(conn, op["username"], "接维修", body.item)
        return {"id": cur.lastrowid}


@app.put("/api/repairs/{rid}")
def repair_update(rid: int, body: RepairIn, request: Request):
    op = _require_auth(request)
    with _db(request) as conn:
        old = conn.execute("SELECT * FROM repairs WHERE id=?", (rid,)).fetchone()
        done = date.today().isoformat() if body.status == "已完成" else (old["done_date"] if old else "")
        conn.execute(
            """UPDATE repairs SET customer=?,phone=?,item=?,issue=?,est_fee=?,actual_fee=?,
               promised_date=?,status=?,technician=?,remark=?,done_date=? WHERE id=?""",
            (body.customer, body.phone, body.item, body.issue, body.est_fee, body.actual_fee,
             body.promised_date, body.status, body.technician, body.remark, done, rid),
        )
        if old and old["status"] != "已完成" and body.status == "已完成" and body.actual_fee:
            _touch_customer(conn, body.customer, body.phone, body.actual_fee, 0)
        conn.commit()
        _log(conn, op["username"], "更新维修", str(rid))
        return {"ok": True}


class PurchaseIn(BaseModel):
    supplier: str = ""
    product: str = ""
    qty: int = 1
    cost: float = 0
    order_date: str = ""
    expected_date: str = ""
    received_date: str = ""
    status: str = "待发货"
    paid: float = 0


@app.get("/api/purchases")
def purchase_list(request: Request, page: int = 1, size: int = 50):
    _require_auth(request)
    with _db(request) as conn:
        total = conn.execute("SELECT COUNT(*) n FROM purchases").fetchone()["n"]
        rows = conn.execute("SELECT * FROM purchases ORDER BY id DESC LIMIT ? OFFSET ?", (size, (page - 1) * size)).fetchall()
        return {"total": total, "page": page, "size": size, "items": [dict(r) for r in rows]}


@app.post("/api/purchases")
def purchase_create(body: PurchaseIn, request: Request):
    op = _require_auth(request)
    with _db(request) as conn:
        cur = conn.execute(
            """INSERT INTO purchases(supplier,product,qty,cost,order_date,expected_date,received_date,status,paid)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (body.supplier, body.product, body.qty, body.cost, body.order_date, body.expected_date, body.received_date, body.status, body.paid),
        )
        conn.commit()
        _log(conn, op["username"], "采购", body.product)
        return {"id": cur.lastrowid}


@app.post("/api/purchases/{pid}/receive")
def purchase_receive(pid: int, request: Request):
    op = _require_auth(request)
    with _db(request) as conn:
        _assert_stock_unfrozen(conn)
        row = conn.execute("SELECT * FROM purchases WHERE id=?", (pid,)).fetchone()
        if not row:
            raise HTTPException(404, "采购单不存在")
        if row["status"] == "已入库":
            raise HTTPException(400, "已入库")
        today = date.today().isoformat()
        conn.execute("UPDATE purchases SET status='已入库', received_date=? WHERE id=?", (today, pid))
        code = _next_code(conn, "CG")
        unit = (row["cost"] / row["qty"]) if row["qty"] else row["cost"]
        cur = conn.execute(
            """INSERT INTO products(code,name,category,material,weight,cost,price,status,store_id) VALUES(?,?,?,?,?,?,?,'在库',1)""",
            (code, row["product"], "其他", "", 0, unit, unit, ),
        )
        _inv(conn, cur.lastrowid, "", "in", op["username"], row["qty"] or 1)
        conn.commit()
        _log(conn, op["username"], "采购入库", code)
        return {"ok": True, "code": code}


class OutsourceIn(BaseModel):
    factory: str = ""
    product: str = ""
    material: str = ""
    weight: float = 0
    gold_price: float = 0
    labor_fee: float = 0
    send_date: str = ""
    expected_date: str = ""
    received_date: str = ""
    status: str = "加工中"


@app.get("/api/outsourcings")
def outsource_list(request: Request, page: int = 1, size: int = 50):
    _require_auth(request)
    with _db(request) as conn:
        total = conn.execute("SELECT COUNT(*) n FROM outsourcings").fetchone()["n"]
        rows = conn.execute("SELECT * FROM outsourcings ORDER BY id DESC LIMIT ? OFFSET ?", (size, (page - 1) * size)).fetchall()
        return {"total": total, "page": page, "size": size, "items": [dict(r) for r in rows]}


@app.post("/api/outsourcings")
def outsource_create(body: OutsourceIn, request: Request):
    op = _require_auth(request)
    with _db(request) as conn:
        cur = conn.execute(
            """INSERT INTO outsourcings(factory,product,material,weight,gold_price,labor_fee,send_date,expected_date,received_date,status)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (body.factory, body.product, body.material, body.weight, body.gold_price, body.labor_fee,
             body.send_date, body.expected_date, body.received_date, body.status),
        )
        conn.commit()
        _log(conn, op["username"], "委外加工", body.product)
        return {"id": cur.lastrowid}


@app.post("/api/outsourcings/{oid}/receive")
def outsource_receive(oid: int, request: Request):
    op = _require_auth(request)
    with _db(request) as conn:
        _assert_stock_unfrozen(conn)
        row = conn.execute("SELECT * FROM outsourcings WHERE id=?", (oid,)).fetchone()
        if not row:
            raise HTTPException(404, "加工单不存在")
        if row["status"] == "已收货":
            raise HTTPException(400, "已收货")
        today = date.today().isoformat()
        cost = row["gold_price"] * row["weight"] + row["labor_fee"]
        conn.execute("UPDATE outsourcings SET status='已收货', received_date=? WHERE id=?", (today, oid))
        code = _next_code(conn, "WW")
        cur = conn.execute(
            """INSERT INTO products(code,name,category,material,weight,cost,price,status,store_id) VALUES(?,?,?,?,?,?,?,'在库',1)""",
            (code, row["product"], "黄金", row["material"], row["weight"], cost, cost * 1.2),
        )
        _inv(conn, cur.lastrowid, "", "in", op["username"])
        conn.commit()
        _log(conn, op["username"], "委外收货入库", code)
        return {"ok": True, "code": code, "cost": cost}


# ---------------------------------------------------------------- 日志 / 预约 / 公开

@app.get("/api/logs")
def log_list(request: Request, page: int = 1, size: int = 80):
    _require_auth(request)
    with _db(request) as conn:
        total = conn.execute("SELECT COUNT(*) n FROM operate_logs").fetchone()["n"]
        rows = conn.execute("SELECT * FROM operate_logs ORDER BY id DESC LIMIT ? OFFSET ?", (size, (page - 1) * size)).fetchall()
        return {"total": total, "page": page, "size": size, "items": [dict(r) for r in rows]}


@app.get("/api/logs/export")
def log_export(request: Request):
    _require_auth(request)
    with _db(request) as conn:
        rows = conn.execute("SELECT * FROM operate_logs ORDER BY id DESC").fetchall()
    lines = ["id,time,operator,store,action,target,result"]
    for r in rows:
        lines.append(",".join(str(r[k]).replace(",", " ") for k in ("id", "time", "operator", "store", "action", "target", "result")))
    return PlainTextResponse("\n".join(lines), media_type="text/csv; charset=utf-8")


@app.get("/api/appointments")
def appointment_list(request: Request):
    _require_auth(request)
    with _db(request) as conn:
        rows = conn.execute("SELECT * FROM appointments ORDER BY id DESC LIMIT 100").fetchall()
        return {"items": [dict(r) for r in rows]}


class ApptStatus(BaseModel):
    status: str


@app.put("/api/appointments/{aid}")
def appointment_update(aid: int, body: ApptStatus, request: Request):
    op = _require_auth(request)
    with _db(request) as conn:
        conn.execute("UPDATE appointments SET status=? WHERE id=?", (body.status, aid))
        conn.commit()
        _log(conn, op["username"], "预约跟进", f"{aid}:{body.status}")
        return {"ok": True}


class PublicAppt(BaseModel):
    name: str = ""
    contact: str = ""
    contact_type: str = "phone"
    category: str = ""
    product_id: int | None = None
    want_date: str = ""
    want_slot: str = ""
    remark: str = ""


@app.post("/api/public/appointments")
def public_appointment(body: PublicAppt, request: Request):
    with _db(request) as conn:
        conn.execute(
            """INSERT INTO appointments(tenant_id,name,contact,contact_type,category,product_id,want_date,want_slot,remark,status)
               VALUES(?,?,?,?,?,?,?,?,?,'pending')""",
            (_tenant_of(request), body.name, body.contact, body.contact_type, body.category, body.product_id, body.want_date, body.want_slot, body.remark),
        )
        conn.commit()
    return {"ok": True, "detail": "预约已提交，店铺将与您联系"}


@app.get("/api/public/showcase")
def public_showcase(request: Request):
    with _db(request) as conn:
        p = conn.execute("SELECT showcase_title, showcase_subtitle FROM tenant_profiles ORDER BY id DESC LIMIT 1").fetchone()
        rows = conn.execute(
            """SELECT id, code, name, category, material, weight, size, price, cert, status, origin,
                      showcase_order, showcase_desc
                 FROM products
                WHERE showcase_public=1 AND status='在库'
                ORDER BY (showcase_order=0) ASC, showcase_order ASC, id DESC
                LIMIT 10"""
        ).fetchall()
        items = []
        for r in rows:
            d = dict(r)
            desc = (d.get("showcase_desc") or "").strip()
            if not desc:
                parts = []
                if d.get("origin"): parts.append(f"产地：{d['origin']}")
                if d.get("material"): parts.append(f"材质：{d['material']}")
                if d.get("weight") and float(d["weight"]) > 0: parts.append(f"金重：{d['weight']:.2f}g")
                if d.get("size"): parts.append(f"尺寸：{d['size']}")
                if d.get("price"): parts.append(f"参考价：¥{d['price']:,.0f}")
                desc = "｜".join(parts)
            d["display_desc"] = desc
            items.append(d)
        return {
            "title": (p and p["showcase_title"]) or "新品橱窗",
            "subtitle": (p and p["showcase_subtitle"]) or "本周臻品 · 限量发售",
            "items": items,
        }


# ---------------- 橱窗管理 API ----------------

class ShowcaseItemIn(BaseModel):
    id: int
    order: int = 0
    desc: str = ""


class ShowcaseSyncIn(BaseModel):
    items: list[ShowcaseItemIn]
    remove: list[int] = []


def _default_desc(row: dict) -> str:
    parts = []
    if row.get("origin"): parts.append(f"产地：{row['origin']}")
    if row.get("material"): parts.append(f"材质：{row['material']}")
    if row.get("weight") and float(row["weight"]) > 0: parts.append(f"金重：{row['weight']:.2f}g")
    if row.get("size"): parts.append(f"尺寸：{row['size']}")
    if row.get("price"): parts.append(f"参考价：¥{float(row['price']):,.0f}")
    return "｜".join(parts)


@app.get("/api/showcase/list")
def showcase_list(request: Request):
    """管理端：列出橱窗商品 + 所有在库商品，前端可勾选加入。"""
    _require_auth(request)
    with _db(request) as conn:
        in_showcase = conn.execute(
            """SELECT id, code, name, category, material, weight, size, price, origin,
                      showcase_order, showcase_desc, status, showcase_public
                 FROM products
                WHERE showcase_public=1 AND status='在库'
                ORDER BY (showcase_order=0) ASC, showcase_order ASC, id DESC
                LIMIT 10"""
        ).fetchall()
        in_items = []
        for r in in_showcase:
            d = dict(r)
            if not d["showcase_desc"]:
                d["showcase_desc"] = _default_desc(d)
            in_items.append(d)
        others = conn.execute(
            """SELECT id, code, name, category, material, weight, size, price, origin, status, showcase_public
                 FROM products
                WHERE showcase_public=0 AND status='在库'
                ORDER BY id DESC
                LIMIT 500"""
        ).fetchall()
        p = conn.execute("SELECT showcase_title, showcase_subtitle FROM tenant_profiles ORDER BY id DESC LIMIT 1").fetchone()
        return {
            "title": (p and p["showcase_title"]) or "新品橱窗",
            "subtitle": (p and p["showcase_subtitle"]) or "本周臻品 · 限量发售",
            "in_showcase": in_items,
            "max": 10,
            "count": len(in_items),
            "pool": [dict(r) for r in others],
        }


@app.put("/api/showcase/sync")
def showcase_sync(body: ShowcaseSyncIn, request: Request):
    """批量同步橱窗：加入/移出/排序/修改描述，最多 10 件。"""
    op = _require_auth(request)
    if len(body.items) > 10:
        raise HTTPException(400, "橱窗商品最多 10 件")
    with _db(request) as conn:
        # 1) 移出：移除的商品从橱窗撤下，清空排序/描述
        for pid in body.remove:
            conn.execute(
                "UPDATE products SET showcase_public=0, showcase_order=0, showcase_desc='' WHERE id=?",
                (pid,),
            )
        # 2) 加入 / 更新：写入橱窗、排序号、自定义描述（描述为空时不覆盖，前端已默认生成好）
        for it in body.items:
            conn.execute(
                """UPDATE products SET showcase_public=1, showcase_order=?, showcase_desc=?
                   WHERE id=? AND status='在库'""",
                (it.order, it.desc or "", it.id),
            )
        conn.commit()
        ids = [str(it.id) for it in body.items]
        _log(conn, op["username"], "同步橱窗", f"{len(body.items)} 件：{','.join(ids[:5])}" + ("..." if len(ids) > 5 else ""))
        return {"ok": True, "count": len(body.items)}


# ---------------------------------------------------------------- 静态资源（最后注册）

@app.get("/{asset_path:path}")
def spa_asset(asset_path: str):
    if asset_path.startswith("api/"):
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    if asset_path.startswith("logo/"):
        f = (LOGO_DIR / Path(asset_path).name).resolve()
        if f.is_file() and _safe_relative(f, LOGO_DIR.resolve()):
            return FileResponse(str(f))
    f = (FRONTEND_DIR / asset_path).resolve()
    if f.is_file() and _safe_relative(f, FRONTEND_DIR.resolve()) and f.suffix.lower() in (
        ".js", ".css", ".map", ".png", ".svg", ".woff2", ".ico", ".jpg", ".webp",
    ):
        # 业务页面脚本/样式每次校验更新，避免发布后浏览器缓存旧版（内网工具，开销可忽略）
        nocache = {"Cache-Control": "no-cache"} if f.suffix.lower() in (".js", ".css", ".html") else None
        return FileResponse(str(f), headers=nocache)
    index = FRONTEND_DIR / "index.html"
    if index.exists() and "." not in Path(asset_path).name:
        return FileResponse(str(index), headers={"Cache-Control": "no-cache"})
    return JSONResponse({"detail": "Not Found"}, status_code=404)


if __name__ == "__main__":
    os.environ.setdefault("UNIFIED_ACCESS_MODE", "STANDALONE")
    logger.info("懿臻珠宝云独立启动 http://%s:%s  账号 admin / 123456", HOST, PORT)
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
