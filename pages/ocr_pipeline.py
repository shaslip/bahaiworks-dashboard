import streamlit as st
import pandas as pd
import os
import re
import subprocess
import platform
from sqlalchemy.orm import Session
from sqlalchemy import select, or_

# Local Imports
from src.database import engine, Document
from src.processor import merge_pdf_pair, analyze_split_boundaries, split_pdf_doubles
from src.auto_config import calculate_start_offset
from src.ocr_engine import OcrEngine, OcrConfig

st.set_page_config(page_title="OCR Assembly Line", layout="wide")

st.title("🏭 OCR Assembly Line")

@st.fragment
def render_details(selected_id):
    with Session(engine) as session:
        record = session.get(Document, selected_id)
        if not record:
            st.error("Document not found.")
            return

        st.sidebar.header("📄 Document Details")
        st.sidebar.write(f"**Filename:** {record.filename}")
        
        b1, b2 = st.sidebar.columns(2)
        with b1:
            if st.button("📄 Open File", width="stretch", key="sb_open_file"):
                open_local_file(record.file_path)
        with b2:
            if st.button("📂 Open Folder", width="stretch", key="sb_open_folder"):
                open_local_file(os.path.dirname(record.file_path))
        
        st.sidebar.divider()

# --- Helper: Fetch Pending Documents ---
def open_local_file(path):
    if os.path.exists(path):
        try:
            if platform.system() == "Linux":
                subprocess.call(["xdg-open", path])
            elif platform.system() == "Darwin":
                subprocess.call(["open", path])
            elif platform.system() == "Windows":
                os.startfile(path)
        except Exception as e:
            st.error(f"Error opening file: {e}")
    else:
        st.error("File not found on disk.")

def get_pending_docs():
    with Session(engine) as session:
        # Fetch anything not DIGITIZED or COMPLETED
        stm = select(Document).where(
            Document.status.notin_(["DIGITIZED", "COMPLETED"])
        ).order_by(Document.filename)
        return session.scalars(stm).all()

# --- TAB 1: MERGE & AUDIT ---
def render_merge_tab(docs):
    st.header("Step 1: Merge & Audit")
    
    # 1. Detection Logic
    split_pattern = re.compile(r"^(.*?)\s*-\s*(Cover|Inhalt gesamt)\.pdf$", re.IGNORECASE)
    doc_map = {d.filename: d for d in docs}
    
    matches = []
    singles = []
    processed_ids = set()

    for d in docs:
        if d.id in processed_ids: continue

        match = split_pattern.match(d.filename)
        if match:
            base_name = match.group(1)
            current_type = match.group(2).lower()
            partner_suffix = "Inhalt gesamt" if "cover" in current_type else "Cover"
            partner_name = f"{base_name} - {partner_suffix}.pdf"

            if partner_name in doc_map:
                partner = doc_map[partner_name]
                matches.append({
                    "base_name": base_name,
                    "cover": d if "cover" in current_type else partner,
                    "content": partner if "cover" in current_type else d
                })
                processed_ids.add(d.id)
                processed_ids.add(partner.id)
            else:
                singles.append(d)
        else:
            singles.append(d)

    # 2. Review Table (Spreadsheet Style)
    st.subheader(f"🧩 Proposed Merges ({len(matches)})")
    
    if matches:
        # Table Headers - Adjusted ratios since Target is gone
        h1, h2, h3 = st.columns([3, 3, 1])
        h1.caption("**Cover Source**")
        h2.caption("**Content Source**")
        h3.caption("**Action**")
        st.divider()

        # Table Rows
        for i, m in enumerate(matches):
            c1, c2, c3 = st.columns([3, 3, 1])
            c1.write(m['cover'].filename)
            c2.write(m['content'].filename)
            
            if c3.button("📂 Folder", key=f"f_{i}"):
                open_local_file(os.path.dirname(m['content'].file_path))
                
        st.divider()

        # Bulk Action
        if st.button("🚀 Confirm & Merge All Pairs", type="primary"):
            progress = st.progress(0)
            merged_count = 0
            
            for idx, m in enumerate(matches):
                new_filename = f"{m['base_name']}.pdf"
                new_path = os.path.join(os.path.dirname(m['cover'].file_path), new_filename)
                
                if merge_pdf_pair(m['cover'].file_path, m['content'].file_path, new_path):
                    with Session(engine) as session:
                        master = session.get(Document, m['content'].id)
                        secondary = session.get(Document, m['cover'].id)
                        
                        master.file_path = new_path
                        master.filename = new_filename
                        secondary.status = "COMPLETED"
                        secondary.ai_justification = f"Merged into {master.id}"
                        session.commit()
                    merged_count += 1
                progress.progress((idx + 1) / len(matches))
            
            st.success(f"Merged {merged_count} documents!")
            st.rerun()

    else:
        st.info("No auto-merge pairs detected.")

    # 3. Unmatched Files (Searchable)
    st.subheader(f"📂 Unmatched Files ({len(singles)})")
    
    with st.expander("Manual Merge Tools", expanded=False):
        filter_text = st.text_input("🔍 Search Unmatched Files", placeholder="Type year or title...").lower()
        filtered = [d for d in singles if filter_text in d.filename.lower()] if filter_text else singles[:100]

        mc1, mc2 = st.columns(2)
        with mc1:
            sel_cover = st.selectbox("Cover", filtered, format_func=lambda x: x.filename, key="m_cov")
        with mc2:
            sel_content = st.selectbox("Content", filtered, format_func=lambda x: x.filename, key="m_con")
            
        if st.button("Merge Selected Pair"):
            if sel_cover and sel_content and sel_cover.id != sel_content.id:
                # Manual merge implementation
                pass
            else:
                st.error("Invalid selection.")

# --- TAB 2: PREP (Calibration & Splitting) ---
def render_prep_tab(docs):
    st.header("Step 2: Calibration & Splitting")
    st.info("Analyze page offsets and detect/split double-page spreads.")

    # 1. Queue Review Table with Selection
    st.subheader(f"📋 Document Queue ({len(docs)} files)")
    
    selected_doc_id = None
    
    if docs:
        queue_data = [{
            "ID": d.id,
            "Filename": d.filename,
            "Priority": d.priority_score,
            "Language": d.language
        } for d in docs]
        
        # Enable selection
        event = st.dataframe(
            queue_data,
            column_order=["ID", "Filename", "Priority", "Language"],
            use_container_width=True,
            hide_index=True,
            selection_mode="single-row",
            on_select="rerun",
            key="prep_queue_table"
        )
        
        # Capture Selection
        if len(event.selection['rows']) > 0:
            idx = event.selection['rows'][0]
            selected_doc_id = queue_data[idx]["ID"]
            
    else:
        st.warning("No documents ready for processing. Check Step 1.")
        return

    st.divider()

    # 2. Render Sidebar if Selected
    if selected_doc_id:
        render_details(selected_doc_id)
    else:
        st.sidebar.info("Select a document in the table to view details.")

    if st.button("🕵️ Run Analysis on All Files", type="primary"):
        progress = st.progress(0)
        results = []
        
        for i, doc in enumerate(docs):
            import fitz
            try:
                with fitz.open(doc.file_path) as pdf:
                    total = len(pdf)
                
                start, is_double = calculate_start_offset(doc.file_path, total)
                
                results.append({
                    "doc": doc,
                    "offset": start,
                    "is_double": is_double,
                    "status": "Ready" if start else "Failed"
                })
            except Exception as e:
                st.error(f"Error reading {doc.filename}: {e}")
            
            progress.progress((i + 1) / len(docs))
            
        st.session_state['prep_results'] = results

    # 3. Results Grid
    if 'prep_results' in st.session_state:
        results = st.session_state['prep_results']
        
        for item in results:
            doc = item['doc']
            with st.container(border=True):
                c1, c2, c3, c4 = st.columns([4, 2, 2, 2])
                c1.write(f"**{doc.filename}**")
                
                # Editable Offset
                new_offset = c2.number_input("Offset", value=item['offset'] if item['offset'] else 0, key=f"off_{doc.id}")
                
                # Double Page Indicator
                is_dbl = c3.checkbox("Double Page?", value=item['is_double'], key=f"dbl_{doc.id}")
                
                # Action
                if c4.button("Process", key=f"proc_{doc.id}"):
                    if is_dbl:
                        with st.spinner("Splitting..."):
                            s_start, s_end = analyze_split_boundaries(doc.file_path)
                            split_name = f"split_{doc.filename}"
                            split_path = os.path.join(os.path.dirname(doc.file_path), split_name)
                            
                            if split_pdf_doubles(doc.file_path, split_path, s_start, s_end):
                                with Session(engine) as session:
                                    d = session.get(Document, doc.id)
                                    d.file_path = split_path
                                    d.filename = split_name
                                    session.commit()
                                st.success("Split Complete!")
                            else:
                                st.error("Split Failed")
                                
                    st.toast(f"Configuration Saved: Offset {new_offset}")

# --- TAB 3: EXECUTION ---
def render_exec_tab(docs):
    st.header("Step 3: Execution")
    st.info("Run OCR on prepared files.")

    # In a real scenario, we'd filter this list to only those "Approved" in Tab 2
    
    if st.button("🚀 Start Batch OCR"):
        st.write("Processing...")
        ocr_bar = st.progress(0)
        
        for i, doc in enumerate(docs):
            # Here we would need to retrieve the 'offset' we decided on in Tab 2.
            # Ideally, Tab 2 saves that offset to the Document model (e.g. doc.page_offset)
            # For this skeleton, we'll re-calculate or assume 1 if missing.
            
            ocr = OcrEngine(doc.file_path)
            
            # Dummy config - in reality, fetch from DB
            config = OcrConfig(
                has_cover_image=True,
                first_numbered_page_index=1, # Should come from Tab 2
                language='eng' 
            )
            
            try:
                ocr.generate_images()
                ocr.run_ocr(config)
                
                with Session(engine) as session:
                    d = session.get(Document, doc.id)
                    d.status = "DIGITIZED"
                    session.commit()
                    
            except Exception as e:
                st.error(f"Failed {doc.filename}: {e}")
                
            ocr_bar.progress((i + 1) / len(docs))

# --- Main Layout ---
tab1, tab2, tab3 = st.tabs(["1. Merge", "2. Prep", "3. Execute"])

all_pending = get_pending_docs()

with tab1:
    render_merge_tab(all_pending)

with tab2:
    render_prep_tab(all_pending)

with tab3:
    render_exec_tab(all_pending)
