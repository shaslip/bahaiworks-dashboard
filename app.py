import subprocess
import platform
import os
from src.processor import extract_preview_images
from src.evaluator import evaluate_document
import streamlit as st
import pandas as pd
from sqlalchemy.orm import Session
from sqlalchemy import select, desc

# Local imports
from src.database import engine, Document

# --- Configuration ---
st.set_page_config(
    page_title="Bahai.works Digitization Dashboard",
    layout="wide",
    initial_sidebar_state="expanded"
)

# --- Helper Functions ---
def load_data():
    """Fetches all documents from the database into a Pandas DataFrame."""
    with Session(engine) as session:
        # Select all fields
        query = select(Document.id, 
                       Document.filename, 
                       Document.status, 
                       Document.priority_score, 
                       Document.language, 
                       Document.summary,
                       Document.file_path)
        
        # Load into DataFrame
        df = pd.read_sql(query, session.bind)
        return df

def get_metrics(df):
    """Calculates basic stats for the top bar."""
    total = len(df)
    pending = len(df[df['status'] == 'PENDING'])
    completed = len(df[df['status'] == 'DIGITIZED'])
    high_priority = len(df[df['priority_score'] >= 8]) if 'priority_score' in df else 0
    return total, pending, completed, high_priority

# --- UI Layout ---
st.title("📚 Bahai.works Prioritization Engine")

# 1. Load Data
df = load_data()

# 2. Metrics Row
m1, m2, m3, m4 = st.columns(4)
total, pending, completed, high_pri = get_metrics(df)

m1.metric("Total Documents Found", total)
m2.metric("Pending AI Review", pending)
m3.metric("Digitized", completed)
m4.metric("High Priority (>8)", high_pri)

st.markdown("---")

# 3. Main Data View
st.subheader("Document Queue")

# Filter Tabs
tab1, tab2 = st.tabs(["All Files", "High Priority Only"])

with tab1:
    # Main interactive table
    # We hide 'file_path' and 'summary' from the main view to keep it clean
    display_cols = ['id', 'filename', 'status', 'priority_score', 'language']
    
    event = st.dataframe(
        df[display_cols],
        use_container_width=True,
        hide_index=True,
        selection_mode="single-row",
        on_select="rerun" # Allows us to detect clicks
    )

with tab2:
    if not df.empty and 'priority_score' in df.columns:
        high_pri_df = df[df['priority_score'] >= 8]
        st.dataframe(high_pri_df[display_cols], use_container_width=True, hide_index=True)
    else:
        st.info("No documents evaluated yet.")

# 4. Detail View (Sidebar)
if len(event.selection['rows']) > 0:
    selected_index = event.selection['rows'][0]
    selected_id = int(df.iloc[selected_index]['id']) # Ensure ID is native Python int
    
    # Fetch full details
    # We use a fresh session query here to ensure we get the absolute latest data 
    # (in case you just updated it)
    with Session(engine) as session:
        record = session.get(Document, selected_id)
        
        with st.sidebar:
            st.header("📄 Document Details")
            st.write(f"**Filename:** {record.filename}")
            
            # Open File Button
            if st.button("📂 Open Local File"):
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

            st.divider()
            
            st.subheader("AI Analysis")
            
            # --- CONDITION: If already evaluated, show results ---
            if record.priority_score is not None:
                st.metric("Priority Score", f"{record.priority_score}/10")
                st.write(f"**Language:** {record.language}")
                st.info(f"**Summary:** {record.summary}")
                with st.expander("See AI Justification"):
                    st.caption(record.ai_justification)
                
                # Optional: Re-run button
                if st.button("🔄 Re-evaluate"):
                     # Reset score to None in DB and rerun logic (implementation implied)
                     pass

            # --- CONDITION: If Pending, show Action Button ---
            else:
                st.warning("Status: Pending Analysis")
                
                if st.button("✨ Run AI Evaluation", type="primary"):
                    with st.spinner("Extracting pages and reading with Gemini..."):
                        
                        # 1. Extract Images (The Eyes)
                        images = extract_preview_images(record.file_path)
                        
                        if not images:
                            st.error("Failed to extract images from PDF. File might be corrupted or password protected.")
                        else:
                            # 2. Evaluate (The Brain)
                            result = evaluate_document(images)
                            
                            if result:
                                # 3. Update Database
                                record.priority_score = result['priority_score']
                                record.summary = result['summary']
                                record.language = result['language']
                                record.ai_justification = result['ai_justification']
                                record.status = "EVALUATED"
                                
                                session.commit()
                                st.success("Analysis Complete!")
                                st.rerun() # Refresh app to show new data
                            else:
                                st.error("AI returned no results. Check API Key or Console logs.")

else:
    with st.sidebar:
        st.info("Select a document from the table to view details.")

# 5. Action Buttons (Placeholder)
if st.sidebar.button("Refresh Database"):
    st.rerun()
