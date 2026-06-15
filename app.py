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
# 配達書（アレンジ・鉢物・花束など日時の決まった配達物）のカテゴリ
DELIVERY_CATEGORIES = ["忌明け花", "枕花", "アレンジメント", "花束", "観葉植物", "鉢物", "その他"]

# Notionプロパティ名（本体アプリと一致）
H = {"souke": "御葬家名", "order_date": "受注日", "service_date": "施行予定日時",
     "teardown_date": "撤収予定日時", "customer": "得意先", "venue": "式場・住所",
     "gender": "性別", "money_transfer": "売上金移動",
     "tax_kind": "税区分", "note": "備考",
     # 配達書用（本体アプリ notion_client.py と一致）
     "doc_type": "帳票種別", "purpose": "用途", "delivery_at": "配達日時",
     "name2": "喪主名・札名", "deliver_address": "届け先住所", "deliver_phone": "届け先電話",
     "cash_receipt": "領収現金", "receipt_needed": "領収書要否", "receipt_name": "領収書名"}
L = {"product": "品名", "rel": "受注書", "category": "カテゴリ",
     "list_price": "上代金額", "unit_price": "受注額", "qty": "本数",
     "tax": "税区分", "amount": "合計"}
# 卸部の月次入力（本体 notion_client.py の R_/P_/LS_ と一致）
OROSHI_DEPT = "福井卸部"
R = {"customer": "得意先", "date": "日付", "amount": "売上額",
     "qty": "数量", "note": "備考"}   # 卸売上(顧客別)・数量=月間販売件数
P = {"item": "品名", "date": "日付", "supplier": "仕入先", "amount": "金額", "note": "備考"}  # 仕入
LS = {"item": "項目", "date": "年月", "amount": "金額", "note": "備考"}        # 月間ロス

SOURCES = json.loads((BASE / "sources.json").read_text(encoding="utf-8"))["departments"]
TOKEN = os.environ.get("NOTION_TOKEN", "")
PASSWORD = os.environ.get("FORM_PASSWORD", "")
USER = os.environ.get("FORM_USER", "staff")
COMPANY = os.environ.get("COMPANY_NAME", "株式会社大栄フラワーサービス福井")

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


def _header_props(header):
    """ヘッダ(受注書/配達書)のNotionプロパティ。帳票種別で施行系/配達系を出し分け。"""
    doc_type = header.get("doc_type") or "施行受注書"
    is_deliv = doc_type == "配達書"
    hp = {
        H["doc_type"]: _select(doc_type),
        H["souke"]: _title(header.get("souke")),
        H["order_date"]: _date(header.get("order_date")),
        H["customer"]: _select(header.get("customer")),
        H["gender"]: _select(header.get("gender")),
        H["tax_kind"]: _select(header.get("tax_kind")),
        H["note"]: _text(header.get("note")),
    }
    if is_deliv:
        hp.update({
            H["purpose"]: _select(header.get("purpose")),
            H["delivery_at"]: _date(header.get("delivery_at")),
            H["name2"]: _text(header.get("name2")),
            H["deliver_address"]: _text(header.get("deliver_address")),
            H["deliver_phone"]: _text(header.get("deliver_phone")),
            H["cash_receipt"]: _select(header.get("cash_receipt")),
            H["receipt_needed"]: _select(header.get("receipt_needed")),
            H["receipt_name"]: _text(header.get("receipt_name")),
        })
    else:
        hp.update({
            H["service_date"]: _date(header.get("service_date")),
            H["teardown_date"]: _date(header.get("teardown_date")),
            H["venue"]: _text(header.get("venue")),
            H["money_transfer"]: _text(header.get("money_transfer")),
        })
    return hp


def _line_props(ln):
    return {
        L["product"]: _title(ln.get("product")),
        L["category"]: _select(ln.get("category")),
        L["list_price"]: _number(ln.get("list_price")),
        L["unit_price"]: _number(ln.get("unit_price")),
        L["qty"]: _number(ln.get("qty")),
        L["tax"]: _select(ln.get("tax_kind") or "税別"),
    }


def create_order(department, header, lines):
    src = SOURCES.get(department)
    if not src:
        raise RuntimeError(f"部門が未設定です: {department}")
    if not TOKEN:
        raise RuntimeError("NOTION_TOKEN が未設定です")
    with httpx.Client(timeout=60) as client:
        r = client.post(f"{API}/pages", headers=_headers(),
                        json={"parent": {"database_id": src["order_header"]}, "properties": _header_props(header)})
        if r.status_code != 200:
            raise RuntimeError(f"受注書の作成に失敗 {r.status_code}: {r.text[:300]}")
        hid = r.json()["id"]
        new_ids = []
        try:
            for ln in lines:
                if not (ln.get("product") or ln.get("unit_price")):
                    continue
                lp = _line_props(ln)
                lp[L["rel"]] = {"relation": [{"id": hid}]}
                rr = client.post(f"{API}/pages", headers=_headers(),
                                 json={"parent": {"database_id": src["order_line"]}, "properties": lp})
                if rr.status_code != 200:
                    raise RuntimeError(f"明細の作成に失敗 {rr.status_code}: {rr.text[:300]}")
                new_ids.append(rr.json()["id"])
        except Exception:
            # 途中失敗：作成済みのヘッダ＋明細をアーカイブして巻き戻す
            for pid in new_ids + [hid]:
                try:
                    client.patch(f"{API}/pages/{pid}", headers=_headers(), json={"archived": True})
                except Exception:  # noqa: BLE001
                    pass
            raise
    return hid


def _existing_line_ids(client, department, page_id):
    """指定受注書にぶら下がる既存明細(Notion)のページID集合。"""
    ldb = SOURCES.get(department, {}).get("order_line")
    ids = set()
    if not ldb:
        return ids
    rq = client.post(f"{API}/databases/{ldb}/query", headers=_headers(),
                     json={"filter": {"property": L["rel"], "relation": {"contains": page_id}},
                           "page_size": 100})
    if rq.status_code == 200:
        ids = {it["id"] for it in rq.json().get("results", [])}
    return ids


def update_order(department, page_id, header, lines):
    """既存受注書/配達書を更新。ヘッダPATCH＋明細upsert（line_page_idありはPATCH/なしは新規）、
    Notion上にあって今回送信されなかった明細はアーカイブ（＝削除）。"""
    src = SOURCES.get(department)
    if not src:
        raise RuntimeError(f"部門が未設定です: {department}")
    if not TOKEN:
        raise RuntimeError("NOTION_TOKEN が未設定です")
    with httpx.Client(timeout=60) as client:
        existing = _existing_line_ids(client, department, page_id)
        r = client.patch(f"{API}/pages/{page_id}", headers=_headers(),
                         json={"properties": _header_props(header)})
        if r.status_code != 200:
            raise RuntimeError(f"受注書の更新に失敗 {r.status_code}: {r.text[:300]}")
        submitted = set()
        new_ids = []
        try:
            for ln in lines:
                if not (ln.get("product") or ln.get("unit_price")):
                    continue
                lid = ln.get("line_page_id")
                lp = _line_props(ln)
                if lid:
                    rr = client.patch(f"{API}/pages/{lid}", headers=_headers(),
                                      json={"properties": lp})
                    submitted.add(lid)
                else:
                    lp[L["rel"]] = {"relation": [{"id": page_id}]}
                    rr = client.post(f"{API}/pages", headers=_headers(),
                                     json={"parent": {"database_id": src["order_line"]}, "properties": lp})
                if rr.status_code != 200:
                    raise RuntimeError(f"明細の更新に失敗 {rr.status_code}: {rr.text[:300]}")
                if not lid:
                    new_ids.append(rr.json()["id"])
        except Exception:
            for pid in new_ids:  # 今回新規作成した明細だけ巻き戻す
                try:
                    client.patch(f"{API}/pages/{pid}", headers=_headers(), json={"archived": True})
                except Exception:  # noqa: BLE001
                    pass
            raise
        for lid in existing - submitted:
            client.patch(f"{API}/pages/{lid}", headers=_headers(), json={"archived": True})
    return page_id


def delete_order(department, page_id):
    """受注書/配達書をまるごと取消（ヘッダ＋ぶら下がる明細をアーカイブ＝非表示化）。"""
    if not TOKEN:
        raise RuntimeError("NOTION_TOKEN が未設定です")
    with httpx.Client(timeout=60) as client:
        for lid in _existing_line_ids(client, department, page_id):
            try:
                client.patch(f"{API}/pages/{lid}", headers=_headers(), json={"archived": True})
            except Exception:  # noqa: BLE001
                pass
        r = client.patch(f"{API}/pages/{page_id}", headers=_headers(), json={"archived": True})
        if r.status_code != 200:
            raise RuntimeError(f"取消に失敗 {r.status_code}: {r.text[:300]}")


def _oroshi_dbs():
    src = SOURCES.get(OROSHI_DEPT, {})
    return src.get("retail_sales"), src.get("purchase"), src.get("loss")


def _query_month(client, db_id, date_prop, month):
    """指定DBから「日付/年月が month(YYYY-MM)で始まる」行を取得。(page_id, props)のリスト。"""
    if not db_id:
        return []
    out = []
    r = client.post(f"{API}/databases/{db_id}/query", headers=_headers(),
                    json={"page_size": 100})
    if r.status_code != 200:
        return out
    for it in r.json().get("results", []):
        pr = it.get("properties", {})
        d = _pget(pr, date_prop) or ""
        if d[:7] == month:
            out.append((it["id"], pr))
    return out


def read_oroshi(month):
    """卸部のその月の 卸売上・仕入・月間ロス を読む（プレフィル用）。"""
    sales, purchases, losses = [], [], []
    if not TOKEN:
        return {"sales": sales, "purchases": purchases, "losses": losses}
    rdb, pdb, ldb = _oroshi_dbs()
    with httpx.Client(timeout=30) as client:
        for pid, pr in _query_month(client, rdb, R["date"], month):
            sales.append({"page_id": pid, "customer": _pget(pr, R["customer"]),
                          "qty": _pget(pr, R["qty"]),
                          "amount": _pget(pr, R["amount"]), "note": _pget(pr, R["note"])})
        for pid, pr in _query_month(client, pdb, P["date"], month):
            purchases.append({"page_id": pid, "supplier": _pget(pr, P["supplier"]),
                              "amount": _pget(pr, P["amount"]), "note": _pget(pr, P["note"])})
        for pid, pr in _query_month(client, ldb, LS["date"], month):
            losses.append({"page_id": pid, "amount": _pget(pr, LS["amount"]),
                           "note": _pget(pr, LS["note"])})
    return {"sales": sales, "purchases": purchases, "losses": losses}


def _reconcile_rows(client, db_id, date_prop, month, rows, props_fn, keep_fn):
    """その月の行を upsert（page_idありPATCH/なし新規）し、今回送信されなかった既存行をアーカイブ。"""
    if not db_id:
        return
    existing = {pid for pid, _ in _query_month(client, db_id, date_prop, month)}
    submitted = set()
    for row in rows:
        if not keep_fn(row):
            continue
        pid = (row.get("page_id") or "").strip()
        props = props_fn(row)
        if pid:
            rr = client.patch(f"{API}/pages/{pid}", headers=_headers(), json={"properties": props})
            submitted.add(pid)
        else:
            rr = client.post(f"{API}/pages", headers=_headers(),
                             json={"parent": {"database_id": db_id}, "properties": props})
        if rr.status_code != 200:
            raise RuntimeError(f"卸部データの保存に失敗 {rr.status_code}: {rr.text[:300]}")
    for pid in existing - submitted:
        client.patch(f"{API}/pages/{pid}", headers=_headers(), json={"archived": True})


def save_oroshi(month, sales, purchases, losses):
    """卸部のその月の 卸売上・仕入・月間ロス をまとめて保存（月単位で upsert＋差分削除）。"""
    if not TOKEN:
        raise RuntimeError("NOTION_TOKEN が未設定です")
    rdb, pdb, ldb = _oroshi_dbs()
    day = f"{month}-01"
    with httpx.Client(timeout=60) as client:
        _reconcile_rows(
            client, rdb, R["date"], month, sales,
            lambda row: {R["customer"]: _title(row.get("customer")),
                         R["date"]: _date(day),
                         R["amount"]: _number(row.get("amount")),
                         R["qty"]: _number(row.get("qty")),
                         R["note"]: _text(row.get("note"))},
            keep_fn=lambda row: (row.get("customer") or "").strip()
                                and (_num(row.get("amount")) or _num(row.get("qty"))))
        _reconcile_rows(
            client, pdb, P["date"], month, purchases,
            lambda row: {P["item"]: _title(row.get("supplier") or "仕入"),
                         P["supplier"]: _text(row.get("supplier")),
                         P["date"]: _date(day),
                         P["amount"]: _number(row.get("amount")),
                         P["note"]: _text(row.get("note"))},
            keep_fn=lambda row: _num(row.get("amount")))
        _reconcile_rows(
            client, ldb, LS["date"], month, losses,
            lambda row: {LS["item"]: _title("月間ロス"),
                         LS["date"]: _date(day),
                         LS["amount"]: _number(row.get("amount")),
                         LS["note"]: _text(row.get("note"))},
            keep_fn=lambda row: _num(row.get("amount")))


def recent_orders(department, limit=60):
    """部門の受注書/配達書を新しい順（作成日時降順）で一覧取得（修正・取消用）。"""
    src = SOURCES.get(department, {})
    hdb = src.get("order_header")
    if not (TOKEN and hdb):
        return []
    out = []
    with httpx.Client(timeout=30) as client:
        r = client.post(f"{API}/databases/{hdb}/query", headers=_headers(),
                        json={"sorts": [{"timestamp": "created_time", "direction": "descending"}],
                              "page_size": limit})
        if r.status_code != 200:
            return []
        for it in r.json().get("results", []):
            hp = it.get("properties", {})
            out.append({
                "id": it["id"],
                "doc_type": _pget(hp, H["doc_type"]) or "施行受注書",
                "souke": _pget(hp, H["souke"]),
                "customer": _pget(hp, H["customer"]),
                "order_date": _pget(hp, H["order_date"]),
                "service_date": _pget(hp, H["service_date"]),
                "delivery_at": _pget(hp, H["delivery_at"]),
                "purpose": _pget(hp, H["purpose"]),
            })
    return out


def read_order(page_id):
    """Notionから受注書/配達書のヘッダ＋明細(line_page_id付き)を読む。(dept, header, lines)。"""
    if not TOKEN:
        return None
    with httpx.Client(timeout=30) as client:
        rp = client.get(f"{API}/pages/{page_id}", headers=_headers())
        if rp.status_code != 200:
            return None
        page = rp.json()
        hp = page.get("properties", {})
        dept = _dept_of((page.get("parent") or {}).get("database_id")) or ""
        header = {
            "doc_type": _pget(hp, H["doc_type"]) or "施行受注書",
            "souke": _pget(hp, H["souke"]), "order_date": _pget(hp, H["order_date"]),
            "service_date": _pget(hp, H["service_date"]), "teardown_date": _pget(hp, H["teardown_date"]),
            "customer": _pget(hp, H["customer"]), "venue": _pget(hp, H["venue"]),
            "gender": _pget(hp, H["gender"]), "money_transfer": _pget(hp, H["money_transfer"]),
            "tax_kind": _pget(hp, H["tax_kind"]), "note": _pget(hp, H["note"]),
            "purpose": _pget(hp, H["purpose"]), "delivery_at": _pget(hp, H["delivery_at"]),
            "name2": _pget(hp, H["name2"]), "deliver_address": _pget(hp, H["deliver_address"]),
            "deliver_phone": _pget(hp, H["deliver_phone"]), "cash_receipt": _pget(hp, H["cash_receipt"]),
            "receipt_needed": _pget(hp, H["receipt_needed"]), "receipt_name": _pget(hp, H["receipt_name"]),
        }
        lines = []
        ldb = SOURCES.get(dept, {}).get("order_line")
        if ldb:
            rq = client.post(f"{API}/databases/{ldb}/query", headers=_headers(),
                             json={"filter": {"property": L["rel"], "relation": {"contains": page_id}},
                                   "page_size": 100})
            for it in rq.json().get("results", []):
                lp = it.get("properties", {})
                lines.append({
                    "line_page_id": it["id"], "category": _normcat(_pget(lp, L["category"]),
                        DELIVERY_CATEGORIES if header["doc_type"] == "配達書" else CATEGORIES),
                    "product": _pget(lp, L["product"]), "tax_kind": _pget(lp, L["tax"]),
                    "list_price": _pget(lp, L["list_price"]), "unit_price": _pget(lp, L["unit_price"]),
                    "qty": _pget(lp, L["qty"]),
                })
    return dept, header, lines


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


def _normcat(c, allowed=CATEGORIES):
    c = (c or "").strip()
    if c in ("OP", "ＯＰ"):
        return "オプション"
    return c if c in allowed else "その他"


def _dept_of(header_db_id):
    for d, src in SOURCES.items():
        if src.get("order_header") == header_db_id:
            return d
    return None


def _dept_of_page(page_id):
    """受注書ページIDから所属部門を割り出す（hidden未送信時のフォールバック）。"""
    if not TOKEN:
        return None
    try:
        with httpx.Client(timeout=20) as client:
            r = client.get(f"{API}/pages/{page_id}", headers=_headers())
            if r.status_code != 200:
                return None
            return _dept_of((r.json().get("parent") or {}).get("database_id"))
    except Exception:  # noqa: BLE001
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
        "delivery_categories": DELIVERY_CATEGORIES,
        "today": datetime.now().strftime("%Y-%m-%d"), "msg": msg, "err": err,
        "edit_mode": False, "form_action": "/create", "h": {}, "init_lines": [],
    })


def _parse_form(f):
    """受注書/配達書フォーム → (department, header, lines, action)。lines は line_page_id 付き。"""
    department = f.get("department") or ""
    header = {k: (f.get(k) or None) for k in
              ("souke", "order_date", "service_date", "teardown_date", "customer",
               "venue", "gender", "money_transfer", "note",
               "doc_type", "purpose", "delivery_at", "name2",
               "deliver_address", "deliver_phone",
               "cash_receipt", "receipt_needed", "receipt_name")}
    cats, prods = f.getlist("line_category"), f.getlist("line_product")
    lps, ups, qtys = f.getlist("line_list_price"), f.getlist("line_unit_price"), f.getlist("line_qty")
    taxes, pids = f.getlist("line_tax"), f.getlist("line_page_id")
    lines = []
    for i in range(len(cats)):
        prod = prods[i] if i < len(prods) else ""
        up = ups[i] if i < len(ups) else ""
        if not (prod or up):
            continue
        lines.append({"category": cats[i], "product": prod,
                      "list_price": lps[i] if i < len(lps) else "",
                      "unit_price": up, "qty": (qtys[i] if i < len(qtys) else "") or 1,
                      "tax_kind": (taxes[i] if i < len(taxes) else "") or "税別",
                      "line_page_id": (pids[i] if i < len(pids) else "") or None})
    return department, header, lines, (f.get("action") or "save")


def _print_response(request, department, header, lines):
    is_deliv = (header.get("doc_type") == "配達書")
    d = _build_d(department, header, lines)
    tpl = "print_delivery.html" if is_deliv else "print.html"
    cats = DELIVERY_CATEGORIES if is_deliv else CATEGORIES
    return templates.TemplateResponse(tpl, {
        "request": request, "d": d, "categories": cats,
        "fmt_money": _fmt_money, "autoprint": True, "company_name": COMPANY})


@app.post("/create")
async def create(request: Request, _u: str = Depends(auth), _c: None = Depends(check_csrf)):
    from urllib.parse import quote
    from fastapi.responses import RedirectResponse
    f = await request.form()
    department, header, lines, action = _parse_form(f)
    try:
        create_order(department, header, lines)
        is_deliv = (header.get("doc_type") == "配達書")
        if action == "print":
            # 直前の入力内容からそのまま印刷（Notion再取得のラグを避ける）
            return _print_response(request, department, header, lines)
        name = header.get("souke") or ""
        m = quote(f"{name} の配達書を登録しました" if is_deliv
                  else f"{name}家 の受注書を登録しました")
        return RedirectResponse(f"/list?department={quote(department)}&msg={m}", status_code=303)
    except Exception as e:  # noqa: BLE001
        return RedirectResponse(f"/?department={quote(department)}&err={quote(str(e))}",
                                status_code=303)


@app.get("/list", response_class=HTMLResponse)
def order_list(request: Request, department: str = "", msg: str = "", err: str = "",
               _u: str = Depends(auth)):
    depts = list(SOURCES.keys())
    if department not in depts:
        department = depts[0] if depts else ""
    return templates.TemplateResponse("list.html", {
        "request": request, "departments": depts, "department": department,
        "orders": recent_orders(department), "msg": msg, "err": err,
    })


@app.get("/edit/{page_id}", response_class=HTMLResponse)
def order_edit(request: Request, page_id: str, err: str = "", _u: str = Depends(auth)):
    from datetime import datetime
    from fastapi.responses import RedirectResponse
    from urllib.parse import quote
    got = read_order(page_id)
    if not got:
        return RedirectResponse("/list", status_code=303)
    dept, header, lines = got
    if not dept:
        return RedirectResponse(f"/list?err={quote('この受注書の部門が特定できませんでした')}",
                                status_code=303)
    # 日付は input 用に整形（date=10桁、datetime-local=16桁）
    h = dict(header)
    for k in ("order_date", "service_date", "teardown_date"):
        if h.get(k):
            h[k] = h[k][:10]
    if h.get("delivery_at"):
        h["delivery_at"] = h["delivery_at"][:16]
    init_lines = [{
        "line_page_id": l["line_page_id"], "category": l["category"], "product": l["product"],
        "list_price": l["list_price"], "unit_price": l["unit_price"],
        "qty": l["qty"], "tax_kind": l["tax_kind"] or "税別",
    } for l in lines]
    depts = list(SOURCES.keys())
    return templates.TemplateResponse("form.html", {
        "request": request, "departments": depts, "department": dept,
        "dept_customers": {d: _candidates(d) for d in depts},
        "categories": CATEGORIES, "delivery_categories": DELIVERY_CATEGORIES,
        "today": datetime.now().strftime("%Y-%m-%d"), "msg": "", "err": err,
        "edit_mode": True, "form_action": f"/update/{page_id}",
        "h": h, "init_lines": init_lines,
    })


@app.post("/update/{page_id}")
async def order_update(request: Request, page_id: str,
                       _u: str = Depends(auth), _c: None = Depends(check_csrf)):
    from urllib.parse import quote
    from fastapi.responses import RedirectResponse
    f = await request.form()
    department, header, lines, action = _parse_form(f)
    try:
        update_order(department, page_id, header, lines)
        if action == "print":
            return _print_response(request, department, header, lines)
        return RedirectResponse(
            f"/list?department={quote(department)}&msg={quote('修正を保存しました')}",
            status_code=303)
    except Exception as e:  # noqa: BLE001
        return RedirectResponse(f"/edit/{page_id}?err={quote('更新に失敗：'+str(e))}",
                                status_code=303)


@app.post("/delete/{page_id}")
async def order_delete(request: Request, page_id: str,
                       _u: str = Depends(auth), _c: None = Depends(check_csrf)):
    from urllib.parse import quote
    from fastapi.responses import RedirectResponse
    f = await request.form()
    department = f.get("department") or (_dept_of_page(page_id) or "")
    try:
        delete_order(department, page_id)
        return RedirectResponse(
            f"/list?department={quote(department)}&msg={quote('受注書/配達書を取消しました')}",
            status_code=303)
    except Exception as e:  # noqa: BLE001
        return RedirectResponse(f"/list?department={quote(department)}&err={quote('取消に失敗：'+str(e))}",
                                status_code=303)


@app.get("/oroshi", response_class=HTMLResponse)
def oroshi_form(request: Request, month: str = "", msg: str = "", err: str = "",
                _u: str = Depends(auth)):
    """卸部の月次入力（卸売上・仕入・月間ロス）。"""
    from datetime import datetime
    if not (len(month) == 7 and month[4] == "-"):
        month = datetime.now().strftime("%Y-%m")
    data = read_oroshi(month)
    return templates.TemplateResponse("oroshi.html", {
        "request": request, "department": OROSHI_DEPT, "month": month,
        "customers": SOURCES.get(OROSHI_DEPT, {}).get("oroshi_customers")
                     or SOURCES.get(OROSHI_DEPT, {}).get("customers", []),
        "sales": data["sales"], "purchases": data["purchases"], "losses": data["losses"],
        "msg": msg, "err": err,
    })


def _parse_oroshi(f):
    def rows(prefix, fields):
        cols = {k: f.getlist(f"{prefix}_{k}") for k in fields}
        n = max((len(v) for v in cols.values()), default=0)
        out = []
        for i in range(n):
            out.append({k: (cols[k][i] if i < len(cols[k]) else "") for k in fields})
        return out
    sales = rows("sale", ("customer", "qty", "amount", "note", "page_id"))
    purchases = rows("purchase", ("supplier", "amount", "note", "page_id"))
    losses = rows("loss", ("amount", "note", "page_id"))
    return sales, purchases, losses


@app.post("/oroshi/save")
async def oroshi_save(request: Request, _u: str = Depends(auth), _c: None = Depends(check_csrf)):
    from urllib.parse import quote
    from fastapi.responses import RedirectResponse
    f = await request.form()
    month = f.get("month") or ""
    sales, purchases, losses = _parse_oroshi(f)
    try:
        save_oroshi(month, sales, purchases, losses)
        return RedirectResponse(
            f"/oroshi?month={quote(month)}&msg={quote(month + ' の卸部データを保存しました')}",
            status_code=303)
    except Exception as e:  # noqa: BLE001
        return RedirectResponse(f"/oroshi?month={quote(month)}&err={quote('保存に失敗：'+str(e))}",
                                status_code=303)


def _fmt_money(v):
    return format(int(round(v or 0)), ",")


def _build_d(department, header, lines):
    is_deliv = (header.get("doc_type") == "配達書")
    cat_set = DELIVERY_CATEGORIES if is_deliv else CATEGORIES
    norm = []
    for l in lines:
        up = _num(l.get("unit_price"))
        qy = _num(l.get("qty")) or 1
        norm.append({"category": _normcat(l.get("category"), cat_set), "product": l.get("product"),
                     "list_price": l.get("list_price"), "unit_price": up, "qty": qy,
                     "amount": up * qy, "tax_kind": l.get("tax_kind") or "税別"})
    groups = {c: [l for l in norm if l["category"] == c] for c in cat_set}
    totals = {c: sum(l["amount"] for l in groups[c]) for c in cat_set}
    other = sum(totals[c] for c in cat_set if c not in ("祭壇", "供花"))
    grand = sum(totals.values())
    return {"header": {**header, "department": department}, "groups": groups,
            "totals": totals, "other_total": other, "grand_total": grand,
            "tax": _tax_from_lines(norm)}


def _tax_from_lines(lines, rate=0.10):
    net = tax = incl = 0
    for l in lines:
        amt = l.get("amount") or 0
        if (l.get("tax_kind") or "税別") == "税込":
            n = round(amt / (1 + rate)); net += n; tax += round(amt - n); incl += round(amt)
        else:
            net += round(amt); tax += round(amt * rate); incl += round(amt + amt * rate)
    return {"net": net, "tax": tax, "incl": incl}


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
                              "tax_kind": _pget(lp, L["tax"]),
                              "list_price": _pget(lp, L["list_price"]),
                              "unit_price": _pget(lp, L["unit_price"]),
                              "qty": _pget(lp, L["qty"])})
    header = {"order_date": _pget(hp, H["order_date"]),
              "service_date": _pget(hp, H["service_date"]), "teardown_date": _pget(hp, H["teardown_date"]),
              "customer": _pget(hp, H["customer"]), "venue": _pget(hp, H["venue"]),
              "souke": _pget(hp, H["souke"]), "gender": _pget(hp, H["gender"]),
              "money_transfer": _pget(hp, H["money_transfer"]), "note": _pget(hp, H["note"]),
              "tax_kind": _pget(hp, H["tax_kind"]),
              "doc_type": _pget(hp, H["doc_type"]), "purpose": _pget(hp, H["purpose"]),
              "delivery_at": _pget(hp, H["delivery_at"]), "name2": _pget(hp, H["name2"]),
              "deliver_address": _pget(hp, H["deliver_address"]),
              "deliver_phone": _pget(hp, H["deliver_phone"]),
              "cash_receipt": _pget(hp, H["cash_receipt"]),
              "receipt_needed": _pget(hp, H["receipt_needed"]),
              "receipt_name": _pget(hp, H["receipt_name"])}
    is_deliv = (header.get("doc_type") == "配達書")
    d = _build_d(dept, header, lines)
    tpl = "print_delivery.html" if is_deliv else "print.html"
    cats = DELIVERY_CATEGORIES if is_deliv else CATEGORIES
    return templates.TemplateResponse(tpl, {
        "request": request, "d": d, "categories": cats, "fmt_money": _fmt_money,
        "company_name": COMPANY})
