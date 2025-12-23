import streamlit as st
import pandas as pd
from sqlalchemy.orm import Session
from src.database import engine

# Page Configuration
st.set_page_config(
    page_title="Bahai.works Utilities",
    layout="wide"
)

st.title("🛠️ Miscellaneous Utilities")

# Navigation back to Home
if st.button("← Back to Dashboard"):
    st.switch_page("app.py")

st.markdown("---")

# We will add tabs here for different utilities
tab_author, tab_maintenance = st.tabs(["👤 Author Manager", "🔧 System Maintenance"])

with tab_author:
    st.header("Create or Link Author")
    st.info("Logic for checking and creating bahai.works Author pages will go here.")

with tab_maintenance:
    st.write("Future database cleanup scripts or logs.")
