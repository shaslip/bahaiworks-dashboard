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
    # Check if we have data passed from the pipeline
    if "chapter_review_data" in st.session_state:
        raw_list = st.session_state["chapter_review_data"]
        
        # Flatten for the Editor (Authors list -> String)
        flat_data = []
        for item in raw_list:
            auth_str = ", ".join(item.get("author", []))
            flat_data.append({
                "Title": item.get("title", ""),
                "Display Title": item.get("display_title", item.get("title", "")),
                "Authors": auth_str,
                "Page Range": item.get("page_range", ""),
                "Page Name (Slug)": item.get("page_name", ""),
                "QID (Existing)": "" # Leave empty to create new
            })
        st.session_state["chapter_data_df"] = pd.DataFrame(flat_data)
        st.success(f"Loaded {len(flat_data)} items from pipeline.")
    else:
        # Initialize Empty Frame for Manual Entry
        st.info("No pipeline data found. Starting with empty table.")
        st.session_state["chapter_data_df"] = pd.DataFrame(
            columns=["Title", "Display Title", "Authors", "Page Range", "Page Name (Slug)", "QID (Existing)"]
        )

# --- 2. Context Inputs ---
c1, c2 = st.columns(2)
with c1:
    parent_qid = st.text_input("Parent Book QID", value=st.session_state.get("chapter_parent_qid", ""))
with c2:
    base_title = st.text_input("Base Page Title (e.g. 'Light of the World')", value=st.session_state.get("chapter_target_base", ""))

st.markdown("---")

# --- 3. The Data Editor ---
st.subheader("Review Items")
st.info("Rows with a 'QID' will be linked. Rows without a 'QID' will be CREATED as new items.")

df = st.session_state["chapter_data_df"]

edited_df = st.data_editor(
    df,
    num_rows="dynamic",
    use_container_width=True,
    column_config={
        "Title": st.column_config.TextColumn("Item Label (Wikibase)", width="medium"),
        "Display Title": st.column_config.TextColumn("Display Title (Visual)", width="medium"),
        "Authors": st.column_config.TextColumn("Authors (comma sep)", width="medium"),
        "Page Range": st.column_config.TextColumn("Pages", width="small"),
        "Page Name (Slug)": st.column_config.TextColumn("URL Slug", width="medium"),
        "QID (Existing)": st.column_config.TextColumn("QID (Optional)", width="small", help="If filled, we skip creation and just link."),
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
        # Skip empty rows
        if not row["Title"]: continue
        
        # Parse Authors
        raw_auth = str(row["Authors"])
        auth_list = [a.strip() for a in raw_auth.split(",") if a.strip()]
        
        item_dict = {
            "title": row["Title"],
            "display_title": row["Display Title"],
            "author": auth_list,
            "page_range": row["Page Range"],
            "page_name": row["Page Name (Slug)"],
            "qid": row["QID (Existing)"] 
        }
        process_list.append(item_dict)

    # 1. Run Import (Creates missing QIDs)
    with st.spinner("Creating/Updating Wikibase Items..."):
        try:
            # We use your existing importer. 
            # NOTE: Your importer might need a small update to handle pre-existing QIDs if it doesn't already.
            # Assuming import_chapters_to_wikibase returns (logs, created_map)
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
        
        total = len(created_map)
        for i, item in enumerate(created_map):
            qid = item.get('qid')
            page_slug = item.get('page_name') # Ensure the importer passes this back
            
            # Fallback if importer didn't return page_name, try to match by title from input
            if not page_slug:
                found = next((x for x in process_list if x['title'] == item['title']), None)
                if found: page_slug = found['page_name']
            
            if qid and page_slug and base_title:
                full_url = f"{base_title}/{page_slug}"
                success, msg = set_sitelink(qid, full_url)
                if success:
                    link_logs.append(f"✅ Linked {qid} -> {full_url}")
                else:
                    link_logs.append(f"❌ Link Fail {qid}: {msg}")
            else:
                link_logs.append(f"⚠️ Skipping Link for {qid} (Missing info)")
            
            progress_bar.progress((i + 1) / total)

        st.write("### Link Logs")
        for log in link_logs:
            st.write(log)
            
        st.success("Done!")
