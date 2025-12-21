import streamlit as st
import pandas as pd
import subprocess
import platform
import os
import glob
from sqlalchemy.orm import Session
from sqlalchemy import select, desc

# Local imports
from src.database import engine, Document
from src.processor import extract_preview_images
from src.evaluator import evaluate_document
# NEW IMPORT:
from src.ocr_engine import OcrEngine, OcrConfig

# --- Configuration ---
st.set_page_config(
    page_title="Bahai.works Digitization Dashboard",
    layout="wide",
    initial_sidebar_state="expanded"
)

# --- Helper Functions ---
def load_data():
    """Fetches all documents with stable sorting."""
    with Session(engine) as session:
        query = select(Document.id, 
                       Document.filename, 
                       Document.status, 
                       Document.priority_score, 
                       Document.language, 
                       Document.summary,
                       Document.ai_justification,
                       Document.file_path)\
                .order_by(
                    desc(Document.priority_score), 
                    Document.filename
                )
        df = pd.read_sql(query, session.bind)
        return df

def get_metrics(df):
    total = len(df)
    pending = len(df[df['status'] == 'PENDING'])
    completed = len(df[df['status'] == 'DIGITIZED'])
    high_priority = len(df[df['priority_score'] >= 8]) if not df.empty else 0
    return total, pending, completed, high_priority

def parse_ranges(text):
    """Parses '48-55, 102' into [(48, 55), (102, 102)]"""
    ranges = []
    if not text or not text.strip(): return []
    try:
        parts = text.split(',')
        for p in parts:
            p = p.strip()
            if '-' in p:
                s, e = p.split('-')
                ranges.append((int(s), int(e)))
            elif p.isdigit():
                ranges.append((int(p), int(p)))
    except:
        pass # Fail silently on bad input
    return ranges

# --- Sidebar Fragment ---
@st.fragment
def render_details(selected_id):
    with Session(engine) as session:
        record = session.get(Document, selected_id)
        if not record:
            st.error("Document not found.")
            return

        st.header("📄 Document Details")
        st.write(f"**Filename:** {record.filename}")
        
        # --- File Actions ---
        b1, b2 = st.columns(2)
        with b1:
            if st.button("📄 Open File", width="stretch"):
                if os.path.exists(record.file_path):
                    try:
                        if platform.system() == "Linux":
                            subprocess.call(["xdg-open", record.file_path])
                        elif platform.system() == "Darwin":
                            subprocess.call(["open", record.file_path])
                        elif platform.system() == "Windows":
                            os.startfile(record.file_path)
                    except Exception as e:
                        st.error(f"Error: {e}")
                else:
                    st.error("File not found!")

        with b2:
            if st.button("📂 Open Folder", width="stretch"):
                folder_path = os.path.dirname(record.file_path)
                if os.path.exists(folder_path):
                    try:
                        if platform.system() == "Linux":
                            subprocess.call(["dolphin", "--select", record.file_path])
                        elif platform.system() == "Darwin":
                            subprocess.call(["open", "-R", record.file_path])
                        elif platform.system() == "Windows":
                            subprocess.Popen(f'explorer /select,"{record.file_path}"')
                    except Exception as e:
                        st.error(f"Error: {e}")
                else:
                    st.error("Folder not found!")

        st.divider()

        # === TABS: AI vs OCR ===
        tab_ai, tab_ocr = st.tabs(["🤖 AI Analyst", "🏭 OCR Factory"])

        # -------------------------
        # TAB 1: AI EVALUATION
        # -------------------------
        with tab_ai:
            if pd.notna(record.priority_score):
                st.metric("Priority Score", f"{record.priority_score}/10")
                st.write(f"**Language:** {record.language}")
                st.info(f"**Summary:** {record.summary}")
                
                with st.expander("Justification"):
                    st.caption(record.ai_justification)
                
                st.divider()
                st.subheader("Manual Controls")
                
                c1, c2 = st.columns(2)
                with c1:
                    new_score = st.number_input(
                        "Set Score", min_value=1, max_value=10, 
                        value=int(record.priority_score), label_visibility="collapsed"
                    )
                with c2:
                    if st.button("💾 Save"):
                        record.priority_score = new_score
                        if "Manually Overridden" not in (record.ai_justification or ""):
                            record.ai_justification = (record.ai_justification or "") + "\n[Manually Overridden]"
                        session.commit()
                        st.rerun()

                if st.button("🔄 Re-run AI"):
                    with st.spinner("Re-processing..."):
                        images = extract_preview_images(record.file_path)
                        if images:
                            result = evaluate_document(images)
                            if result:
                                record.priority_score = result['priority_score']
                                record.summary = result['summary']
                                record.language = result['language']
                                record.ai_justification = result['ai_justification']
                                record.status = "EVALUATED"
                                session.commit()
                                st.rerun()
            else:
                st.warning("Status: Pending Analysis")
                if st.button("✨ Run AI Evaluation", type="primary"):
                    with st.spinner("Analyzing..."):
                        images = extract_preview_images(record.file_path)
                        if images:
                            result = evaluate_document(images)
                            if result:
                                record.priority_score = result['priority_score']
                                record.summary = result['summary']
                                record.language = result['language']
                                record.ai_justification = result['ai_justification']
                                record.status = "EVALUATED"
                                session.commit()
                                st.rerun()

        # -------------------------
        # TAB 2: OCR FACTORY
        # -------------------------
        with tab_ocr:
            ocr = OcrEngine(record.file_path)
            
            # 1. Image Generation Check
            # We look for the hidden temp folder to see if images exist
            has_images = os.path.exists(ocr.cache_dir) and len(glob.glob(os.path.join(ocr.cache_dir, "*.png"))) > 0
            
            if not has_images:
                st.info("Step 1: Generate PNGs from PDF.")
                if st.button("📸 Generate Images", type="primary"):
                    with st.spinner("Running pdftoppm..."):
                        count = ocr.generate_images()
                        st.success(f"Generated {count} images!")
                        st.rerun()
            else:
                st.success("✅ Images Ready")
                
                # Spot Checker (Outside Form)
                with st.expander("🔍 Spot Checker (Verify Pages)"):
                    check_page = st.number_input("Check Page #", min_value=1, value=1)
                    # Find file with padding logic (001 vs 0001)
                    # We just glob for it because we don't know the exact padding here easily
                    # A robust way is to just listdir and sort
                    images = sorted(glob.glob(os.path.join(ocr.cache_dir, "*.png")), key=ocr._natural_sort_key)
                    if 0 <= check_page-1 < len(images):
                        st.image(images[check_page-1], caption=f"Image {check_page}")
                    else:
                        st.error("Page out of range")

                # Configuration Form
                with st.form("ocr_config_form"):
                    st.subheader("Step 2: Configuration")
                    
                    lang_opts = ["eng", "fas", "deu", "fra", "spa", "rus"]
                    # Try to auto-select language based on AI analysis
                    default_idx = 0
                    if record.language and "German" in record.language: default_idx = 2
                    if record.language and "Persian" in record.language: default_idx = 1
                    
                    sel_lang = st.selectbox("Language", lang_opts, index=default_idx)
                    
                    has_cover = st.checkbox("Has Cover Image?", value=True)
                    
                    first_num = st.number_input("Start of 'Page 1'", min_value=1, value=14, 
                                              help="The image number where the printed 'Page 1' begins.")
                    
                    illus_text = st.text_input("Illustration Ranges", placeholder="e.g. 48-55, 102-105", 
                                             help="Ranges will be labeled 'illus.X'")
                    
                    submitted = st.form_submit_button("🚀 Start OCR Job", type="primary")
                
                if submitted:
                    # Parse Inputs
                    ranges = parse_ranges(illus_text)
                    config = OcrConfig(
                        has_cover_image=has_cover,
                        first_numbered_page_index=int(first_num),
                        illustration_ranges=ranges,
                        language=sel_lang
                    )
                    
                    # Run OCR
                    progress_bar = st.progress(0, text="Starting Tesseract...")
                    
                    def update_prog(curr, total):
                        progress_bar.progress(curr / total, text=f"Processing Page {curr}/{total}")

                    try:
                        final_path = ocr.run_ocr(config, progress_callback=update_prog)
                        st.success(f"OCR Complete! Saved to: {os.path.basename(final_path)}")
                        
                        # Mark as Digitized in DB
                        record.status = "DIGITIZED"
                        session.commit()
                        
                        # Cleanup Button
                        if st.button("🧹 Cleanup Temp Images"):
                            ocr.cleanup()
                            st.rerun()
                            
                    except Exception as e:
                        st.error(f"OCR Failed: {e}")

# --- Main App Execution ---

st.title("📚 Bahai.works Prioritization Engine")

# 1. Load Data
df = load_data()

# 2. Metrics
m1, m2, m3, m4 = st.columns(4)
total, pending, completed, high_pri = get_metrics(df)
m1.metric("Total Documents Found", total)
m2.metric("Pending AI Review", pending)
m3.metric("Digitized", completed)
m4.metric("High Priority (>8)", high_pri)

st.markdown("---")

# 3. Main Interactive Table
st.subheader("Document Queue")

if "selected_doc_id" not in st.session_state:
    st.session_state.selected_doc_id = None

# UPDATED: Added tab3 for "Digitized"
tab1, tab2, tab3 = st.tabs(["All Files", "High Priority Only", "Digitized"])
display_cols = ['id', 'filename', 'status', 'priority_score', 'language']

with tab1:
    event = st.dataframe(
        df[display_cols],
        width="stretch",
        hide_index=True,
        selection_mode="single-row",
        on_select="rerun",
        key="main_table"
    )
    if len(event.selection['rows']) > 0:
        idx = event.selection['rows'][0]
        st.session_state.selected_doc_id = int(df.iloc[idx]['id'])

with tab2:
    if not df.empty and 'priority_score' in df.columns:
        high_pri_df = df[df['priority_score'] >= 8]
        event_hp = st.dataframe(
            high_pri_df[display_cols], 
            width="stretch", 
            hide_index=True,
            selection_mode="single-row",
            on_select="rerun",
            key="hp_table"
        )
        if len(event_hp.selection['rows']) > 0:
            idx = event_hp.selection['rows'][0]
            st.session_state.selected_doc_id = int(high_pri_df.iloc[idx]['id'])
    else:
        st.info("No documents evaluated yet.")

# NEW: Tab 3 Logic
with tab3:
    if not df.empty:
        digitized_df = df[df['status'] == 'DIGITIZED']
        event_dig = st.dataframe(
            digitized_df[display_cols], 
            width="stretch", 
            hide_index=True,
            selection_mode="single-row",
            on_select="rerun",
            key="dig_table"
        )
        if len(event_dig.selection['rows']) > 0:
            idx = event_dig.selection['rows'][0]
            st.session_state.selected_doc_id = int(digitized_df.iloc[idx]['id'])
    else:
        st.info("No digitized documents found.")

# 4. Render Sidebar (Caller)
with st.sidebar:
    if st.session_state.selected_doc_id is not None:
        render_details(st.session_state.selected_doc_id)
    else:
        st.info("Select a document from the table to view details.")
