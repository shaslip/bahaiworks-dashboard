import streamlit as st
import pandas as pd
from src.chapter_importer import import_chapters_to_wikibase
from src.sitelink_manager import set_sitelink

st.set_page_config(layout="wide", page_title="Chapter Manager")

st.title("📑 Chapter Item Manager")
st.caption("Review and create Wikibase items for book chapters/articles.")

if st.button("⬅️ Back to Dashboard"):
    st.switch_page("app.py")

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
            "Item Label (Wikibase)", 
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
    
    with st.spinner("Checking which authors need pages on Bahai.works..."):
        # 1. Gather Unique Authors
        unique_authors = set()
        for idx, row in edited_df.iterrows():
            if row["Authors"]:
                # Split comma-separated names
                names = [n.strip() for n in str(row["Authors"]).split(",") if n.strip()]
                unique_authors.update(names)
        
        if not unique_authors:
            st.info("No authors found in this batch.")
        else:
            # 2. Check Existence via API
            # We check "Author:Name" for each
            import requests
            
            missing_authors = []
            author_list = list(unique_authors)
            
            # Batch request (max 50 titles per MediaWiki API call)
            # For simplicity in this dashboard, we'll do chunks of 50
            chunk_size = 50
            for i in range(0, len(author_list), chunk_size):
                chunk = author_list[i:i + chunk_size]
                titles_to_check = [f"Author:{name}" for name in chunk]
                
                try:
                    api_url = "https://bahai.works/api.php"
                    params = {
                        "action": "query",
                        "titles": "|".join(titles_to_check),
                        "format": "json"
                    }
                    resp = requests.get(api_url, params=params).json()
                    pages = resp.get("query", {}).get("pages", {})
                    
                    # If page has key "-1", it is missing
                    for pid, pdata in pages.items():
                        if int(pid) < 0:
                            # Extract name from title "Author:Name"
                            missing_name = pdata['title'].replace("Author:", "")
                            missing_authors.append(missing_name)
                            
                except Exception as e:
                    st.error(f"API Check Failed: {e}")

            # 3. Display Results & Action
            if not missing_authors:
                st.success("✅ All authors already have pages on Bahai.works!")
            else:
                st.warning(f"⚠️ {len(missing_authors)} Authors are missing pages on Bahai.works.")
                st.write(f"**Missing:** {', '.join(missing_authors)}")
                
                if st.button("👤 Proceed to create author pages", type="primary"):
                    # Pass the list to the next page
                    st.session_state["batch_author_list"] = missing_authors
                    st.switch_page("pages/06_misc_tasks.py")
