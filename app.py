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
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, Depends, HTTPException, status
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

BASE = Path(__file__).resolve().parent
API = "https://api.notion.com/v1"
CATEGORIES = ["祭壇", "供花", "OP", "その他"]
SOSHISHA = ["ダイキ", "ワシダ", "典礼", "パシオン", "奥越"]

# Notionプロパティ名（本体アプリと一致）
H = {"souke": "御葬家名", "order_date": "受注日", "service_date": "施行予定日時",
     "teardown_date": "撤収予定日時", "customer": "得意先", "venue": "式場・住所",
     "gender": "性別", "person": "ご担当者", "money_transfer": "売上金移動",
     "tax_kind": "税区分", "note": "備考"}
L = {"product": "品名", "rel": "受注書", "category": "カテゴリ",
     "list_price": "上代金額", "unit_price": "受注額", "qty": "本数"}

SOURCES = json.loads((BASE / "sources.json").read_text(encoding="utf-8"))["departments"]
TOKEN = os.environ.get("NOTION_TOKEN", "")
PASSWORD = os.environ.get("FORM_PASSWORD", "")
USER = os.environ.get("FORM_USER", "staff")

app = FastAPI(title="受注書入力フォーム")
templates = Jinja2Templates(directory=str(BASE / "templates"))
security = HTTPBasic()


def auth(creds: HTTPBasicCredentials = Depends(security)):
    ok_u = secrets.compare_digest(creds.username, USER)
    ok_p = bool(PASSWORD) and secrets.compare_digest(creds.password, PASSWORD)
    if not (ok_u and ok_p):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="認証が必要です", headers={"WWW-Authenticate": "Basic"})
    return creds.username


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
            H["person"]: _text(header.get("person")),
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
    return templates.TemplateResponse("form.html", {
        "request": request, "departments": depts, "department": department,
        "soshisha": SOSHISHA, "categories": CATEGORIES,
        "today": datetime.now().strftime("%Y-%m-%d"), "msg": msg, "err": err,
    })


@app.post("/create")
async def create(request: Request, _u: str = Depends(auth)):
    from urllib.parse import quote
    from fastapi.responses import RedirectResponse
    f = await request.form()
    department = f.get("department") or ""
    header = {k: (f.get(k) or None) for k in
              ("souke", "order_date", "service_date", "teardown_date", "customer",
               "venue", "gender", "person", "money_transfer", "tax_kind", "note")}
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
    try:
        create_order(department, header, lines)
        m = quote(f"{header.get('souke') or ''}家 の受注書を登録しました")
        return RedirectResponse(f"/?department={quote(department)}&msg={m}", status_code=303)
    except Exception as e:  # noqa: BLE001
        return RedirectResponse(f"/?department={quote(department)}&err={quote(str(e))}",
                                status_code=303)
