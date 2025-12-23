import streamlit as st
import re
from src.mediawiki_uploader import upload_to_bahaiworks

st.set_page_config(
    page_title="Bahai.works Utilities",
    layout="wide"
)

st.title("🛠️ Miscellaneous Utilities")

if st.button("← Back to Dashboard"):
    st.switch_page("app.py")

st.markdown("---")

tab_author, tab_maintenance = st.tabs(["👤 Author Manager", "🔧 System Maintenance"])

# --- HELPER FUNCTIONS ---

def get_lastname_firstname(full_name):
    """Converts 'Aaron Emmel' to 'Emmel, Aaron'"""
    parts = full_name.strip().split()
    if len(parts) == 1:
        return parts[0]
    else:
        lastname = parts[-1]
        firstname = ' '.join(parts[:-1])
        return f"{lastname}, {firstname}"

def format_author_page(name, book_title=None, book_year=None, use_dynamic=True):
    """
    Generates the content for the Author:Name page.
    If use_dynamic is True, it uses the Lua module.
    Otherwise, it links the specific book provided.
    """
    content = "{{author2}}\n\n===Publications===\n"
    
    if use_dynamic:
        content += "==== Contributing author====\n{{#invoke:Chapters|getChaptersByAuthor}}\n"
    else:
        if book_title and book_year:
            content += f"[[{book_title}]] ({book_year})\n"
        elif book_title:
             content += f"[[{book_title}]]\n"
             
    content += "\n__NOTOC__"
    return content

def format_author_cat_page(name):
    """Generates content for Category:Name"""
    sort_key = get_lastname_firstname(name)
    return f"{{{{authorcat_desc}}}}\n[[Category:Authors|{sort_key}]]"

def format_works_cat_page(name):
    """Generates content for Category:Text_of_works_by_Name"""
    return f"{{{{Textof_desc}}}}\n[[Category:{name}]]"

# --- TAB: AUTHOR MANAGER ---

with tab_author:
    st.header("Create Author Pages")
    st.info("This tool creates the three required pages for a new author on Bahai.works.")

    c1, c2 = st.columns(2)
    
    with c1:
        author_name = st.text_input("Author Name", placeholder="e.g. Aaron Emmel")
        
        # Options for the Author Page content
        st.subheader("Configuration")
        content_mode = st.radio("Publications List Style", ["Dynamic (Lua Module)", "Static (Specific Book)"])
        
        book_title = ""
        book_year = ""
        
        if content_mode == "Static (Specific Book)":
            book_title = st.text_input("Book Title", placeholder="e.g. The Bahá'í Faith")
            book_year = st.text_input("Year", placeholder="e.g. 1919")

    with c2:
        st.subheader("Preview Actions")
        
        if author_name:
            # Calculate Page Titles
            p_author = f"Author:{author_name.strip()}"
            p_cat_main = f"Category:{author_name.strip()}"
            p_cat_works = f"Category:Text_of_works_by_{author_name.strip().replace(' ', '_')}"

            # 1. Author Page
            st.markdown(f"**1. {p_author}**")
            use_dyn = (content_mode == "Dynamic (Lua Module)")
            txt_author = format_author_page(author_name, book_title, book_year, use_dyn)
            st.code(txt_author, language="mediawiki")

            # 2. Main Category
            st.markdown(f"**2. {p_cat_main}**")
            txt_cat_main = format_author_cat_page(author_name)
            st.code(txt_cat_main, language="mediawiki")

            # 3. Works Category
            st.markdown(f"**3. {p_cat_works}**")
            txt_cat_works = format_works_cat_page(author_name)
            st.code(txt_cat_works, language="mediawiki")

            st.divider()

            if st.button(f"🚀 Create All Pages for '{author_name}'", type="primary"):
                # We use a progress bar because we are making 3 network requests
                prog = st.progress(0)
                status = st.empty()

                try:
                    # Step 1
                    status.write(f"Creating {p_author}...")
                    upload_to_bahaiworks(p_author, txt_author, "Created Author page (Misc Tool)")
                    prog.progress(33)

                    # Step 2
                    status.write(f"Creating {p_cat_main}...")
                    upload_to_bahaiworks(p_cat_main, txt_cat_main, "Created Author Category (Misc Tool)")
                    prog.progress(66)

                    # Step 3
                    status.write(f"Creating {p_cat_works}...")
                    upload_to_bahaiworks(p_cat_works, txt_cat_works, "Created Text of Works Category (Misc Tool)")
                    prog.progress(100)

                    status.success(f"✅ Successfully created all pages for {author_name}!")
                    st.balloons()

                except Exception as e:
                    status.error(f"Error: {e}")
                    # In production, you might want to check for 'articleexists' error specifically 
                    # inside upload_to_bahaiworks or catch it here if the API raises it.

with tab_maintenance:
    st.write("Future database cleanup scripts or logs will go here.")
