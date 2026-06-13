"""受注書 入力フォーム（クラウド公開用・独立アプリ）。

- 受注書(親)＋売上明細(子)を Notion に直接書き込むだけのアプリ。
- 経営数字(P&L)には一切触れない（このアプリはNotionの顧客DBにしか書き込まない）。
- 設定:
    環境変数 NOTION_TOKEN   … Notion連携トークン（必須・秘匿）
    環境変数 FORM_PASSWORD  … 入力時の共通パスワード（必須）
    環境変数 FORM_USER      … ユーザー名（任意・既定 "staff"）
    sources.json            … 部門ごとの受注書/売上明細DBのID（秘匿不要）
"""
import json
import os
import secrets
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, Request, Depends, HTTPException, status
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

BASE = Path(__file__).resolve().parent
API = "https://api.notion.com/v1"
CATEGORIES = ["祭壇", "供花", "オプション", "生花", "花束", "アレンジ", "仏花", "その他"]

# Notionプロパティ名（本体アプリと一致）
H = {"souke": "御葬家名", "order_date": "受注日", "service_date": "施行予定日時",
     "teardown_date": "撤収予定日時", "customer": "得意先", "venue": "式場・住所",
     "gender": "性別", "money_transfer": "売上金移動",
     "tax_kind": "税区分", "note": "備考"}
L = {"product": "品名", "rel": "受注書", "category": "カテゴリ",
     "list_price": "上代金額", "unit_price": "受注額", "qty": "本数", "amount": "合計"}

SOURCES = json.loads((BASE / "sources.json").read_text(encoding="utf-8"))["departments"]
TOKEN = os.environ.get("NOTION_TOKEN", "")
PASSWORD = os.environ.get("FORM_PASSWORD", "")
USER = os.environ.get("FORM_USER", "staff")

app = FastAPI(title="受注書入力フォーム")
templates = Jinja2Templates(directory=str(BASE / "templates"))
security = HTTPBasic()


_fails = {}  # ip -> [失敗時刻...] 認証総当たり対策（単一インスタンス前提の簡易版）
_FAIL_WINDOW = 300   # 秒
_FAIL_MAX = 10       # この回数を超えたら一時ロック


def auth(request: Request, creds: HTTPBasicCredentials = Depends(security)):
    ip = (request.client.host if request.client else "?")
    now = time.monotonic()
    recent = [t for t in _fails.get(ip, []) if now - t < _FAIL_WINDOW]
    if len(recent) >= _FAIL_MAX:
        _fails[ip] = recent
        raise HTTPException(status_code=429, detail="試行回数が多すぎます。しばらく待ってください。")
    ok_u = secrets.compare_digest(creds.username, USER)
    ok_p = bool(PASSWORD) and secrets.compare_digest(creds.password, PASSWORD)
    if not (ok_u and ok_p):
        recent.append(now)
        _fails[ip] = recent
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="認証が必要です", headers={"WWW-Authenticate": "Basic"})
    _fails.pop(ip, None)
    return creds.username


def check_csrf(request: Request):
    """POSTのCSRF対策：Origin/Refererのホストが自分自身と一致することを要求。"""
    host = request.headers.get("host", "")
    origin = request.headers.get("origin") or request.headers.get("referer")
    if origin:
        if urlparse(origin).netloc != host:
            raise HTTPException(status_code=403, detail="不正なリクエスト送信元です。")


def _headers():
    return {"Authorization": f"Bearer {TOKEN}", "Notion-Version": "2022-06-28",
            "Content-Type": "application/json"}


def _title(s):
    return {"title": [{"text": {"content": s or ""}}]}


def _text(s):
    return {"rich_text": ([{"text": {"content": s}}] if s else [])}


def _date(s):
    return {"date": ({"start": s} if s else None)}


def _select(s):
    return {"select": ({"name": s} if s else None)}


def _number(x):
    if x in (None, ""):
        return {"number": None}
    try:
        return {"number": float(x)}
    except (TypeError, ValueError):
        return {"number": None}


def create_order(department, header, lines):
    src = SOURCES.get(department)
    if not src:
        raise RuntimeError(f"部門が未設定です: {department}")
    if not TOKEN:
        raise RuntimeError("NOTION_TOKEN が未設定です")
    with httpx.Client(timeout=60) as client:
        hp = {
            H["souke"]: _title(header.get("souke")),
            H["order_date"]: _date(header.get("order_date")),
            H["service_date"]: _date(header.get("service_date")),
            H["teardown_date"]: _date(header.get("teardown_date")),
            H["customer"]: _select(header.get("customer")),
            H["venue"]: _text(header.get("venue")),
            H["gender"]: _select(header.get("gender")),
            H["money_transfer"]: _text(header.get("money_transfer")),
            H["tax_kind"]: _select(header.get("tax_kind")),
            H["note"]: _text(header.get("note")),
        }
        r = client.post(f"{API}/pages", headers=_headers(),
                        json={"parent": {"database_id": src["order_header"]}, "properties": hp})
        if r.status_code != 200:
            raise RuntimeError(f"受注書の作成に失敗 {r.status_code}: {r.text[:300]}")
        hid = r.json()["id"]
        for ln in lines:
            if not (ln.get("product") or ln.get("unit_price")):
                continue
            lp = {
                L["product"]: _title(ln.get("product")),
                L["rel"]: {"relation": [{"id": hid}]},
                L["category"]: _select(ln.get("category")),
                L["list_price"]: _number(ln.get("list_price")),
                L["unit_price"]: _number(ln.get("unit_price")),
                L["qty"]: _number(ln.get("qty")),
            }
            rr = client.post(f"{API}/pages", headers=_headers(),
                             json={"parent": {"database_id": src["order_line"]}, "properties": lp})
            if rr.status_code != 200:
                raise RuntimeError(f"明細の作成に失敗 {rr.status_code}: {rr.text[:300]}")
    return hid


def _pget(props, name):
    p = props.get(name) or {}
    t = p.get("type")
    if t == "title":
        return "".join(r.get("plain_text", "") for r in p.get("title", []))
    if t == "rich_text":
        return "".join(r.get("plain_text", "") for r in p.get("rich_text", []))
    if t == "select":
        return (p.get("select") or {}).get("name")
    if t == "number":
        return p.get("number")
    if t == "date":
        return (p.get("date") or {}).get("start")
    if t == "formula":
        f = p.get("formula") or {}
        return f.get(f.get("type"))
    return None


def _normcat(c):
    c = (c or "").strip()
    if c in ("OP", "ＯＰ"):
        return "オプション"
    return c if c in CATEGORIES else "その他"


def _dept_of(header_db_id):
    for d, src in SOURCES.items():
        if src.get("order_header") == header_db_id:
            return d
    return None


def _select_options(db_id, prop_name):
    """指定DBのselectプロパティの現存オプション名一覧（候補の自動追記用）。"""
    if not (TOKEN and db_id):
        return []
    try:
        with httpx.Client(timeout=20) as client:
            r = client.get(f"{API}/databases/{db_id}", headers=_headers())
            if r.status_code != 200:
                return []
            p = (r.json().get("properties", {}).get(prop_name) or {})
            return [o.get("name") for o in (p.get("select", {}).get("options") or [])]
    except Exception:  # noqa: BLE001
        return []


def _candidates(department):
    """部門の得意先候補＝固定リスト ∪ Notion現存オプション。"""
    src = SOURCES.get(department, {})
    out = list(src.get("customers", []))
    seen = set(out)
    for name in _select_options(src.get("order_header"), H["customer"]):
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
def form(request: Request, department: str = "", msg: str = "", err: str = "",
         _u: str = Depends(auth)):
    from datetime import datetime
    depts = list(SOURCES.keys())
    if department not in depts:
        department = depts[0] if depts else ""
    dept_customers = {d: _candidates(d) for d in depts}
    return templates.TemplateResponse("form.html", {
        "request": request, "departments": depts, "department": department,
        "dept_customers": dept_customers, "categories": CATEGORIES,
        "today": datetime.now().strftime("%Y-%m-%d"), "msg": msg, "err": err,
    })


@app.post("/create")
async def create(request: Request, _u: str = Depends(auth), _c: None = Depends(check_csrf)):
    from urllib.parse import quote
    from fastapi.responses import RedirectResponse
    f = await request.form()
    department = f.get("department") or ""
    header = {k: (f.get(k) or None) for k in
              ("souke", "order_date", "service_date", "teardown_date", "customer",
               "venue", "gender", "money_transfer", "tax_kind", "note")}
    cats, prods = f.getlist("line_category"), f.getlist("line_product")
    lps, ups, qtys = f.getlist("line_list_price"), f.getlist("line_unit_price"), f.getlist("line_qty")
    lines = []
    for i in range(len(cats)):
        prod = prods[i] if i < len(prods) else ""
        up = ups[i] if i < len(ups) else ""
        if not (prod or up):
            continue
        lines.append({"category": cats[i], "product": prod,
                      "list_price": lps[i] if i < len(lps) else "",
                      "unit_price": up, "qty": (qtys[i] if i < len(qtys) else "") or 1})
    action = f.get("action") or "save"
    try:
        create_order(department, header, lines)
        if action == "print":
            # 直前の入力内容からそのまま印刷（Notion再取得のラグを避ける）
            d = _build_d(department, header, lines)
            return templates.TemplateResponse("print.html", {
                "request": request, "d": d, "categories": CATEGORIES,
                "fmt_money": _fmt_money, "autoprint": True})
        m = quote(f"{header.get('souke') or ''}家 の受注書を登録しました")
        return RedirectResponse(f"/?department={quote(department)}&msg={m}", status_code=303)
    except Exception as e:  # noqa: BLE001
        return RedirectResponse(f"/?department={quote(department)}&err={quote(str(e))}",
                                status_code=303)


def _fmt_money(v):
    return format(int(round(v or 0)), ",")


def _build_d(department, header, lines):
    norm = []
    for l in lines:
        up = _num(l.get("unit_price"))
        qy = _num(l.get("qty")) or 1
        norm.append({"category": _normcat(l.get("category")), "product": l.get("product"),
                     "list_price": l.get("list_price"), "unit_price": up, "qty": qy,
                     "amount": up * qy})
    groups = {c: [l for l in norm if l["category"] == c] for c in CATEGORIES}
    totals = {c: sum(l["amount"] for l in groups[c]) for c in CATEGORIES}
    other = sum(totals[c] for c in CATEGORIES if c not in ("祭壇", "供花"))
    grand = sum(totals.values())
    return {"header": {**header, "department": department}, "groups": groups,
            "totals": totals, "other_total": other, "grand_total": grand,
            "tax": _tax_breakdown(header.get("tax_kind"), grand)}


def _tax_breakdown(tax_kind, grand_total, rate=0.10):
    g = grand_total or 0
    if (tax_kind or "税別") == "税込":
        net = round(g / (1 + rate))
        return {"kind": "税込", "net": net, "tax": round(g - net), "incl": round(g)}
    tax = round(g * rate)
    return {"kind": "税別", "net": round(g), "tax": tax, "incl": round(g + tax)}


def _num(x):
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


@app.get("/print/{page_id}", response_class=HTMLResponse)
def print_order(request: Request, page_id: str, _u: str = Depends(auth)):
    """登録済み受注書をA4縦1枚で印刷（Notionから読み出し）。"""
    with httpx.Client(timeout=30) as client:
        rp = client.get(f"{API}/pages/{page_id}", headers=_headers())
        if rp.status_code != 200:
            return HTMLResponse("受注書が見つかりません。", status_code=404)
        page = rp.json()
        hp = page.get("properties", {})
        dept = _dept_of((page.get("parent") or {}).get("database_id")) or ""
        line_db = SOURCES.get(dept, {}).get("order_line")
        lines = []
        if line_db:
            rq = client.post(f"{API}/databases/{line_db}/query", headers=_headers(),
                             json={"filter": {"property": L["rel"],
                                              "relation": {"contains": page_id}}, "page_size": 100})
            for it in rq.json().get("results", []):
                lp = it.get("properties", {})
                lines.append({"category": _pget(lp, L["category"]),
                              "product": _pget(lp, L["product"]),
                              "list_price": _pget(lp, L["list_price"]),
                              "unit_price": _pget(lp, L["unit_price"]),
                              "qty": _pget(lp, L["qty"])})
    header = {"order_date": _pget(hp, H["order_date"]),
              "service_date": _pget(hp, H["service_date"]), "teardown_date": _pget(hp, H["teardown_date"]),
              "customer": _pget(hp, H["customer"]), "venue": _pget(hp, H["venue"]),
              "souke": _pget(hp, H["souke"]), "gender": _pget(hp, H["gender"]),
              "money_transfer": _pget(hp, H["money_transfer"]), "note": _pget(hp, H["note"]),
              "tax_kind": _pget(hp, H["tax_kind"])}
    d = _build_d(dept, header, lines)
    return templates.TemplateResponse("print.html", {
        "request": request, "d": d, "categories": CATEGORIES, "fmt_money": _fmt_money})
