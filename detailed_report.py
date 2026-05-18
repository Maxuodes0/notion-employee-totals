# detailed_report.py
# قراءة المشاريع + تجميع مبالغ الموظفين + تحديث جدول Notion

import os
import re
import time
import requests
from collections import defaultdict

RAW_PROJECTS_DB_ID = "23e6fe2a5e8e8003a6bfcf99ae01ba0c"
TEAM_DB_NAME = "فريق المشروع"

TOTALS_DB_ID_ENV = os.getenv("EMP_TOTALS_DB_ID")

EMP_NAME_PROP = "اسم الموظف"
TRANSFERRED_AMOUNT_PROP = "المبلغ المحول"
NOT_TRANSFERRED_AMOUNT_PROP = "المبلغ غير المحول"
TOTAL_AMOUNT_PROP = "المجموع"
PROJECT_COUNT_PROP = "عدد المشاريع"

TRANSFERRED_PROJECTS_PROP = "مشاريع محولة"
NOT_TRANSFERRED_PROJECTS_PROP = "مشاريع غير محولة"

DEFAULT_TIMEOUT = 30
SLEEP = 0.15

EMP_KEYWORDS = [
    "employee",
    "موظف",
    "member",
    "عضو",
    "team",
    "hr",
    "database",
    "اسم"
]

AMOUNT_KEYWORDS = [
    "total",
    "amount",
    "إجمالي",
    "المجموع",
    "قيمة",
    "مبلغ",
    "sum"
]

STATUS_KEYWORDS = [
    "status",
    "transfer",
    "تحويل",
    "حالة",
    "الحوالة",
    "محول",
    "محولة"
]

TRANSFER_TRUE_VALUES = {
    "transferred",
    "paid",
    "done",
    "تم",
    "محول",
    "محولة",
}

API_KEY = os.getenv("NOTION_API_KEY") or os.getenv("NOTION_TOKEN")

assert API_KEY, "🚫 أضف NOTION_API_KEY"
assert TOTALS_DB_ID_ENV, "🚫 أضف EMP_TOTALS_DB_ID"

HDRS = {
    "Authorization": f"Bearer {API_KEY}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

SESSION = requests.Session()
SESSION.headers.update(HDRS)


def hyphenate(nid: str) -> str:
    if not nid:
        return nid

    nid = nid.strip()

    if "-" in nid:
        return nid

    if re.fullmatch(r"[0-9a-fA-F]{32}", nid):
        return (
            f"{nid[:8]}-"
            f"{nid[8:12]}-"
            f"{nid[12:16]}-"
            f"{nid[16:20]}-"
            f"{nid[20:]}"
        )

    return nid


PROJECTS_DB_ID = hyphenate(RAW_PROJECTS_DB_ID)
TOTALS_DB_ID = hyphenate(TOTALS_DB_ID_ENV)


def _request(method, url, **kwargs):
    last = None

    for attempt in range(4):
        try:
            response = SESSION.request(
                method,
                url,
                timeout=DEFAULT_TIMEOUT,
                **kwargs
            )

            if response.status_code in (429, 500, 502, 503, 504):
                time.sleep(0.5 * (attempt + 1))
                continue

            if not response.ok:
                print("❌ Notion Error:")
                print(response.text)

            response.raise_for_status()
            return response

        except requests.RequestException as e:
            last = e
            time.sleep(0.5 * (attempt + 1))

    raise last


def http_get(url, params=None):
    return _request("GET", url, params=params)


def http_post(url, json=None):
    return _request("POST", url, json=json or {})


def http_patch(url, json=None):
    return _request("PATCH", url, json=json or {})


def retrieve_database(db_id):
    return http_get(
        f"https://api.notion.com/v1/databases/{hyphenate(db_id)}"
    ).json()


def query_database_pages(db_id, page_size=100, filter_body=None):
    url = f"https://api.notion.com/v1/databases/{hyphenate(db_id)}/query"

    results = []
    cursor = None

    while True:
        body = {
            "page_size": min(page_size, 100)
        }

        if cursor:
            body["start_cursor"] = cursor

        if filter_body:
            body["filter"] = filter_body

        data = http_post(url, body).json()

        results.extend(data.get("results", []))

        if not data.get("has_more"):
            break

        cursor = data.get("next_cursor")
        time.sleep(SLEEP)

    return results


def list_block_children_all(block_id):
    url = f"https://api.notion.com/v1/blocks/{hyphenate(block_id)}/children"

    results = []
    cursor = None

    while True:
        params = {}

        if cursor:
            params["start_cursor"] = cursor

        data = http_get(url, params=params).json()

        results.extend(data.get("results", []))

        if not data.get("has_more"):
            break

        cursor = data.get("next_cursor")
        time.sleep(SLEEP)

    return results


def title_from_page(page):
    for prop in (page.get("properties") or {}).values():
        if prop.get("type") == "title":
            arr = prop.get("title", [])

            if arr:
                return arr[0].get("plain_text", "").strip()

    return "(بدون عنوان)"


def find_team_db_id_in_project_page(page_id):
    for block in list_block_children_all(page_id):
        if block.get("type") == "child_database":
            title = (
                block.get("child_database") or {}
            ).get("title", "").strip()

            if title == TEAM_DB_NAME:
                return block.get("id")

    return None


def score_name(name, keywords):
    name = (name or "").lower()
    return sum(1 for keyword in keywords if keyword in name)


def detect_team_schema(team_db_id):
    db = retrieve_database(team_db_id)

    props = db.get("properties", {}) or {}

    people = [
        n for n, m in props.items()
        if m.get("type") == "people"
    ]

    relation = [
        n for n, m in props.items()
        if m.get("type") == "relation"
    ]

    textlike = [
        n for n, m in props.items()
        if m.get("type") in ("title", "rich_text")
    ]

    numbery = [
        n for n, m in props.items()
        if m.get("type") in ("number", "formula", "rollup")
    ]

    statusy = [
        n for n, m in props.items()
        if m.get("type") in ("select", "status", "rich_text")
    ]

    emp_key = (
        max(
            people,
            key=lambda n: (
                score_name(n, EMP_KEYWORDS),
                len(n)
            )
        )
        if people else
        max(
            relation,
            key=lambda n: (
                score_name(n, EMP_KEYWORDS),
                len(n)
            )
        )
        if relation else
        max(
            textlike,
            key=lambda n: (
                score_name(n, EMP_KEYWORDS),
                len(n)
            )
        )
        if textlike else
        None
    )

    amount_key = (
        max(
            numbery,
            key=lambda n: (
                score_name(n, AMOUNT_KEYWORDS),
                len(n)
            )
        )
        if numbery else
        None
    )

    status_key = (
        max(
            statusy,
            key=lambda n: (
                score_name(n, STATUS_KEYWORDS),
                len(n)
            )
        )
        if statusy else
        None
    )

    return emp_key, amount_key, status_key


def extract_textlike(prop):
    if not prop:
        return ""

    prop_type = prop.get("type")

    if prop_type == "title":
        arr = prop.get("title", [])

    elif prop_type == "rich_text":
        arr = prop.get("rich_text", [])

    else:
        return ""

    return (
        arr[0].get("plain_text", "").strip()
        if arr else ""
    )


def extract_amount(prop):
    if not prop:
        return 0.0

    prop_type = prop.get("type")

    if prop_type == "number":
        return float(prop.get("number") or 0)

    if prop_type == "formula":
        formula = prop.get("formula") or {}

        if formula.get("type") == "number":
            return float(formula.get("number") or 0)

    if prop_type == "rollup":
        rollup = prop.get("rollup") or {}

        if rollup.get("type") == "number":
            return float(rollup.get("number") or 0)

    return 0.0


def extract_employee_name(prop):
    if not prop:
        return ""

    prop_type = prop.get("type")

    if prop_type == "people":
        people = prop.get("people", [])

        return (
            people[0].get("name", "").strip()
            if people else ""
        )

    if prop_type in ("title", "rich_text"):
        return extract_textlike(prop)

    return ""


def extract_status_label(prop):
    if not prop:
        return ""

    prop_type = prop.get("type")

    if prop_type in ("select", "status"):
        return (
            prop.get(prop_type) or {}
        ).get("name", "").strip()

    if prop_type in ("title", "rich_text"):
        return extract_textlike(prop)

    return ""


def is_transferred(label):
    return (
        (label or "").strip().lower()
        in TRANSFER_TRUE_VALUES
    )


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

    missing = [
        prop for prop in required
        if prop not in props
    ]

    if missing:
        raise ValueError(
            f"🚫 الأعمدة غير موجودة: {missing}"
        )

    print("✅ تم التحقق من الأعمدة")


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
        filter_body=filter_body
    )

    return rows[0]["id"] if rows else None


def build_properties(employee_name, data):
    transferred_amount = float(data["transferred_amount"])
    not_transferred_amount = float(data["not_transferred_amount"])

    total_amount = (
        transferred_amount +
        not_transferred_amount
    )

    project_count = len(data["all_projects"])

    transferred_projects = "، ".join(
        sorted(data["transferred_projects"])
    )

    not_transferred_projects = "، ".join(
        sorted(data["not_transferred_projects"])
    )

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
                        "content": transferred_projects[:1900]
                    }
                }
            ]
        },

        NOT_TRANSFERRED_PROJECTS_PROP: {
            "rich_text": [
                {
                    "type": "text",
                    "text": {
                        "content": not_transferred_projects[:1900]
                    }
                }
            ]
        },
    }


def create_employee_row(employee_name, data):
    body = {
        "parent": {
            "database_id": TOTALS_DB_ID
        },

        "properties": build_properties(
            employee_name,
            data
        )
    }

    return http_post(
        "https://api.notion.com/v1/pages",
        body
    ).json()


def update_employee_row(page_id, employee_name, data):
    body = {
        "properties": build_properties(
            employee_name,
            data
        )
    }

    return http_patch(
        f"https://api.notion.com/v1/pages/{hyphenate(page_id)}",
        body
    ).json()


def upsert_employee(employee_name, data):
    existing_page_id = find_employee_row(employee_name)

    if existing_page_id:
        update_employee_row(
            existing_page_id,
            employee_name,
            data
        )
        return "updated"

    create_employee_row(
        employee_name,
        data
    )

    return "created"


def collect_totals():
    totals = defaultdict(lambda: {
        "transferred_amount": 0.0,
        "not_transferred_amount": 0.0,
        "all_projects": set(),
        "transferred_projects": set(),
        "not_transferred_projects": set(),
    })

    projects = query_database_pages(PROJECTS_DB_ID)

    print(f"📦 عدد المشاريع: {len(projects)}")

    for index, project_page in enumerate(projects, 1):
        project_id = project_page["id"]

        project_name = title_from_page(project_page)

        print(f"\n[{index}] {project_name}")

        team_db_id = find_team_db_id_in_project_page(project_id)

        if not team_db_id:
            print("  ⚠️ لا يوجد فريق مشروع")
            continue

        emp_key, amount_key, status_key = detect_team_schema(team_db_id)

        if not emp_key or not amount_key:
            print("  ⚠️ تعذر اكتشاف الأعمدة")
            continue

        rows = query_database_pages(team_db_id)

        print(f"  👥 الصفوف: {len(rows)}")

        for row in rows:
            props = row.get("properties", {}) or {}

            employee_name = extract_employee_name(
                props.get(emp_key)
            )

            amount = extract_amount(
                props.get(amount_key)
            )

            if not employee_name:
                continue

            raw_status = (
                extract_status_label(
                    props.get(status_key)
                )
                if status_key else ""
            )

            transferred = is_transferred(raw_status)

            totals[employee_name]["all_projects"].add(
                project_name
            )

            if transferred:
                totals[employee_name]["transferred_amount"] += amount

                totals[employee_name][
                    "transferred_projects"
                ].add(project_name)

                status_text = "محول"

            else:
                totals[employee_name]["not_transferred_amount"] += amount

                totals[employee_name][
                    "not_transferred_projects"
                ].add(project_name)

                status_text = "غير محول"

            print(
                f"    ✓ {employee_name} | "
                f"{amount} | "
                f"{status_text}"
            )

            time.sleep(SLEEP)

    return totals


def update_totals_database():
    print("⏳ بدء تحديث جدول التجميع...")

    validate_totals_database()

    totals = collect_totals()

    print(f"\n👥 عدد الموظفين: {len(totals)}")

    created_count = 0
    updated_count = 0

    for employee_name, data in totals.items():
        action = upsert_employee(
            employee_name,
            data
        )

        if action == "created":
            created_count += 1
        else:
            updated_count += 1

        total_amount = (
            data["transferred_amount"] +
            data["not_transferred_amount"]
        )

        print(
            f"✓ {employee_name} | "
            f"المجموع: {total_amount} | "
            f"{action}"
        )

        time.sleep(SLEEP)

    print("\n✅ انتهى التحديث")
    print(f"🟢 جديد: {created_count}")
    print(f"🟡 محدث: {updated_count}")


if __name__ == "__main__":
    update_totals_database()
