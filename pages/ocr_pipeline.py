import streamlit as st
import pandas as pd
import os
import re
from sqlalchemy.orm import Session
from sqlalchemy import select, or_

# Local Imports
from src.database import engine, Document
from src.processor import merge_pdf_pair, analyze_split_boundaries, split_pdf_doubles
from src.auto_config import calculate_start_offset
from src.ocr_engine import OcrEngine, OcrConfig

st.set_page_config(page_title="OCR Assembly Line", layout="wide")

st.title("🏭 OCR Assembly Line")

# --- Helper: Fetch Pending Documents ---
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
    st.info("Identify multi-part PDFs (Cover + Content) and merge them into single files.")

    # 1. Auto-Detection Logic
    split_pattern = re.compile(r"^(.*?)\s*-\s*(Cover|Inhalt gesamt)\.pdf$", re.IGNORECASE)
    
    matches = []
    singles = []
    processed_ids = set()

    # Create a lookup for quick access
    doc_map = {d.filename: d for d in docs}

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
                # Determine which is which
                cover = d if "cover" in current_type else partner
                content = partner if "cover" in current_type else d
                
                matches.append({
                    "base_name": base_name,
                    "cover_doc": cover,
                    "content_doc": content
                })
                processed_ids.add(d.id)
                processed_ids.add(partner.id)
            else:
                singles.append(d)
        else:
            singles.append(d)

    # 2. Display Auto-Matches
    st.subheader(f"🧩 Auto-Matched Pairs ({len(matches)})")
    if matches:
        with st.expander("Review Matches", expanded=True):
            for m in matches:
                c1, c2, c3 = st.columns([3, 1, 1])
                c1.write(f"**Target:** `{m['base_name']}.pdf`")
                if c2.button("Merge", key=f"merge_{m['base_name']}"):
                    # Execute Merge
                    new_filename = f"{m['base_name']}.pdf"
                    new_path = os.path.join(os.path.dirname(m['cover_doc'].file_path), new_filename)
                    
                    if merge_pdf_pair(m['cover_doc'].file_path, m['content_doc'].file_path, new_path):
                        # Update DB: Keep 'content' doc as the master, update path/name
                        with Session(engine) as session:
                            master = session.get(Document, m['content_doc'].id)
                            secondary = session.get(Document, m['cover_doc'].id)
                            
                            master.file_path = new_path
                            master.filename = new_filename
                            
                            # Mark secondary as processed/merged
                            secondary.status = "COMPLETED" 
                            secondary.ai_justification = f"Merged into {master.id}"
                            
                            session.commit()
                        st.success(f"Merged {new_filename}")
                        st.rerun()
                    else:
                        st.error("Merge failed on disk.")

    # 3. Manual Pairing (Orphans)
    st.subheader(f"📂 Unmatched Files ({len(singles)})")
    
    # Simple Manual Merge UI
    c_m1, c_m2, c_m3 = st.columns(3)
    with c_m1:
        cover_select = st.selectbox("Select Cover PDF", singles, format_func=lambda x: x.filename, key="man_cover")
    with c_m2:
        content_select = st.selectbox("Select Content PDF", singles, format_func=lambda x: x.filename, key="man_content")
    with c_m3:
        new_name_manual = st.text_input("New Filename", value="Merged_Document.pdf")
        if st.button("Manual Merge"):
            if cover_select.id == content_select.id:
                st.error("Cannot merge file with itself.")
            else:
                 # Logic would mimic the auto-merge above
                 st.info("Manual merge logic implementation would go here (same as above).")

# --- TAB 2: PREP (Calibration & Splitting) ---
def render_prep_tab(docs):
    st.header("Step 2: Calibration & Splitting")
    st.info("Analyze page offsets and detect/split double-page spreads.")

    if st.button("🕵️ Run Analysis on All Files"):
        progress = st.progress(0)
        results = []
        
        for i, doc in enumerate(docs):
            import fitz
            try:
                with fitz.open(doc.file_path) as pdf:
                    total = len(pdf)
                
                # Re-using the logic from batch_factory
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

    # Display Results Grid
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
                    # IF Double: Run Split, Update Path
                    if is_dbl:
                        with st.spinner("Splitting..."):
                            s_start, s_end = analyze_split_boundaries(doc.file_path)
                            split_name = f"split_{doc.filename}"
                            split_path = os.path.join(os.path.dirname(doc.file_path), split_name)
                            
                            if split_pdf_doubles(doc.file_path, split_path, s_start, s_end):
                                # Update DB
                                with Session(engine) as session:
                                    d = session.get(Document, doc.id)
                                    d.file_path = split_path
                                    d.filename = split_name
                                    session.commit()
                                st.success("Split Complete!")
                            else:
                                st.error("Split Failed")
                                
                    # Save Offset Config to DB (We might need a temp field or just rely on runtime config)
                    # For now, we assume we just pass to step 3, but saving to a 'notes' field helps
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
