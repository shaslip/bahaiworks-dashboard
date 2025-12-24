import streamlit as st
import re
import requests
from src.mediawiki_uploader import upload_to_bahaiworks

st.set_page_config(
    page_title="Bahai.works Utilities",
    layout="wide"
)

st.title("🛠️ Miscellaneous Utilities")

if st.button("← Back to Dashboard"):
    st.switch_page("app.py")

st.markdown("---")

tab_author, tab_book, tab_maintenance = st.tabs(["👤 Author Manager", "📖 Book Manager", "🔧 System Maintenance"])

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
| style="text-align:center;" | {{#expr: {{PAGESINCATEGORY:Authors-A}} + {{PAGESINCATEGORY:Authors-B}} + {{PAGESINCATEGORY:Authors-C}} + {{PAGESINCATEGORY:Authors-D}} + {{PAGESINCATEGORY:Authors-E}} + {{PAGESINCATEGORY:Authors-F}} + {{PAGESINCATEGORY:Authors-G}} + {{PAGESINCATEGORY:Authors-H}} + {{PAGESINCATEGORY:Authors-I}} + {{PAGESINCATEGORY:Authors-J}} + {{PAGESINCATEGORY:Authors-K}} + {{PAGESINCATEGORY:Authors-L}} + {{PAGESINCATEGORY:Authors-M}} + {{PAGESINCATEGORY:Authors-N}} + {{PAGESINCATEGORY:Authors-O}} + {{PAGESINCATEGORY:Authors-P}} + {{PAGESINCATEGORY:Authors-Q}} + {{PAGESINCATEGORY:Authors-R}} + {{PAGESINCATEGORY:Authors-S}} + {{PAGESINCATEGORY:Authors-T}} + {{PAGESINCATEGORY:Authors-U}} + {{PAGESINCATEGORY:Authors-V}} + {{PAGESINCATEGORY:Authors-W}} + {{PAGESINCATEGORY:Authors-X}} + {{PAGESINCATEGORY:Authors-Y}} + {{PAGESINCATEGORY:Authors-X}} }}
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

# --- TAB: AUTHOR MANAGER ---

with tab_author:
    st.header("Create Author Pages")
    st.info("This tool creates the three required pages for a new author on Bahai.works.")

    c1, c2 = st.columns(2)
    
    with c1:
        author_name = st.text_input("Author Name", placeholder="e.g. Aaron Emmel")
        
        # Options for the Author Page content
        st.subheader("Configuration")
        content_mode = st.radio("Publications List Style", ["Dynamic (Lua Module)", "Static (Specific Book)"])
        
        book_title = ""
        book_year = ""
        
        if content_mode == "Static (Specific Book)":
            book_title = st.text_input("Book Title", placeholder="e.g. The Bahá'í Faith")
            book_year = st.text_input("Year", placeholder="e.g. 1919")

    with c2:
        st.subheader("Preview Actions")
        
        if author_name:
            # Calculate Page Titles
            p_author = f"Author:{author_name.strip()}"
            p_cat_main = f"Category:{author_name.strip()}"
            p_cat_works = f"Category:Text_of_works_by_{author_name.strip().replace(' ', '_')}"

            # 1. Author Page
            st.markdown(f"**1. {p_author}**")
            use_dyn = (content_mode == "Dynamic (Lua Module)")
            txt_author = format_author_page(author_name, book_title, book_year, use_dyn)
            st.code(txt_author, language="mediawiki")

            # 2. Main Category
            st.markdown(f"**2. {p_cat_main}**")
            txt_cat_main = format_author_cat_page(author_name)
            st.code(txt_cat_main, language="mediawiki")

            # 3. Works Category
            st.markdown(f"**3. {p_cat_works}**")
            txt_cat_works = format_works_cat_page(author_name)
            st.code(txt_cat_works, language="mediawiki")

            st.divider()

            if st.button(f"🚀 Create All Pages for '{author_name}'", type="primary"):
                # We use a progress bar because we are making 3 network requests
                prog = st.progress(0)
                status = st.empty()

                try:
                    # Step 1: Author Page (Safe Mode: ON)
                    status.write(f"Creating {p_author}...")
                    upload_to_bahaiworks(
                        p_author, 
                        txt_author, 
                        "Created Author page (Misc Tool)", 
                        check_exists=True  # <--- PREVENTS OVERWRITE
                    )
                    prog.progress(33)

                    # Step 2: Main Category (Safe Mode: ON)
                    status.write(f"Creating {p_cat_main}...")
                    upload_to_bahaiworks(
                        p_cat_main, 
                        txt_cat_main, 
                        "Created Author Category (Misc Tool)", 
                        check_exists=True  # <--- PREVENTS OVERWRITE
                    )
                    prog.progress(66)

                    # Step 3: Works Category (Safe Mode: ON)
                    status.write(f"Creating {p_cat_works}...")
                    upload_to_bahaiworks(
                        p_cat_works, 
                        txt_cat_works, 
                        "Created Text of Works Category (Misc Tool)", 
                        check_exists=True  # <--- PREVENTS OVERWRITE
                    )
                    prog.progress(100)

                    status.success(f"✅ Successfully created all pages for {author_name}!")
                    st.balloons()

                except FileExistsError as e:
                    status.error(f"⚠️ Safety Stop: {e}")
                    prog.empty()
                except Exception as e:
                    status.error(f"Error: {e}")

# --- TAB: BOOK MANAGER ---
with tab_book:
    st.header("Book Utilities")
    
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

# --- TAB: MAINTENANCE ---
with tab_maintenance:
    st.header("🔧 System Maintenance")
    
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
                
                # Only write header if we have content (Fixes "Problem 3")
                if valid_members:
                    full_wikitext += f"==== {letter} ====\n"
                    
                    for page_title in valid_members:
                        # Strip "Author:" prefix for the display name logic
                        clean_name = page_title.replace("Author:", "")
                        
                        # Use our robust parser (Fixes "Problem 2")
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
