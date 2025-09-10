# project_profit.py — ربح كل مشروع = (قيمة بدون ضريبة) - (مجموع التكاليف)
# يقرأ من قواعد فرعية داخل كل مشروع:
#   1) "قيمة المشروع": عمود "بدون ضريبة" (أو يحوّل من شامل إلى غير شامل بقسمة على 1+VAT_RATE)
#   2) "تكاليف المشروع": عمود "مجموع التكاليف"
# ثم يكتب في قاعدة "ربحية المشاريع" (Upsert بربط Relation بالمشروع)

from __future__ import annotations
import os, re, time, requests
from typing import Optional, List, Dict

# ===== إعدادات عامة =====
PROJECTS_DB_ID = os.getenv("PROJECTS_DB_ID", "23e6fe2a5e8e8003a6bfcf99ae01ba0c")  # غيّرها أو مرّرها من Actions
VALUE_DB_NAME  = "قيمة المشروع"
COSTS_DB_NAME  = "تكاليف المشروع"

# إعدادات إخراج النتائج
PROFIT_DB_TITLE        = "ربحية المشاريع"
PROFIT_DB_ID_ENV       = os.getenv("PROFIT_DB_ID")          # لو موجود يكتب مباشرة فيها
PROFIT_PARENT_PAGE_ENV = os.getenv("PROFIT_PARENT_PAGE_ID") # وإلا ينشئ/يستخدم قاعدة تحت هذه الصفحة

# ضريبة القيمة المضافة (لو اضطررنا نحول من "شامل" إلى "غير شامل")
VAT_RATE = float(os.getenv("VAT_RATE", "0.15"))

# ===== Notion =====
API_KEY = os.getenv("NOTION_API_KEY") or os.getenv("NOTION_TOKEN")
assert API_KEY, "🚫 ضف NOTION_API_KEY أو NOTION_TOKEN."
NOTION_VERSION = os.getenv("NOTION_VERSION", "2025-09-03")

HDRS = {
    "Authorization": f"Bearer {API_KEY}",
    "Notion-Version": NOTION_VERSION,
    "Content-Type": "application/json",
}

DEFAULT_TIMEOUT = 30
SLEEP = 0.12

# مفاتيح البحث عن الأعمدة
NET_REV_KEYS    = ["بدون ضريبة", "قبل الضريبة", "ex vat", "pre vat", "غير شامل", "net"]
GROSS_REV_KEYS  = ["شامل", "بعد الضريبة", "with vat", "incl vat", "gross"]
COST_TOTAL_KEYS = ["مجموع التكاليف", "إجمالي التكاليف", "total cost", "overall cost"]

# ===== HTTP helpers مع Retries =====
def _req(method: str, url: str, **kwargs):
    for i in range(6):
        try:
            r = requests.request(method, url, headers=HDRS, timeout=DEFAULT_TIMEOUT, **kwargs)
            if r.status_code == 429:
                time.sleep(int(r.headers.get("Retry-After", "1"))); continue
            r.raise_for_status()
            return r
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code in (502, 503, 504):
                time.sleep(0.5 * (2 ** i)); continue
            raise
    raise RuntimeError("HTTP retries exceeded")

def GET(url, params=None, **k):   return _req("GET", url, params=params, **k)
def POST(url, j=None, **k):       return _req("POST", url, json=j or {}, **k)
def PATCH(url, j=None, **k):      return _req("PATCH", url, json=j or {}, **k)

# ===== Notion helpers =====
def hyphenate(nid: str) -> str:
    nid = (nid or "").strip()
    if "-" in nid: return nid
    if re.fullmatch(r"[0-9a-fA-F]{32}", nid):
        return f"{nid[0:8]}-{nid[8:12]}-{nid[12:16]}-{nid[16:20]}-{nid[20:32]}"
    return nid

PROJECTS_DB_ID = hyphenate(PROJECTS_DB_ID)

def retrieve_db(db_id: str) -> dict:
    return GET(f"https://api.notion.com/v1/databases/{db_id}").json()

def query_db(db_id: str, page_size=100) -> List[dict]:
    url = f"https://api.notion.com/v1/databases/{db_id}/query"
    out, cur = [], None
    while True:
        body = {"page_size": page_size}
        if cur: body["start_cursor"] = cur
        data = POST(url, body).json()
        out.extend(data.get("results", []))
        if not data.get("has_more"): break
        cur = data.get("next_cursor"); time.sleep(SLEEP)
    return out

def list_children(block_id: str) -> List[dict]:
    url = f"https://api.notion.com/v1/blocks/{block_id}/children"
    out, cur = [], None
    while True:
        params = {}
        if cur: params["start_cursor"] = cur
        data = GET(url, params=params).json()
        out.extend(data.get("results", []))
        if not data.get("has_more"): break
        cur = data.get("next_cursor"); time.sleep(SLEEP)
    return out

def title_of(page: dict) -> str:
    for v in (page.get("properties") or {}).values():
        if v.get("type") == "title" and v.get("title"):
            return v["title"][0].get("plain_text", "")
    return "(بدون عنوان)"

def find_child_db(page_id: str, wanted_title: str) -> Optional[str]:
    for b in list_children(page_id):
        if b.get("type") == "child_database":
            t = (b.get("child_database") or {}).get("title", "")
            if t.strip() == wanted_title.strip():
                return b.get("id")
    return None

def find_named_number_prop(db_id: str, name_keys: List[str]) -> Optional[str]:
    """يرجع أول عمود رقمي/صيغة/رول-أب اسمه يحتوي أي كلمة من name_keys."""
    db = retrieve_db(db_id); props = db.get("properties", {}) or {}
    for n, meta in props.items():
        if meta.get("type") in ("number", "formula", "rollup"):
            nn = (n or "").lower()
            if any(k.lower() in nn for k in name_keys):
                return n
    return None

def sum_numeric_column(db_id: str, col: str) -> float:
    if not col: return 0.0
    total = 0.0
    for row in query_db(db_id):
        cell = (row.get("properties") or {}).get(col, {})
        t = cell.get("type")
        if t == "number":
            total += float(cell.get("number") or 0)
        elif t == "formula":
            total += float((cell.get("formula") or {}).get("number") or 0)
        elif t == "rollup":
            r = cell.get("rollup") or {}
            if r.get("type") == "number":
                total += float(r.get("number") or 0)
    return round(total, 2)

# ===== إنشاء/كتابة قاعدة "ربحية المشاريع" =====
OUT_TITLE_PROP   = "المشروع"
OUT_REL_PROJECT  = "رابط المشروع"
OUT_NET_REV_PROP = "قيمة بدون ضريبة (SAR)"
OUT_COST_PROP    = "مجموع التكاليف (SAR)"
OUT_PROFIT_PROP  = "الربح (SAR)"
OUT_MARGIN_PROP  = "الهامش %"

def create_profit_db(parent_page_id: str) -> str:
    payload = {
        "parent": {"type": "page_id", "page_id": parent_page_id},
        "title": [{"type": "text", "text": {"content": PROFIT_DB_TITLE}}],
        "properties": {
            OUT_TITLE_PROP:   {"title": {}},
            OUT_REL_PROJECT:  {"relation": {"database_id": PROJECTS_DB_ID}},
            OUT_NET_REV_PROP: {"number": {}},
            OUT_COST_PROP:    {"number": {}},
            OUT_PROFIT_PROP:  {"number": {}},
            OUT_MARGIN_PROP:  {"number": {}},
        }
    }
    data = POST("https://api.notion.com/v1/databases", payload).json()
    print(f"🆕 أنشأنا قاعدة الربحية: {data.get('id')}")
    return data.get("id")

def get_or_create_profit_db() -> str:
    if PROFIT_DB_ID_ENV:
        return hyphenate(PROFIT_DB_ID_ENV)
    assert PROFIT_PARENT_PAGE_ENV, "💡 وفّر PROFIT_DB_ID أو PROFIT_PARENT_PAGE_ID."
    parent = hyphenate(PROFIT_PARENT_PAGE_ENV)
    for b in list_children(parent):
        if b.get("type") == "child_database":
            t = (b.get("child_database") or {}).get("title", "")
            if t.strip() == PROFIT_DB_TITLE:
                return b.get("id")
    return create_profit_db(parent)

def get_existing_by_project_relation(db_id: str) -> Dict[str, dict]:
    """خريطة project_id -> page_id للـ upsert."""
    m = {}
    for p in query_db(db_id):
        rel = (p.get("properties", {}).get(OUT_REL_PROJECT, {}).get("relation", []))
        if rel:
            m[rel[0].get("id")] = {"id": p["id"]}
    return m

def build_props(rec: dict) -> dict:
    return {
        OUT_TITLE_PROP:   {"title": [{"type": "text", "text": {"content": rec["project_name"]}}]},
        OUT_REL_PROJECT:  {"relation": [{"id": rec["project_id"]}]},
        OUT_NET_REV_PROP: {"number": rec["net_revenue"]},
        OUT_COST_PROP:    {"number": rec["total_cost"]},
        OUT_PROFIT_PROP:  {"number": rec["profit"]},
        OUT_MARGIN_PROP:  {"number": rec["margin_pct"]},
    }

def upsert_rows(db_id: str, recs: List[dict]):
    existing = get_existing_by_project_relation(db_id)
    creates = updates = 0
    for r in recs:
        payload = {"properties": build_props(r)}
        ex = existing.get(r["project_id"])
        if ex:
            PATCH(f"https://api.notion.com/v1/pages/{ex['id']}", payload); updates += 1
        else:
            payload["parent"] = {"database_id": db_id}
            POST("https://api.notion.com/v1/pages", payload); creates += 1
        time.sleep(SLEEP)
    print(f"✅ Profit upsert: {creates} إنشاء | {updates} تحديث")

# ===== المنطق الرئيسي =====
def compute_project_numbers(pid: str, name: str) -> dict:
    value_db = find_child_db(pid, VALUE_DB_NAME)
    costs_db = find_child_db(pid, COSTS_DB_NAME)

    net_revenue = 0.0
    total_cost  = 0.0

    # 1) قيمة بدون ضريبة
    if value_db:
        net_col   = find_named_number_prop(value_db, NET_REV_KEYS)
        gross_col = find_named_number_prop(value_db, GROSS_REV_KEYS)
        if net_col:
            net_revenue = sum_numeric_column(value_db, net_col)
        elif gross_col:  # تحويل من شامل إلى غير شامل
            gross = sum_numeric_column(value_db, gross_col)
            net_revenue = round(gross / (1.0 + VAT_RATE), 2)

    # 2) مجموع التكاليف
    if costs_db:
        total_col = find_named_number_prop(costs_db, COST_TOTAL_KEYS)
        if total_col:
            total_cost = sum_numeric_column(costs_db, total_col)

    profit = round(net_revenue - total_cost, 2)
    margin = round((profit / net_revenue * 100.0), 2) if net_revenue > 0 else 0.0

    print(f"• {name}: Net {net_revenue:,.2f} - Cost {total_cost:,.2f} = Profit {profit:,.2f} ({margin:.2f}%)")
    return {
        "project_id": pid,
        "project_name": name,
        "net_revenue": net_revenue,
        "total_cost": total_cost,
        "profit": profit,
        "margin_pct": margin,
    }

def main():
    print("⏳ حساب الربحية من (قيمة بدون ضريبة) و(مجموع التكاليف)…")
    out_db = get_or_create_profit_db()

    recs: List[dict] = []
    for page in query_db(PROJECTS_DB_ID):
        pid = page["id"]; name = title_of(page)
        recs.append(compute_project_numbers(pid, name))
        time.sleep(SLEEP)

    upsert_rows(out_db, recs)
    try:
        url = retrieve_db(out_db).get("url"); print(f"📄 افتح قاعدة الربحية: {url}")
    except Exception:
        pass
    print("🎯 اكتمل.")

if __name__ == "__main__":
    try:
        main()
    except requests.HTTPError as e:
        print("❌ HTTPError:", e)
        try:
            print("↪️", e.response.status_code, e.response.text)
        except Exception:
            pass
    except Exception as e:
        print("❌ Unexpected:", e)
