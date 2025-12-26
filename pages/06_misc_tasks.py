
import streamlit as st
import re
import json
import os
import time
import requests
import urllib.parse
import pandas as pd
from src.mediawiki_uploader import upload_to_bahaiworks
from src.sitelink_manager import set_sitelink
from src.wikibase_importer import get_or_create_author

st.set_page_config(
    page_title="Bahai.works Utilities",
    layout="wide"
)

st.title("🛠️ Miscellaneous Utilities")

# --- HELP SECTION ---
with st.expander("ℹ️ Help / Instructions"):
    st.markdown("""
    **1. Create Author Pages**
    * **Batch Creation:** Paste a list of author names (comma-separated) to create their pages in bulk.
    * **Modes:**
        * *Dynamic:* Uses a Lua module to automatically list all chapters by this author in the works of others.
        * *Static:* Hard-codes a link to a specific book. Use this for authors with a single major work.
    * **Bahaidata:** This tool automatically creates the Bahaidata item for the author (if missing) and links it to the new Bahai.works page.

    **2. AC Messages**
    * **Copyright AC-Message:** Generates the special `/AC-Message` subpage required for copyright-protected books.
    * **Usage:** Enter the exact Book Title (Page Name) and the cover image filename.

    **3. Update Author list**
    * **Update Authors Index:** Scans all `Category:Authors-A` through `Z` and rebuilds the main [[Authors]] index page. Run this periodically if the main index feels out of date.
    """)

st.markdown("---")

# --- TABS ---
tab_create_author, tab_ac, tab_update_author, tab_maintenance = st.tabs([
    "👤 Create Author Pages", 
    "📖 AC Messages", 
    "📝 Update Author list",
    "🔧 Maintenance"
])

# --- Author page maintenance and exclusions ---
AUTHORS_PAGE_HEADER = """{{header
 | title      = Authors
 | author     =
 | translator =
 | section    = 
 | previous   = 
 | next       = 
 | notes      = Provided below is an incomplete, but growing list of Bahá’í authors and their works. Also included are authors who have published articles in books or periodicals such as [[World Order]]. 
}}
{| class="wikitable" style="float:right; margin-left: 10px;"
! Number of listed authors
|-
| style="text-align:center;" | {{#expr: {{PAGESINCATEGORY:Authors-A}} + {{PAGESINCATEGORY:Authors-B}} + {{PAGESINCATEGORY:Authors-C}} + {{PAGESINCATEGORY:Authors-D}} + {{PAGESINCATEGORY:Authors-E}} + {{PAGESINCATEGORY:Authors-F}} + {{PAGESINCATEGORY:Authors-G}} + {{PAGESINCATEGORY:Authors-H}} + {{PAGESINCATEGORY:Authors-I}} + {{PAGESINCATEGORY:Authors-J}} + {{PAGESINCATEGORY:Authors-K}} + {{PAGESINCATEGORY:Authors-L}} + {{PAGESINCATEGORY:Authors-M}} + {{PAGESINCATEGORY:Authors-N}} + {{PAGESINCATEGORY:Authors-O}} + {{PAGESINCATEGORY:Authors-P}} + {{PAGESINCATEGORY:Authors-Q}} + {{PAGESINCATEGORY:Authors-R}} + {{PAGESINCATEGORY:Authors-S}} + {{PAGESINCATEGORY:Authors-T}} + {{PAGESINCATEGORY:Authors-U}} + {{PAGESINCATEGORY:Authors-V}} + {{PAGESINCATEGORY:Authors-W}} + {{PAGESINCATEGORY:Authors-X}} + {{PAGESINCATEGORY:Authors-Y}} + {{PAGESINCATEGORY:Authors-Z}} }}
|}
* [[Author:Bahá’u’lláh|Bahá’u’lláh]]
* [[Author:The Báb|The Báb]]
* [[Author:‘Abdu’l-Bahá|‘Abdu’l-Bahá]]
* [[Author:Shoghi Effendi|Shoghi Effendi]]
* [[Author:Universal House of Justice|Universal House of Justice]]
* [[Author:Institutional authors|Other institutional authors]]


{{CompactTOC}}
"""

EXCLUSION_LIST = [
    "Author:‘Abdu’l-Bahá", "Author:Association for Bahá’í Studies North America", "Author:The Báb",
    "Author:Bahá’u’lláh", "Author:Bahá’í Canada Publications", "Author:Bahá’í International Community",
    "Author:Bahá’í Publishing Trust, United States", "Author:Bahá’í World Centre",
    "Author:Canadian Bahá’í Community National Office", "Author:Child Education Committee",
    "Author:Shoghi Effendi", "Author:Steve Gregg", "Author:Marriage and Family Development Committee",
    "Author:National Bahá’í Women’s group", "Author:National Bahá’í Youth Committee, United States",
    "Author:National Community Development Committee", "Author:National Education Committee, United States",
    "Author:National Programming Committee", "Author:National Reference Library Committee",
    "Author:NSA, United Kingdom", "Author:National Spiritual Assembly of the United States",
    "Author:National Spiritual Assembly of the British Isles", "Author:National Teaching Committee, United States",
    "Author:Office of Public Information", "Author:Office of Social and Economic Development",
    "Author:Office of the Treasurer, United States", "Author:Charles Mason Remey", "Author:Research Department",
    "Author:Study Aids Committee, United States", "Author:Study Outline Committee, United States",
    "Author:NSA, Australia", "Author:NSA, British Isles", "Author:NSA, United States",
    "Author:NSA, United States and Canada", "Author:NSA, Iran", "Author:NSA, South Africa",
    "Author:Universal House of Justice"
]

# --- HELPER FUNCTIONS ---

def get_lastname_firstname(full_name):
    """
    Parses names into 'Lastname, Firstname' format.
    
    1. Handles suffixes (Jr., Sr., III) -> 'Trafton, Jr., Burton'
    2. Handles particles (de, van, von) -> 'de Araujo, Victor'
    """
    # 1. Clean and Split
    name = full_name.strip()
    parts = name.split()
    
    if len(parts) <= 1:
        return name

    # 2. Extract Suffixes (Case-insensitive check)
    suffixes = ['jr', 'jr.', 'sr', 'sr.', 'ii', 'iii', 'iv', 'v', 'vi']
    suffix = ""
    
    # Check the last word to see if it's a suffix
    last_word_norm = parts[-1].lower().replace(',', '').replace('.', '')
    
    # We strip '.' from the check list above to match "Jr." or "Jr"
    if last_word_norm in [s.replace('.', '') for s in suffixes]:
        suffix = parts[-1].replace(',', '') # Store the suffix (e.g. "Jr.")
        parts = parts[:-1] # Remove suffix from the working list
        # Clean any trailing comma from the new last word (e.g., "Gulick," -> "Gulick")
        parts[-1] = parts[-1].rstrip(',')

    # 3. Handle Connectors/Particles
    # These are words that signal the start of a Last Name
    connectors = {"de", "dos", "da", "do", "von", "van", "den", "der"}
    
    split_index = -1
    
    # Find the *first* occurrence of a connector to start the Last Name there
    for i, part in enumerate(parts):
        # We generally assume a connector won't be the very first name (Index 0)
        # unless it's a mononym, but for safety we check i > 0 usually.
        # However, "De Man" is a valid name. Let's stick to your logic:
        if part.lower() in connectors:
            split_index = i
            break
    
    if split_index > 0: 
        # Case: "Victor [de] Araujo"
        firstname_part = " ".join(parts[:split_index])
        lastname_part = " ".join(parts[split_index:])
    else:
        # Standard Case: Split at the very last word
        firstname_part = " ".join(parts[:-1])
        lastname_part = parts[-1]

    # 4. Final formatting
    if suffix:
        # Format: Lastname, Suffix, Firstname
        return f"{lastname_part}, {suffix}, {firstname_part}"
    else:
        # Format: Lastname, Firstname
        return f"{lastname_part}, {firstname_part}"

def format_author_page(name, book_title=None, book_year=None, use_dynamic=True):
    """
    Generates the content for the Author:Name page.
    If use_dynamic is True, it uses the Lua module.
    Otherwise, it links the specific book provided.
    """
    content = "{{author2}}\n\n===Publications===\n"
    
    if use_dynamic:
        content += "==== Contributing author====\n{{#invoke:Chapters|getChaptersByAuthor}}\n"
    else:
        if book_title and book_year:
            content += f"[[{book_title}]] ({book_year})\n"
        elif book_title:
             content += f"[[{book_title}]]\n"
             
    content += "\n__NOTOC__"
    return content

def format_author_cat_page(name):
    """Generates content for Category:Name"""
    sort_key = get_lastname_firstname(name)
    return f"{{{{authorcat_desc}}}}\n[[Category:Authors|{sort_key}]]"

def format_works_cat_page(name):
    """Generates content for Category:Text_of_works_by_Name"""
    return f"{{{{Textof_desc}}}}\n[[Category:{name}]]"

def format_ac_message(title, cover_file):
    return f"""{{{{AC-Template
| title    = {title}
| cover    = {cover_file}
}}}}"""

def ensure_wikibase_author(author_name):
    """
    1. Gets/Creates Wikibase Item (using clean src logic).
    2. Links it to 'Author:Name'.
    """
    # 1. Get QID (Creates if missing)
    qid = get_or_create_author(author_name)
    
    if not qid:
        return None, False, "Failed to retrieve QID"

    # 2. Link to Bahai.works
    # Ensure target page format is "Author:Name"
    if author_name.startswith("Author:"):
        target_page = author_name
    else:
        target_page = f"Author:{author_name}"
        
    success, msg = set_sitelink(qid, target_page)
    
    return qid, success, msg

# --- TAB: CREATE AUTHOR PAGES ---
with tab_create_author:
    st.header("Create Author Pages")
    st.info("This tool creates the three required pages (Author, Category, Works Category) for each author.")

    # Define a specific key for the text area so we can manipulate it
    text_area_key = "author_input_area"

    # 1. Handle Incoming Data from Chapter Items
    if "batch_author_list" in st.session_state:
        # Convert list to comma-separated string
        imported_authors = ", ".join(st.session_state["batch_author_list"])
        
        # FORCE update the widget's state directly
        st.session_state[text_area_key] = imported_authors
        
        st.success(f"📥 Received {len(st.session_state['batch_author_list'])} missing authors from Chapter Manager.")
        
        # Cleanup the transfer variable
        del st.session_state["batch_author_list"]

    # Ensure the key exists in session state to avoid KeyErrors
    if text_area_key not in st.session_state:
        st.session_state[text_area_key] = ""

    c1, c2 = st.columns(2)
    
    with c1:
        # Because 'key' is set, it will automatically read from st.session_state[text_area_key]
        raw_authors = st.text_area(
            "Author Names (comma separated)", 
            key=text_area_key,
            placeholder="e.g. Aaron Emmel, John Doe, Jane Smith",
            height=150
        )
        
        # Options for the Author Page content
        st.subheader("Configuration")
        content_mode = st.radio("Publications List Style", ["Dynamic (Lua Module)", "Static (Specific Book)"])
        
        book_title = ""
        book_year = ""
        
        if content_mode == "Static (Specific Book)":
            book_title = st.text_input("Book Title", placeholder="e.g. The Bahá'í Faith")
            book_year = st.text_input("Year", placeholder="e.g. 1919")

    with c2:
        st.subheader("Preview & Execute")
        
        # Parse Input
        if raw_authors:
            # Split by comma, strip whitespace, remove empty strings
            author_list = [name.strip() for name in raw_authors.split(",") if name.strip()]
            
            if not author_list:
                st.warning("Please enter valid author names.")
            else:
                st.write(f"**Found {len(author_list)} author(s):**")
                
                # Show a collapsed preview list
                with st.expander("Show List"):
                    for a in author_list:
                        st.text(f"- {a}")

                # Preview the content for the FIRST author just as a sample
                sample_author = author_list[0]
                st.caption(f"Previewing generated code for sample: **{sample_author}**")
                
                use_dyn = (content_mode == "Dynamic (Lua Module)")
                
                # 1. Author Page Preview
                st.markdown(f"**1. Author:{sample_author}**")
                txt_author = format_author_page(sample_author, book_title, book_year, use_dyn)
                st.code(txt_author, language="mediawiki")

                # 2. Main Category Preview
                st.markdown(f"**2. Category:{sample_author}**")
                txt_cat_main = format_author_cat_page(sample_author)
                st.code(txt_cat_main, language="mediawiki")

                # 3. Works Category Preview
                cat_works_title = f"Category:Text_of_works_by_{sample_author.replace(' ', '_')}"
                st.markdown(f"**3. {cat_works_title}**")
                txt_cat_works = format_works_cat_page(sample_author)
                st.code(txt_cat_works, language="mediawiki")

                st.divider()

                # Calculate totals for clarity
                num_authors = len(author_list)
                total_wikipages = num_authors * 3
                
                if st.button(f"🚀 Process {num_authors} Author(s) (Creates {total_wikipages} Wiki Pages)", type="primary"):
                    progress_bar = st.progress(0)
                    status_box = st.empty()
                    
                    total_ops = len(author_list)
                    success_count = 0
                    
                    for i, author_name in enumerate(author_list):
                        status_box.write(f"Processing **{author_name}** ({i+1}/{total_ops})...")
                        
                        # Prepare Content
                        p_author = f"Author:{author_name}"
                        p_cat_main = f"Category:{author_name}"
                        p_cat_works = f"Category:Text_of_works_by_{author_name.replace(' ', '_')}"
                        
                        txt_author = format_author_page(author_name, book_title, book_year, use_dyn)
                        txt_cat_main = format_author_cat_page(author_name)
                        txt_cat_works = format_works_cat_page(author_name)

                        try:
                            # --- A. Bahai.works Uploads ---
                            # 1. Author Page
                            upload_to_bahaiworks(p_author, txt_author, "Created Author page (Misc Tool)", check_exists=True)
                            
                            # 2. Main Category
                            upload_to_bahaiworks(p_cat_main, txt_cat_main, "Created Author Category", check_exists=True)
                            
                            # 3. Works Category
                            upload_to_bahaiworks(p_cat_works, txt_cat_works, "Created Works Category", check_exists=True)
                            
                            # --- B. Bahaidata Sync (NEW) ---
                            status_box.write(f"Syncing Bahaidata for **{author_name}**...")
                            qid, linked, link_msg = ensure_wikibase_author(author_name)
                            
                            if linked:
                                st.toast(f"✅ {author_name}: Pages created & Linked to {qid}")
                            else:
                                st.warning(f"Pages created, but Link failed for {qid}: {link_msg}")
                            
                            success_count += 1

                        except FileExistsError:
                            # Even if pages exist, we should still try to ensure the Link exists!
                            try:
                                qid, linked, link_msg = ensure_wikibase_author(author_name)
                                if linked:
                                    st.toast(f"Updated Link for existing author: {qid}")
                            except Exception as e_link:
                                st.error(f"Link Error on {author_name}: {e_link}")
                                
                        except Exception as e:
                            st.error(f"❌ Error on {author_name}: {e}")
                        
                        progress_bar.progress((i + 1) / total_ops)

                    status_box.success(f"✅ Process Complete! Processed {success_count} authors.")
                    st.balloons()

# --- TAB: AC MESSAGE ---
with tab_ac:
    st.header("AC Message")
    
    st.subheader("Copyright AC-Message Creator")
    st.info("Creates the /AC-Message subpage required for copyright-protected works.")
    
    col_b1, col_b2 = st.columns(2)
    
    with col_b1:
        bk_title = st.text_input("Book Title (Page Name)", placeholder="e.g. My_Book_Title")
        # Helper to suggest cover name if title is entered
        suggested_cover = f"{bk_title}.png" if bk_title else ""
        bk_cover = st.text_input("Cover Image Filename", value=suggested_cover, placeholder="e.g. My_Book_Title.png")
    
    with col_b2:
        if bk_title and bk_cover:
            target_page = f"{bk_title.strip()}/AC-Message"
            ac_content = format_ac_message(bk_title.strip(), bk_cover.strip())
            
            st.markdown(f"**Target:** `{target_page}`")
            st.code(ac_content, language="mediawiki")
            
            if st.button("🚀 Create AC-Message Page", type="primary"):
                try:
                    upload_to_bahaiworks(
                        target_page, 
                        ac_content, 
                        "Created AC-Message (Misc Tool)", 
                        check_exists=True
                    )
                    st.success(f"✅ Created {target_page}")
                except FileExistsError:
                    st.error(f"⚠️ Page '{target_page}' already exists.")
                except Exception as e:
                    st.error(f"Error: {e}")

# --- TAB: UPDATE AUTHOR PAGE ---
with tab_update_author:
    st.header("🔧 Update Author list")
    
    st.subheader("Update 'Authors' Index Page")
    st.info("Scans categories Authors-A through Authors-Z, formats the list, and updates the [[Authors]] page.")
    
    if st.button("🔄 Scan & Update [[Authors]]", type="primary"):
        status_box = st.empty()
        prog_bar = st.progress(0)
        
        full_wikitext = AUTHORS_PAGE_HEADER
        
        try:
            # Iterate A-Z
            letters = [chr(i) for i in range(ord('A'), ord('Z') + 1)]
            
            for i, letter in enumerate(letters):
                status_box.write(f"Scanning Category:Authors-{letter}...")
                
                # Fetch members via public API (faster, no auth needed for read)
                cat_url = "https://bahai.works/api.php"
                params = {
                    'action': 'query',
                    'list': 'categorymembers',
                    'cmtitle': f'Category:Authors-{letter}',
                    'format': 'json',
                    'cmlimit': 'max'
                }
                
                # Retrieve all pages in category
                members = []
                while True:
                    resp = requests.get(cat_url, params=params).json()
                    members.extend([p['title'] for p in resp.get('query', {}).get('categorymembers', [])])
                    if 'continue' not in resp:
                        break
                    params['cmcontinue'] = resp['continue']['cmcontinue']
                
                # Filter exclusions
                valid_members = [m for m in members if m not in EXCLUSION_LIST]
                
                # Only write header if we have content
                if valid_members:
                    full_wikitext += f"==== {letter} ====\n"
                    
                    for page_title in valid_members:
                        # Strip "Author:" prefix for the display name logic
                        clean_name = page_title.replace("Author:", "")
                        
                        # Use our robust parser
                        display_name = get_lastname_firstname(clean_name)
                        
                        full_wikitext += f"* [[{page_title}|{display_name}]]\n"
                    
                    full_wikitext += "\n\n"
                
                prog_bar.progress((i + 1) / 26)

            # Upload
            status_box.write("Uploading result...")
            
            # Using your existing uploader with overwrite protection DISABLED 
            # (because we WANT to update this specific index page)
            upload_to_bahaiworks(
                "Authors", 
                full_wikitext, 
                "Automated update of Authors index (Maintenance Tool)", 
                check_exists=False 
            )
            
            status_box.success("✅ [[Authors]] page has been updated!")
            st.balloons()
            
            with st.expander("View Generated Source"):
                st.code(full_wikitext, language="mediawiki")

        except Exception as e:
            status_box.error(f"Error: {e}")

# ==============================================================================
# TAB 4: MAINTENANCE (AUDIT)
# ==============================================================================
with tab_maintenance:
    st.header("🔧 Maintenance Audit")
    
    # ==========================================
    # 1. BLACKLIST MANAGEMENT
    # ==========================================
    BLACKLIST_FILE = "excluded_authors.json"

    def load_blacklist():
        if os.path.exists(BLACKLIST_FILE):
            with open(BLACKLIST_FILE, "r") as f:
                return set(json.load(f))
        return set()

    def save_blacklist(blocked_set):
        with open(BLACKLIST_FILE, "w") as f:
            json.dump(list(blocked_set), f)

    blacklist = load_blacklist()

    with st.expander(f"🚫 Manage Excluded Authors ({len(blacklist)})"):
        if blacklist:
            st.write("The following authors are permanently hidden from this audit:")
            to_remove = st.multiselect(
                "Select authors to un-block:", 
                options=sorted(list(blacklist))
            )
            if to_remove:
                if st.button("Un-block Selected"):
                    blacklist.difference_update(to_remove)
                    save_blacklist(blacklist)
                    st.success("Updated blacklist! Refreshing...")
                    time.sleep(0.5)
                    st.rerun()
        else:
            st.info("No authors are currently blacklisted.")

    st.markdown("---")

    # ==========================================
    # 2. AUDIT LOGIC
    # ==========================================
    st.markdown("""
    **Goal:** Identify authors who have publications in Bahaidata but are missing the 
    corresponding dynamic code on their Bahai.works author page.
    """)

    def query_bahaidata_authors():
        endpoint = "https://query.bahaidata.org/proxy/sparql" 
        
        # HEADERS: Critical for JSON response
        headers = {
            "User-Agent": "Bot BahaiWorks-Pipeline/1.0",
            "Accept": "application/sparql-results+json"
        }
        
        author_map = {}

        def run_query(sparql_query, is_chapter_query):
            try:
                r = requests.get(endpoint, params={'format': 'json', 'query': sparql_query}, headers=headers)
                r.raise_for_status()
                data = r.json()
                
                for row in data['results']['bindings']:
                    label = row['itemLabel']['value']
                    
                    if label not in author_map:
                        page_title = None
                        url = row.get('sitelink', {}).get('value')
                        if url:
                            page_title = urllib.parse.unquote(url.split("bahai.works/")[-1]).replace("_", " ")

                        author_map[label] = {
                            "Author": label,
                            "Page Title": page_title,
                            "Has Chapters": False,
                            "Has Articles": False
                        }
                    
                    if is_chapter_query:
                        author_map[label]["Has Chapters"] = True
                    else:
                        author_map[label]["Has Articles"] = True
                        
            except Exception as e:
                st.error(f"Error running {'Chapter' if is_chapter_query else 'Article'} query: {e}")

        # Query 1: Chapters (P11 -> P9)
        q_chapters = """
        SELECT DISTINCT ?item ?itemLabel ?sitelink WHERE {
          ?item wdt:P11 ?target .
          ?target wdt:P9 ?anyBook .
          OPTIONAL {
            ?sitelink schema:about ?item .
            FILTER(CONTAINS(STR(?sitelink), "bahai.works"))
          }
          SERVICE wikibase:label { bd:serviceParam wikibase:language "[AUTO_LANGUAGE],en". }
        }
        """
        run_query(q_chapters, True)

        # Query 2: Articles (P11 -> P7)
        q_articles = """
        SELECT DISTINCT ?item ?itemLabel ?sitelink WHERE {
          ?item wdt:P11 ?target .
          ?target wdt:P7 ?anyIssue .
          OPTIONAL {
            ?sitelink schema:about ?item .
            FILTER(CONTAINS(STR(?sitelink), "bahai.works"))
          }
          SERVICE wikibase:label { bd:serviceParam wikibase:language "[AUTO_LANGUAGE],en". }
        }
        """
        run_query(q_articles, False)

        return pd.DataFrame(list(author_map.values()))

    # ==========================================
    # 3. INTERFACE & EXECUTION
    # ==========================================
    
    # --- A. Run Audit Button (Updates Session State) ---
    if st.button("🔎 Run Audit (SPARQL + Content Check)", type="primary"):
        with st.spinner("1/2 Querying Bahaidata..."):
            df_audit = query_bahaidata_authors()
            
            # Filter Blacklist
            if not df_audit.empty:
                df_audit = df_audit[~df_audit["Author"].isin(blacklist)]

        if df_audit.empty:
            st.success("No active authors found matching criteria (or all are blacklisted).")
            # Clear previous results if any
            if "audit_missing" in st.session_state:
                del st.session_state["audit_missing"]
            if "audit_update" in st.session_state:
                del st.session_state["audit_update"]
        else:
            # --- B. Check Content ---
            with st.spinner("2/2 Verifying Page Content on Bahai.works..."):
                pages_to_check = df_audit[df_audit["Page Title"].notna()]["Page Title"].unique().tolist()
                
                content_map = {}
                chunk_size = 50
                api_url = "https://bahai.works/api.php"
                
                for i in range(0, len(pages_to_check), chunk_size):
                    chunk = pages_to_check[i:i+chunk_size]
                    params = {
                        "action": "query",
                        "titles": "|".join(chunk),
                        "prop": "revisions",
                        "rvprop": "content",
                        "format": "json"
                    }
                    try:
                        r = requests.get(api_url, params=params).json()
                        pages = r.get("query", {}).get("pages", {})
                        for pid, pdata in pages.items():
                            title = pdata['title']
                            if "revisions" in pdata:
                                content_map[title] = pdata["revisions"][0]["*"]
                            else:
                                content_map[title] = "" 
                    except:
                        pass
            
            # --- C. Categorize ---
            missing_pages = []
            needs_update = []

            for idx, row in df_audit.iterrows():
                p_title = row["Page Title"]
                
                if not p_title:
                    missing_pages.append({
                        "Author": row["Author"],
                        "Has Chapters": row["Has Chapters"],
                        "Has Articles": row["Has Articles"]
                    })
                else:
                    txt = content_map.get(p_title, "")
                    issue_details = []
                    if row["Has Chapters"] and "getChaptersByAuthor" not in txt:
                        issue_details.append("Missing 'getChaptersByAuthor'")
                    if row["Has Articles"] and "getArticlesByAuthor" not in txt:
                        issue_details.append("Missing 'getArticlesByAuthor'")
                    
                    if issue_details:
                        needs_update.append({
                            "Author": row["Author"],
                            "Page Title": p_title,
                            "Issues": ", ".join(issue_details)
                        })

            # Save to Session State (Persist across reruns)
            st.session_state["audit_missing"] = missing_pages
            st.session_state["audit_update"] = needs_update

    # --- D. Persistent Display Logic ---
    # This runs regardless of whether the "Run Audit" button was just clicked
    if "audit_missing" in st.session_state and "audit_update" in st.session_state:
        
        missing_pages = st.session_state["audit_missing"]
        needs_update = st.session_state["audit_update"]

        if not missing_pages and not needs_update:
            st.success("✅ Amazing! All authors are perfectly synced.")
        else:
            tab_missing, tab_fix = st.tabs([
                f"🚨 Missing Pages ({len(missing_pages)})", 
                f"🛠️ Needs Code Update ({len(needs_update)})"
            ])

            # TAB 1: MISSING PAGES
            with tab_missing:
                if not missing_pages:
                    st.success("No missing pages found!")
                else:
                    df_missing = pd.DataFrame(missing_pages)
                    df_missing.insert(0, "Create?", True)
                    df_missing.insert(1, "Blacklist?", False)
                    
                    edited_missing = st.data_editor(
                        df_missing,
                        column_config={
                            "Create?": st.column_config.CheckboxColumn("Create", default=True),
                            "Blacklist?": st.column_config.CheckboxColumn("Ignore", default=False),
                        },
                        disabled=["Author", "Has Chapters", "Has Articles"],
                        hide_index=True,
                        width='stretch',
                        key="editor_missing" # Unique key is good practice
                    )
                    
                    if st.button("Add Bahai.works author pages"):
                        # 1. Handle Blacklist
                        to_blacklist = edited_missing[edited_missing["Blacklist?"] == True]["Author"].tolist()
                        if to_blacklist:
                            blacklist.update(to_blacklist)
                            save_blacklist(blacklist)
                            st.toast(f"🚫 Blacklisted {len(to_blacklist)} authors.")
                        
                        # 2. Handle Creation
                        to_create = edited_missing[
                            (edited_missing["Create?"] == True) & 
                            (edited_missing["Blacklist?"] == False)
                        ]
                        
                        if not to_create.empty:
                            pb = st.progress(0)
                            log = st.empty()
                            for i, (idx, row) in enumerate(to_create.iterrows()):
                                author = row["Author"]
                                log.write(f"🔨 Creating Page: **{author}**...")
                                
                                # --- CREATION LOGIC ---
                                content = f"{{{{Author|author={author}}}}}"
                                upload_to_bahaiworks(author, content, "Auto-creating Author Page")
                                # ----------------------
                                time.sleep(0.1) 
                                pb.progress((i+1)/len(to_create))
                            
                            log.success(f"✅ Created {len(to_create)} pages!")
                        
                        # Rerun to refresh list (audit must be re-run manually or we clear state)
                        # Option: Clear state to force re-audit
                        del st.session_state["audit_missing"]
                        del st.session_state["audit_update"]
                        time.sleep(1)
                        st.rerun()

            # TAB 2: NEEDS UPDATE
            with tab_fix:
                if not needs_update:
                    st.success("All existing pages have correct code!")
                else:
                    df_upd = pd.DataFrame(needs_update)
                    df_upd.insert(0, "Fix Code?", True)
                    
                    edited_upd = st.data_editor(
                        df_upd,
                        column_config={
                            "Fix Code?": st.column_config.CheckboxColumn("Fix", default=True),
                            "Page Title": st.column_config.LinkColumn("Page Link"),
                        },
                        disabled=["Author", "Issues", "Page Title"],
                        hide_index=True,
                        width='stretch',
                        key="editor_update"
                    )
                    
                    if st.button("Fix Bahai.works author pages"):
                        st.info("This feature is coming soon.")
