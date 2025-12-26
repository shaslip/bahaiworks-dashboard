import os
import requests
from dotenv import load_dotenv

load_dotenv()

API_URL = 'https://bahaidata.org/api.php'
# Using Wikibase credentials
WB_USER = os.getenv("WIKI_USERNAME")
WB_PASS = os.getenv("WIKI_PASSWORD")

def get_csrf_token(session):
    if not WB_USER or not WB_PASS:
        raise ValueError("Missing WIKI_USERNAME or WIKI_PASSWORD")

    # 1. Login Token
    login_token_resp = session.get(API_URL, params={
        'action': 'query', 'meta': 'tokens', 'type': 'login', 'format': 'json'
    })
    login_token = login_token_resp.json()['query']['tokens']['logintoken']

    # 2. Login
    login_resp = session.post(API_URL, data={
        'action': 'login', 'lgname': WB_USER, 'lgpassword': WB_PASS, 
        'lgtoken': login_token, 'format': 'json'
    })
    if login_resp.json().get('login', {}).get('result') != "Success":
        raise PermissionError(f"Wikibase Login failed")

    # 3. CSRF Token
    csrf_resp = session.get(API_URL, params={
        'action': 'query', 'meta': 'tokens', 'format': 'json'
    })
    return csrf_resp.json()['query']['tokens']['csrftoken']

def set_sitelink(item_id, page_title, site_id='works'):
    """
    Links a Wikibase Item (item_id) to a MediaWiki Page (page_title).
    """
    session = requests.Session()
    try:
        csrf_token = get_csrf_token(session)
        
        params = {
            'action': 'wbsetsitelink',
            'id': item_id,
            'linksite': site_id,
            'linktitle': page_title,
            'token': csrf_token,
            'format': 'json'
        }
        response = session.post(API_URL, data=params)
        data = response.json()
        
        if 'error' in data:
            raise Exception(data['error']['info'])
            
        return True, f"Linked {item_id} -> {page_title}"
        
    except Exception as e:
        return False, str(e)
