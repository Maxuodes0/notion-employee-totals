# detailed_report.py — تقرير تفصيلي: كل موظف + مشروعه + حالة التحويل
import os, re, time, requests
from collections import defaultdict

# ========= ضع معرّف قاعدة Projects (بشرطات أو بدون) =========
RAW_PROJECTS_DB_ID = "23e6fe2a5e8e8003a6bfcf99ae01ba0c"
# ==========================================================

TEAM_DB_NAME = "فريق المشروع"

# معلومات القاعدة الجديدة
OUT_DB_ID_ENV = os.getenv("DETAILED_REPORT_DB_ID")
OUT_PARENT_ENV = os.getenv("DETAILED_REPORT_PARENT_PAGE_ID")

OUT_DB_TITLE = "تقرير تفصيلي - الموظفين والمشاريع"
OUT_EMP_PROP = "اسم الموظف"
OUT_PROJECT_PROP = "المشروع"
OUT_AMOUNT_PROP = "المبلغ"
OUT_STATUS_PROP = "حالة التحويل"

DEFAULT_TIMEOUT = 30
SLEEP = 0.15
MAX_PROJECTS = None
RESOLVE_NAMES = True

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

def http_get(url, params=None):
    r = requests.get(url, headers=HDRS, params=params, timeout=DEFAULT_TIMEOUT)
    r.raise_for_status()
    return r

def http_post(url, json=None):
    r = requests.post(url, headers=HDRS, json=json or {}, timeout=DEFAULT_TIMEOUT)
    r.raise_for_status()
    return r

def http_patch(url, json=None):
    r = requests.patch(url, headers=HDRS, json=json or {}, timeout=DEFAULT_TIMEOUT)
    r.raise_for_status()
    return r

def hyphenate(nid: str) -> str:
    nid = nid.strip()
    if "-" in nid:
        return nid
    if re.fullmatch(r"[0-9a-fA-F]{32}", nid):
        return f"{nid[0:8]}-{nid[8:12]}-{nid[12:16]}-{nid[16:20]}-{nid[20:32]}"
    return nid

PROJECTS_DB_ID = hyphenate(RAW_PROJECTS_DB_ID)

def retrieve_database(db_id):
    return http_get(f"https://api.notion.com/v1/databases/{db_id}").json()

def query_database_pages(db_id, page_size=100, limit=None, filter_payload=None):
    url = f"https://api.notion.com/v1/databases/{db_id}/query"
    res, cursor = [], None
    while True:
        body = {"page_size": page_size}
        if cursor:
            body["start_cursor"] = cursor
        if filter_payload:
            body.update(filter_payload)
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
            t = (blk.get("child_database") or {}).get("title", "")
            if t.strip() == TEAM_DB_NAME.strip():
                return blk.get("id")
    return None

def score_name(name: str, keywords):
    n = (name or "").lower()
    return sum(1 for kw in keywords if kw in n)

def detect_team_schema(team_db_id):
    db = retrieve_database(team_db_id)
    props = db.get("properties", {}) or {}
    people = [n for n, m in props.items() if m.get("type") == "people"]
    relation = [n for n, m in props.items() if m.get("type") == "relation"]
    rollup = [n for n, m in props.items() if m.get("type") == "rollup"]
    textlike = [n for n, m in props.items() if m.get("type") in ("rich_text","title")]
    numbery = [n for n, m in props.items() if m.get("type") in ("number","formula")]
    statusy = [n for n, m in props.items() if m.get("type") in ("select","status","rich_text")]

    emp_key = (max(people, key=lambda n:(score_name(n,EMP_KEYWORDS),len(n))) if people else
               max(relation,key=lambda n:(score_name(n,EMP_KEYWORDS),len(n))) if relation else
               max(rollup, key=lambda n:(score_name(n,EMP_KEYWORDS),len(n))) if rollup else
               max(textlike,key=lambda n:(score_name(n,EMP_KEYWORDS),len(n))) if textlike else None)
    
    amt_key = (max(numbery, key=lambda n:(score_name(n,AMOUNT_KEYWORDS),len(n))) if numbery else None)
    status_key = (max(statusy, key=lambda n:(score_name(n,STATUS_KEYWORDS),len(n))) if statusy else None)

    emp_relation_db_id = None
    if emp_key and props[emp_key].get("type") == "relation":
        rel_meta = props[emp_key].get("relation") or {}
        emp_relation_db_id = rel_meta.get("database_id")
    return emp_key, amt_key, status_key, db, emp_relation_db_id

def extract_textlike(cell):
    if cell.get("type") == "title":
        arr = cell.get("title", [])
        return (arr[0].get("plain_text") or "").strip() if arr else ""
    if cell.get("type") == "rich_text":
        arr = cell.get("rich_text", [])
        return (arr[0].get("plain_text") or "").strip() if arr else ""
    return ""

def extract_rollup_text(cell):
    if cell.get("type") != "rollup":
        return ""
    r = cell.get("rollup") or {}
    if r.get("type") == "array":
        arr = r.get("array") or []
        if not arr:
            return ""
        it = arr[0]
        if it.get("type") == "title":
            t = it.get("title") or []
            return (t[0].get("plain_text") or "").strip() if t else ""
        if it.get("type") == "rich_text":
            rt = it.get("rich_text") or []
            return (rt[0].get("plain_text") or "").strip() if rt else ""
    return ""

def extract_employee_key(prop):
    t = prop.get("type")
    if t == "people":
        ppl = prop.get("people", [])
        return (ppl[0].get("name") or ppl[0].get("id") or "").strip() if ppl else ""
    if t == "relation":
        rel = prop.get("relation", [])
        return rel[0].get("id") if rel else ""
    if t == "rollup":
        return extract_rollup_text(prop)
    if t in ("title","rich_text"):
        return extract_textlike(prop)
    return ""

def extract_status_label(prop):
    t = prop.get("type")
    if t == "select":
        return (prop.get("select") or {}).get("name","").strip()
    if t == "status":
        return (prop.get("status") or {}).get("name","").strip()
    if t in ("rich_text","title"):
        return extract_textlike(prop)
    return ""

def extract_amount(prop):
    """استخراج المبلغ من أنواع مختلفة من الخلايا"""
    t = prop.get("type")
    if t == "number":
        return float(prop.get("number") or 0)
    if t == "formula":
        return float((prop.get("formula") or {}).get("number") or 0)
    if t == "rollup":
        r = prop.get("rollup") or {}
        if r.get("type") == "number":
            return float(r.get("number") or 0)
    return 0.0

def is_transferred(label: str) -> bool:
    l = (label or "").strip().lower()
    if l in TRANSFER_TRUE_VALUES:
        return True
    if l in TRANSFER_FALSE_VALUES:
        return False
    return False

def looks_like_id(s: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", s, re.I))

def get_child_database_in_page_by_title(page_id: str, title: str):
    for blk in list_block_children_all(page_id):
        if blk.get("type") == "child_database":
            t = (blk.get("child_database") or {}).get("title","")
            if t.strip() == title.strip():
                return blk.get("id")
    return None

def create_output_db_under_page(parent_page_id: str) -> str:
    payload = {
        "parent": {"type": "page_id", "page_id": parent_page_id},
        "title": [{"type":"text","text":{"content": OUT_DB_TITLE}}],
        "properties": {
            OUT_EMP_PROP: {"rich_text": {}},
            OUT_PROJECT_PROP: {"relation": {
                "database_id": PROJECTS_DB_ID,
                "single_property": {}
            }},
            OUT_AMOUNT_PROP: {"number": {"format": "riyal"}},
            OUT_STATUS_PROP: {"select": {"options": [
                {"name": "محول", "color": "green"},
                {"name": "غير محول", "color": "red"}
            ]}}
        }
    }
    data = http_post("https://api.notion.com/v1/databases", payload).json()
    print(f"🆕 أنشأنا قاعدة التقرير التفصيلي: {data.get('id')}")
    return data.get("id")

def ensure_output_db() -> str:
    if OUT_DB_ID_ENV:
        print(f"🔗 استخدام قاعدة موجودة DETAILED_REPORT_DB_ID={OUT_DB_ID_ENV}")
        return hyphenate(OUT_DB_ID_ENV)
    
    assert OUT_PARENT_ENV, "🚫 وفّر DETAILED_REPORT_PARENT_PAGE_ID أو DETAILED_REPORT_DB_ID."
    parent_id = hyphenate(OUT_PARENT_ENV)
    existing = get_child_database_in_page_by_title(parent_id, OUT_DB_TITLE)
    if existing:
        print(f"🔎 وجدنا قاعدة التقرير داخل الصفحة: {existing}")
        return existing
    
    print("➕ إنشاء قاعدة التقرير التفصيلي…")
    return create_output_db_under_page(parent_id)

def ensure_output_columns(db_id: str):
    """التأكد من وجود الأعمدة المطلوبة (نادراً ما يُستخدم بعد الإنشاء الأول)"""
    try:
        db = retrieve_database(db_id)
        props = db.get("properties", {}) or {}
        patch = {"properties": {}}
        
        # لا نحاول إضافة relation بعد الإنشاء لتجنب مشاكل الصلاحيات
        # فقط نتأكد من الأعمدة الأساسية الأخرى
        if OUT_EMP_PROP not in props:
            patch["properties"][OUT_EMP_PROP] = {"rich_text": {}}
        if OUT_AMOUNT_PROP not in props:
            patch["properties"][OUT_AMOUNT_PROP] = {"number": {"format": "riyal"}}
        if OUT_STATUS_PROP not in props:
            patch["properties"][OUT_STATUS_PROP] = {"select": {"options": [
                {"name": "محول", "color": "green"},
                {"name": "غير محول", "color": "red"}
            ]}}
        
        if patch["properties"]:
            http_patch(f"https://api.notion.com/v1/databases/{db_id}", patch)
            print("🔧 تأكدنا من الأعمدة الأساسية.")
    except Exception as e:
        print(f"⚠️ تحذير: تعذر التحقق من الأعمدة: {e}")

def clear_existing_rows(db_id: str):
    """مسح جميع الصفوف الموجودة لتجنب التكرار"""
    print("🧹 مسح الصفوف القديمة...")
    pages = query_database_pages(db_id, page_size=100)
    for page in pages:
        try:
            # أرشفة الصفحة (مسحها)
            http_patch(f"https://api.notion.com/v1/pages/{page['id']}", {"archived": True})
            time.sleep(SLEEP)
        except Exception as e:
            print(f"⚠️ تعذر مسح صفحة: {e}")
    print(f"✅ تم مسح {len(pages)} صف")

def create_detail_row(db_id: str, emp_name: str, project_id: str, amount: float, status: str):
    """إنشاء صف جديد في التقرير التفصيلي"""
    payload = {
        "parent": {"database_id": db_id},
        "properties": {
            OUT_EMP_PROP: {"rich_text": [{"type":"text","text":{"content": emp_name}}]},
            OUT_PROJECT_PROP: {"relation": [{"id": project_id}]},
            OUT_AMOUNT_PROP: {"number": amount},
            OUT_STATUS_PROP: {"select": {"name": status}}
        }
    }
    http_post("https://api.notion.com/v1/pages", payload)

def generate_detailed_report():
    projects_db = retrieve_database(PROJECTS_DB_ID)
    print(f"✅ Projects DB: {projects_db.get('title',[{}])[0].get('plain_text','(No title)')}")
    
    projects = query_database_pages(PROJECTS_DB_ID, page_size=100, limit=MAX_PROJECTS)
    print(f"📦 عدد المشاريع: {len(projects)}")

    # جمع أسماء الموظفين
    employee_db_ids = set()
    name_map = {}
    
    # إعداد قاعدة النتائج
    out_db_id = ensure_output_db()
    clear_existing_rows(out_db_id)
    
    rows_created = 0

    for idx, page in enumerate(projects, 1):
        pid = page["id"]
        ptitle = title_from_page(page)
        print(f"\n[{idx}] {ptitle}")
        
        team_db_id = find_team_db_id_in_project_page(pid)
        if not team_db_id:
            print("  ⚠️ لا يوجد جدول 'فريق المشروع'")
            continue
        
        emp_key, amt_key, status_key, _schema, emp_rel_db = detect_team_schema(team_db_id)
        if not emp_key:
            print("  ⚠️ لم نحدد عمود الموظف")
            continue
        
        if emp_rel_db:
            employee_db_ids.add(hyphenate(emp_rel_db))
        
        team_rows = query_database_pages(team_db_id, page_size=100)
        print(f"  👥 صفوف الفريق: {len(team_rows)}")

        for r in team_rows:
            props = r.get("properties", {})
            emp_cell = props.get(emp_key)
            amt_cell = props.get(amt_key) if amt_key else None
            
            if not emp_cell:
                continue
            
            key = extract_employee_key(emp_cell)
            if not key:
                continue
            
            # استخراج المبلغ
            amount = 0.0
            if amt_cell:
                amount = extract_amount(amt_cell)
            
            # تحديد حالة التحويل
            status = "غير محول"  # القيمة الافتراضية
            if status_key and status_key in props:
                if is_transferred(extract_status_label(props[status_key])):
                    status = "محول"
            
            # حفظ معلومات قاعدة الموظفين للاستخدام لاحقاً
            if looks_like_id(key) and emp_rel_db:
                if key not in name_map:
                    name_map[key] = {"db": emp_rel_db}
            
            # استخدام الـ key مباشرة كاسم مؤقت
            emp_name = key
            
            # إنشاء الصف
            create_detail_row(out_db_id, emp_name, pid, amount, status)
            rows_created += 1
            time.sleep(SLEEP)
        
        time.sleep(SLEEP)

    # الآن نحل أسماء الموظفين
    if RESOLVE_NAMES and employee_db_ids:
        print("\n🔎 تحميل أسماء الموظفين...")
        for emp_db in list(employee_db_ids):
            pages = query_database_pages(emp_db, page_size=100)
            for pg in pages:
                name_map[pg["id"]] = title_from_page(pg)
            time.sleep(SLEEP)
        
        # تحديث أسماء الموظفين في التقرير
        print("📝 تحديث أسماء الموظفين...")
        report_pages = query_database_pages(out_db_id, page_size=100)
        for rp in report_pages:
            props = rp.get("properties", {})
            emp_text = props.get(OUT_EMP_PROP, {})
            if emp_text.get("rich_text"):
                current_name = emp_text["rich_text"][0].get("plain_text", "")
                if looks_like_id(current_name) and current_name in name_map:
                    new_name = name_map[current_name]
                    try:
                        http_patch(
                            f"https://api.notion.com/v1/pages/{rp['id']}",
                            {"properties": {OUT_EMP_PROP: {"rich_text": [{"type":"text","text":{"content": new_name}}]}}}
                        )
                        time.sleep(SLEEP)
                    except Exception as e:
                        print(f"⚠️ تعذر تحديث اسم: {e}")

    print(f"\n✅ تم إنشاء {rows_created} صف في التقرير التفصيلي")
    
    try:
        db_info = retrieve_database(out_db_id)
        print(f"📄 افتح التقرير مباشرة: {db_info.get('url')}")
    except:
        pass
    
    print("🎯 اكتمل التقرير التفصيلي.")

if __name__ == "__main__":
    print("⏳ إنشاء التقرير التفصيلي: الموظفين والمشاريع وحالة التحويل…")
    try:
        generate_detailed_report()
    except requests.exceptions.Timeout:
        print("⏰ Timeout — الشبكة بطيئة/رد Notion تأخر.")
    except requests.HTTPError as e:
        print("❌ HTTPError:", e)
        try:
            print("↪️ Response:", e.response.status_code, e.response.text)
        except:
            pass
    except AssertionError as e:
        print("❗", e)
    except Exception as e:
        print("❌ Unexpected:", e)
