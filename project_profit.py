# project_profit.py — ربحية المشاريع = (قيمة بدون ضريبة) - (مجموع التكاليف)
# يقرأ من قواعد فرعية داخل كل مشروع:
#   1) "قيمة المشروع": عمود "بدون ضريبة" (أو يحوّل من "شامل" إلى "غير شامل" بقسمة على 1+VAT_RATE)
#   2) "تكاليف المشروع": يجمع عمود التكلفة لكل صف (ولو ما وجد "مجموع التكاليف" كاسم عمود، يختار أفضل عمود رقمي تلقائيًا)
# ثم يكتب في قاعدة "ربحية المشاريع" (Upsert) مع Relation للمشروع (إن توفّر بال-schema)

import os, re, time, requests

# ========= ضع معرّف قاعدة Projects (بشرطات أو بدون) =========
RAW_PROJECTS_DB_ID = os.getenv("PROJECTS_DB_ID", "23e6fe2a5e8e8003a6bfcf99ae01ba0c")
# ==========================================================

VALUE_DB_NAME = "قيمة المشروع"
COSTS_DB_NAME = "تكاليف المشروع"

# إخراج النتائج
OUT_DB_ID_ENV  = os.getenv("PROFIT_DB_ID")
OUT_PARENT_ENV = os.getenv("PROFIT_PARENT_PAGE_ID") or os.getenv("EMP_TOTALS_PARENT_PAGE_ID")
OUT_DB_TITLE   = "ربحية المشاريع"

# أسماء الأعمدة الافتراضية المتوقعة (سنستخدمها فقط إن كانت موجودة بالschema)
OUT_TITLE_PROP  = "المشروع"
OUT_NET_PROP    = "قيمة بدون ضريبة (SAR)"
OUT_COST_PROP   = "مجموع التكاليف (SAR)"
OUT_PROFIT_PROP = "الربح (SAR)"
OUT_MARGIN_PROP = "الهامش %"
OUT_REL_PROJECT = "رابط المشروع"

DEFAULT_TIMEOUT = 30
SLEEP = 0.12
MAX_PROJECTS = None

# كلمات مفتاحية
NET_REV_KEYS    = ["بدون ضريبة","غير شامل","قبل الضريبة","ex vat","ex-vat","pre vat","pre-vat","net"]
GROSS_REV_KEYS  = ["شامل","شامل الضريبة","بعد الضريبة","with vat","incl vat","inclusive","gross"]
COST_TOTAL_KEYS = ["مجموع التكاليف","إجمالي التكاليف","اجمالي التكاليف","total cost","overall cost","sum cost"]
COST_ITEM_HINTS = ["التكلفة","cost","amount","قيمة","price","سعر","رسوم","مصروف","expense"]

VAT_RATE = float(os.getenv("VAT_RATE", "0.15"))

# ===== Notion =====
API_KEY = os.getenv("NOTION_API_KEY") or os.getenv("NOTION_TOKEN")
assert API_KEY, "🚫 أضف NOTION_API_KEY أو NOTION_TOKEN في Secrets."

NOTION_VERSION = "2022-06-28"  # ثابتة وآمنة (نفس main.py)
print(f"🔧 Using Notion API version: {NOTION_VERSION}")

HDRS = {
    "Authorization": f"Bearer {API_KEY}",
    "Notion-Version": NOTION_VERSION,
    "Content-Type": "application/json",
}

def http_get(url, params=None):
    r = requests.get(url, headers=HDRS, params=params, timeout=DEFAULT_TIMEOUT); r.raise_for_status(); return r
def http_post(url, json=None):
    r = requests.post(url, headers=HDRS, json=json or {}, timeout=DEFAULT_TIMEOUT); r.raise_for_status(); return r
def http_patch(url, json=None):
    r = requests.patch(url, headers=HDRS, json=json or {}, timeout=DEFAULT_TIMEOUT); r.raise_for_status(); return r

def hyphenate(nid: str) -> str:
    nid = (nid or "").strip()
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
        body = {"page_size": min(page_size, 100)}
        if cursor: body["start_cursor"] = cursor
        if filter_payload and isinstance(filter_payload, dict):
            if "filter" in filter_payload: body["filter"] = filter_payload["filter"]
            if "sorts"  in filter_payload: body["sorts"]  = filter_payload["sorts"]
        print(f"📤 Querying database: {db_id[:8]}...")
        resp = requests.post(url, headers=HDRS, json=body, timeout=DEFAULT_TIMEOUT)
        if resp.status_code != 200:
            print(f"❌ Query failed: {resp.status_code}\n❌ Response: {resp.text}")
            resp.raise_for_status()
        data = resp.json()
        chunk = data.get("results", [])
        res.extend(chunk)
        print(f"📊 Retrieved {len(chunk)} pages (total: {len(res)})")
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

def get_child_database_in_page_by_title(page_id: str, title: str):
    for blk in list_block_children_all(page_id):
        if blk.get("type") == "child_database":
            t = (blk.get("child_database") or {}).get("title","")
            if t.strip() == title.strip(): return blk.get("id")
    return None

def detect_number_prop_by_keywords(db_id, keywords):
    db = retrieve_database(db_id); props = db.get("properties", {}) or {}
    for name, meta in props.items():
        if meta.get("type") in ("number","formula","rollup"):
            low = (name or "").lower()
            if any(k.lower() in low for k in keywords):
                return name
    return None

def extract_number_cell(prop):
    t = prop.get("type")
    if t == "number":  return float(prop.get("number") or 0)
    if t == "formula": return float((prop.get("formula") or {}).get("number") or 0)
    if t == "rollup":
        r = prop.get("rollup") or {}
        if r.get("type") == "number": return float(r.get("number") or 0)
        if r.get("type") == "array":
            total = 0.0
            for it in r.get("array") or []:
                if it.get("type") == "number":
                    total += float(it.get("number") or 0)
                elif it.get("type") == "formula":
                    n = (it.get("formula") or {}).get("number")
                    if n is not None: total += float(n)
            return total
    return 0.0

def sum_numeric_column(db_id, col_name):
    if not col_name: return 0.0
    total = 0.0
    for row in query_database_pages(db_id, page_size=100):
        props = row.get("properties", {})
        cell = props.get(col_name, {})
        total += extract_number_cell(cell)
    return round(total, 2)

def pick_best_numeric_column(db_id, hints=None):
    """لو ما وجد عمود ‘مجموع التكاليف’، اختَر أفضل عمود رقمي (أعلى مجموع)."""
    db = retrieve_database(db_id); props = db.get("properties", {}) or {}
    cands = [n for n,m in props.items() if m.get("type") in ("number","formula","rollup")]
    # لو فيه تلميحات (مثل "التكلفة") خلها أولاً
    ordered = []
    if hints:
        for n in cands:
            low = n.lower()
            if any(h.lower() in low for h in hints):
                ordered.append(n)
        ordered += [n for n in cands if n not in ordered]
    else:
        ordered = cands[:]

    best, best_sum = None, 0.0
    for name in ordered:
        s = sum_numeric_column(db_id, name)
        if s > best_sum:
            best, best_sum = name, s
    return best, best_sum

# ===== قاعدة المخرجات: التعامل الذكي مع الـschema =====
def get_db_schema(db_id):
    db = retrieve_database(db_id)
    props = db.get("properties", {}) or {}
    title_prop = None
    for n, m in props.items():
        if m.get("type") == "title":
            title_prop = n; break
    return props, title_prop

def create_output_db_under_page(parent_page_id: str, projects_db_id_for_relation: str) -> str:
    payload = {
        "parent": {"type": "page_id", "page_id": parent_page_id},
        "title": [{"type":"text","text":{"content": OUT_DB_TITLE}}],
        "properties": {
            OUT_TITLE_PROP:  {"title": {}},
            OUT_NET_PROP:    {"number": {}},
            OUT_COST_PROP:   {"number": {}},
            OUT_PROFIT_PROP: {"number": {}},
            OUT_MARGIN_PROP: {"number": {}},
            OUT_REL_PROJECT: {"relation": {"database_id": projects_db_id_for_relation}},
        }
    }
    data = http_post("https://api.notion.com/v1/databases", payload).json()
    print(f"🆕 أنشأنا قاعدة الربحية: {data.get('id')}")
    return data.get("id")

def ensure_output_db(projects_db_id_for_relation: str) -> str:
    if OUT_DB_ID_ENV:
        print(f"🔗 استخدام قاعدة موجودة PROFIT_DB_ID={OUT_DB_ID_ENV}")
        return hyphenate(OUT_DB_ID_ENV)
    assert OUT_PARENT_ENV, "🚫 وفّر PROFIT_DB_ID أو PROFIT_PARENT_PAGE_ID (أو EMP_TOTALS_PARENT_PAGE_ID)."
    parent_id = hyphenate(OUT_PARENT_ENV)
    existing = get_child_database_in_page_by_title(parent_id, OUT_DB_TITLE)
    if existing:
        print(f"🔎 وجدنا قاعدة الربحية داخل الصفحة: {existing}")
        return existing
    print("➕ إنشاء قاعدة الربحية داخل صفحة الأم…")
    return create_output_db_under_page(parent_id, projects_db_id_for_relation)

def ensure_output_columns(db_id: str, projects_db_id_for_relation: str):
    db = retrieve_database(db_id); props = db.get("properties", {}) or {}
    patch = {"properties": {}}
    if OUT_REL_PROJECT not in props:
        patch["properties"][OUT_REL_PROJECT] = {"relation": {"database_id": projects_db_id_for_relation}}
    for name in (OUT_TITLE_PROP, OUT_NET_PROP, OUT_COST_PROP, OUT_PROFIT_PROP, OUT_MARGIN_PROP):
        if name not in props:
            if name == OUT_TITLE_PROP: patch["properties"][name] = {"title": {}}
            else: patch["properties"][name] = {"number": {}}
    if patch["properties"]:
        try:
            http_patch(f"https://api.notion.com/v1/databases/{db_id}", patch)
            print("🔧 تأكدنا من أعمدة الربحية (أضفنا الناقص).")
        except requests.HTTPError as e:
            print("⚠️ تعذّر تعديل خصائص القاعدة (غالبًا علاقات/صلاحيات):", e)
            try:
                print("↪️ PATCH response:", e.response.status_code, e.response.text)
            except: pass

def build_props_for_row(rec, available_props, title_prop_name):
    props = {}
    # عنوان
    props[title_prop_name] = {"title":[{"type":"text","text":{"content": rec["name"]}}]}
    # أرقام إذا موجودة
    if OUT_NET_PROP in available_props:    props[OUT_NET_PROP]    = {"number": rec["net_revenue"]}
    else: print(f"⚠️ مفقود في القاعدة: {OUT_NET_PROP}")
    if OUT_COST_PROP in available_props:   props[OUT_COST_PROP]   = {"number": rec["total_cost"]}
    else: print(f"⚠️ مفقود في القاعدة: {OUT_COST_PROP}")
    if OUT_PROFIT_PROP in available_props: props[OUT_PROFIT_PROP] = {"number": rec["profit"]}
    else: print(f"⚠️ مفقود في القاعدة: {OUT_PROFIT_PROP}")
    if OUT_MARGIN_PROP in available_props: props[OUT_MARGIN_PROP] = {"number": rec["margin_pct"]}
    else: print(f"⚠️ مفقود في القاعدة: {OUT_MARGIN_PROP}")
    # Relation اختياري
    if OUT_REL_PROJECT in available_props:
        props[OUT_REL_PROJECT] = {"relation": [{"id": rec["project_id"]}]}
    else:
        print(f"⚠️ مفقود في القاعدة (اختياري): {OUT_REL_PROJECT}")
    return props

def get_existing_rows_map(db_id: str, title_prop_name: str):
    pages = query_database_pages(db_id, page_size=100)
    m = {}
    for p in pages:
        props = p.get("properties", {})
        if title_prop_name in props and props[title_prop_name].get("title"):
            name = props[title_prop_name]["title"][0].get("plain_text","")
        else:
            name = title_from_page(p)
        m[name] = {"id": p["id"]}
    return m

def create_result_row(db_id: str, rec, available_props, title_prop_name):
    payload = {
        "parent": {"database_id": db_id},
        "properties": build_props_for_row(rec, available_props, title_prop_name)
    }
    http_post("https://api.notion.com/v1/pages", payload)

def update_result_row(page_id: str, rec, available_props, title_prop_name):
    payload = {"properties": build_props_for_row(rec, available_props, title_prop_name)}
    http_patch(f"https://api.notion.com/v1/pages/{page_id}", payload)

def upsert_bulk(db_id: str, records):
    props, title_prop_name = get_db_schema(db_id)
    assert title_prop_name, "🚫 قاعدة الربحية لا تحتوي عمود Title."
    existing = get_existing_rows_map(db_id, title_prop_name)
    available_props = set(props.keys())
    creates, updates = 0, 0
    for rec in records:
        ex = existing.get(rec["name"])
        if ex: update_result_row(ex["id"], rec, available_props, title_prop_name); updates += 1
        else:   create_result_row(db_id, rec, available_props, title_prop_name);   creates += 1
        time.sleep(SLEEP)
    print(f"✅ Profit upsert: {creates} إنشاء | {updates} تحديث")

# ======== الحساب والكتابة ========
def aggregate_and_write():
    print(f"🔍 Using database ID: {PROJECTS_DB_ID}")
    projects_db = retrieve_database(PROJECTS_DB_ID)
    print(f"✅ Projects DB: {projects_db.get('title',[{}])[0].get('plain_text','(No title)')}")
    projects = query_database_pages(PROJECTS_DB_ID, page_size=100, limit=MAX_PROJECTS)
    print(f"📦 عدد المشاريع: {len(projects)}")

    # جهّز قاعدة المخرجات
    out_db_id = ensure_output_db(PROJECTS_DB_ID)
    ensure_output_columns(out_db_id, PROJECTS_DB_ID)

    records = []

    for idx, page in enumerate(projects, 1):
        pid = page["id"]; ptitle = title_from_page(page)
        print(f"\n[{idx}] {ptitle}")

        value_db_id = get_child_database_in_page_by_title(pid, VALUE_DB_NAME)
        costs_db_id = get_child_database_in_page_by_title(pid, COSTS_DB_NAME)

        net_revenue = 0.0
        total_cost  = 0.0

        # 1) قيمة بدون ضريبة
        if value_db_id:
            net_col   = detect_number_prop_by_keywords(value_db_id, NET_REV_KEYS)
            gross_col = detect_number_prop_by_keywords(value_db_id, GROSS_REV_KEYS)
            if net_col:
                print(f"  [VALUE] using column: {net_col}")
                net_revenue = sum_numeric_column(value_db_id, net_col)
            elif gross_col:
                print(f"  [VALUE] using gross column: {gross_col} → convert / (1+VAT)")
                gross = sum_numeric_column(value_db_id, gross_col)
                net_revenue = round(gross / (1.0 + VAT_RATE), 2)
            else:
                # اختياري: التقط أفضل عمود رقمي لو ما لقى شيء صريح
                best, s = pick_best_numeric_column(value_db_id, hints=["قيمة","value","amount","بدون","gross"])
                if best:
                    print(f"  [VALUE] fallback column: {best} (sum={s:,.2f}) — treated as Net")
                    net_revenue = round(s, 2)
        else:
            print("  ⚠️ لا يوجد جدول 'قيمة المشروع'")

        # 2) مجموع التكاليف
        if costs_db_id:
            tot_col = detect_number_prop_by_keywords(costs_db_id, COST_TOTAL_KEYS)
            if tot_col:
                print(f"  [COST] using column: {tot_col} (mode=keyword)")
                total_cost = sum_numeric_column(costs_db_id, tot_col)
            else:
                # اجمع أفضل عمود رقمي (عادةً عمود 'التكلفة' لكل صف)
                best, s = pick_best_numeric_column(costs_db_id, hints=COST_ITEM_HINTS)
                if best:
                    print(f"  [COST] using column: {best} (mode=fallback, sum={s:,.2f})")
                    total_cost = round(s, 2)
                else:
                    print("  ⚠️ لم نجد عمود رقمي مناسب داخل 'تكاليف المشروع'")
        else:
            print("  ⚠️ لا يوجد جدول 'تكاليف المشروع'")

        profit = round(net_revenue - total_cost, 2)
        margin = round((profit / net_revenue * 100.0), 2) if net_revenue > 0 else 0.0

        print(f"  ➜ Net {net_revenue:,.2f} - Cost {total_cost:,.2f} = Profit {profit:,.2f} ({margin:.2f}%)")

        records.append({
            "name": ptitle,
            "project_id": pid,
            "net_revenue": net_revenue,
            "total_cost": total_cost,
            "profit": profit,
            "margin_pct": margin,
        })
        time.sleep(SLEEP)

    upsert_bulk(out_db_id, records)
    try:
        db_info = retrieve_database(out_db_id); print(f"📄 افتح القاعدة مباشرة: {db_info.get('url')}")
    except: pass
    print("🎯 اكتمل التحديث.")

if __name__ == "__main__":
    print("⏳ حساب ربحية المشاريع وكتابتها في Notion…")
    try:
        aggregate_and_write()
    except requests.exceptions.Timeout:
        print("⏰ Timeout — الشبكة بطيئة/رد Notion تأخر.")
    except requests.HTTPError as e:
        print("❌ HTTPError:", e)
        try: print("↪️ Response:", e.response.status_code, e.response.text)
        except: pass
    except AssertionError as e:
        print("❗", e)
    except Exception as e:
        print("❌ Unexpected:", e)
