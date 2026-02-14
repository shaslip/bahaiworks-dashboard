import os
import re
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

def fetch_wikitext(title, session=None):
    """
    Fetches the absolute latest revision.
    Args:
        title (str): Page title
        session (requests.Session, optional): Authenticated session. 
         If None, uses anonymous requests.
    """
    try:
        headers = {"User-Agent": "BahaiWorksBot/1.0"}
        params = {
            "action": "query",
            "prop": "revisions",
            "titles": title,
            "rvprop": "content",
            "format": "json",
            "rvslots": "main"
        }
        
        # Use provided session (authenticated) or fallback to generic requests
        requester = session if session else requests
        response = requester.get(API_URL, params=params, headers=headers, timeout=10)
        data = response.json()
        
        pages = data.get('query', {}).get('pages', {})
        for pid in pages:
            if pid == "-1":
                # Special handling for restricted pages if we are anonymous
                if "badrevids" in data.get("query", {}):
                     return None, f"Page '{title}' exists but content is hidden (Auth required)."
                return None, f"Page '{title}' does not exist (ID -1)."
            
            # Check if content is actually returned (it won't be if restricted and anonymous)
            try:
                return pages[pid]['revisions'][0]['slots']['main']['*'], None
            except KeyError:
                return None, f"Content hidden or permission denied for '{title}'"
            
    except Exception as e:
        return None, str(e)
    
    return None, "Unknown Error"

def inject_text_into_page(wikitext, page_num, new_content, pdf_filename="File.pdf"):
    """
    Surgically replaces content FOLLOWING {{page|X...}} tag.
    Auto-removes any {{ocr}} tags found in the text.
    Preserves {{BN_header...}} templates if found immediately after the page tag.
    """
    # 0. Global Cleanup: Remove {{ocr}} tags anywhere they exist
    wikitext = re.sub(r'\{\{ocr.*?\}\}\n?', '', wikitext, flags=re.IGNORECASE)

    # 1. Try to find the existing tag
    pattern_tag_start = re.compile(r'\{\{page\s*\|\s*' + str(page_num) + r'(?:\||\}\})', re.IGNORECASE)
    match = pattern_tag_start.search(wikitext)
    
    if match:
        # --- EXISTING PAGE LOGIC ---
        tag_start_index = match.start()
        tag_end_index = wikitext.find("}}", tag_start_index)
        
        if tag_end_index == -1:
             return None, f"Malformed tag: {{page|{page_num}}} has no closing '}}'."
             
        # Content normally starts after closing }}
        content_start_pos = tag_end_index + 2
        
        # --- PRESERVATION LOGIC ---
        # Check if a header template (like {{BN_header_...}}) follows immediately
        remaining_text = wikitext[content_start_pos:]
        header_match = re.match(r'^\s*\{\{BN_header_.*?\}\}', remaining_text, re.DOTALL | re.IGNORECASE)
        
        if header_match:
            content_start_pos += header_match.end()

        # Find start of NEXT tag to define end of content
        pattern_next = re.compile(r'\{\{page\s*\|')
        match_next = pattern_next.search(wikitext, content_start_pos)
        
        content_end_pos = match_next.start() if match_next else len(wikitext)
        
        # Splice
        new_wikitext = wikitext[:content_start_pos] + "\n" + new_content.strip() + "\n" + wikitext[content_end_pos:]
        return new_wikitext, None

    else:
        # --- NEW PAGE APPEND LOGIC ---
        # The tag doesn't exist. Append to end.
        new_tag = f"{{{{page|{page_num}|file={pdf_filename}|page={page_num}}}}}"
        
        if not wikitext.endswith("\n"):
            wikitext += "\n"
            
        new_wikitext = wikitext + "\n" + new_tag + "\n" + new_content.strip()
        
        return new_wikitext, None

def generate_header(current_issue_num, year=None, volume=None):
    """
    Generates the MediaWiki {{header}} template.
    Handles both Standard Issues (Issue 01) and Volume/Issue structures.
    """
    try:
        # --- 1. Calculate Prev/Next Math ---
        # (This preserves your original logic for ranges like "04-01")
        if '-' in str(current_issue_num):
            parts = str(current_issue_num).split('-')
            start_num = int(parts[0])
            end_num = int(parts[-1])
            curr_display = current_issue_num
            prev_num = start_num - 1
            next_num = end_num + 1
        else:
            curr = int(current_issue_num)
            curr_display = str(curr)
            prev_num = curr - 1
            next_num = curr + 1
        
        # --- 2. Determine Depth & Section Title ---
        if volume:
            # VOLUME MODE: Deep linking
            title_link = "[[../../../]]"  # Go up 3 levels (Text -> Issue -> Vol -> Base)
            section_display = f"Volume {volume}, Issue {curr_display}"
            
            # For Volume/Issue, siblings are still usually ../../Issue_X if inside the Volume folder
            # Adjust this prefix if your folders are named "No 1" instead of "Issue 1"
            link_prefix = "../../Issue_" 
        else:
            # STANDARD MODE
            title_link = "[[../../]]"     # Go up 2 levels (Text -> Issue -> Base)
            section_display = f"Issue {curr_display}"
            link_prefix = "../../Issue "

        # --- 3. Construct Links ---
        # Note: We use the calculated numbers. 
        # For Volumes, this works for issues within the same volume (1 -> 2).
        # It does NOT automatically handle Volume rollover (Vol 1 Issue 12 -> Vol 2 Issue 1).
        prev_link = f"[[{link_prefix}{prev_num}/Text|Previous]]" if prev_num > 0 else ""
        next_link = f"[[{link_prefix}{next_num}/Text|Next]]"
        
        cat_str = str(year) if year else ""

        # --- 4. Build Template ---
        header = f"""{{{{header
 | title      = {title_link}
 | author     = 
 | translator = 
 | section    = {section_display}
 | previous   = {prev_link}
 | next       = {next_link}
 | notes      = {{{{bnreturn}}}}{{{{ps|1}}}}
 | categories = {cat_str}
}}}}"""
        return header

    except ValueError:
        return ""

def cleanup_page_seams(wikitext):
    """
    Fixes text artifacts at page boundaries safely.
    """
    # 1. Fix Hyphenated Words (word- \n {{page}} \n suffix)
    wikitext = re.sub(
        r'([a-zA-Z]+)-\s*\n\s*(\{\{page\|[^}]+\}\})\s*\n\s*([a-z]+)',
        r'\2\1\3',
        wikitext
    )

    # 2. Fix Sentence Flow ({{page}} \n Word)
    wikitext = re.sub(
        r'(\{\{page\|[^}]+\}\})\n(?![{|!=*#])',
        r'\1',
        wikitext
    )
    
    return wikitext

def upload_to_bahaiworks(title, content, summary="Bot upload", check_exists=False, session=None):
    """
    Uploads text to a specific page on bahai.works.
    Returns the API response.

    Args:
        title (str): Page title
        content (str): Wiki text
        summary (str): Edit summary
        check_exists (bool): If True, raises FileExistsError if page already exists.
    """
    local_session = False
    if session is None:
        session = requests.Session()
        local_session = True
    
    try:
        # Authenticate if we are using a local/new session
        # OR if the passed session doesn't have a token yet? 
        # Actually, get_csrf_token handles the login flow if needed.
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
        raise e
    finally:
        # Only close if we created it here
        if local_session:
            session.close()

def update_header_ps_tag(wikitext):
    """
    Updates the {{header}} template to ensure {{ps|1}} is present in the notes parameter.
    - If {{ps|x}} exists (e.g. 0), updates to {{ps|1}}.
    - If {{ps|x}} is missing, appends {{ps|1}} to notes.
    - If | notes = is missing, adds it.
    """
    # 1. Find the header block: {{header ... }}
    # Captures: 1=StartTag, 2=Body, 3=EndTag
    header_pattern = re.compile(r'(\{\{header\s*\n)(.*?)(\n\}\})', re.DOTALL | re.IGNORECASE)
    match = header_pattern.search(wikitext)
    
    if not match:
        return wikitext # No header to update

    start_tag, body, end_tag = match.groups()

    def _process_notes(m):
        prefix = m.group(1) # "| notes = "
        value = m.group(2)  # Current value
        
        # Check for existing ps tag (handles {{ps|0}}, {{ps|1}}, {{ps| 0 }}, etc)
        if re.search(r'\{\{ps\|\s*\d+\s*\}\}', value, re.IGNORECASE):
            # Update existing to 1
            new_value = re.sub(r'\{\{ps\|\s*\d+\s*\}\}', '{{ps|1}}', value, flags=re.IGNORECASE)
        else:
            # Append ps|1 (safe append)
            new_value = value.rstrip() + "{{ps|1}}"
        
        return f"{prefix}{new_value}"

    # 2. Find/Update the notes parameter
    # Matches "| notes =" followed by content until the next pipe or end of string
    notes_pattern = re.compile(r'(\|\s*notes\s*=\s*)(.*?)(?=\n\s*\||$)', re.DOTALL | re.IGNORECASE)
    
    if notes_pattern.search(body):
        # Modify existing notes param
        new_body = notes_pattern.sub(_process_notes, body)
    else:
        # Notes param missing. Insert it.
        # Try to insert before | categories for cleanliness
        if re.search(r'\|\s*categories', body, re.IGNORECASE):
             new_body = re.sub(
                 r'(\|\s*categories)', 
                 r'| notes      = {{ps|1}}\n\1', 
                 body, 
                 flags=re.IGNORECASE
             )
        else:
             # Fallback: Append to the end of the body
             new_body = body.rstrip() + "\n | notes      = {{ps|1}}"

    # 3. Reconstruct
    new_wikitext = wikitext.replace(match.group(0), f"{start_tag}{new_body}{end_tag}")
    return new_wikitext
