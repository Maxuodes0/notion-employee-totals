# employee_totals_update.py

import os
import re
import time
import requests
from collections import defaultdict

RAW_PROJECTS_DB_ID = "23e6fe2a5e8e8003a6bfcf99ae01ba0c"
TEAM_DB_NAME = "فريق المشروع"

TOTALS_DB_ID = os.getenv("EMP_TOTALS_DB_ID")

EMP_NAME_PROP = "اسم الموظف"
TRANSFERRED_AMOUNT_PROP = "المبلغ المحول"
NOT_TRANSFERRED_AMOUNT_PROP = "المبلغ غير المحول"
TOTAL_AMOUNT_PROP = "المجموع"
PROJECT_COUNT_PROP = "عدد المشاريع"
TRANSFERRED_PROJECTS_PROP = "أسماء المشاريع (محولة)"
NOT_TRANSFERRED_PROJECTS_PROP = "أسماء المشاريع (غير محولة)"

DEFAULT_TIMEOUT = 30
SLEEP = 0.15
MAX_PROJECTS = None

EMP_KEYWORDS = ["employee", "موظف", "member", "عضو", "team", "hr", "database", "اسم"]
AMOUNT_KEYWORDS = ["total", "amount", "إجمالي", "المجموع", "قيمة", "مبلغ", "sum"]
STATUS_KEYWORDS = ["status", "transfer", "تحويل", "حالة", "الحوالة", "محول", "محولة"]

TRANSFER_TRUE_VALUES = {
    "transferred",
    "paid",
    "done",
    "تم",
    "محول",
    "محولة"
}

API_KEY = os.getenv("NOTION_API_KEY") or os.getenv("NOTION_TOKEN")
assert API_KEY, "🚫 أضف NOTION_API_KEY أو NOTION_TOKEN في Secrets."
assert TOTALS_DB_ID, "🚫 أضف EMP_TOTALS_DB_ID في Secrets."

HDRS = {
    "Authorization": f"Bearer {API_KEY}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

SESSION = requests.Session()
SESSION.headers.update(HDRS)

PAGE_TITLE_CACHE = {}


def hyphenate(nid: str) -> str:
    if not nid:
        return nid

    nid = nid.strip()

    if "-" in nid:
        return nid

    if re.fullmatch(r"[0-9a-fA-F]{32}", nid):
        return f"{nid[:8]}-{nid[8:12]}-{nid[12:16]}-{nid[16:20]}-{nid[20:]}"

    return nid


PROJECTS_DB_ID = hyphenate(RAW_PROJECTS_DB_ID)
TOTALS_DB_ID = hyphenate(TOTALS_DB_ID)


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

            if not r.ok:
                print("❌ Notion Error Response:")
                print(r.text)

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


def retrieve_database(db_id):
    return http_get(f"https://api.notion.com/v1/databases/{hyphenate(db_id)}").json()


def retrieve_page(page_id):
    page_id = hyphenate(page_id)

    if page_id in PAGE_TITLE_CACHE:
        return PAGE_TITLE_CACHE[page_id]

    page = http_get(f"https://api.notion.com/v1/pages/{page_id}").json()
    title = title_from_page(page)

    PAGE_TITLE_CACHE[page_id] = title
    return title


def query_database_pages(db_id, page_size=100, limit=None, filter_body=None):
    url = f"https://api.notion.com/v1/databases/{hyphenate(db_id)}/query"
    res = []
    cursor = None

    while True:
        body = {
            "page_size": min(int(page_size), 100)
        }

        if cursor:
            body["start_cursor"] = cursor

        if filter_body:
            body["filter"] = filter_body

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
    url = f"https://api.notion.com/v1/blocks/{hyphenate(block_id)}/children"
    res = []
    cursor = None

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
    for value in (page.get("properties") or {}).values():
        if value.get("type") == "title":
            arr = value.get("title", [])
            if arr:
                return arr[0].get("plain_text", "").strip()

    return "(بدون عنوان)"


def find_team_db_id_in_project_page(page_id):
    for block in list_block_children_all(page_id):
        if block.get("type") == "child_database":
            title = (block.get("child_database") or {}).get("title", "").strip()

            if title == TEAM_DB_NAME:
                return block.get("id")

    return None


def score_name(name, keywords):
    name = (name or "").lower()
    return sum(1 for k in keywords if k in name)


def detect_team_schema(team_db_id):
    db = retrieve_database(team_db_id)
    props = db.get("properties", {}) or {}

    people = [n for n, m in props.items() if m.get("type") == "people"]
    relation = [n for n, m in props.items() if m.get("type") == "relation"]
    rollup = [n for n, m in props.items() if m.get("type") == "rollup"]
    textlike = [n for n, m in props.items() if m.get("type") in ("title", "rich_text")]
    numbery = [n for n, m in props.items() if m.get("type") in ("number", "formula", "rollup")]
    statusy = [n for n, m in props.items() if m.get("type") in ("select", "status", "rich_text")]

    emp_key = (
        max(people, key=lambda n: (score_name(n, EMP_KEYWORDS), len(n))) if people else
        max(relation, key=lambda n: (score_name(n, EMP_KEYWORDS), len(n))) if relation else
        max(rollup, key=lambda n: (score_name(n, EMP_KEYWORDS), len(n))) if rollup else
        max(textlike, key=lambda n: (score_name(n, EMP_KEYWORDS), len(n))) if textlike else
        None
    )

    amt_key = (
        max(numbery, key=lambda n: (score_name(n, AMOUNT_KEYWORDS), len(n)))
        if numbery else None
    )

    status_key = (
        max(statusy, key=lambda n: (score_name(n, STATUS_KEYWORDS), len(n)))
        if statusy else None
    )

    return emp_key, amt_key, status_key


def extract_textlike(prop):
    if not prop:
        return ""

    t = prop.get("type")

    if t == "title":
        arr = prop.get("title", [])
    elif t == "rich_text":
        arr = prop.get("rich_text", [])
    else:
        return ""

    return arr[0].get("plain_text", "").strip() if arr else ""


def extract_amount(prop):
    if not prop:
        return 0.0

    t = prop.get("type")

    if t == "number":
        return float(prop.get("number") or 0)

    if t == "formula":
        formula = prop.get("formula") or {}
        if formula.get("type") == "number":
            return float(formula.get("number") or 0)
        return 0.0

    if t == "rollup":
        rollup = prop.get("rollup") or {}

        if rollup.get("type") == "number":
            return float(rollup.get("number") or 0)

        if rollup.get("type") == "array":
            total = 0.0
            for item in rollup.get("array", []):
                total += extract_amount(item)
            return total

    return 0.0


def extract_employee_name(prop):
    if not prop:
        return ""

    t = prop.get("type")

    if t == "people":
        people = prop.get("people", [])
        return people[0].get("name", "").strip() if people else ""

    if t == "relation":
        rel = prop.get("relation", [])
        if not rel:
            return ""

        page_id = rel[0].get("id")
        return retrieve_page(page_id)

    if t == "rollup":
        rollup = prop.get("rollup") or {}

        if rollup.get("type") == "array":
            for item in rollup.get("array", []):
                name = extract_employee_name(item)
                if name:
                    return name

        return ""

    if t in ("title", "rich_text"):
        return extract_textlike(prop)

    return ""


def extract_status_label(prop):
    if not prop:
        return ""

    t = prop.get("type")

    if t in ("select", "status"):
        return (prop.get(t) or {}).get("name", "").strip()

    if t in ("title", "rich_text"):
        return extract_textlike(prop)

    return ""


def is_transferred(label):
    return (label or "").strip().lower() in TRANSFER_TRUE_VALUES


def safe_text(value, limit=1900):
    value = str(value or "").strip()
    return value[:limit]


def validate_totals_database():
    db = retrieve_database(TOTALS_DB_ID)
    props = db.get("properties", {}) or {}

    required = [
        EMP_NAME_PROP,
        TRANSFERRED_AMOUNT_PROP,
        NOT_TRANSFERRED_AMOUNT_PROP,
        TOTAL_AMOUNT_PROP,
        PROJECT_COUNT_PROP,
        TRANSFERRED_PROJECTS_PROP,
        NOT_TRANSFERRED_PROJECTS_PROP,
    ]

    missing = [p for p in required if p not in props]

    if missing:
        raise ValueError(f"🚫 الأعمدة التالية غير موجودة في جدول التجميع: {missing}")

    print("✅ تم التحقق من أعمدة جدول التجميع")


def find_employee_row(employee_name):
    filter_body = {
        "property": EMP_NAME_PROP,
        "title": {
            "equals": employee_name
        }
    }

    rows = query_database_pages(
        TOTALS_DB_ID,
        page_size=1,
        limit=1,
        filter_body=filter_body
    )

    return rows[0]["id"] if rows else None


def build_totals_properties(employee_name, data):
    transferred_amount = float(data["transferred_amount"])
    not_transferred_amount = float(data["not_transferred_amount"])
    total_amount = transferred_amount + not_transferred_amount

    transferred_projects = "، ".join(sorted(data["transferred_projects"]))
    not_transferred_projects = "، ".join(sorted(data["not_transferred_projects"]))

    project_count = len(data["all_projects"])

    return {
        EMP_NAME_PROP: {
            "title": [
                {
                    "type": "text",
                    "text": {
                        "content": employee_name
                    }
                }
            ]
        },
        TRANSFERRED_AMOUNT_PROP: {
            "number": transferred_amount
        },
        NOT_TRANSFERRED_AMOUNT_PROP: {
            "number": not_transferred_amount
        },
        TOTAL_AMOUNT_PROP: {
            "number": total_amount
        },
        PROJECT_COUNT_PROP: {
            "number": project_count
        },
        TRANSFERRED_PROJECTS_PROP: {
            "rich_text": [
                {
                    "type": "text",
                    "text": {
                        "content": safe_text(transferred_projects)
                    }
                }
            ]
        },
        NOT_TRANSFERRED_PROJECTS_PROP: {
            "rich_text": [
                {
                    "type": "text",
                    "text": {
                        "content": safe_text(not_transferred_projects)
                    }
                }
            ]
        }
    }


def create_employee_row(employee_name, data):
    body = {
        "parent": {
            "database_id": TOTALS_DB_ID
        },
        "properties": build_totals_properties(employee_name, data)
    }

    return http_post("https://api.notion.com/v1/pages", body).json()


def update_employee_row(page_id, employee_name, data):
    body = {
        "properties": build_totals_properties(employee_name, data)
    }

    return http_patch(
        f"https://api.notion.com/v1/pages/{hyphenate(page_id)}",
        body
    ).json()


def upsert_employee_total(employee_name, data):
    existing_page_id = find_employee_row(employee_name)

    if existing_page_id:
        update_employee_row(existing_page_id, employee_name, data)
        return "updated"

    create_employee_row(employee_name, data)
    return "created"


def collect_employee_totals():
    totals = defaultdict(lambda: {
        "transferred_amount": 0.0,
        "not_transferred_amount": 0.0,
        "all_projects": set(),
        "transferred_projects": set(),
        "not_transferred_projects": set(),
    })

    projects = query_database_pages(PROJECTS_DB_ID, limit=MAX_PROJECTS)

    print(f"📦 عدد المشاريع: {len(projects)}")

    for idx, project_page in enumerate(projects, 1):
        project_id = project_page["id"]
        project_name = title_from_page(project_page)

        print(f"\n[{idx}] {project_name}")

        team_db_id = find_team_db_id_in_project_page(project_id)

        if not team_db_id:
            print("  ⚠️ لا يوجد جدول فريق المشروع")
            continue

        emp_key, amt_key, status_key = detect_team_schema(team_db_id)

        if not emp_key:
            print("  ⚠️ لم يتم العثور على عمود الموظف")
            continue

        if not amt_key:
            print("  ⚠️ لم يتم العثور على عمود المبلغ")
            continue

        rows = query_database_pages(team_db_id)

        print(f"  👥 صفوف الفريق: {len(rows)}")
        print(f"  🧩 عمود الموظف: {emp_key}")
        print(f"  🧩 عمود المبلغ: {amt_key}")
        print(f"  🧩 عمود حالة التحويل: {status_key or 'غير موجود'}")

        for row in rows:
            props = row.get("properties", {}) or {}

            employee_name = extract_employee_name(props.get(emp_key))
            amount = extract_amount(props.get(amt_key))

            if not employee_name:
                continue

            raw_status = extract_status_label(props.get(status_key)) if status_key else ""
            transferred = is_transferred(raw_status)

            totals[employee_name]["all_projects"].add(project_name)

            if transferred:
                totals[employee_name]["transferred_amount"] += amount
                totals[employee_name]["transferred_projects"].add(project_name)
                status_text = "محول"
            else:
                totals[employee_name]["not_transferred_amount"] += amount
                totals[employee_name]["not_transferred_projects"].add(project_name)
                status_text = "غير محول"

            print(f"    ✓ {employee_name} | {amount} | {status_text}")

            time.sleep(SLEEP)

    return totals


def update_totals_database():
    print("⏳ بدء تحديث جدول تجميع مبالغ الموظفين...")

    validate_totals_database()

    totals = collect_employee_totals()

    print(f"\n👥 عدد الموظفين بعد التجميع: {len(totals)}")

    created_count = 0
    updated_count = 0

    for employee_name, data in totals.items():
        action = upsert_employee_total(employee_name, data)

        if action == "created":
            created_count += 1
        else:
            updated_count += 1

        total_amount = data["transferred_amount"] + data["not_transferred_amount"]

        print(
            f"✓ {employee_name} | "
            f"محول: {data['transferred_amount']} | "
            f"غير محول: {data['not_transferred_amount']} | "
            f"المجموع: {total_amount} | "
            f"{action}"
        )

        time.sleep(SLEEP)

    print("\n✅ انتهى التحديث")
    print(f"🟢 سجلات جديدة: {created_count}")
    print(f"🟡 سجلات محدثة: {updated_count}")


if __name__ == "__main__":
    update_totals_database()
