import os
import requests
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

API_URL = 'https://bahai.works/api.php'
BW_USER = os.getenv("BAHAIWORKS_USER")
BW_PASS = os.getenv("BAHAIWORKS_PASSWORD")

def get_csrf_token(session):
    """
    Authenticates with MediaWiki and retrieves a CSRF token.
    """
    if not BW_USER or not BW_PASS:
        raise ValueError("Missing BAHAIWORKS_USER or BAHAIWORKS_PASSWORD in .env")

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

def upload_to_bahaiworks(title, content, summary="Bot upload via Dashboard"):
    """
    Uploads text to a specific page on bahai.works.
    Returns the API response.
    """
    session = requests.Session()
    
    try:
        # Authenticate
        csrf_token = get_csrf_token(session)
        
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
        raise Exception(f"Upload failed: {e}")
