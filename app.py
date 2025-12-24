import streamlit as st
import pandas as pd
import subprocess
import platform
import os
import re
from sqlalchemy.orm import Session
from sqlalchemy import select, desc
from src.evaluator import evaluate_document, translate_summary
from src.database import engine, Document
from src.processor import extract_preview_images
from src.evaluator import evaluate_document

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
    pending = len(df[df['status'].isin(['PENDING', 'READY_FOR_OCR'])]) 
    digitized = len(df[df['status'] == 'DIGITIZED'])
    completed = len(df[df['status'] == 'COMPLETED'])
    high_priority = len(df[df['priority_score'] >= 8]) if not df.empty else 0
    return total, pending, digitized, completed, high_priority

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

def parse_filename_metadata(filename):
    """
    Attempts to extract Year, Author, and Title from:
    'G 020 Schw D 1919 - Schwarz-Alice - Die Universale Weltreligion.pdf'
    """
    meta = {"year": "", "author": "", "title": ""}
    
    # Remove extension
    clean_name = os.path.splitext(filename)[0]
    
    # Regex for standard pattern: Code - Author - Title
    # Looks for '1919 - ' separator
    match = re.search(r"(\d{4})\s*-\s*(.*?)\s*-\s*(.*)", clean_name)
    if match:
        meta["year"] = match.group(1)
        # Flip 'Schwarz-Alice' to 'Alice Schwarz' if hyphenated
        author_part = match.group(2).replace("-", " ").strip()
        # Optional: heuristic to flip Last First -> First Last? 
        # For now, just keeping it clean.
        meta["author"] = author_part 
        meta["title"] = match.group(3)
    else:
        # Fallback: Use whole name as title
        meta["title"] = clean_name
        
    return meta

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

        # === TABS ===
        tab_ai, tab_pub = st.tabs(["🤖 AI Analyst", "📝 Publisher"])

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
        # TAB 2: PUBLISHER (NEW)
        # -------------------------
        with tab_pub:
            st.subheader("🌐 Wikitext Generator")
            
            # 1. Parse Metadata Defaults
            defaults = parse_filename_metadata(record.filename)
            
            # 2. Controls
            pub_type = st.radio("Publication Type", 
                                ["Periodical (PDF Only)", "Unstructured (Summary + Links)", "Book (Full TOC)"],
                                index=1)
            
            # === NEW BOOK PIPELINE REDIRECT ===
            if "Book" in pub_type:
                st.info("📚 **Advanced Workflow Required**")
                st.write("Books require the dedicated pipeline for copyright verification, TOC extraction, and chapter splitting.")
                
                if st.button("🚀 Launch Book Pipeline", type="primary"):
                    st.switch_page("pages/book_pipeline.py")
                
                # Stop rendering the rest of this simple tab
                return
            # ==================================
            
            c_meta1, c_meta2 = st.columns(2)
            with c_meta1:
                pub_title = st.text_input("Title", value=defaults['title'])
                pub_author = st.text_input("Author", value=defaults['author'])
            with c_meta2:
                pub_year = st.text_input("Year", value=defaults['year'])

            suggested_name = f"{pub_title.strip()}.pdf"
            pub_filename = st.text_input("Target Filename (MediaWiki)", value=suggested_name)

            # --- START OF FIXED BLOCK ---
            
            # Summary Editor
            summary_key = f"summary_{record.id}"
            
            # Initialize session state if not set
            if summary_key not in st.session_state:
                st.session_state[summary_key] = record.summary or ""

            st.write("**Summary (German)**")
            
            # 1. Create a placeholder for the text area
            summary_placeholder = st.empty()
            
            # 2. Render the button and handle logic BEFORE the text area is instantiated
            if st.button("🤖 Translate to German"):
                with st.spinner("Translating..."):
                    current_text = st.session_state.get(summary_key, "")
                    german_text = translate_summary(current_text)
                    if german_text:
                        st.session_state[summary_key] = german_text
            
            # 3. Render the text area into the placeholder
            pub_summary = summary_placeholder.text_area("Summary", height=150, key=summary_key)
            
            # --- END OF FIXED BLOCK ---

            st.divider()
            
            # 3. Generate Logic
            st.subheader("Preview")
            
            wiki_text = ""
            clean_title_url = pub_title.replace(" ", "_") # Rough URL encoding
            
            # Wrap summary in {{ai}} template
            summary_block = f"{{{{ai|{pub_summary}}}}}" if pub_summary else ""

            # HEADER Template (Common)
            header = f"""{{{{header
 | title      = {pub_title}
 | author     = {pub_author}
 | translator = 
 | section    = 
 | previous   = 
 | next       = 
 | year       = {pub_year}
 | notes      = {{{{home |link= | pdf=[{{{{filepath:{pub_filename}}}}} PDF] }}}}
}}}}"""

            if "Periodical" in pub_type:
                wiki_text = f"""{header}

<pdf>File:{pub_filename}</pdf>"""

            elif "Unstructured" in pub_type:
                wiki_text = f"""{header}

{summary_block}

== Zugang ==
* [{{{{filepath:{pub_filename}}}}} PDF]
* Für den Volltext siehe [[/Text]]."""

            st.code(wiki_text, language="mediawiki")
            
            # Link to text page
            st.info(f"**Target URL:** de.bahai.works/{clean_title_url}")
            st.info(f"**Text URL:** de.bahai.works/{clean_title_url}/Text")

            st.markdown("---")

            if st.button("✅ Mark as Completed (Remove from Queue)", type="primary"):
                record.status = "COMPLETED"
                session.commit()
                st.success("Document marked as COMPLETED!")
                st.rerun()

# --- Main App Execution ---

st.title("📚 Bahai.works Prioritization Engine")

# 1. Load Data
df = load_data()

# 2. Metrics
m1, m2, m3, m4, m5 = st.columns(5)
total, pending, digitized, completed, high_pri = get_metrics(df)

m1.metric("Total Documents", total)
m2.metric("Pending AI", pending)
m3.metric("Digitized (Queue)", digitized)
m4.metric("Completed", completed)
m5.metric("High Priority", high_pri)

st.markdown("---")

# 3. Main Interactive Table
st.subheader("Document Queue")

if "selected_doc_id" not in st.session_state:
    st.session_state.selected_doc_id = None

# UPDATED: Added tab4 for "Completed"
tab1, tab2, tab3, tab4 = st.tabs(["All Files", "High Priority Only", "Digitized", "Completed"])
display_cols = ['id', 'filename', 'status', 'priority_score', 'language']

with tab1:
    # UPDATED: Filter out 'Completed' status
    filtered_df = df[df['status'] != 'COMPLETED']

    event = st.dataframe(
        filtered_df[display_cols],
        width="stretch",
        hide_index=True,
        selection_mode="single-row",
        on_select="rerun",
        key="main_table"
    )
    if len(event.selection['rows']) > 0:
        idx = event.selection['rows'][0]
        # UPDATED: Use filtered_df to get the correct ID
        st.session_state.selected_doc_id = int(filtered_df.iloc[idx]['id'])

with tab2:
    if not df.empty and 'priority_score' in df.columns:
        # UPDATED: Filter priority >= 8 AND exclude "Completed" status
        high_pri_df = df[(df['priority_score'] >= 8) & (df['status'] != 'COMPLETED')]
        
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

with tab4:
    if not df.empty:
        completed_df = df[df['status'] == 'COMPLETED']
        event_comp = st.dataframe(
            completed_df[display_cols], 
            width="stretch", 
            hide_index=True,
            selection_mode="single-row",
            on_select="rerun",
            key="comp_table"
        )
        if len(event_comp.selection['rows']) > 0:
            idx = event_comp.selection['rows'][0]
            st.session_state.selected_doc_id = int(completed_df.iloc[idx]['id'])
    else:
        st.info("No completed documents yet.")

# 4. Render Sidebar (Caller)
with st.sidebar:
    # Existing code for document details...
    if st.session_state.selected_doc_id is not None:
        render_details(st.session_state.selected_doc_id)
    else:
        st.info("Select a document from the table to view details.")

    # --- ADD THIS SECTION BELOW ---
    st.markdown("---")
    st.subheader("🛠️ Utilities")
    
    if st.button("⚡ Open Misc Tasks", width="stretch"):
        st.switch_page("pages/misc_tasks.py")
