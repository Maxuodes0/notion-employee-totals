# project_profit.py — ربحية المشاريع = (قيمة بدون ضريبة) - (مجموع التكاليف)

import os, re, time, requests
from collections import defaultdict

# ========= ضع معرّف قاعدة Projects (بشرطات أو بدون) =========
RAW_PROJECTS_DB_ID = "23e6fe2a5e8e8003a6bfcf99ae01ba0c"
# ==========================================================

VALUE_DB_NAME = "قيمة المشروع"
COSTS_DB_NAME = "تكاليف المشروع"

OUT_DB_ID_ENV  = os.getenv("PROFIT_DB_ID")
OUT_PARENT_ENV = os.getenv("PROFIT_PARENT_PAGE_ID") or os.getenv("EMP_TOTALS_PARENT_PAGE_ID")
OUT_DB_TITLE   = "ربحية المشاريع"

OUT_TITLE_PROP  = "المشروع"
OUT_NET_PROP    = "قيمة بدون ضريبة (SAR)"
OUT_COST_PROP   = "مجموع التكاليف (SAR)"
OUT_PROFIT_PROP = "الربح (SAR)"
OUT_MARGIN_PROP = "الهامش %"
OUT_REL_PROJECT = "رابط المشروع"

DEFAULT_TIMEOUT = 30
SLEEP = 0.15
MAX_PROJECTS = None

NET_REV_KEYS    = ["بدون ضريبة","غير شامل","قبل الضريبة","ex vat","pre vat","net"]
GROSS_REV_KEYS  = ["قيمة المشروع مع الضريبة","شامل الضريبة","after tax","gross"]
COST_TOTAL_KEYS = ["مجموع التكاليف","إجمالي التكاليف","total cost","sum cost"]

VAT_RATE = float(os.getenv("VAT_RATE", "0.15"))

API_KEY = os.getenv("NOTION_API_KEY") or os.getenv("NOTION_TOKEN")
assert API_KEY, "🚫 أضف NOTION_API_KEY أو NOTION_TOKEN في Secrets."

NOTION_VERSION = "2022-06-28"
print(f"🔧 Using Notion API version: {NOTION_VERSION}")

HDRS = {
    "Authorization": f"Bearer {API_KEY}",
    "Notion-Version": NOTION_VERSION,
    "Content-Type": "application/json",
}

# ===== تحسينات تشغيلية فقط (لا تغيّر النتائج) =====
SESSION = requests.Session()
SESSION.headers.update(HDRS)

def _backoff(attempt):
    time.sleep(min(2.0, 0.25 * (2 ** attempt)))

def _request(method, url, **kwargs):
    last = None
    for attempt in range(4):
        try:
            r = SESSION.request(method, url, timeout=DEFAULT_TIMEOUT, **kwargs)
            if r.status_code in (429, 500, 502, 503, 504):
                _backoff(attempt)
                continue
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            last = e
            _backoff(attempt)
    raise last

def http_get(url, params=None):
    return _request("GET", url, params=params)

def http_post(url, json=None):
    return _request("POST", url, json=json or {})

def http_patch(url, json=None):
    return _request("PATCH", url, json=json or {})

# =================================================

def hyphenate(nid: str) -> str:
    nid = nid.strip()
    if "-" in nid: return nid
    if re.fullmatch(r"[0-9a-fA-F]{32}", nid):
        return f"{nid[:8]}-{nid[8:12]}-{nid[12:16]}-{nid[16:20]}-{nid[20:]}"
    return nid

PROJECTS_DB_ID = hyphenate(RAW_PROJECTS_DB_ID)

def retrieve_database(db_id):
    return http_get(f"https://api.notion.com/v1/databases/{db_id}").json()

def retrieve_page(page_id):
    return http_get(f"https://api.notion.com/v1/pages/{page_id}").json()

def query_database_pages(db_id, page_size=100, limit=None):
    url = f"https://api.notion.com/v1/databases/{db_id}/query"
    res, cursor = [], None
    while True:
        body = {"page_size": min(int(page_size), 100)}
        if cursor:
            body["start_cursor"] = cursor
        data = http_post(url, body).json()
        res.extend(data.get("results", []))
        if limit and len(res) >= limit:
            return res[:limit]
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
        time.sleep(SLEEP)
    return res

def list_block_children_all(block_id):
    url = f"https://api.notion.com/v1/blocks/{block_id}/children"
    res, cursor = [], None
    while True:
        params = {}
        if cursor:
            params["start_cursor"] = cursor
        data = http_get(url, params=params).json()
        res.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
        time.sleep(SLEEP)
    return res

def title_from_page(page):
    for v in (page.get("properties") or {}).values():
        if v.get("type") == "title" and v.get("title"):
            return v["title"][0]["plain_text"]
    return "(بدون عنوان)"

def get_child_database_in_page_by_title(page_id, title):
    for blk in list_block_children_all(page_id):
        if blk.get("type") == "child_database":
            if (blk.get("child_database") or {}).get("title","").strip() == title.strip():
                return blk.get("id")
    return None

def detect_number_prop_by_keywords(db_id, keys):
    db = retrieve_database(db_id)
    for name, meta in (db.get("properties") or {}).items():
        if meta.get("type") in ("number","formula","rollup"):
            if any(k.lower() in name.lower() for k in keys):
                return name
    return None

def extract_number_cell(prop):
    t = prop.get("type")
    if t == "number":
        return float(prop.get("number") or 0)
    if t == "formula":
        return float((prop.get("formula") or {}).get("number") or 0)
    if t == "rollup":
        r = prop.get("rollup") or {}
        if r.get("type") == "number":
            return float(r.get("number") or 0)
        if r.get("type") == "array":
            return sum(
                float((it.get("number") or (it.get("formula") or {}).get("number") or 0))
                for it in r.get("array") or []
            )
    return 0.0

def sum_numeric_column(db_id, col):
    total = 0.0
    for row in query_database_pages(db_id):
        total += extract_number_cell((row.get("properties") or {}).get(col, {}))
    return round(total, 2)

# ===== إخراج النتائج =====
def create_output_db_under_page(parent_page_id):
    payload = {
        "parent": {"type": "page_id", "page_id": parent_page_id},
        "title": [{"type":"text","text":{"content": OUT_DB_TITLE}}],
        "properties": {
            OUT_TITLE_PROP:  {"title": {}},
            OUT_NET_PROP:    {"number": {}},
            OUT_COST_PROP:   {"number": {}},
            OUT_PROFIT_PROP: {"number": {}},
            OUT_MARGIN_PROP: {"number": {}},
            OUT_REL_PROJECT: {"relation": {"database_id": PROJECTS_DB_ID}},
        }
    }
    return http_post("https://api.notion.com/v1/databases", payload).json()["id"]

def ensure_output_db():
    if OUT_DB_ID_ENV:
        return hyphenate(OUT_DB_ID_ENV)
    assert OUT_PARENT_ENV, "🚫 PROFIT_PARENT_PAGE_ID غير موجود"
    parent = hyphenate(OUT_PARENT_ENV)
    found = get_child_database_in_page_by_title(parent, OUT_DB_TITLE)
    return found or create_output_db_under_page(parent)

def get_existing_rows_map(db_id):
    m = {}
    for p in query_database_pages(db_id):
        m[title_from_page(p)] = p["id"]
    return m

def create_row(db_id, rec):
    http_post("https://api.notion.com/v1/pages", {
        "parent": {"database_id": db_id},
        "properties": {
            OUT_TITLE_PROP:  {"title":[{"text":{"content": rec["name"]}}]},
            OUT_NET_PROP:    {"number": rec["net"]},
            OUT_COST_PROP:   {"number": rec["cost"]},
            OUT_PROFIT_PROP: {"number": rec["profit"]},
            OUT_MARGIN_PROP: {"number": rec["margin"]},
            OUT_REL_PROJECT: {"relation":[{"id": rec["pid"]}]},
        }
    })

def update_row(pid, rec):
    http_patch(f"https://api.notion.com/v1/pages/{pid}", {
        "properties": {
            OUT_NET_PROP:    {"number": rec["net"]},
            OUT_COST_PROP:   {"number": rec["cost"]},
            OUT_PROFIT_PROP: {"number": rec["profit"]},
            OUT_MARGIN_PROP: {"number": rec["margin"]},
            OUT_REL_PROJECT: {"relation":[{"id": rec["pid"]}]},
        }
    })

def aggregate_and_write():
    projects = query_database_pages(PROJECTS_DB_ID, limit=MAX_PROJECTS)
    records = []

    for page in projects:
        pid = page["id"]
        name = title_from_page(page)
        print(f"\n{name}")

        vdb = get_child_database_in_page_by_title(pid, VALUE_DB_NAME)
        cdb = get_child_database_in_page_by_title(pid, COSTS_DB_NAME)

        net = cost = 0.0
        if vdb:
            net_col = detect_number_prop_by_keywords(vdb, NET_REV_KEYS)
            gross_col = detect_number_prop_by_keywords(vdb, GROSS_REV_KEYS)
            if net_col:
                net = sum_numeric_column(vdb, net_col)
            elif gross_col:
                net = round(sum_numeric_column(vdb, gross_col) / (1 + VAT_RATE), 2)

        if cdb:
            cost_col = detect_number_prop_by_keywords(cdb, COST_TOTAL_KEYS)
            if cost_col:
                cost = sum_numeric_column(cdb, cost_col)

        profit = round(net - cost, 2)
        margin = round((profit / net * 100), 2) if net else 0.0

        records.append({
            "name": name, "pid": pid,
            "net": net, "cost": cost,
            "profit": profit, "margin": margin
        })

    out_db = ensure_output_db()
    existing = get_existing_rows_map(out_db)

    for r in records:
        if r["name"] in existing:
            update_row(existing[r["name"]], r)
        else:
            create_row(out_db, r)
        time.sleep(SLEEP)

    print("🎯 اكتمل تحديث ربحية المشاريع")

if __name__ == "__main__":
    aggregate_and_write()
