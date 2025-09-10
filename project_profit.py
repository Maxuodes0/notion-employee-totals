# debug_notion_access.py
# Script to debug Notion database access issues

import os, re, requests

# Your database ID (as it appears in your code)
RAW_PROJECTS_DB_ID = "23e6fe2a5e8e8003a6bfcf99ae01ba0c"

# Notion API setup
API_KEY = os.getenv("NOTION_API_KEY") or os.getenv("NOTION_TOKEN")
HDRS = {
    "Authorization": f"Bearer {API_KEY}",
    "Notion-Version": os.getenv("NOTION_VERSION", "2022-06-28"),
    "Content-Type": "application/json",
}

def hyphenate(nid: str) -> str:
    nid = nid.strip()
    if "-" in nid: return nid
    if re.fullmatch(r"[0-9a-fA-F]{32}", nid):
        return f"{nid[0:8]}-{nid[8:12]}-{nid[12:16]}-{nid[16:20]}-{nid[20:32]}"
    return nid

def test_database_access():
    print("🔍 Testing Notion database access...")
    print(f"Raw DB ID: {RAW_PROJECTS_DB_ID}")
    
    # Test the hyphenation function
    hyphenated_id = hyphenate(RAW_PROJECTS_DB_ID)
    print(f"Hyphenated ID: {hyphenated_id}")
    
    # Check API key
    if not API_KEY:
        print("❌ ERROR: No API key found. Set NOTION_API_KEY or NOTION_TOKEN environment variable.")
        return
    
    print(f"✅ API Key found: {API_KEY[:10]}...")
    
    # Test 1: Try to retrieve the database
    print("\n📋 Test 1: Retrieving database...")
    try:
        url = f"https://api.notion.com/v1/databases/{hyphenated_id}"
        print(f"URL: {url}")
        response = requests.get(url, headers=HDRS, timeout=30)
        print(f"Status Code: {response.status_code}")
        
        if response.status_code == 200:
            data = response.json()
            print("✅ Database retrieved successfully!")
            print(f"Title: {data.get('title', [{}])[0].get('plain_text', '(No title)')}")
        else:
            print(f"❌ Failed to retrieve database: {response.status_code}")
            print(f"Response: {response.text}")
            
    except Exception as e:
        print(f"❌ Exception during database retrieval: {e}")
    
    # Test 2: Try querying the database
    print("\n📊 Test 2: Querying database...")
    try:
        url = f"https://api.notion.com/v1/databases/{hyphenated_id}/query"
        print(f"URL: {url}")
        response = requests.post(url, headers=HDRS, json={"page_size": 1}, timeout=30)
        print(f"Status Code: {response.status_code}")
        
        if response.status_code == 200:
            data = response.json()
            print("✅ Database query successful!")
            print(f"Number of results: {len(data.get('results', []))}")
        else:
            print(f"❌ Failed to query database: {response.status_code}")
            print(f"Response: {response.text}")
            
    except Exception as e:
        print(f"❌ Exception during database query: {e}")
    
    # Test 3: List all accessible databases (if possible)
    print("\n🔍 Test 3: Testing API connectivity...")
    try:
        # Try a simple API call to test connectivity
        response = requests.post("https://api.notion.com/v1/search", 
                               headers=HDRS, 
                               json={"query": "", "filter": {"value": "database", "property": "object"}}, 
                               timeout=30)
        print(f"Search API Status Code: {response.status_code}")
        
        if response.status_code == 200:
            data = response.json()
            print(f"✅ API is working! Found {len(data.get('results', []))} databases")
            print("Accessible databases:")
            for db in data.get('results', [])[:5]:  # Show first 5
                title = db.get('title', [{}])[0].get('plain_text', '(No title)')
                db_id = db.get('id', '')
                print(f"  - {title} (ID: {db_id})")
        else:
            print(f"❌ API connectivity issue: {response.status_code}")
            print(f"Response: {response.text}")
            
    except Exception as e:
        print(f"❌ Exception during API test: {e}")

if __name__ == "__main__":
    test_database_access()
