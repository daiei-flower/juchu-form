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
from fastapi import FastAPI, Request, Depends, HTTPException, status, UploadFile, File
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

_CFG = json.loads((BASE / "sources.json").read_text(encoding="utf-8"))
SOURCES = _CFG["departments"]
SEIKA_DB = _CFG.get("seika_cost", "")
SHIJISHO_DB = _CFG.get("shijisho", "")
STAFF = _CFG.get("staff", [])
VEHICLES = _CFG.get("vehicles", [])
MASTERS_DB = _CFG.get("masters", "")
# フォーム設定(マスタ)DBのプロパティ名
MS = {"title": "名前", "type": "種別", "dept": "部門", "order": "並び"}
# 日次作業指示書DBのプロパティ名（本体 382d8bdf... と一致）
SHJ = {"title": "指示日", "date": "対象日", "blocks": "ブロックJSON", "editor": "更新者"}
# シフトDB（月次シフト表の取込先）
SHIFT_DB = _CFG.get("shift", "")
SHF = {"title": "期間", "start": "開始日", "end": "終了日", "data": "データJSON"}
# 部門→既定勤務地（指示書の当日名簿）。加賀以外は福井。
DEPT_LOCATION = {"加賀業務部": "加賀"}
# 当日の出社者カラムの並び（所属部門ごとに縦5分割）
DEPT_COLS = ["福井業務部", "福井卸部", "加賀業務部", "造花部", "本部"]
TOKEN = os.environ.get("NOTION_TOKEN", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
SEIKA_MODEL = "claude-sonnet-4-6"
# 生花原価明細DBのプロパティ名（本体 380d8bdf... と一致）
SK = {"title": "花材・色", "date": "出荷日", "material": "花材", "color": "色",
      "qty": "本数", "unit": "単価", "amount": "金額", "supplier": "仕入先",
      "dept": "部門", "note": "備考"}
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


_MASTERS_CACHE = {}
_SHIFT_CACHE = {}


def load_masters(force=False):
    """フォーム設定DBを読み {staff, vehicles, venues, customers:{dept:[...]}} を返す（簡易キャッシュ）。"""
    if _MASTERS_CACHE and not force:
        return _MASTERS_CACHE
    out = {"staff": [], "vehicles": [], "venues": [], "customers": {}}
    if not (TOKEN and MASTERS_DB):
        return out
    rows = []
    with httpx.Client(timeout=30) as client:
        cursor = None
        while True:
            body = {"page_size": 100, "sorts": [{"property": MS["order"], "direction": "ascending"}]}
            if cursor:
                body["start_cursor"] = cursor
            r = client.post(f"{API}/databases/{MASTERS_DB}/query", headers=_headers(), json=body)
            if r.status_code != 200:
                break
            j = r.json()
            rows.extend(j.get("results", []))
            if not j.get("has_more"):
                break
            cursor = j.get("next_cursor")
    for it in rows:
        p = it.get("properties", {})
        name = (_pget(p, MS["title"]) or "").strip()
        typ = _pget(p, MS["type"])
        if not name:
            continue
        if typ == "担当":
            out["staff"].append(name)
        elif typ == "車両":
            out["vehicles"].append(name)
        elif typ == "式場":
            out["venues"].append(name)
        elif typ == "得意先":
            d = _pget(p, MS["dept"]) or ""
            out["customers"].setdefault(d, []).append(name)
    _MASTERS_CACHE.clear()
    _MASTERS_CACHE.update(out)
    return out


def _masters_rows():
    """設定ページ用：全マスタ行を (page_id, 種別, 名前, 部門) で返す。"""
    rows = []
    if not (TOKEN and MASTERS_DB):
        return rows
    with httpx.Client(timeout=30) as client:
        cursor = None
        while True:
            body = {"page_size": 100}
            if cursor:
                body["start_cursor"] = cursor
            r = client.post(f"{API}/databases/{MASTERS_DB}/query", headers=_headers(), json=body)
            if r.status_code != 200:
                break
            j = r.json()
            for it in j.get("results", []):
                p = it.get("properties", {})
                rows.append({"id": it["id"], "type": _pget(p, MS["type"]),
                             "name": _pget(p, MS["title"]), "dept": _pget(p, MS["dept"])})
            if not j.get("has_more"):
                break
            cursor = j.get("next_cursor")
    return rows


def _candidates(department):
    """部門の得意先候補＝固定リスト ∪ 設定(マスタ) ∪ Notion現存オプション。"""
    src = SOURCES.get(department, {})
    out = list(src.get("customers", []))
    seen = set(out)
    for name in load_masters().get("customers", {}).get(department, []):
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    for name in _select_options(src.get("order_header"), H["customer"]):
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def _venues():
    """式場候補（設定マスタから）。"""
    return load_masters().get("venues", [])


# ───────────────────────── シフト（月次シフト表の取込・反映） ─────────────────────────
_SHIFT_SKIP_COLS = {"日付", "曜日", "出社数", "全出社数", "備考", ""}
# 記号の表記ゆれを吸収（半角/別字種も拾う）。未知の値は素通しせず後段で扱う。
_SHIFT_SYM = {"出": "出", "出勤": "出",
              "●": "休", "○": "休", "◯": "休", "休": "休",
              "▲": "希望休", "△": "希望休", "希望休": "希望休",
              "出張": "出張"}


def parse_shift_xlsx(file_bytes):
    """シフト表Excelを {start, end, days:{date:{name:status}}, depts:{name:dept}} に解析。
    記号正規化: 出→出 / ●→休 / ▲→希望休 / 出張→出張 / 空→未記入。年跨ぎ対応。"""
    import io
    import re
    import openpyxl
    try:
        wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True, read_only=True)
    except Exception:  # noqa: BLE001
        raise ValueError("Excelファイルを開けませんでした。壊れていないか、xlsx形式かご確認ください。")
    try:
        ws = wb.worksheets[0]
        grid = [list(row) for row in ws.iter_rows(values_only=True)]
    finally:
        wb.close()
    start_year = None
    for row in grid[:3]:
        for v in row:
            m = re.search(r"(\d{4})\s*年", str(v or ""))
            if m:
                start_year = int(m.group(1))
                break
        if start_year:
            break
    hidx = None
    for i, row in enumerate(grid):
        if str(row[0] or "").strip() == "日付":
            hidx = i
            break
    if hidx is None:
        raise ValueError("ヘッダ行（『日付』の行）が見つかりません。シフト表の形式をご確認ください。")
    header = grid[hidx]
    deptrow = grid[hidx - 1] if hidx > 0 else [None] * len(header)
    filled, cur = [], ""
    for v in deptrow:
        s = str(v or "").strip()
        if s:
            cur = s
        filled.append(cur)
    cols, depts = {}, {}
    for c, h in enumerate(header):
        nm = str(h or "").strip()
        if nm and nm not in _SHIFT_SKIP_COLS:
            cols[c] = nm
            depts[nm] = filled[c] if c < len(filled) else ""
    days = {}
    prev_month = None
    year = start_year or 2000
    for row in grid[hidx + 1:]:
        a = str(row[0] or "").strip()
        m = re.match(r"^(\d{1,2})\s*/\s*(\d{1,2})$", a)
        if not m:
            continue
        mo, da = int(m.group(1)), int(m.group(2))
        if prev_month is not None and mo < prev_month:
            year += 1
        prev_month = mo
        date = f"{year:04d}-{mo:02d}-{da:02d}"
        rec = {}
        for c, nm in cols.items():
            raw = str(row[c] or "").strip() if c < len(row) else ""
            key = raw.replace(" ", "").replace("　", "")
            rec[nm] = _SHIFT_SYM.get(key, "未記入" if key == "" else key)
        days[date] = rec
    dates = sorted(days.keys())
    if not dates:
        raise ValueError("日付データが読み取れませんでした。シフト表の形式をご確認ください。")
    return {"start": dates[0], "end": dates[-1], "days": days, "depts": depts}


def save_shift(parsed, editor=""):
    """シフトを保存（同じ開始日の既存ページをarchiveしてから作成＝1取込1ページ）。"""
    if not (TOKEN and SHIFT_DB):
        raise RuntimeError("シフトDBが未設定です")
    start, end = parsed.get("start", ""), parsed.get("end", "")
    payload = json.dumps(parsed, ensure_ascii=False)
    with httpx.Client(timeout=60) as client:
        r = client.post(f"{API}/databases/{SHIFT_DB}/query", headers=_headers(),
                        json={"filter": {"property": SHF["start"], "date": {"equals": start}},
                              "page_size": 10})
        if r.status_code == 200:
            for it in r.json().get("results", []):
                client.patch(f"{API}/pages/{it['id']}", headers=_headers(), json={"archived": True})
        props = {
            SHF["title"]: _title(f"{start}〜{end}"),
            SHF["start"]: _date(start),
            SHF["end"]: _date(end),
            SHF["data"]: _rich_chunks(payload),
        }
        rc = client.post(f"{API}/pages", headers=_headers(),
                         json={"parent": {"database_id": SHIFT_DB}, "properties": props})
        if rc.status_code != 200:
            raise RuntimeError(f"シフトの保存に失敗 {rc.status_code}: {rc.text[:300]}")
    _SHIFT_CACHE.clear()
    return len(parsed.get("days", {}))


def _shift_pages():
    """取込済みシフトの一覧 (id, 期間, 開始日, 終了日) を返す（新しい順）。"""
    rows = []
    if not (TOKEN and SHIFT_DB):
        return rows
    with httpx.Client(timeout=30) as client:
        r = client.post(f"{API}/databases/{SHIFT_DB}/query", headers=_headers(),
                        json={"sorts": [{"property": SHF["start"], "direction": "descending"}],
                              "page_size": 50})
        if r.status_code == 200:
            for it in r.json().get("results", []):
                p = it.get("properties", {})
                rows.append({"id": it["id"], "label": _pget(p, SHF["title"]) or "",
                             "start": _pget(p, SHF["start"]) or "", "end": _pget(p, SHF["end"]) or ""})
    return rows


def load_shift_status(date):
    """指定日の {name: status} を返す（該当期間のシフトページを探索・簡易キャッシュ）。"""
    if not (TOKEN and SHIFT_DB and date):
        return {}, {}
    if date in _SHIFT_CACHE:
        return _SHIFT_CACHE[date]
    result = ({}, {})
    with httpx.Client(timeout=30) as client:
        r = client.post(f"{API}/databases/{SHIFT_DB}/query", headers=_headers(),
                        json={"filter": {"and": [
                            {"property": SHF["start"], "date": {"on_or_before": date}},
                            {"property": SHF["end"], "date": {"on_or_after": date}}]},
                              "sorts": [{"property": SHF["start"], "direction": "descending"}],
                              "page_size": 5})
        if r.status_code == 200:
            for it in r.json().get("results", []):
                raw = _pget(it.get("properties", {}), SHF["data"]) or ""
                try:
                    data = json.loads(raw) if raw else {}
                except (ValueError, TypeError):
                    continue
                day = data.get("days", {}).get(date)
                if day:
                    result = (day, data.get("depts", {}))
                    break
    _SHIFT_CACHE[date] = result
    return result


def _norm_name(s):
    """氏名の表記ゆれ吸収（全角括弧/空白→半角・除去）して同一人物の重複を防ぐ。"""
    return (s or "").translate(str.maketrans("（）　", "() ")).replace(" ", "").strip()


def staff_for_date(date):
    """指定日の担当パレット [{name,status,dept}]（シフト名を部門順 ∪ 設定の担当）。
    全角/半角の表記ゆれは同一人物として重複表示しない。"""
    day, depts = load_shift_status(date)
    dept_rank = {"本部": 0, "福井業務部": 1, "加賀業務部": 2, "福井卸部": 3}
    names = sorted(day.keys(), key=lambda n: (dept_rank.get(depts.get(n, ""), 9), n))
    out, seen = [], set()
    for n in names:
        key = _norm_name(n)
        if key in seen:        # シフト内の表記ゆれ重複（例「山田 太郎」と「山田太郎」）を排除
            continue
        seen.add(key)
        out.append({"name": n, "status": day.get(n, ""), "dept": depts.get(n, "")})
    for n in load_masters().get("staff", []) or STAFF:
        if n and _norm_name(n) not in seen:
            seen.add(_norm_name(n))
            out.append({"name": n, "status": "", "dept": ""})
    return out


def roster_columns(date):
    """『当日の出社者』を5部門カラムで返す。各部門の所属者をシフト列順
    （責任者→入社順を想定）で常時表示。status(出/休/希望休/出張)も付ける。
    返り値: [{dept, members:[{name,dept,status,location,arrive}]}]（DEPT_COLS順）。"""
    day, depts = load_shift_status(date)
    cols = {d: [] for d in DEPT_COLS}
    seen = set()
    for name, d in depts.items():        # depts はシフト列の並びを保持
        key = _norm_name(name)
        if key in seen:
            continue
        seen.add(key)
        col = d if d in cols else "本部"   # 未知部門は本部へ寄せる（保険）
        cols[col].append({"name": name, "dept": d, "status": day.get(name, ""),
                          "location": DEPT_LOCATION.get(d, "福井"), "arrive": ""})
    return [{"dept": d, "members": cols[d]} for d in DEPT_COLS]


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, _u: str = Depends(auth)):
    """責任者用トップ。全部門の直近の動きと、各機能への入口。"""
    from datetime import datetime
    depts = list(SOURCES.keys())
    month = datetime.now().strftime("%Y-%m")
    recent, counts = [], {}
    for d in depts:
        rs = recent_orders(d, limit=30)
        c = 0
        for o in rs:
            basis = o.get("delivery_at") or o.get("service_date") or o.get("order_date") or ""
            o["_dept"] = d
            o["_date"] = basis
            if basis[:7] == month:
                c += 1
        counts[d] = c
        recent.extend(rs)
    recent.sort(key=lambda o: o.get("_date") or "", reverse=True)
    return templates.TemplateResponse("dashboard.html", {
        "request": request, "departments": depts, "recent": recent[:12],
        "counts": counts, "month": month})


@app.get("/new", response_class=HTMLResponse)
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
        "delivery_categories": DELIVERY_CATEGORIES, "venues": _venues(),
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
        return RedirectResponse(f"/new?department={quote(department)}&err={quote(str(e))}",
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
        "venues": _venues(),
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


# ───────────────────────── 生花原価（スキャン→AI取込） ─────────────────────────
SEIKA_PROMPT = """この画像/PDFは生花の「出荷伝票一覧（卸→各部門への原価移動）」です。\
各明細行を読み取り、JSONで返してください。

列：出荷日 / 伝票NO / コード(大分類:中菊・SP菊など) / 商品名(品種・色・等級・サイズ) / 出荷本数 / 売上単価 / 仕入単価 / 仕入先。

各行を次の形に正規化してください：
- date: 出荷日 (YYYY-MM-DD)
- material: 花材名。コード列の大分類、または商品名の先頭の花材名。例 中菊/SP菊/小菊/デンファレ/ドラセナ類/トルコキキョウ/デルフィニューム/カーネーション/オリエンタル百合/胡蝶蘭 等。
- color: 色だけ。例 白/ピンク/薄紫/赤/黄/オレンジ/紫。色が無ければ ""。
- qty: 出荷本数 (整数)
- unit: 売上単価 (整数)
- supplier: 仕入先（㈱○○・京○○など。読めなければ ""）
重要な正規化ルール：「業務」「秀」「2L」「L」「M」「精の一世」「トリトンLV」等の等級・規格・サイズ・品種コードは捨て、花材名と色だけ残す。
例:「トルコキキョウ・業務・ピンク」→ material=トルコキキョウ, color=ピンク。
例:「デルフィニューム・トリトンLV・薄紫」→ material=デルフィニューム, color=薄紫。
例: コード=中菊, 商品名=「2L・精の一世・白・秀・2L」→ material=中菊, color=白。
合計行・小計行・見出しは除外。出力は {"lines":[{...}, ...]} のJSONのみ。説明文は不要。"""


def seika_extract(file_bytes, content_type, filename):
    """アップロードされたPDF/画像をClaude visionで読み、明細行のリストを返す。"""
    if not ANTHROPIC_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY が未設定です（RenderのEnvに登録してください）")
    b64 = __import__("base64").standard_b64encode(file_bytes).decode()
    name = (filename or "").lower()
    ct = (content_type or "").lower()
    if "pdf" in ct or name.endswith(".pdf"):
        media = {"type": "document",
                 "source": {"type": "base64", "media_type": "application/pdf", "data": b64}}
    else:
        mt = "image/png" if name.endswith(".png") else "image/jpeg"
        media = {"type": "image", "source": {"type": "base64", "media_type": mt, "data": b64}}
    content = [{"type": "text", "text": SEIKA_PROMPT}, media]
    r = httpx.post("https://api.anthropic.com/v1/messages",
                   headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
                            "content-type": "application/json"},
                   json={"model": SEIKA_MODEL, "max_tokens": 8000,
                         "messages": [{"role": "user", "content": content}]},
                   timeout=180)
    if r.status_code != 200:
        raise RuntimeError(f"AI読み取りに失敗 {r.status_code}: {r.text[:300]}")
    txt = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
    s, e = txt.find("{"), txt.rfind("}")
    if s < 0 or e < 0:
        raise RuntimeError("AIの出力を解析できませんでした")
    data = json.loads(txt[s:e + 1])
    out = []
    for ln in data.get("lines", []):
        q = int(_num(ln.get("qty")))
        u = int(_num(ln.get("unit")))
        if not (q or u):
            continue
        out.append({"date": (ln.get("date") or "")[:10], "material": (ln.get("material") or "").strip(),
                    "color": (ln.get("color") or "").strip(), "qty": q, "unit": u,
                    "supplier": (ln.get("supplier") or "").strip(), "amount": q * u})
    return out


def seika_aggregate(lines):
    """明細行を 花材×色 / 色別 / 全体 に集計（プレビュー・分析共通）。"""
    from collections import defaultdict
    mc = defaultdict(lambda: [0, 0])   # (material,color)->[qty,amount]
    col = defaultdict(int)             # color->qty
    tot_q = tot_a = 0
    for l in lines:
        q, a = int(l.get("qty") or 0), int(l.get("amount") or (int(l.get("qty") or 0) * int(l.get("unit") or 0)))
        mc[(l.get("material") or "?", l.get("color") or "")][0] += q
        mc[(l.get("material") or "?", l.get("color") or "")][1] += a
        if l.get("color"):
            col[l["color"]] += q
        tot_q += q
        tot_a += a
    matcolor = [{"material": m, "color": c, "qty": q, "amount": a,
                 "avg": (a / q) if q else 0} for (m, c), (q, a) in mc.items()]
    matcolor.sort(key=lambda x: -x["amount"])
    colors = [{"color": c, "qty": q} for c, q in sorted(col.items(), key=lambda x: -x[1])]
    return {"matcolor": matcolor, "colors": colors, "total_qty": tot_q, "total_amount": tot_a,
            "unit_avg": (tot_a / tot_q) if tot_q else 0}


def _seika_props(dept, l):
    mat, col = l.get("material") or "", l.get("color") or ""
    q = int(_num(l.get("qty")))
    u = int(_num(l.get("unit")))
    return {
        SK["title"]: _title(f"{mat}・{col}" if col else mat),
        SK["date"]: _date(l.get("date") or None),
        SK["material"]: _select(mat or None),
        SK["color"]: _select(col or None),
        SK["qty"]: _number(q),
        SK["unit"]: _number(u),
        SK["amount"]: _number(q * u),
        SK["supplier"]: _text(l.get("supplier")),
        SK["dept"]: _select(dept),
    }


def _seika_query(client, dept=None, month=None):
    """生花原価明細を取得。dept/month で絞り込み（client側でフィルタ）。(page_id, line)のリスト。"""
    out = []
    if not (TOKEN and SEIKA_DB):
        return out
    cursor = None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        r = client.post(f"{API}/databases/{SEIKA_DB}/query", headers=_headers(), json=body)
        if r.status_code != 200:
            break
        j = r.json()
        for it in j.get("results", []):
            pr = it.get("properties", {})
            d = _pget(pr, SK["date"]) or ""
            dp = _pget(pr, SK["dept"])
            if dept and dp != dept:
                continue
            if month and d[:7] != month:
                continue
            out.append((it["id"], {
                "date": d, "material": _pget(pr, SK["material"]), "color": _pget(pr, SK["color"]),
                "qty": _pget(pr, SK["qty"]) or 0, "unit": _pget(pr, SK["unit"]) or 0,
                "amount": _pget(pr, SK["amount"]) or 0, "supplier": _pget(pr, SK["supplier"])}))
        if not j.get("has_more"):
            break
        cursor = j.get("next_cursor")
    return out


def save_seika(dept, lines):
    """生花原価明細を保存。同部門・同じ出荷日範囲の既存行をarchiveしてから作成（再取込で重複しない）。"""
    if not (TOKEN and SEIKA_DB):
        raise RuntimeError("生花原価DBが未設定です")
    dates = [l["date"] for l in lines if l.get("date")]
    dmin, dmax = (min(dates), max(dates)) if dates else (None, None)
    with httpx.Client(timeout=120) as client:
        # 同部門・取込期間に重なる既存行をアーカイブ
        if dmin and dmax:
            for pid, l in _seika_query(client, dept=dept):
                if l["date"] and dmin <= l["date"] <= dmax:
                    client.patch(f"{API}/pages/{pid}", headers=_headers(), json={"archived": True})
        created = 0
        for l in lines:
            if not (int(_num(l.get("qty"))) or int(_num(l.get("unit")))):
                continue
            rr = client.post(f"{API}/pages", headers=_headers(),
                             json={"parent": {"database_id": SEIKA_DB}, "properties": _seika_props(dept, l)})
            if rr.status_code != 200:
                raise RuntimeError(f"生花原価の保存に失敗 {rr.status_code}: {rr.text[:300]}")
            created += 1
    return created, (dmin, dmax)


def _month_sales(dept, month):
    """その月の受注書/配達書の売上合計（原価率の分母）。order_header→明細を集計。"""
    src = SOURCES.get(dept, {})
    hdb, ldb = src.get("order_header"), src.get("order_line")
    if not (TOKEN and hdb and ldb):
        return 0
    total = 0
    with httpx.Client(timeout=60) as client:
        r = client.post(f"{API}/databases/{hdb}/query", headers=_headers(),
                        json={"sorts": [{"timestamp": "created_time", "direction": "descending"}],
                              "page_size": 100})
        if r.status_code != 200:
            return 0
        ids = []
        for it in r.json().get("results", []):
            hp = it.get("properties", {})
            basis = (_pget(hp, H["delivery_at"]) or _pget(hp, H["service_date"])
                     or _pget(hp, H["order_date"]) or "")
            if basis[:7] == month:
                ids.append(it["id"])
        for hid in ids:
            rq = client.post(f"{API}/databases/{ldb}/query", headers=_headers(),
                             json={"filter": {"property": L["rel"], "relation": {"contains": hid}},
                                   "page_size": 100})
            for it in rq.json().get("results", []):
                lp = it.get("properties", {})
                up = _num(_pget(lp, L["unit_price"]))
                qy = _num(_pget(lp, L["qty"])) or 1
                total += up * qy
    return total


def read_seika_report(dept, month):
    with httpx.Client(timeout=60) as client:
        rows = [l for _, l in _seika_query(client, dept=dept, month=month)]
    agg = seika_aggregate(rows)
    sales = _month_sales(dept, month)
    agg["sales"] = sales
    agg["cost_ratio"] = (agg["total_amount"] / sales) if sales else None
    return agg


@app.get("/seika", response_class=HTMLResponse)
def seika_upload(request: Request, department: str = "", msg: str = "", err: str = "",
                 _u: str = Depends(auth)):
    depts = list(SOURCES.keys())
    if department not in depts:
        department = depts[0] if depts else ""
    return templates.TemplateResponse("seika_upload.html", {
        "request": request, "departments": depts, "department": department,
        "ai_ready": bool(ANTHROPIC_KEY), "msg": msg, "err": err})


@app.post("/seika/extract", response_class=HTMLResponse)
async def seika_do_extract(request: Request, department: str = "", file: UploadFile = File(...),
                           _u: str = Depends(auth), _c: None = Depends(check_csrf)):
    from urllib.parse import quote
    from fastapi.responses import RedirectResponse
    try:
        data = await file.read()
        if not data:
            raise RuntimeError("ファイルが空です")
        if len(data) > 15 * 1024 * 1024:
            raise RuntimeError("ファイルが大きすぎます（15MBまで）")
        name = (file.filename or "").lower()
        if not name.endswith((".pdf", ".png", ".jpg", ".jpeg")):
            raise RuntimeError("PDFまたは画像（PNG/JPG）を選んでください")
        lines = seika_extract(data, file.content_type, file.filename)
        if not lines:
            raise RuntimeError("明細を読み取れませんでした。画像が鮮明か確認してください。")
    except Exception as e:  # noqa: BLE001
        return RedirectResponse(f"/seika?department={quote(department)}&err={quote(str(e))}",
                                status_code=303)
    return templates.TemplateResponse("seika_confirm.html", {
        "request": request, "department": department, "lines": lines,
        "agg": seika_aggregate(lines), "fmt_money": _fmt_money})


@app.post("/seika/save")
async def seika_save(request: Request, _u: str = Depends(auth), _c: None = Depends(check_csrf)):
    from urllib.parse import quote
    from fastapi.responses import RedirectResponse
    f = await request.form()
    department = f.get("department") or ""
    dates, mats, cols = f.getlist("ln_date"), f.getlist("ln_material"), f.getlist("ln_color")
    qtys, units, sups = f.getlist("ln_qty"), f.getlist("ln_unit"), f.getlist("ln_supplier")
    lines = []
    for i in range(len(mats)):
        q = int(_num(qtys[i] if i < len(qtys) else 0))
        u = int(_num(units[i] if i < len(units) else 0))
        if not ((mats[i] or "").strip() and (q or u)):
            continue
        lines.append({"date": (dates[i] if i < len(dates) else "")[:10],
                      "material": mats[i].strip(), "color": (cols[i] if i < len(cols) else "").strip(),
                      "qty": q, "unit": u, "supplier": (sups[i] if i < len(sups) else "").strip(),
                      "amount": q * u})
    try:
        created, (dmin, _dmax) = save_seika(department, lines)
        month = (dmin or "")[:7]
        return RedirectResponse(
            f"/seika/report?department={quote(department)}&month={quote(month)}"
            f"&msg={quote(f'{created}行を保存しました')}", status_code=303)
    except Exception as e:  # noqa: BLE001
        return RedirectResponse(f"/seika?department={quote(department)}&err={quote('保存に失敗：'+str(e))}",
                                status_code=303)


@app.get("/seika/report", response_class=HTMLResponse)
def seika_report(request: Request, department: str = "", month: str = "", msg: str = "",
                 _u: str = Depends(auth)):
    from datetime import datetime
    depts = list(SOURCES.keys())
    if department not in depts:
        department = depts[0] if depts else ""
    if not (len(month) == 7 and month[4] == "-"):
        month = datetime.now().strftime("%Y-%m")
    agg = read_seika_report(department, month)
    return templates.TemplateResponse("seika_report.html", {
        "request": request, "departments": depts, "department": department, "month": month,
        "agg": agg, "fmt_money": _fmt_money, "msg": msg})


# ───────────────────────── 日次 作業指示書 ─────────────────────────
def _rich_chunks(s):
    """rich_text は1要素2000字上限。分割して複数要素で書き込む。読込は_pgetで自動連結。"""
    s = s or ""
    parts = [s[i:i + 1900] for i in range(0, len(s), 1900)] or [""]
    return {"rich_text": [{"text": {"content": p}} for p in parts]}


def _order_items(client, dept, page_id):
    """受注書/配達書の明細を配列で返す（カテゴリ・商品名・本数・受注額=下代）＋サマリ文字列。"""
    ldb = SOURCES.get(dept, {}).get("order_line")
    items, agg, order = [], {}, []
    if not ldb:
        return items, ""
    rq = client.post(f"{API}/databases/{ldb}/query", headers=_headers(),
                     json={"filter": {"property": L["rel"], "relation": {"contains": page_id}},
                           "page_size": 100})
    if rq.status_code != 200:
        return items, ""
    for it in rq.json().get("results", []):
        lp = it.get("properties", {})
        cat = (_pget(lp, L["category"]) or "その他").strip()
        qty = _num(_pget(lp, L["qty"])) or 0
        items.append({"category": cat, "product": _pget(lp, L["product"]) or "",
                      "qty": int(qty), "price": _num(_pget(lp, L["unit_price"]))})
        if cat not in agg:
            agg[cat] = 0
            order.append(cat)
        agg[cat] += qty
    summary = "・".join(f"{c}{int(agg[c])}" for c in order if agg[c])
    return items, summary


def day_tasks(target_date):
    """指定日の全部門の作業を 施行/撤収/配達 のカードにして返す（受注書DBは読むだけ）。
    施行=施行予定日, 撤収=撤収予定日, 配達=配達日時 が当日のもの。1受注書が施行と撤収の両方に出ることあり。"""
    if not TOKEN:
        return []
    cards = []
    with httpx.Client(timeout=90) as client:
        for dept in SOURCES:
            hdb = SOURCES.get(dept, {}).get("order_header")
            if not hdb:
                continue
            # 作成日時降順を全ページ走査（先付け登録で当日分が100件目以降にあっても取りこぼさない）。
            # 暴走防止に最大10ページ(=1000件/部門)で打ち切り。
            rows, cursor, pages = [], None, 0
            while True:
                body = {"sorts": [{"timestamp": "created_time", "direction": "descending"}],
                        "page_size": 100}
                if cursor:
                    body["start_cursor"] = cursor
                r = client.post(f"{API}/databases/{hdb}/query", headers=_headers(), json=body)
                if r.status_code != 200:
                    break
                j = r.json()
                rows.extend(j.get("results", []))
                pages += 1
                if not j.get("has_more") or pages >= 10:
                    break
                cursor = j.get("next_cursor")
            for it in rows:
                hp = it.get("properties", {})
                doc = _pget(hp, H["doc_type"]) or "施行受注書"
                pid = it["id"]
                if doc == "配達書":
                    if (_pget(hp, H["delivery_at"]) or "")[:10] != target_date:
                        continue
                    items, summary = _order_items(client, dept, pid)
                    cash = _num(_pget(hp, H["cash_receipt"]))
                    cards.append({"task_type": "配達", "order_page_id": pid, "department": dept,
                                  "souke_or_customer": _pget(hp, H["customer"]) or "",
                                  "customer": _pget(hp, H["customer"]) or "",
                                  "datetime": _pget(hp, H["delivery_at"]) or "",
                                  "place": _pget(hp, H["deliver_address"]) or "",
                                  "purpose": _pget(hp, H["purpose"]) or "",
                                  "cash_receipt": cash,
                                  "receipt_needed": _pget(hp, H["receipt_needed"]) or "",
                                  "items": items, "items_summary": summary})
                    continue
                # 施行受注書 → 施行 / 撤収 を別々に判定
                svc = (_pget(hp, H["service_date"]) or "")
                tear = (_pget(hp, H["teardown_date"]) or "")
                if svc[:10] != target_date and tear[:10] != target_date:
                    continue
                items, summary = _order_items(client, dept, pid)
                base = {"order_page_id": pid, "department": dept,
                        "souke_or_customer": _pget(hp, H["souke"]) or "",
                        "customer": _pget(hp, H["customer"]) or "",
                        "place": _pget(hp, H["venue"]) or "",
                        "items": items, "items_summary": summary}
                if svc[:10] == target_date:
                    cards.append({**base, "task_type": "施行", "datetime": svc})
                if tear[:10] == target_date:
                    cards.append({**base, "task_type": "撤収", "datetime": tear})
    order_rank = {"施行": 0, "撤収": 1, "配達": 2}
    cards.sort(key=lambda c: (order_rank.get(c["task_type"], 9), len(c["datetime"]) < 16, c["datetime"]))
    return cards


def _shijisho_page(client, target_date):
    """指定日の指示書ページ(page_id, props)を返す。なければ(None,None)。"""
    if not SHIJISHO_DB:
        return None, None
    r = client.post(f"{API}/databases/{SHIJISHO_DB}/query", headers=_headers(),
                    json={"filter": {"property": SHJ["date"], "date": {"equals": target_date}},
                          "page_size": 1})
    if r.status_code == 200:
        res = r.json().get("results", [])
        if res:
            return res[0]["id"], res[0].get("properties", {})
    return None, None


def _normalize_shijisho(raw):
    """保存値を {header:{souban,shijisha,roster}, blocks:[...]} に正規化（旧list形式も吸収）。"""
    try:
        data = json.loads(raw) if raw else None
    except (ValueError, TypeError):
        data = None
    if isinstance(data, list):
        data = {"header": {}, "blocks": data}
    elif not isinstance(data, dict):
        data = {"header": {}, "blocks": []}
    header = data.get("header") or {}
    header.setdefault("souban", "")
    header.setdefault("shijisha", "")
    header.setdefault("roster", [])
    blocks = [b for b in (data.get("blocks") or []) if isinstance(b, dict)]
    # 旧ブロック（staff[]＋vehicle）を assignments[{name,vehicle}] に変換
    for b in blocks:
        if "assignments" not in b:
            veh = b.get("vehicle") or ""
            b["assignments"] = [{"name": n, "vehicle": (veh if i == 0 else "")}
                                for i, n in enumerate(b.get("staff") or [])]
    return {"header": header, "blocks": blocks}


def read_shijisho(target_date):
    """指定日の保存済み {header, blocks} を返す（なければ空）。"""
    empty = {"header": {"souban": "", "shijisha": "", "roster": []}, "blocks": []}
    if not TOKEN or not SHIJISHO_DB:
        return empty
    with httpx.Client(timeout=30) as client:
        _pid, props = _shijisho_page(client, target_date)
        if not props:
            return empty
        return _normalize_shijisho(_pget(props, SHJ["blocks"]) or "")


def save_shijisho(target_date, header, blocks, editor=""):
    """指定日の指示書を保存（同日の既存ページをarchiveしてから作成＝1日1ページ）。"""
    if not (TOKEN and SHIJISHO_DB):
        raise RuntimeError("指示書DBが未設定です")
    payload = json.dumps({"header": header, "blocks": blocks}, ensure_ascii=False)
    with httpx.Client(timeout=60) as client:
        pid, _props = _shijisho_page(client, target_date)
        if pid:
            client.patch(f"{API}/pages/{pid}", headers=_headers(), json={"archived": True})
        props = {
            SHJ["title"]: _title(target_date),
            SHJ["date"]: _date(target_date),
            SHJ["blocks"]: _rich_chunks(payload),
            SHJ["editor"]: _text(editor),
        }
        r = client.post(f"{API}/pages", headers=_headers(),
                        json={"parent": {"database_id": SHIJISHO_DB}, "properties": props})
        if r.status_code != 200:
            raise RuntimeError(f"指示書の保存に失敗 {r.status_code}: {r.text[:300]}")
    return len(blocks)


@app.get("/shijisho", response_class=HTMLResponse)
def shijisho_builder(request: Request, date: str = "", msg: str = "", err: str = "",
                     _u: str = Depends(auth)):
    from datetime import datetime, timedelta
    if not (len(date) == 10 and date[4] == "-" and date[7] == "-"):
        # 基本は翌日ぶんを作るので既定＝翌日
        date = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    cards = day_tasks(date)
    saved = read_shijisho(date)
    header = saved["header"]
    blocks = saved["blocks"]
    m = load_masters()
    staff = staff_for_date(date)            # [{name,status,dept}] シフト由来＋設定担当
    vehicles = m["vehicles"] or VEHICLES
    roster_cols = roster_columns(date)      # 当日の出社者（部門別5カラム・常時全員）
    return templates.TemplateResponse("shijisho.html", {
        "request": request, "date": date, "cards": cards, "blocks": blocks, "header": header,
        "blocks_json": json.dumps(blocks, ensure_ascii=False),
        "header_json": json.dumps(header, ensure_ascii=False),
        "cards_json": json.dumps(cards, ensure_ascii=False),
        "roster_cols_json": json.dumps(roster_cols, ensure_ascii=False),
        "staff": staff, "vehicles": vehicles, "msg": msg, "err": err})


@app.post("/shijisho/save")
async def shijisho_save(request: Request, _u: str = Depends(auth), _c: None = Depends(check_csrf)):
    from urllib.parse import quote
    from fastapi.responses import RedirectResponse
    f = await request.form()
    date = f.get("date") or ""
    try:
        blocks = json.loads(f.get("blocks") or "[]")
        header = json.loads(f.get("header") or "{}")
        n = save_shijisho(date, header, blocks, editor=_u)
        return RedirectResponse(
            f"/shijisho?date={quote(date)}&msg={quote(f'{n}件で保存しました')}", status_code=303)
    except Exception as e:  # noqa: BLE001
        return RedirectResponse(f"/shijisho?date={quote(date)}&err={quote('保存に失敗：'+str(e))}",
                                status_code=303)


@app.get("/shijisho/print", response_class=HTMLResponse)
def shijisho_print(request: Request, date: str = "", _u: str = Depends(auth)):
    from datetime import datetime, timedelta
    if not (len(date) == 10 and date[4] == "-"):
        date = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    saved = read_shijisho(date)
    return templates.TemplateResponse("shijisho_print.html", {
        "request": request, "date": date, "blocks": saved["blocks"], "header": saved["header"],
        "company_name": COMPANY})


# ───────────────────────── シフト取込（責任者がxlsxをアップロード） ─────────────────────────
@app.get("/shifts", response_class=HTMLResponse)
def shifts_page(request: Request, msg: str = "", err: str = "", _u: str = Depends(auth)):
    return templates.TemplateResponse("shifts.html", {
        "request": request, "pages": _shift_pages(), "msg": msg, "err": err})


@app.post("/shifts/upload")
async def shifts_upload(request: Request, file: UploadFile = File(...),
                        _u: str = Depends(auth), _c: None = Depends(check_csrf)):
    from urllib.parse import quote
    from fastapi.responses import RedirectResponse
    try:
        raw = await file.read()
        if not raw:
            raise RuntimeError("ファイルが空です")
        if len(raw) > 5 * 1024 * 1024:
            raise RuntimeError("ファイルが大きすぎます（5MBまで）")
        if not (file.filename or "").lower().endswith(".xlsx"):
            raise RuntimeError("xlsx形式のシフト表を選んでください")
        if raw[:2] != b"PK":      # xlsx は zip。最低限のマジックバイト確認
            raise RuntimeError("Excelファイルとして読み取れません")
        parsed = parse_shift_xlsx(raw)
        n = save_shift(parsed, editor=_u)
        msg = f"{parsed['start']}〜{parsed['end']} を取込みました（{n}日分）"
        return RedirectResponse(f"/shifts?msg={quote(msg)}", status_code=303)
    except Exception as e:  # noqa: BLE001
        return RedirectResponse(f"/shifts?err={quote('取込に失敗：'+str(e))}", status_code=303)


@app.post("/shifts/delete")
async def shifts_delete(request: Request, _u: str = Depends(auth), _c: None = Depends(check_csrf)):
    from urllib.parse import quote
    from fastapi.responses import RedirectResponse
    f = await request.form()
    pid = f.get("page_id") or ""
    try:
        with httpx.Client(timeout=30) as client:
            r = client.patch(f"{API}/pages/{pid}", headers=_headers(), json={"archived": True})
            if r.status_code != 200:
                raise RuntimeError(f"削除に失敗 {r.status_code}")
        _SHIFT_CACHE.clear()
        return RedirectResponse(f"/shifts?msg={quote('削除しました')}", status_code=303)
    except Exception as e:  # noqa: BLE001
        return RedirectResponse(f"/shifts?err={quote(str(e))}", status_code=303)


# ───────────────────────── 設定（マスタ：担当・車・式場・得意先） ─────────────────────────
MASTER_TYPES = ["担当", "車両", "式場", "得意先"]


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, msg: str = "", err: str = "", _u: str = Depends(auth)):
    rows = _masters_rows()
    grouped = {t: [] for t in MASTER_TYPES}
    for r in rows:
        grouped.setdefault(r["type"] or "", []).append(r)
    for t in grouped:
        grouped[t].sort(key=lambda r: ((r["dept"] or ""), (r["name"] or "")))
    return templates.TemplateResponse("settings.html", {
        "request": request, "grouped": grouped, "types": MASTER_TYPES,
        "departments": list(SOURCES.keys()), "msg": msg, "err": err})


@app.post("/settings/add")
async def settings_add(request: Request, _u: str = Depends(auth), _c: None = Depends(check_csrf)):
    from urllib.parse import quote
    from fastapi.responses import RedirectResponse
    f = await request.form()
    typ = (f.get("type") or "").strip()
    name = (f.get("name") or "").strip()
    dept = (f.get("department") or "").strip()
    try:
        if not (typ in MASTER_TYPES and name):
            raise RuntimeError("種別と名前を入力してください")
        if not (TOKEN and MASTERS_DB):
            raise RuntimeError("設定DBが未設定です")
        props = {MS["title"]: _title(name), MS["type"]: _select(typ)}
        if typ == "得意先" and dept:
            props[MS["dept"]] = _select(dept)
        with httpx.Client(timeout=30) as client:
            r = client.post(f"{API}/pages", headers=_headers(),
                            json={"parent": {"database_id": MASTERS_DB}, "properties": props})
            if r.status_code != 200:
                raise RuntimeError(f"追加に失敗 {r.status_code}: {r.text[:200]}")
        _MASTERS_CACHE.clear()
        return RedirectResponse(f"/settings?msg={quote(f'{typ}「{name}」を追加しました')}", status_code=303)
    except Exception as e:  # noqa: BLE001
        return RedirectResponse(f"/settings?err={quote(str(e))}", status_code=303)


@app.post("/settings/delete")
async def settings_delete(request: Request, _u: str = Depends(auth), _c: None = Depends(check_csrf)):
    from urllib.parse import quote
    from fastapi.responses import RedirectResponse
    f = await request.form()
    pid = f.get("page_id") or ""
    try:
        with httpx.Client(timeout=30) as client:
            r = client.patch(f"{API}/pages/{pid}", headers=_headers(), json={"archived": True})
            if r.status_code != 200:
                raise RuntimeError(f"削除に失敗 {r.status_code}")
        _MASTERS_CACHE.clear()
        return RedirectResponse(f"/settings?msg={quote('削除しました')}", status_code=303)
    except Exception as e:  # noqa: BLE001
        return RedirectResponse(f"/settings?err={quote(str(e))}", status_code=303)


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
