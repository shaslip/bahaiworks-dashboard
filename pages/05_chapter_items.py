import streamlit as st
import pandas as pd
from src.chapter_importer import import_chapters_to_wikibase
from src.sitelink_manager import set_sitelink

st.set_page_config(layout="wide", page_title="Chapter Manager")

st.title("📑 Chapter Item Manager")

with st.expander("ℹ️ Help / Instructions"):
    st.markdown("""
    This tool allows you to define that a certain book chapter is authored by someone other than the author of the book. 
    Typically this is used in the case of a book that is a collection of scholarly articles or papers. Defining the relationship
    in this way allows us to:
    * Automatically list the chapters an individual has authored in works compiled by others
    * When users search the text of works by a certain author, they are also shown the chapters they have authored

    **Bahaidata Parent Book QID** Bahaidata item number corresponding to the book where the chapter can be found
    
    **Bahai.works Page Title** The exact title of the page for the book separatedon bahai.works

    Under "Review Items"

    **Page Name** If the bahai.works page where the content existed was "Light of the World/Chapter 1" enter "Chapter 1"
    
    **Item Label** The label will match the Page Name by default. You can leave this blank.
    
    **Authors** Enter the author name, or a comma separated list of authors
    
    **Pages** Enter the page where the chapter begins and ends like this 4-10 or 34-100.
    """)

st.divider()

# --- 1. Load / Initialize Data ---
if "chapter_data_df" not in st.session_state:
    if "chapter_review_data" in st.session_state:
        raw_list = st.session_state["chapter_review_data"]
        
        # Flatten for the Editor
        flat_data = []
        for item in raw_list:
            auth_str = ", ".join(item.get("author", []))
            
            flat_data.append({
                "Page Name (Slug)": item.get("page_name", ""),
                "Item Label": item.get("title", ""),
                "Authors": auth_str,
                "Pages": item.get("page_range", "")
            })
        st.session_state["chapter_data_df"] = pd.DataFrame(flat_data)
        st.success(f"Loaded {len(flat_data)} items from pipeline.")
    else:
        st.info("No pipeline data found. Starting with empty table.")
        # Initialize with specific column order
        st.session_state["chapter_data_df"] = pd.DataFrame(
            columns=["Page Name (Slug)", "Item Label", "Authors", "Pages"]
        )

# --- 2. Context Inputs ---
c1, c2 = st.columns(2)
with c1:
    parent_qid = st.text_input("Bahaidata Parent Book QID", value=st.session_state.get("chapter_parent_qid", ""))
with c2:
    base_title = st.text_input("Bahai.works Page Title (e.g. 'Light of the World')", value=st.session_state.get("chapter_target_base", ""))

st.markdown("---")

# --- 3. The Data Editor ---
st.subheader("Review Items")
st.info("Edit the table below. If 'Item Label' is blank, we will use 'Page Name' as the label.")

df = st.session_state["chapter_data_df"]

edited_df = st.data_editor(
    df,
    num_rows="dynamic",
    width='stretch',
    column_config={
        "Page Name (Slug)": st.column_config.TextColumn(
            "Page Name (URL after Base Page)",
            width="medium",
            help="The part of the URL after the book title."
        ),
        "Item Label": st.column_config.TextColumn(
            "Item Label (If different than Page Name)",
            width="medium",
            help="The name of the item in the database. Leave blank to use Page Name."
        ),
        "Authors": st.column_config.TextColumn(
            "Authors (comma separated)", 
            width="medium"
        ),
        "Pages": st.column_config.TextColumn(
            "Pages (eg 4-6)", 
            width="small",
        )
    }
)

# --- 4. Execution ---
st.markdown("---")

if st.button("🚀 Process Items (Create & Link)", type="primary"):
    if not parent_qid:
        st.error("Parent QID is required.")
        st.stop()
        
    if edited_df.empty:
        st.warning("No data to process.")
        st.stop()

    # Reconstruct standard list format for the importer
    process_list = []
    for idx, row in edited_df.iterrows():
        p_name = row["Page Name (Slug)"]
        
        # Skip rows with no identifier
        if not p_name and not row["Item Label"]: 
            continue
        
        # Logic: If Item Label is blank, fallback to Page Name
        final_label = row["Item Label"] if row["Item Label"].strip() else p_name
        
        # Parse Authors
        raw_auth = str(row["Authors"])
        auth_list = [a.strip() for a in raw_auth.split(",") if a.strip()]
        
        item_dict = {
            "title": final_label,        # Used for Item Label
            "display_title": final_label,
            "author": auth_list,
            "page_range": row["Pages"],
            "page_name": p_name,         # Used for URL
            "qid": ""                    # Always empty -> Always create new
        }
        process_list.append(item_dict)

    # 1. Run Import (Creates QIDs)
    with st.spinner("Creating Wikibase Items..."):
        try:
            logs, created_map = import_chapters_to_wikibase(parent_qid, process_list)
            st.write("### Import Logs")
            st.text(logs)
        except Exception as e:
            st.error(f"Import Error: {e}")
            st.stop()

    # 2. Run Linking
    with st.spinner("Linking pages..."):
        link_logs = []
        progress_bar = st.progress(0)
        
        # Helper: Map Titles to Page Slugs from our original input list
        # This ensures we have the slug even if the importer doesn't return it.
        slug_lookup = {x['title']: x['page_name'] for x in process_list}
        
        total = len(created_map)
        for i, item in enumerate(created_map):
            qid = item.get('qid')
            title = item.get('title')
            
            # 1. Try getting slug from importer return
            page_slug = item.get('page_name')
            
            # 2. If missing, look it up from our input data using the Title
            if not page_slug and title:
                page_slug = slug_lookup.get(title)
            
            # 3. Check Base Title
            if not base_title:
                link_logs.append(f"⚠️ Skipping Link for {qid} (Missing Base Page Title)")
                continue

            # 4. Execute Link
            if qid and page_slug:
                full_url = f"{base_title}/{page_slug}"
                
                success, msg = set_sitelink(qid, full_url)
                if success:
                    link_logs.append(f"✅ Linked {qid} -> {full_url}")
                else:
                    link_logs.append(f"❌ Link Fail {qid}: {msg}")
            else:
                link_logs.append(f"⚠️ Skipping Link for {qid} (Missing info: Slug='{page_slug}')")
            
            progress_bar.progress((i + 1) / total)

        st.write("### Link Logs")
        for log in link_logs:
            st.write(log)
            
        st.success("Done!")

# --- 3. Check for Missing Author Pages ---
st.divider()
st.subheader("3. Author Page Verification")

# Initialize Cache
if "missing_authors_cache" not in st.session_state:
    st.session_state["missing_authors_cache"] = None

# Determine if we need to run the check (First run OR Manual Refresh)
should_check = st.session_state["missing_authors_cache"] is None

c_check, c_refresh = st.columns([3, 1])
with c_refresh:
    if st.button("🔄 Force Re-scan", type="secondary"):
        st.session_state["missing_authors_cache"] = None
        st.rerun()

# 1. Logic: Gather Authors & Check API
if should_check:
    with st.spinner("Checking which authors need pages on Bahai.works..."):
        # Gather Unique Authors from the Editor above
        unique_authors = set()
        # Ensure we access the edited dataframe from the earlier variable
        if not edited_df.empty:
            for idx, row in edited_df.iterrows():
                if row["Authors"]:
                    names = [n.strip() for n in str(row["Authors"]).split(",") if n.strip()]
                    unique_authors.update(names)
        
        if not unique_authors:
            st.session_state["missing_authors_cache"] = []
        else:
            # Check Existence via API
            import requests
            missing_list = []
            author_list = list(unique_authors)
            
            try:
                # Batch request (chunk size 50)
                chunk_size = 50
                for i in range(0, len(author_list), chunk_size):
                    chunk = author_list[i:i + chunk_size]
                    titles_to_check = [f"Author:{name}" for name in chunk]
                    
                    api_url = "https://bahai.works/api.php"
                    params = {
                        "action": "query",
                        "titles": "|".join(titles_to_check),
                        "format": "json"
                    }
                    resp = requests.get(api_url, params=params).json()
                    pages = resp.get("query", {}).get("pages", {})
                    
                    for pid, pdata in pages.items():
                        if int(pid) < 0:
                            missing_name = pdata['title'].replace("Author:", "")
                            missing_list.append(missing_name)
                
                st.session_state["missing_authors_cache"] = missing_list
                
            except Exception as e:
                st.error(f"API Check Failed: {e}")
                # Keep cache None so user can try again
                st.session_state["missing_authors_cache"] = None

# 2. Display Results (Read from Cache)
missing_authors = st.session_state.get("missing_authors_cache")

if missing_authors is not None:
    if not missing_authors:
        st.success("✅ All authors already have pages on Bahai.works!")
    else:
        st.warning(f"⚠️ {len(missing_authors)} Authors are missing pages on Bahai.works.")
        st.write(f"**Missing:** {', '.join(missing_authors)}")
        
        # This button works now because it is no longer hidden inside the Process button
        if st.button("👤 Proceed to create author pages", type="primary"):
            st.session_state["batch_author_list"] = missing_authors
            st.switch_page("pages/06_misc_tasks.py")
