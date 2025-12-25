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
        
        total = len(created_map)
        for i, item in enumerate(created_map):
            qid = item.get('qid')
            page_slug = item.get('page_name') 
            
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
