def query_database_pages(db_id, page_size=100, limit=None, filter_payload=None):
    """Fixed version of the query function"""
    url = f"https://api.notion.com/v1/databases/{db_id}/query"
    res, cursor = [], None
    
    while True:
        # Build the request body more carefully
        body = {"page_size": min(page_size, 100)}  # Notion max is 100
        
        if cursor: 
            body["start_cursor"] = cursor
            
        # Only add filter if it's properly structured
        if filter_payload and isinstance(filter_payload, dict):
            if "filter" in filter_payload:
                body["filter"] = filter_payload["filter"]
            if "sorts" in filter_payload:
                body["sorts"] = filter_payload["sorts"]
        
        print(f"📤 Sending request to: {url}")
        print(f"📦 Body: {body}")
        
        try:
            response = requests.post(url, headers=HDRS, json=body, timeout=DEFAULT_TIMEOUT)
            print(f"📨 Response status: {response.status_code}")
            
            if response.status_code != 200:
                print(f"❌ Response text: {response.text}")
                response.raise_for_status()
            
            data = response.json()
            results = data.get("results", [])
            res.extend(results)
            
            print(f"📊 Got {len(results)} results (total so far: {len(res)})")
            
            if limit and len(res) >= limit: 
                return res[:limit]
                
            if not data.get("has_more"): 
                break
                
            cursor = data.get("next_cursor")
            time.sleep(SLEEP)
            
        except requests.exceptions.RequestException as e:
            print(f"❌ Request failed: {e}")
            if hasattr(e, 'response') and e.response is not None:
                print(f"❌ Response status: {e.response.status_code}")
                print(f"❌ Response text: {e.response.text}")
            raise
    
    return res

# Alternative simple version - try this if the above doesn't work
def simple_query_database_pages(db_id, page_size=100):
    """Simplified version with minimal payload"""
    url = f"https://api.notion.com/v1/databases/{db_id}/query"
    
    try:
        # Try with completely empty body first
        response = requests.post(url, headers=HDRS, json={}, timeout=DEFAULT_TIMEOUT)
        
        if response.status_code == 200:
            data = response.json()
            return data.get("results", [])
        else:
            print(f"❌ Empty body failed: {response.status_code} - {response.text}")
            
            # Try with minimal body
            response = requests.post(url, headers=HDRS, json={"page_size": page_size}, timeout=DEFAULT_TIMEOUT)
            response.raise_for_status()
            data = response.json()
            return data.get("results", [])
            
    except Exception as e:
        print(f"❌ Query failed: {e}")
        raise
