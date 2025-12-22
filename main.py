# main.py — تجميع مبالغ الموظفين عبر جميع المشاريع + كتابة النتائج في Notion
# يكتب: المبلغ المحول / غير المحول / المجموع / عدد المشاريع
# + أسماء المشاريع المحوّلة / غير المحوّلة كنص، وعلاقات اختيارية للمشاريع

import os, re, time, requests
from collections import defaultdict

# ========= ضع معرّف قاعدة Projects (بشرطات أو بدون) =========
RAW_PROJECTS_DB_ID = "23e6fe2a5e8e8003a6bfcf99ae01ba0c"
# ==========================================================

TEAM_DB_NAME = "فريق المشروع"   # اسم جدول الفريق داخل صفحة المشروع

OUT_DB_ID_ENV   = os.getenv("EMP_TOTALS_DB_ID")
OUT_PARENT_ENV  = os.getenv("EMP_TOTALS_PARENT_PAGE_ID")

OUT_DB_TITLE   = "تجميع مبالغ الموظفين"
OUT_TITLE_PROP = "اسم الموظف"
OUT_TR_PROP    = "المبلغ المحول"
OUT_PD_PROP    = "المبلغ غير المحول"
OUT_TOT_PROP   = "المجموع"
OUT_PCNT_PROP  = "عدد المشاريع"

OUT_TR_PROJS_TEXT_PROP = "أسماء المشاريع (محوّلة)"
OUT_PD_PROJS_TEXT_PROP = "أسماء المشاريع (غير محوّلة)"
OUT_REL_TR_PROJECTS = "مشاريع محوّلة"
OUT_REL_PD_PROJECTS = "مشاريع غير محوّلة"

DEFAULT_TIMEOUT = 30
SLEEP = 0.15
MAX_PROJECTS = None
RESOLVE_NAMES = True

EMP_KEYWORDS     = ["employee","موظف","member","عضو","team","hr","database","اسم"]
AMOUNT_KEYWORDS  = ["total","amount","إجمالي","المجموع","قيمة","مبلغ","sum"]
STATUS_KEYWORDS  = ["status","transfer","تحويل","حالة","الحوالة","محول","محولة"]

TRANSFER_TRUE_VALUES  = {"transferred","paid","done","تم","محول","محولة"}
TRANSFER_FALSE_VALUES = {"not yet","pending","unpaid","لم","غير","لم تُحوّل","لم تحول"}

API_KEY = os.getenv("NOTION_API_KEY") or os.getenv("NOTION_TOKEN")
assert API_KEY, "🚫 أضف NOTION_API_KEY أو NOTION_TOKEN في Secrets."

HDRS = {
    "Authorization": f"Bearer {API_KEY}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

# ============ تحسينات تشغيلية (لا تغيّر النتيجة) ============
SESSION = requests.Session()
SESSION.headers.update(HDRS)

def _sleep_backoff(attempt: int):
    # Backoff بسيط جداً لتخفيف 429/5xx
    time.sleep(min(2.0, 0.25 * (2 ** attempt)))

def _request(method: str, url: str, *, params=None, json=None, timeout=DEFAULT_TIMEOUT):
    # Retry خفيف على 429/5xx فقط (ما يغيّر المنطق/النتائج)
    last_exc = None
    for attempt in range(4):  # 0..3
        try:
            r = SESSION.request(method, url, params=params, json=json, timeout=timeout)
            if r.status_code in (429, 500, 502, 503, 504):
                _sleep_backoff(attempt)
                continue
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            last_exc = e
            _sleep_backoff(attempt)
            continue
    # آخر محاولة: ارفع الخطأ
    if isinstance(last_exc, requests.HTTPError):
        raise last_exc
    raise last_exc

def http_get(url, params=None):
    return _request("GET", url, params=params)

def http_post(url, json=None):
    return _request("POST", url, json=json or {})

def http_patch(url, json=None):
    return _request("PATCH", url, json=json or {})
# ============================================================

def hyphenate(nid: str) -> str:
    nid = nid.strip()
    if "-" in nid: return nid
    if re.fullmatch(r"[0-9a-fA-F]{32}", nid):
        return f"{nid[0:8]}-{nid[8:12]}-{nid[12:16]}-{nid[16:20]}-{nid[20:32]}"
    return nid

PROJECTS_DB_ID = hyphenate(RAW_PROJECTS_DB_ID)

def retrieve_database(db_id):  return http_get(f"https://api.notion.com/v1/databases/{db_id}").json()
def retrieve_page(page_id):    return http_get(f"https://api.notion.com/v1/pages/{page_id}").json()

def query_database_pages(db_id, page_size=100, limit=None, filter_payload=None):
    url = f"https://api.notion.com/v1/databases/{db_id}/query"
    res, cursor = [], None
    while True:
        # Notion max page_size = 100
        body = {"page_size": min(int(page_size or 100), 100)}
        if cursor: body["start_cursor"] = cursor
        if filter_payload: body.update(filter_payload)
        data = http_post(url, body).json()
        res.extend(data.get("results", []))
        if limit and len(res) >= limit: return res[:limit]
        if not data.get("has_more"): break
        cursor = data.get("next_cursor"); time.sleep(SLEEP)
    return res

def list_block_children_all(block_id):
    url = f"https://api.notion.com/v1/blocks/{block_id}/children"
    res, cursor = [], None
    while True:
        params = {}
        if cursor: params["start_cursor"] = cursor
        data = http_get(url, params=params).json()
        res.extend(data.get("results", []))
        if not data.get("has_more"): break
        cursor = data.get("next_cursor"); time.sleep(SLEEP)
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
    n=(name or "").lower(); 
    return sum(1 for kw in keywords if kw in n)

def detect_team_schema(team_db_id):
    db = retrieve_database(team_db_id); props = db.get("properties", {}) or {}
    people   = [n for n, m in props.items() if m.get("type") == "people"]
    relation = [n for n, m in props.items() if m.get("type") == "relation"]
    rollup   = [n for n, m in props.items() if m.get("type") == "rollup"]
    textlike = [n for n, m in props.items() if m.get("type") in ("rich_text","title")]
    numbery  = [n for n, m in props.items() if m.get("type") in ("number","formula")]
    statusy  = [n for n, m in props.items() if m.get("type") in ("select","status","rich_text")]

    emp_key = (max(people, key=lambda n:(score_name(n,EMP_KEYWORDS),len(n))) if people else
               max(relation,key=lambda n:(score_name(n,EMP_KEYWORDS),len(n))) if relation else
               max(rollup,  key=lambda n:(score_name(n,EMP_KEYWORDS),len(n))) if rollup else
               max(textlike,key=lambda n:(score_name(n,EMP_KEYWORDS),len(n))) if textlike else None)
    amt_key = (max(numbery, key=lambda n:(score_name(n,AMOUNT_KEYWORDS),len(n))) if numbery else None)
    status_key = (max(statusy, key=lambda n:(score_name(n,STATUS_KEYWORDS),len(n))) if statusy else None)

    emp_relation_db_id = None
    if emp_key and props[emp_key].get("type") == "relation":
        rel_meta = props[emp_key].get("relation") or {}
        emp_relation_db_id = rel_meta.get("database_id")
    return emp_key, amt_key, status_key, db, emp_relation_db_id

def extract_amount(prop):
    t = prop.get("type")
    if t == "number":  return float(prop.get("number") or 0)
    if t == "formula": return float((prop.get("formula") or {}).get("number") or 0)
    if t == "rollup":
        r = prop.get("rollup") or {}
        if r.get("type") == "number": return float(r.get("number") or 0)
    return 0.0

def extract_textlike(cell):
    if cell.get("type") == "title":
        arr = cell.get("title", []);  return (arr[0].get("plain_text") or "").strip() if arr else ""
    if cell.get("type") == "rich_text":
        arr = cell.get("rich_text", []);  return (arr[0].get("plain_text") or "").strip() if arr else ""
    return ""

def extract_rollup_text(cell):
    if cell.get("type") != "rollup": return ""
    r = cell.get("rollup") or {}
    if r.get("type") == "array":
        arr = r.get("array") or []
        if not arr: return ""
        it = arr[0]
        if it.get("type") == "title":
            t = it.get("title") or [];  return (t[0].get("plain_text") or "").strip() if t else ""
        if it.get("type") == "rich_text":
            rt = it.get("rich_text") or [];  return (rt[0].get("plain_text") or "").strip() if rt else ""
    return ""

def extract_employee_key(prop):
    t = prop.get("type")
    if t == "people":
        ppl = prop.get("people", []);  return (ppl[0].get("name") or ppl[0].get("id") or "").strip() if ppl else ""
    if t == "relation":
        rel = prop.get("relation", []); return rel[0].get("id") if rel else ""
    if t == "rollup":  return extract_rollup_text(prop)
    if t in ("title","rich_text"): return extract_textlike(prop)
    return ""

def extract_status_label(prop):
    t = prop.get("type")
    if t == "select": return (prop.get("select") or {}).get("name","").strip()
    if t == "status": return (prop.get("status") or {}).get("name","").strip()
    if t in ("rich_text","title"): return extract_textlike(prop)
    return ""

def is_transferred(label: str) -> bool:
    l = (label or "").strip().lower()
    if l in TRANSFER_TRUE_VALUES:  return True
    if l in TRANSFER_FALSE_VALUES: return False
    return False

def looks_like_id(s: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", s, re.I))

def get_child_database_in_page_by_title(page_id: str, title: str):
    for blk in list_block_children_all(page_id):
        if blk.get("type") == "child_database":
            t = (blk.get("child_database") or {}).get("title","")
            if t.strip() == title.strip(): return blk.get("id")
    return None

def create_output_db_under_page(parent_page_id: str) -> str:
    payload = {
        "parent": {"type": "page_id", "page_id": parent_page_id},
        "title": [{"type":"text","text":{"content": OUT_DB_TITLE}}],
        "properties": {
            OUT_TITLE_PROP: {"title": {}},
            OUT_TR_PROP:    {"number": {}},
            OUT_PD_PROP:    {"number": {}},
            OUT_TOT_PROP:   {"number": {}},
            OUT_PCNT_PROP:  {"number": {}},
            OUT_TR_PROJS_TEXT_PROP: {"rich_text": {}},
            OUT_PD_PROJS_TEXT_PROP: {"rich_text": {}},
        }
    }
    data = http_post("https://api.notion.com/v1/databases", payload).json()
    print(f"🆕 أنشأنا قاعدة النتائج: {data.get('id')}")
    return data.get("id")

def ensure_output_db() -> str:
    if OUT_DB_ID_ENV:
        print(f"🔗 استخدام قاعدة موجودة EMP_TOTALS_DB_ID={OUT_DB_ID_ENV}")
        return hyphenate(OUT_DB_ID_ENV)
    assert OUT_PARENT_ENV, "🚫 وفّر EMP_TOTALS_PARENT_PAGE_ID (Page ID لصفحة Finance) أو EMP_TOTALS_DB_ID."
    parent_id = hyphenate(OUT_PARENT_ENV)
    existing = get_child_database_in_page_by_title(parent_id, OUT_DB_TITLE)
    if existing:
        print(f"🔎 وجدنا قاعدة النتائج داخل الصفحة: {existing}")
        return existing
    print("➕ إنشاء قاعدة النتائج داخل صفحة Finance…")
    return create_output_db_under_page(parent_id)

def ensure_output_columns(db_id: str):
    db = retrieve_database(db_id); props = db.get("properties", {}) or {}
    patch = {"properties": {}}
    if OUT_TR_PROJS_TEXT_PROP not in props:
        patch["properties"][OUT_TR_PROJS_TEXT_PROP] = {"rich_text": {}}
    if OUT_PD_PROJS_TEXT_PROP not in props:
        patch["properties"][OUT_PD_PROJS_TEXT_PROP] = {"rich_text": {}}
    if OUT_REL_TR_PROJECTS not in props:
        patch["properties"][OUT_REL_TR_PROJECTS] = {"relation": {"database_id": PROJECTS_DB_ID}}
    if OUT_REL_PD_PROJECTS not in props:
        patch["properties"][OUT_REL_PD_PROJECTS] = {"relation": {"database_id": PROJECTS_DB_ID}}
    if patch["properties"]:
        try:
            http_patch(f"https://api.notion.com/v1/databases/{db_id}", patch)
            print("🔧 تأكدنا من أعمدة النص/العلاقات (أضفنا الناقص).")
        except requests.HTTPError as e:
            print("⚠️ تعذّر إضافة بعض الأعمدة (غالبًا علاقات) بسبب الصلاحيات:", e)

def get_existing_rows_map(db_id: str):
    pages = query_database_pages(db_id, page_size=100)
    m = {}
    for p in pages:
        props = p.get("properties", {})
        name = ""
        if OUT_TITLE_PROP in props and props[OUT_TITLE_PROP].get("title"):
            name = props[OUT_TITLE_PROP]["title"][0].get("plain_text","")
        else:
            name = title_from_page(p)
        m[name] = {"id": p["id"]}
    return m

def rt(text):  return {"rich_text":[{"type":"text","text":{"content": text or ""}}]}

def create_result_row(db_id: str, rec):
    payload = {
        "parent": {"database_id": db_id},
        "properties": {
            OUT_TITLE_PROP: {"title":[{"type":"text","text":{"content": rec["name"]}}]},
            OUT_TR_PROP:    {"number": rec["transferred"]},
            OUT_PD_PROP:    {"number": rec["pending"]},
            OUT_TOT_PROP:   {"number": rec["total"]},
            OUT_PCNT_PROP:  {"number": rec["projects_count"]},
            OUT_TR_PROJS_TEXT_PROP: rt(rec.get("tr_projects_text","")),
            OUT_PD_PROJS_TEXT_PROP: rt(rec.get("pd_projects_text","")),
            OUT_REL_TR_PROJECTS: {"relation": [{"id": pid} for pid in rec.get("tr_project_ids", [])]},
            OUT_REL_PD_PROJECTS: {"relation": [{"id": pid} for pid in rec.get("pd_project_ids", [])]},
        }
    }
    http_post("https://api.notion.com/v1/pages", payload)

def update_result_row(page_id: str, rec):
    payload = {
        "properties": {
            OUT_TITLE_PROP: {"title":[{"type":"text","text":{"content": rec["name"]}}]},
            OUT_TR_PROP:    {"number": rec["transferred"]},
            OUT_PD_PROP:    {"number": rec["pending"]},
            OUT_TOT_PROP:   {"number": rec["total"]},
            OUT_PCNT_PROP:  {"number": rec["projects_count"]},
            OUT_TR_PROJS_TEXT_PROP: rt(rec.get("tr_projects_text","")),
            OUT_PD_PROJS_TEXT_PROP: rt(rec.get("pd_projects_text","")),
            OUT_REL_TR_PROJECTS: {"relation": [{"id": pid} for pid in rec.get("tr_project_ids", [])]},
            OUT_REL_PD_PROJECTS: {"relation": [{"id": pid} for pid in rec.get("pd_project_ids", [])]},
        }
    }
    http_patch(f"https://api.notion.com/v1/pages/{page_id}", payload)

def upsert_bulk(db_id: str, records):
    existing = get_existing_rows_map(db_id)
    creates, updates = 0, 0
    for rec in records:
        ex = existing.get(rec["name"])
        if ex: update_result_row(ex["id"], rec); updates += 1
        else: create_result_row(db_id, rec); creates += 1
        time.sleep(SLEEP)
    print(f"✅ تم: {creates} إنشاء | {updates} تحديث")

def aggregate_and_write():
    projects_db = retrieve_database(PROJECTS_DB_ID)
    print(f"✅ Projects DB: {projects_db.get('title',[{}])[0].get('plain_text','(No title)')}")
    projects = query_database_pages(PROJECTS_DB_ID, page_size=100, limit=MAX_PROJECTS)
    print(f"📦 عدد المشاريع: {len(projects)}")

    totals = defaultdict(lambda: {"transferred": 0.0, "pending": 0.0})
    proj_sets_all = defaultdict(set); proj_sets_tr = defaultdict(set); proj_sets_pd = defaultdict(set)
    page_titles = {}; employee_db_ids = set()

    for idx, page in enumerate(projects, 1):
        pid = page["id"]; ptitle = title_from_page(page); page_titles[pid] = ptitle
        print(f"\n[{idx}] {ptitle}")
        team_db_id = find_team_db_id_in_project_page(pid)
        if not team_db_id: print("  ⚠️ لا يوجد جدول 'فريق المشروع'"); continue
        emp_key, amt_key, status_key, _schema, emp_rel_db = detect_team_schema(team_db_id)
        if not emp_key or not amt_key: print("  ⚠️ لم نحدد أعمدة الموظف/المبلغ"); continue
        if emp_rel_db: employee_db_ids.add(hyphenate(emp_rel_db))
        team_rows = query_database_pages(team_db_id, page_size=100)
        print(f"  👥 صفوف الفريق: {len(team_rows)}")

        for r in team_rows:
            props = r.get("properties", {})
            emp_cell = props.get(emp_key); amt_cell = props.get(amt_key)
            if not emp_cell or not amt_cell: continue
            key = extract_employee_key(emp_cell); 
            if not key: continue
            amt = extract_amount(amt_cell); 
            if amt <= 0: continue
            transferred = False
            if status_key and status_key in props:
                transferred = is_transferred(extract_status_label(props[status_key]))
            if transferred:
                totals[key]["transferred"] += amt; proj_sets_tr[key].add(pid)
            else:
                totals[key]["pending"] += amt; proj_sets_pd[key].add(pid)
            proj_sets_all[key].add(pid)
        time.sleep(SLEEP)

    name_map = {}
    if RESOLVE_NAMES and employee_db_ids:
        for emp_db in list(employee_db_ids):
            print(f"🔎 تحميل أسماء الموظفين من قاعدة: {emp_db}")
            pages = query_database_pages(emp_db, page_size=100)
            for pg in pages: name_map[pg["id"]] = title_from_page(pg)
            time.sleep(SLEEP)

    records = []
    print("\n================= الإجمالي عبر كل المشاريع =================")
    for key, buckets in sorted(totals.items(), key=lambda kv: -(kv[1]['transferred']+kv[1]['pending'])):
        label = name_map.get(key, key) if looks_like_id(key) else key
        tr = buckets["transferred"]; pd = buckets["pending"]; tot = tr + pd; cnt = len(proj_sets_all[key])
        tr_names = [page_titles.get(pp, pp) for pp in sorted(proj_sets_tr[key])]
        pd_names = [page_titles.get(pp, pp) for pp in sorted(proj_sets_pd[key])]
        tr_text = " • ".join(tr_names) if tr_names else ""; pd_text = " • ".join(pd_names) if pd_names else ""
        print(f"{label}: محوّل {tr:,.2f} | غير محوّل {pd:,.2f} | الإجمالي {tot:,.2f} | المشاريع {cnt}")
        records.append({
            "name": label, "transferred": round(tr,2), "pending": round(pd,2), "total": round(tot,2),
            "projects_count": cnt, "tr_projects_text": tr_text, "pd_projects_text": pd_text,
            "tr_project_ids": sorted(list(proj_sets_tr[key])), "pd_project_ids": sorted(list(proj_sets_pd[key])),
        })

    out_db_id = ensure_output_db(); ensure_output_columns(out_db_id); upsert_bulk(out_db_id, records)
    try:
        db_info = retrieve_database(out_db_id); print(f"📄 افتح القاعدة مباشرة: {db_info.get('url')}")
    except:
        pass
    print("🎯 اكتمل التحديث.")

if __name__ == "__main__":
    print("⏳ تجميع وكتابة نتائج الموظفين…")
    try:
        aggregate_and_write()
    except requests.exceptions.Timeout:
        print("⏰ Timeout — الشبكة بطيئة/رد Notion تأخر.")
    except requests.HTTPError as e:
        print("❌ HTTPError:", e)
        try:
            # طباعة أوضح وقت الخطأ (ما تظهر إلا إذا صار خطأ)
            print("↪️ Response:", e.response.status_code, e.response.text)
        except:
            pass
    except AssertionError as e:
        print("❗", e)
    except Exception as e:
        print("❌ Unexpected:", e)
