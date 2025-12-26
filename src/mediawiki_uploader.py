import os
import requests
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

API_URL = 'https://bahai.works/api.php'
BW_USER = os.getenv("WIKI_USERNAME")
BW_PASS = os.getenv("WIKI_PASSWORD")

def get_csrf_token(session):
    """
    Authenticates with MediaWiki and retrieves a CSRF token.
    """
    if not BW_USER or not BW_PASS:
        raise ValueError("Missing WIKI_USERNAME or WIKI_PASSWORD in .env")

    # 1. Get Login Token
    login_token_response = session.get(API_URL, params={
        'action': 'query',
        'meta': 'tokens',
        'type': 'login',
        'format': 'json'
    })
    login_token = login_token_response.json()['query']['tokens']['logintoken']

    # 2. Perform Login
    login_response = session.post(API_URL, data={
        'action': 'login',
        'lgname': BW_USER,
        'lgpassword': BW_PASS,
        'lgtoken': login_token,
        'format': 'json'
    })
    
    login_data = login_response.json()
    if login_data.get('login', {}).get('result') != "Success":
        raise PermissionError(f"Login failed: {login_data}")

    # 3. Get CSRF Token
    csrf_token_response = session.get(API_URL, params={
        'action': 'query',
        'meta': 'tokens',
        'format': 'json'
    })
    return csrf_token_response.json()['query']['tokens']['csrftoken']

def page_exists(session, title):
    """
    Checks if a page exists on the wiki.
    """
    params = {
        'action': 'query',
        'titles': title,
        'format': 'json'
    }
    response = session.get(API_URL, params=params)
    data = response.json()
    
    # MediaWiki returns a negative pageid (e.g., "-1") if the page is missing
    pages = data.get('query', {}).get('pages', {})
    for page_id in pages:
        if int(page_id) < 0:
            return False
    return True

def upload_to_bahaiworks(title, content, summary="Bot upload via Dashboard", check_exists=False):
    """
    Uploads text to a specific page on bahai.works.
    Returns the API response.
    
    Args:
        title (str): Page title
        content (str): Wiki text
        summary (str): Edit summary
        check_exists (bool): If True, raises FileExistsError if page already exists.
    """
    session = requests.Session()
    
    try:
        # Authenticate
        csrf_token = get_csrf_token(session)
        
        # Safety Check: Page Existence
        if check_exists:
            if page_exists(session, title):
                raise FileExistsError(f"Page '{title}' already exists on bahai.works. Operation aborted.")
        
        # Post Edit
        create_params = {
            'action': 'edit',
            'title': title,
            'text': content,
            'summary': summary,
            'token': csrf_token,
            'format': 'json'
        }
        response = session.post(API_URL, data=create_params)
        data = response.json()
        
        if 'error' in data:
            raise Exception(data['error']['info'])
            
        return data
        
    except Exception as e:
        # Pass through the FileExistsError or wrap other exceptions
        raise e
