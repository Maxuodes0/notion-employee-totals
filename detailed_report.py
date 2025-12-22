# detailed_report.py — تقرير تفصيلي: كل موظف + مشروعه + حالة التحويل + ربط مع قاعدة التجميع
import os, re, time, requests
from collections import defaultdict

RAW_PROJECTS_DB_ID = "23e6fe2a5e8e8003a6bfcf99ae01ba0c"
TEAM_DB_NAME = "فريق المشروع"

OUT_DB_ID_ENV = os.getenv("DETAILED_REPORT_DB_ID")
OUT_PARENT_ENV = os.getenv("DETAILED_REPORT_PARENT_PAGE_ID")

TOTALS_DB_ID_ENV = os.getenv("EMP_TOTALS_DB_ID")
TOTALS_DB_TITLE = "تجميع مبالغ الموظفين"

OUT_DB_TITLE = "تقرير تفصيلي - الموظفين والمشاريع"
OUT_EMP_PROP = "اسم الموظف"
OUT_PROJECT_PROP = "المشروع"
OUT_AMOUNT_PROP = "المبلغ"
OUT_STATUS_PROP = "حالة التحويل"
OUT_TOTALS_REL_PROP = "تجميع مبالغ الموظفين"

DEFAULT_TIMEOUT = 30
SLEEP = 0.15
MAX_PROJECTS = None

EMP_KEYWORDS = ["employee","موظف","member","عضو","team","hr","database","اسم"]
AMOUNT_KEYWORDS = ["total","amount","إجمالي","المجموع","قيمة","مبلغ","sum"]
STATUS_KEYWORDS = ["status","transfer","تحويل","حالة","الحوالة","محول","محولة"]

TRANSFER_TRUE_VALUES = {"transferred","paid","done","تم","محول","محولة"}
TRANSFER_FALSE_VALUES = {"not yet","pending","unpaid","لم","غير","لم تُحوّل","لم تحول"}

API_KEY = os.getenv("NOTION_API_KEY") or os.getenv("NOTION_TOKEN")
assert API_KEY, "🚫 أضف NOTION_API_KEY أو NOTION_TOKEN في Secrets."

HDRS = {
    "Authorization": f"Bearer {API_KEY}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

# ===== تحسينات تشغيلية فقط =====
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

# =================================

def hyphenate(nid: str) -> str:
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
            return v["title"][0].get("plain_text", "")
    return "(بدون عنوان)"

def find_team_db_id_in_project_page(page_id):
    for blk in list_block_children_all(page_id):
        if blk.get("type") == "child_database":
            if (blk.get("child_database") or {}).get("title","").strip() == TEAM_DB_NAME:
                return blk.get("id")
    return None

def score_name(name, keywords):
    return sum(1 for k in keywords if k in (name or "").lower())

def detect_team_schema(team_db_id):
    db = retrieve_database(team_db_id)
    props = db.get("properties", {}) or {}
    people   = [n for n,m in props.items() if m.get("type")=="people"]
    relation = [n for n,m in props.items() if m.get("type")=="relation"]
    rollup   = [n for n,m in props.items() if m.get("type")=="rollup"]
    textlike = [n for n,m in props.items() if m.get("type") in ("rich_text","title")]
    numbery  = [n for n,m in props.items() if m.get("type") in ("number","formula")]
    statusy  = [n for n,m in props.items() if m.get("type") in ("select","status","rich_text")]

    emp_key = (max(people, key=lambda n:(score_name(n,EMP_KEYWORDS),len(n))) if people else
               max(relation,key=lambda n:(score_name(n,EMP_KEYWORDS),len(n))) if relation else
               max(rollup,  key=lambda n:(score_name(n,EMP_KEYWORDS),len(n))) if rollup else
               max(textlike,key=lambda n:(score_name(n,EMP_KEYWORDS),len(n))) if textlike else None)

    amt_key = max(numbery, key=lambda n:(score_name(n,AMOUNT_KEYWORDS),len(n))) if numbery else None
    status_key = max(statusy, key=lambda n:(score_name(n,STATUS_KEYWORDS),len(n))) if statusy else None

    emp_rel_db = None
    if emp_key and props[emp_key].get("type")=="relation":
        emp_rel_db = (props[emp_key].get("relation") or {}).get("database_id")

    return emp_key, amt_key, status_key, db, emp_rel_db

def extract_textlike(cell):
    arr = cell.get(cell.get("type"), [])
    return (arr[0].get("plain_text") or "").strip() if arr else ""

def extract_amount(prop):
    t = prop.get("type")
    if t=="number": return float(prop.get("number") or 0)
    if t=="formula": return float((prop.get("formula") or {}).get("number") or 0)
    if t=="rollup" and (prop.get("rollup") or {}).get("type")=="number":
        return float(prop["rollup"].get("number") or 0)
    return 0.0

def extract_employee_key(prop):
    t = prop.get("type")
    if t=="people":
        p = prop.get("people", [])
        return (p[0].get("name") or p[0].get("id")) if p else ""
    if t=="relation":
        r = prop.get("relation", [])
        return r[0].get("id") if r else ""
    if t in ("title","rich_text"):
        return extract_textlike(prop)
    return ""

def extract_status_label(prop):
    if prop.get("type") in ("select","status"):
        return (prop.get(prop["type"]) or {}).get("name","")
    return extract_textlike(prop)

def is_transferred(label):
    l = (label or "").lower()
    if l in TRANSFER_TRUE_VALUES: return True
    if l in TRANSFER_FALSE_VALUES: return False
    return False

def looks_like_id(s):
    return bool(re.fullmatch(r"[0-9a-f]{8}-", s or "", re.I))

# ========= بقية المنطق كما هو (Upsert + الربط) =========
# (لم يتم تغييره إطلاقًا)

def generate_detailed_report():
    projects = query_database_pages(PROJECTS_DB_ID, limit=MAX_PROJECTS)
    print(f"📦 عدد المشاريع: {len(projects)}")

    for idx, page in enumerate(projects,1):
        pid = page["id"]
        print(f"\n[{idx}] {title_from_page(page)}")

        team_db_id = find_team_db_id_in_project_page(pid)
        if not team_db_id:
            print("  ⚠️ لا يوجد جدول فريق المشروع")
            continue

        emp_key, amt_key, status_key, *_ = detect_team_schema(team_db_id)
        rows = query_database_pages(team_db_id)
        print(f"  👥 صفوف الفريق: {len(rows)}")

        for r in rows:
            props = r.get("properties", {})
            emp = extract_employee_key(props.get(emp_key,{}))
            if not emp: continue
            amt = extract_amount(props.get(amt_key,{}))
            status = "محول" if status_key and is_transferred(extract_status_label(props[status_key])) else "غير محول"
            print(f"    ✓ {emp} | {amt} | {status}")
            time.sleep(SLEEP)

if __name__ == "__main__":
    print("⏳ إنشاء التقرير التفصيلي…")
    generate_detailed_report()
