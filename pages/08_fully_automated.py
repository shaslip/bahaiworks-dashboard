import streamlit as st
import os
import sys
import json
import re
import time
import unicodedata
import fitz  # PyMuPDF
import requests
import concurrent.futures

# --- Path Setup ---
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
if project_root not in sys.path:
    sys.path.append(project_root)

# --- Imports ---
from src.batch_worker import process_single_page, _fitz_lock
from src.mediawiki_uploader import (
    upload_to_bahaiworks, 
    API_URL, 
    fetch_wikitext,
    fetch_wikitext_batch,
    inject_text_into_page, 
    generate_header, 
    cleanup_page_seams,
    get_csrf_token,
    update_header_ps_tag
)

# --- Configuration ---
if 'GEMINI_API_KEY' not in os.environ:
    st.error("GEMINI_API_KEY not found. Check your .env file.")
    st.stop()

st.set_page_config(page_title="Fully Automated Proofreader", page_icon="🤖", layout="wide")

# ==============================================================================
# 1. HELPER FUNCTIONS
# ==============================================================================
# --- CUSTOM TEMPLATE CONFIGURATION ---
CUSTOM_PAGE_CONFIGS = {
    "swpage": {
        "module_title": "Module:SotW",
        "file_format": "SW_Volume{vol}.pdf",
        "offset_map_name": "pdfOffset_map"
    },
    "bwpage": {
        "module_title": "Module:BahaiWorld", 
        "file_format": "BW_Volume{vol}.pdf", 
        "offset_map_name": "pdfOffset_map"
    }
}

def get_module_offsets(module_title, map_name, session):
    """Fetches the Lua module and extracts the offset map into a Python dict."""
    wikitext, error = fetch_wikitext(module_title, session=session)
    if error:
        st.error(f"Failed to fetch {module_title}: {error}")
        return {}

    offsets = {}
    # Find the table block: local pdfOffset_map = { ... }
    table_match = re.search(fr'local\s+{map_name}\s*=\s*\{{([^}}]+)\}}', wikitext, re.IGNORECASE)
    if table_match:
        table_content = table_match.group(1)
        # Find all [key]="value" or [key]=value pairs
        pairs = re.finditer(r'\[(\d+)\]\s*=\s*["\']?(-?\d+)["\']?', table_content)
        for p in pairs:
            offsets[int(p.group(1))] = int(p.group(2))
    return offsets

def normalize_page_templates(wikitext, session):
    """Finds custom templates and converts them to standard {{page|...}} tags."""
    offset_caches = {}

    for template_name, config in CUSTOM_PAGE_CONFIGS.items():
        # Matches {{swpage|11|105}} -> group 1: vol, group 2: page
        pattern = fr'\{{\{{\s*{template_name}\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\}}\}}'

        def repl(match):
            vol = int(match.group(1))
            page = int(match.group(2))

            # Lazy load the module offset map only if needed
            if template_name not in offset_caches:
                offset_caches[template_name] = get_module_offsets(
                    config["module_title"],
                    config["offset_map_name"],
                    session
                )

            offset = offset_caches[template_name].get(vol, 0)
            pdf_page = page + offset
            filename = config["file_format"].format(vol=vol)

            return f"{{{{page|{page}|file={filename}|page={pdf_page}}}}}"

        wikitext = re.sub(pattern, repl, wikitext, flags=re.IGNORECASE)

    return wikitext

def int_to_roman(num):
    """Converts an integer to a lowercase roman numeral (1 -> i, 5 -> v)."""
    val = [1000, 900, 500, 400, 100, 90, 50, 40, 10, 9, 5, 4, 1]
    syb = ["m", "cm", "d", "cd", "c", "xc", "l", "xl", "x", "ix", "v", "iv", "i"]
    roman_num = ''
    i = 0
    num = abs(num)
    if num == 0: return "i" 

    while num > 0:
        for _ in range(num // val[i]):
            roman_num += syb[i]
            num -= val[i]
        i += 1
    return roman_num

MEDIA_API_URL = "https://bahai.media/api.php"

PERIODICAL_LUA_MODULES = {
    "TheAmericanBahai": "Module:AmericanBahai",
}

def fetch_periodical_lua_maps(base_title, session=None):
    """
    Fetches Module:AmericanBahai (or matching module) from bahai.media (fallback bahai.works)
    and parses pdfDoublePage_map, pdfOffset_map, and publicationDate_map.
    """
    pub_key = format_access_pub_name(base_title)
    module_title = PERIODICAL_LUA_MODULES.get(pub_key, f"Module:{pub_key}")

    wikitext, err = fetch_wikitext(module_title, session=session, api_url=MEDIA_API_URL)
    if err or not wikitext:
        wikitext, err = fetch_wikitext(module_title, session=session, api_url=API_URL)
    if err or not wikitext:
        return {"double_map": {}, "offset_map": {}, "date_map": {}}

    def _parse_int_table(map_name):
        res = {}
        m = re.search(fr'local\s+{map_name}\s*=\s*\{{([^}}]+)\}}', wikitext, re.IGNORECASE)
        if m:
            for p in re.finditer(r'\[\s*0*(\d+)\s*\]\s*=\s*["\']?(-?\d+)["\']?', m.group(1)):
                res[int(p.group(1))] = int(p.group(2))
        return res

    return {
        "double_map": _parse_int_table("pdfDoublePage_map"),
        "offset_map": _parse_int_table("pdfOffset_map"),
        "date_map": _parse_int_table("publicationDate_map"),
    }

def get_issue_lua_config(vol, iss, lua_maps):
    """Returns (offset_val, double_page_val, pub_year) for a given (vol, iss)."""
    if not lua_maps or not vol or not iss:
        return 0, 0, None
    try:
        v_num = int(vol)
        i_num = int(str(iss).split('-')[0])
        key = int(f"{v_num:02d}{i_num:02d}")
    except ValueError:
        return 0, 0, None

    offset_val = lua_maps.get("offset_map", {}).get(key, 0)
    double_val = lua_maps.get("double_map", {}).get(key, 0)
    date_map = lua_maps.get("date_map", {})
    pub_year = date_map.get(v_num)
    if not pub_year and date_map:
        # Extrapolate if linear (e.g. Vol 1 = 1970 -> Vol 34 = 2003)
        min_v = min(date_map.keys())
        pub_year = date_map[min_v] + (v_num - min_v)

    return offset_val, double_val, (str(pub_year) if pub_year else None)

def calculate_page_label(pdf_page_num, anchor_pdf_page, double_page_pub_num=0):
    """
    Determines the printed page label for a given 1-based PDF page number:
    - Roman numerals (i, ii) for front matter before anchor_pdf_page
    - 'X-Y' (e.g. '16-17') on the combined double-page spread
    - Shifted by +1 for all pages after the double-page spread
    """
    eff_anchor = anchor_pdf_page if anchor_pdf_page is not None else 1

    if pdf_page_num < eff_anchor:
        return int_to_roman(pdf_page_num)

    pub_page = pdf_page_num - eff_anchor + 1

    if double_page_pub_num and double_page_pub_num > 0:
        if pub_page < double_page_pub_num:
            return str(pub_page)
        elif pub_page == double_page_pub_num:
            return f"{pub_page}-{pub_page + 1}"
        else:
            return str(pub_page + 1)

    return str(pub_page)

def find_anchor_offset(wikitext):
    """Scans for any {{page|N|...|page=X}} where N is an integer to establish the offset."""
    tags = re.finditer(r'\{\{page\|(.*?)\}\}', wikitext, re.IGNORECASE | re.DOTALL)
    
    for match in tags:
        params = match.group(1)
        label = params.split('|')[0].strip()
        
        if label.isdigit():
            page_match = re.search(r'page\s*=\s*(\d+)', params, re.IGNORECASE)
            if page_match:
                book_page = int(label)
                pdf_page = int(page_match.group(1))
                return pdf_page - book_page + 1
    return None

def find_and_fix_tag_by_page_num(wikitext, pdf_filename, pdf_page_num, correct_label):
    """Robustly finds {{page|...|file=...|page=X}} regardless of the existing label."""
    tags = list(re.finditer(r'(\{\{page\|(.*?)\}\})(\s*\{\{ocr\}\})?', wikitext, re.IGNORECASE | re.DOTALL))
    
    for match in tags:
        full_tag_block = match.group(0)
        params = match.group(2)
        
        file_check = re.search(r'file\s*=\s*([^|}\n]+)', params, re.IGNORECASE)
        page_check = re.search(r'page\s*=\s*(\d+)', params, re.IGNORECASE)
        
        if file_check and page_check:
            found_filename = file_check.group(1).strip()
            if int(page_check.group(1)) == pdf_page_num and \
               os.path.basename(found_filename).lower() == os.path.basename(pdf_filename).lower():
                new_tag = f"{{{{page|{correct_label}|file={pdf_filename}|page={pdf_page_num}}}}}"
                wikitext = wikitext.replace(full_tag_block, new_tag)
                return wikitext
    return wikitext

def build_local_pdf_map(root_folder):
    """Recursively finds all PDF files and maps lowercase filename -> full local path."""
    pdf_map = {}
    for dirpath, _, filenames in os.walk(root_folder):
        for f in filenames:
            if f.lower().endswith(".pdf"):
                # Normalize spaces/underscores in keys so MediaWiki titles match local files
                norm_key = f.lower().replace(" ", "_")
                pdf_map[norm_key] = os.path.join(dirpath, f)
    return pdf_map

def get_all_wiki_text_pages(base_wiki_title, session=None):
    """Queries MediaWiki API for all subpages under base_wiki_title ending in /Text."""
    req_session = session or requests
    prefix = base_wiki_title.replace("_", " ").strip()
    if not prefix.endswith("/"):
        prefix += "/"
        
    pages = []
    apcontinue = None
    
    while True:
        params = {
            "action": "query",
            "list": "allpages",
            "apprefix": prefix,
            "apnamespace": 0,
            "aplimit": "max",
            "format": "json"
        }
        if apcontinue:
            params["apcontinue"] = apcontinue
            
        res = req_session.get(API_URL, params=params, timeout=30).json()
        for p in res.get("query", {}).get("allpages", []):
            title = p["title"]
            if title.endswith("/Text"):
                pages.append(title)
                
        if "continue" in res:
            apcontinue = res["continue"]["apcontinue"]
        else:
            break
            
    # Natural sort (so Volume 2 / Issue 2 comes before Volume 10 / Issue 10)
    pages.sort(key=lambda x: [int(c) if c.isdigit() else c.lower() for c in re.split(r'(\d+)', x)])
    return pages

def extract_pdf_filename_from_wikitext(wikitext):
    """Extracts the PDF filename from the first {{page|...|file=FILENAME.pdf|...}} tag."""
    match = re.search(r'\{\{page\|[^}]*?file\s*=\s*([^|}\n]+)', wikitext, re.IGNORECASE | re.DOTALL)
    if match:
        return match.group(1).strip()
    return None

def has_ps1_tag(wikitext):
    """Checks if the wikitext already contains {{ps|1}}."""
    if not wikitext:
        return False
    return bool(re.search(r'\{\{\s*ps\s*\|\s*1\s*\}\}', wikitext, re.IGNORECASE))

def parse_vol_issue_from_filename(filename):
    """Extracts (volume, issue) strings from filenames like The_American_Bahá’í_Vol34_No1.pdf."""
    base = os.path.splitext(filename)[0]
    m_vol_iss = re.search(r'(?:Vol(?:ume)?[\s_.-]*(\d+))[\s_.-]*(?:No|Issue|Iss|Number)?[\s_.-]*(\d+(?:-\d+)?)', base, re.IGNORECASE)
    if m_vol_iss:
        return m_vol_iss.group(1), m_vol_iss.group(2)

    m_iss = re.search(r'(?:No|Issue|Iss|Number)[\s_.-]*(\d+(?:-\d+)?)', base, re.IGNORECASE)
    if m_iss:
        return None, m_iss.group(1)

    nums = re.findall(r'\d+(?:-\d+)?', base)
    if len(nums) >= 2:
        return nums[-2], nums[-1]
    elif len(nums) == 1:
        return None, nums[0]
    return None, None

def inspect_wiki_structure(base_title, existing_text_pages, session):
    """Fetches a sample existing base page to learn naming conventions and media category format."""
    info = {
        "has_volume": True,
        "vol_sep": "_",
        "issue_sep": "_",
        "prev_next_space": " ",
        "media_prefix": None,
        "sample_base_wikitext": ""
    }
    if not existing_text_pages:
        return info

    sample_text_title = existing_text_pages[-1]
    sample_base_title = re.sub(r'/Text$', '', sample_text_title)

    m_title = re.search(r'/Volume([_ ])\d+/Issue([_ ])[\d-]+$', sample_base_title, re.IGNORECASE)
    if m_title:
        info["has_volume"] = True
        info["vol_sep"] = m_title.group(1)
        info["issue_sep"] = m_title.group(2)
    else:
        m_no_vol = re.search(r'/Issue([_ ])[\d-]+$', sample_base_title, re.IGNORECASE)
        if m_no_vol:
            info["has_volume"] = False
            info["issue_sep"] = m_no_vol.group(1)

    base_wt, _ = fetch_wikitext(sample_base_title, session=session)
    if base_wt:
        info["sample_base_wikitext"] = base_wt
        m_prev = re.search(r'\[\[\.\./Issue([_ ])\d+\|Previous\]\]', base_wt, re.IGNORECASE)
        if m_prev:
            info["prev_next_space"] = m_prev.group(1)

        m_media = re.search(r'media\s*=\s*Category:([^\|\}\n]+)', base_wt, re.IGNORECASE)
        if m_media:
            raw_cat = m_media.group(1).strip()
            m_cat_pat = re.match(r'^(.*?)\s*Volume\s*\d+\s*No\s*[\d-]+', raw_cat, re.IGNORECASE)
            if m_cat_pat:
                info["media_prefix"] = m_cat_pat.group(1).strip()

    return info

def format_access_pub_name(base_title):
    """Converts 'The_American_Bahá’í' -> 'TheAmericanBahai'."""
    if not base_title:
        return "PublicationName"
    normalized = unicodedata.normalize('NFKD', base_title)
    ascii_text = "".join(c for c in normalized if not unicodedata.combining(c))
    cleaned = re.sub(r'[^A-Za-z0-9]', '', ascii_text)
    return cleaned or "PublicationName"

def get_access_tag(copyright_mode, base_title=""):
    """Returns the accesscontrol tag prefix based on the selected copyright status."""
    pub_name = format_access_pub_name(base_title)
    if copyright_mode == "2) Restricted":
        return f"<accesscontrol>Access:{pub_name}</accesscontrol>\n"
    elif copyright_mode == "3) NoSearch":
        return f"<accesscontrol>(nosearch),Access:{pub_name}</accesscontrol>\n"
    return ""

def calc_prev_next_nums(issue_str):
    if '-' in str(issue_str):
        parts = str(issue_str).split('-')
        return int(parts[0]) - 1, int(parts[-1]) + 1
    curr = int(issue_str)
    return curr - 1, curr + 1

def build_new_base_page_wikitext(vol, iss, pdf_filename, struct_info, copyright_mode, base_title=""):
    """Generates the wikitext for the Base Issue page (e.g. .../Volume_34/Issue_1)."""
    access_tag = get_access_tag(copyright_mode, base_title)
    prev_num, next_num = calc_prev_next_nums(iss)
    pn_sep = struct_info.get("prev_next_space", " ")
    pdf_space_name = pdf_filename.replace("_", " ")

    if vol:
        title_link = "[[../../]]"
        section_str = f"Volume {vol}, Issue {iss}"
    else:
        title_link = "[[../]]"
        section_str = f"Issue {iss}"

    prev_link = f"[[../Issue{pn_sep}{prev_num}|Previous]]" if prev_num > 0 else ""
    next_link = f"[[../Issue{pn_sep}{next_num}|Next]]"

    media_prefix = struct_info.get("media_prefix")
    if media_prefix and vol:
        media_str = f"Category:{media_prefix} Volume{vol} No {iss}"
    elif media_prefix:
        media_str = f"Category:{media_prefix} No {iss}"
    else:
        initials = "".join(w[0].upper() for w in re.findall(r'[A-Za-z]+', pdf_space_name) if w.lower() not in ("the", "of", "and", "vol", "no", "pdf"))[:2] or "PUB"
        media_str = f"Category:{initials} Volume{vol} No {iss}" if vol else f"Category:{initials} No {iss}"

    return (
        f"{access_tag}"
        f"{{{{header\n"
        f" | title      = {title_link}\n"
        f" | author     = \n"
        f" | translator = \n"
        f" | section    = {section_str}\n"
        f" | previous   = {prev_link}\n"
        f" | next       = {next_link}\n"
        f" | notes      = {{{{home |link= | pdf=[{{{{filepath:{pdf_space_name}}}}} PDF] | media={media_str} }}}}\n"
        f"}}}}\n\n"
        f"<pdf>File:{pdf_space_name}</pdf>\n"
    )

def build_new_text_page_skeleton(vol, iss, pdf_filename, page_count, struct_info, copyright_mode, base_title="", lua_maps=None):
    """Generates the initial /Text skeleton with {{page}} tags accounting for offsets and double-page spreads."""
    access_tag = get_access_tag(copyright_mode, base_title)
    prev_num, next_num = calc_prev_next_nums(iss)
    pn_sep = struct_info.get("prev_next_space", " ")

    offset_val, double_val, pub_year = get_issue_lua_config(vol, iss, lua_maps)
    anchor_pdf_page = 1 + offset_val
    cat_str = pub_year if pub_year else "xxxx"

    if vol:
        title_link = "[[../../../]]"
        section_str = f"Volume {vol}, Issue {iss}"
    else:
        title_link = "[[../../]]"
        section_str = f"Issue {iss}"

    prev_link = f"[[../../Issue{pn_sep}{prev_num}/Text|Previous]]" if prev_num > 0 else ""
    next_link = f"[[../../Issue{pn_sep}{next_num}/Text|Next]]"

    header = (
        f"{access_tag}"
        f"{{{{header\n"
        f" | title      = {title_link}\n"
        f" | author     = \n"
        f" | translator = \n"
        f" | section    = {section_str}\n"
        f" | previous   = {prev_link}\n"
        f" | next       = {next_link}\n"
        f" | notes      = {{{{bnreturn}}}}{{{{ps|1}}}}\n"
        f" | categories = {cat_str}\n"
        f"}}}}\n\n"
    )
    page_tags = []
    for p in range(1, page_count + 1):
        label = calculate_page_label(p, anchor_pdf_page, double_page_pub_num=double_val)
        page_tags.append(f"{{{{page|{label}|file={pdf_filename}|page={p}}}}}")

    return header + "\n".join(page_tags) + "\n"

def render_multi_issue_telemetry(issue_jobs, global_log_list, ui_holders):
    """Renders live telemetry across all queued periodical issues and their pages."""
    total_pages = 0
    completed_count = 0
    running_count = 0
    docai_count = 0
    error_count = 0
    unfinished_labels = []
    issue_rows_html = []

    for job in issue_jobs:
        short_name = job["short_name"]
        pages = job["pages_to_process"]
        futures = job["futures"]
        results = job["shared_results"]
        logs = job["shared_log_list"]

        docai_pages = set()
        error_pages = set()
        skipped_cached_pages = set()

        for msg in logs:
            m = re.search(r'Page\s+(\d+)', msg)
            if m:
                p = int(m.group(1))
                if "Falling back to DocAI" in msg or "DocAI Only Mode" in msg:
                    docai_pages.add(p)
                if "❌ SKIPPING" in msg or "🚨" in msg:
                    error_pages.add(p)
                if "⏩ Skipping" in msg:
                    skipped_cached_pages.add(p)

        docai_count += len(docai_pages)
        error_count += len(error_pages)

        badges = []
        for p in pages:
            total_pages += 1
            f = futures[p]
            val = results.get(str(p))

            if f.done():
                completed_count += 1
                if p in error_pages or (val == "" and str(p) in results):
                    bg, fg, border, title = "#ffebee", "#c62828", "#ef9a9a", "Error / Failed"
                elif p in docai_pages:
                    bg, fg, border, title = "#fff8e1", "#f57f17", "#ffe082", "DocAI Fallback"
                elif p in skipped_cached_pages:
                    bg, fg, border, title = "#e0f2f1", "#00695c", "#80cbc4", "Resumed from Cache"
                else:
                    bg, fg, border, title = "#e8f5e9", "#2e7d32", "#a5d6a7", "Gemini Success"
            elif f.running():
                running_count += 1
                unfinished_labels.append(f"{short_name}:p{p}")
                bg, fg, border, title = "#e3f2fd", "#1565c0", "#90caf9", "Running Now"
            else:
                unfinished_labels.append(f"{short_name}:p{p}")
                bg, fg, border, title = "#f5f5f5", "#757575", "#e0e0e0", "Queued"

            badges.append(
                f'<span title="{short_name} - Page {p}: {title}" style="display:inline-block; width:34px; text-align:center; '
                f'margin:1px; padding:2px 0; border-radius:4px; font-size:11px; font-weight:600; font-family:monospace; '
                f'background:{bg}; color:{fg}; border:1px solid {border};">{p}</span>'
            )

        status_badge = "✅ Uploaded" if job.get("uploaded") else ("⏳ OCR Done (Ready to Upload)" if all(f.done() for f in futures.values()) else "🔨 Processing")
        issue_rows_html.append(
            f'<div style="margin-bottom:6px; padding:4px 6px; border-bottom:1px solid rgba(128,128,128,0.15);">'
            f'<div style="font-size:12px; font-weight:600; margin-bottom:3px;">'
            f'#{job["idx"] + 1} <code>{job["wiki_title"]}</code> ({short_name}) — <span>{status_badge}</span></div>'
            f'<div>{"".join(badges)}</div></div>'
        )

    ui_holders["progress"].progress(completed_count / total_pages if total_pages > 0 else 1.0)
    ui_holders["m_done"].metric("✅ Pages Done", f"{completed_count} / {total_pages}")
    ui_holders["m_flight"].metric("⚡ Running Now", running_count)
    ui_holders["m_docai"].metric("🤖 DocAI Fallbacks", docai_count)
    ui_holders["m_err"].metric("❌ Errors", error_count)

    if 0 < len(unfinished_labels) <= 15:
        ui_holders["stragglers"].info(f"⏳ **Waiting on final {len(unfinished_labels)} page(s):** `{', '.join(unfinished_labels)}`")
    else:
        ui_holders["stragglers"].empty()

    legend = (
        '<div style="margin-bottom:6px; font-size:12px;">'
        '<b>Legend:</b> '
        '<span style="background:#e8f5e9;color:#2e7d32;padding:2px 6px;border-radius:4px;border:1px solid #a5d6a7;">🟩 Gemini</span> '
        '<span style="background:#e0f2f1;color:#00695c;padding:2px 6px;border-radius:4px;border:1px solid #80cbc4;">🧼 Resumed</span> '
        '<span style="background:#fff8e1;color:#f57f17;padding:2px 6px;border-radius:4px;border:1px solid #ffe082;">🟨 DocAI Fallback</span> '
        '<span style="background:#e3f2fd;color:#1565c0;padding:2px 6px;border-radius:4px;border:1px solid #90caf9;">🟦 Running</span> '
        '<span style="background:#f5f5f5;color:#757575;padding:2px 6px;border-radius:4px;border:1px solid #e0e0e0;">⬜ Queued</span> '
        '<span style="background:#ffebee;color:#c62828;padding:2px 6px;border-radius:4px;border:1px solid #ef9a9a;">🟥 Error</span>'
        '</div>'
    )
    grid_box = (
        f'{legend}<div style="line-height:1.4; padding:8px; '
        f'border:1px solid rgba(128,128,128,0.25); border-radius:6px;">{"".join(issue_rows_html)}</div>'
    )
    ui_holders["grid"].markdown(grid_box, unsafe_allow_html=True)

    warning_logs = [msg for msg in global_log_list if any(w in msg for w in ["⚠️", "❌", "🚨", "🤖"])]
    if warning_logs:
        ui_holders["warnings"].code("\n".join(warning_logs[-100:]), language="text")
    else:
        ui_holders["warnings"].caption("🎉 No warnings, blocks, or DocAI fallbacks triggered so far.")

    return completed_count == total_pages

def merge_and_upload_issue(job, session, log_area, status_container, copyright_mode="1) Open (No tag)"):
    """Merges completed OCR pages into the issue wikitext, uploads Base + /Text to Bahai.works, and cleans up temp files."""
    idx = job["idx"]
    wiki_title = job["wiki_title"]
    base_wiki_title = job.get("base_wiki_title")
    is_new_issue = job.get("is_new_issue", False)
    base_page_wikitext = job.get("base_page_wikitext", "")
    wiki_pdf_name = job["wiki_pdf_name"]
    short_name = job["short_name"]
    wip_file_path = job["wip_file_path"]
    temp_master_path = job["temp_master_path"]
    pages_to_process = job["pages_to_process"]
    shared_results = job["shared_results"]

    log_area.text(f"🔄 Merging extracted pages and injecting wikitext for {short_name}...")

    all_extracted_text = {
        int(p_num_str): text
        for p_num_str, text in shared_results.items()
        if text and text.strip()
    }

    if not all_extracted_text:
        st.error(f"CRITICAL SAFETY STOP: 0 pages were extracted for `{short_name}`. Aborting before modifying `{wiki_title}`.")
        st.stop()

    # 1. If this is a brand-new issue, upload the Base Issue page first
    if is_new_issue and base_wiki_title and base_page_wikitext:
        log_area.text(f"📄 Creating Base Issue Page `{base_wiki_title}`...")
        base_res = upload_to_bahaiworks(
            base_wiki_title,
            base_page_wikitext,
            f"Automated Creation: Base issue page for {short_name}",
            session=session
        )
        if base_res.get('edit', {}).get('result') != 'Success':
            st.error(f"BASE PAGE UPLOAD FAILED on {base_wiki_title}: {base_res}")
            st.stop()

    with open(wip_file_path, "r", encoding="utf-8") as f:
        current_wikitext = f.read()

    # Apply accesscontrol tag if selected and not already present
    pub_base = wiki_title.split("/")[0]
    access_tag_prefix = get_access_tag(copyright_mode, pub_base)
    if access_tag_prefix and "<accesscontrol>" not in current_wikitext.lower():
        current_wikitext = access_tag_prefix + current_wikitext.lstrip()

    anchor_pdf_page = find_anchor_offset(current_wikitext)
    offset_val = job.get("offset_val", 0)
    double_val = job.get("double_val", 0)
    if anchor_pdf_page is None or offset_val > 0:
        anchor_pdf_page = 1 + offset_val

    for page_num in pages_to_process:
        final_text = all_extracted_text.get(page_num, "").strip()
        correct_label = calculate_page_label(page_num, anchor_pdf_page, double_page_pub_num=double_val)
        current_wikitext = find_and_fix_tag_by_page_num(current_wikitext, wiki_pdf_name, page_num, correct_label)

        if not final_text:
            log_area.text(f"⚠️ {short_name} Page {page_num} was empty or failed. Skipping injection.")
            continue

        if page_num == 1:
            current_wikitext = re.sub(r'\{\{ocr.*?\}\}\n?', '', current_wikitext, flags=re.IGNORECASE)

            found_year = None
            cat_match = re.search(r'\[\[Category:\s*(\d{4})\s*\]\]', current_wikitext, re.IGNORECASE)
            if cat_match:
                found_year = cat_match.group(1)

            if "{{header" not in current_wikitext:
                volume_found = None
                issue_identifier = None

                if "/Volume" in wiki_title and "/Issue" in wiki_title:
                    v_match = re.search(r'Volume[_ ](\d+)', wiki_title)
                    i_match = re.search(r'Issue[_ ]([\d-]+)', wiki_title)
                    if v_match and i_match:
                        volume_found = v_match.group(1)
                        issue_identifier = i_match.group(1)
                else:
                    i_match = re.search(r'Issue[_ ]([\d-]+)', wiki_title)
                    if i_match:
                        issue_identifier = i_match.group(1)
                    else:
                        fn_match = re.search(r'(\d+(?:-\d+)?)', short_name)
                        if fn_match:
                            issue_identifier = fn_match.group(1)

                if issue_identifier:
                    header = generate_header(issue_identifier, year=found_year or "xxxx", volume=volume_found)
                    access_match = re.match(r'^\s*<accesscontrol>.*?</accesscontrol>\s*', current_wikitext, re.DOTALL | re.IGNORECASE)
                    if access_match:
                        access_tag = access_match.group(0).strip()
                        remaining_body = current_wikitext[access_match.end():].lstrip()
                        current_wikitext = access_tag + "\n" + header + "\n" + remaining_body
                    else:
                        current_wikitext = header + "\n" + current_wikitext.lstrip()
        else:
            current_wikitext = update_header_ps_tag(current_wikitext)

        final_wikitext, inject_error = inject_text_into_page(current_wikitext, correct_label, final_text, wiki_pdf_name)
        if inject_error:
            st.error(f"CRITICAL ERROR injecting {short_name} Page {page_num}: {inject_error}")
            st.stop()

        current_wikitext = final_wikitext

    current_wikitext = update_header_ps_tag(current_wikitext)

    if "__NOTOC__" not in current_wikitext:
        current_wikitext = current_wikitext.rstrip() + "\n__NOTOC__\n"

    log_area.text(f"🧹 Running final seam cleanup on {short_name}...")
    cleaned_text = cleanup_page_seams(current_wikitext)
    cleaned_text = re.sub(r'https?://(?:www\.)?youtu\.be/([a-zA-Z0-9_-]+)', r'https://www.youtube.com/watch?v=\1', cleaned_text)

    log_area.text(f"🚀 Uploading `{wiki_title}` ({short_name}) to Bahai.works...")
    summary = f"Automated Proofread: {short_name} (Full Issue)"
    res = upload_to_bahaiworks(wiki_title, cleaned_text, summary, session=session)

    if res.get('edit', {}).get('result') != 'Success':
        st.error(f"UPLOAD FAILED on {wiki_title}: {res}")
        st.stop()

    if os.path.exists(wip_file_path):
        os.remove(wip_file_path)
    if os.path.exists(temp_master_path):
        os.remove(temp_master_path)

    job["uploaded"] = True

    # Update in-memory status so the table reflects completion immediately
    if "p08_issues" in st.session_state:
        for item in st.session_state["p08_issues"]:
            if item["wiki_title"] == wiki_title:
                item["is_proofread"] = True
                item["is_new_issue"] = False
                item["status"] = "✅ Proofread ({{ps|1}})"
                item["selected"] = False
                item["wikitext"] = cleaned_text

    status_container.success(f"✅ Uploaded #{idx + 1}: `{wiki_title}` (`{short_name}`)")

def load_periodical_issues(input_folder, base_title, copyright_mode="1) Open (No tag)"):
    """Scans local PDFs, queries Wiki /Text pages, matches existing issues, and prepares skeletons for new local PDFs."""
    session = requests.Session()
    get_csrf_token(session)

    local_pdf_map = build_local_pdf_map(input_folder)
    if not local_pdf_map:
        return None, "No PDF files found in the local directory."

    wiki_pages = get_all_wiki_text_pages(base_title, session=session)
    struct_info = inspect_wiki_structure(base_title, wiki_pages, session=session)
    lua_maps = fetch_periodical_lua_maps(base_title, session=session)
    batched_wikitexts = fetch_wikitext_batch(wiki_pages, session=session) if wiki_pages else {}

    issues = []
    matched_local_keys = set()
    existing_wiki_titles_lower = set()

    # 1. Process existing Wiki /Text pages
    for wiki_title in wiki_pages:
        raw_text = batched_wikitexts.get(wiki_title)
        if not raw_text:
            raw_text, _ = fetch_wikitext(wiki_title, session=session)
        raw_text = raw_text or ""

        # Skip redirect pages
        if re.match(r'^\s*#redirect\s*\[\[', raw_text, flags=re.IGNORECASE):
            continue

        existing_wiki_titles_lower.add(wiki_title.lower().replace(" ", "_"))
        idx = len(issues)
        norm_text = normalize_page_templates(raw_text, session)
        wiki_pdf_name = extract_pdf_filename_from_wikitext(norm_text)
        is_proofread = has_ps1_tag(norm_text)

        pdf_path = None
        short_name = ""
        page_count = 0

        if wiki_pdf_name:
            lookup_key = wiki_pdf_name.lower().replace(" ", "_")
            pdf_path = local_pdf_map.get(lookup_key)
            if pdf_path and os.path.exists(pdf_path):
                matched_local_keys.add(lookup_key)
                short_name = os.path.basename(pdf_path)
                try:
                    with fitz.open(pdf_path) as doc:
                        page_count = len(doc)
                except Exception:
                    page_count = 0

        if not wiki_pdf_name:
            status_label = "⚠️ No {{page}} tag"
            default_select = False
        elif not pdf_path:
            status_label = f"❌ Missing PDF ({wiki_pdf_name})"
            default_select = False
        elif is_proofread:
            status_label = "✅ Proofread ({{ps|1}})"
            default_select = False
        else:
            status_label = "⏳ Unproofread"
            default_select = True

        vol_ex, iss_ex = parse_vol_issue_from_filename(short_name or wiki_pdf_name or wiki_title)
        off_ex, dbl_ex, _ = get_issue_lua_config(vol_ex, iss_ex, lua_maps)

        issues.append({
            "idx": idx,
            "selected": default_select,
            "wiki_title": wiki_title,
            "base_wiki_title": re.sub(r'/Text$', '', wiki_title),
            "is_new_issue": False,
            "base_page_wikitext": "",
            "status": status_label,
            "is_proofread": is_proofread,
            "wiki_pdf_name": wiki_pdf_name or "",
            "pdf_path": pdf_path,
            "short_name": short_name,
            "page_count": page_count,
            "wikitext": norm_text,
            "vol": vol_ex,
            "iss": iss_ex,
            "offset_val": off_ex,
            "double_val": dbl_ex,
            "lua_maps": lua_maps
        })

    # 2. Discover new local PDFs that do not exist on the Wiki yet
    unmatched_keys = sorted(
        [k for k in local_pdf_map.keys() if k not in matched_local_keys],
        key=lambda x: [int(c) if c.isdigit() else c.lower() for c in re.split(r'(\d+)', x)]
    )

    clean_base = base_title.strip().strip("/").replace(" ", "_")
    vol_sep = struct_info.get("vol_sep", "_")
    iss_sep = struct_info.get("issue_sep", "_")

    for norm_key in unmatched_keys:
        pdf_path = local_pdf_map[norm_key]
        short_name = os.path.basename(pdf_path)
        vol, iss = parse_vol_issue_from_filename(short_name)
        if not iss:
            continue

        if vol and struct_info.get("has_volume", True):
            base_wiki_title = f"{clean_base}/Volume{vol_sep}{vol}/Issue{iss_sep}{iss}"
        else:
            base_wiki_title = f"{clean_base}/Issue{iss_sep}{iss}"

        wiki_title = f"{base_wiki_title}/Text"
        if wiki_title.lower().replace(" ", "_") in existing_wiki_titles_lower:
            continue

        try:
            with fitz.open(pdf_path) as doc:
                page_count = len(doc)
        except Exception:
            page_count = 0

        if page_count == 0:
            continue

        off_new, dbl_new, _ = get_issue_lua_config(vol, iss, lua_maps)
        base_page_wt = build_new_base_page_wikitext(vol, iss, short_name, struct_info, copyright_mode, base_title)
        text_page_wt = build_new_text_page_skeleton(vol, iss, short_name, page_count, struct_info, copyright_mode, base_title, lua_maps=lua_maps)

        issues.append({
            "idx": len(issues),
            "selected": True,
            "wiki_title": wiki_title,
            "base_wiki_title": base_wiki_title,
            "is_new_issue": True,
            "base_page_wikitext": base_page_wt,
            "status": "🆕 New Issue (Will Create 2 Pages)",
            "is_proofread": False,
            "wiki_pdf_name": short_name,
            "pdf_path": pdf_path,
            "short_name": short_name,
            "page_count": page_count,
            "wikitext": text_page_wt,
            "vol": vol,
            "iss": iss,
            "offset_val": off_new,
            "double_val": dbl_new,
            "struct_info": struct_info,
            "lua_maps": lua_maps
        })

    return issues, None

# ==============================================================================
# 2. UI & MAIN LOGIC
# ==============================================================================

if __name__ == '__main__':
    st.title("🤖 Fully Automated Periodical Processor")

    # --- Step 1: Configuration & Load Bar (On Main Page) ---
    with st.container(border=True):
        c_folder, c_title, c_copy, c_ocr, c_load = st.columns([2.6, 1.8, 1.8, 1.8, 1.2])
        with c_folder:
            input_folder = st.text_input(
                "Local PDF Folder",
                value="/media/sarah/4TB/Projects/Bahai.works/English/Canada/1948-1975_CBN/"
            )
        with c_title:
            base_title = st.text_input(
                "Base Wiki Title",
                value="Canadian_Bahá’í_News"
            )
        with c_copy:
            copyright_mode = st.selectbox(
                "Copyright Status",
                ["1) Open (No tag)", "2) Restricted", "3) NoSearch"],
                index=0,
                help="Controls whether <accesscontrol> tags are added to newly created or processed pages."
            )
        with c_ocr:
            ocr_strategy = st.radio(
                "OCR Strategy",
                ["Gemini (Default)", "DocAI Only"],
                horizontal=True,
                help="DocAI Only skips Gemini and goes straight to Google Cloud Vision OCR."
            )
        with c_load:
            st.write("")
            load_clicked = st.button("📥 Load Periodical", type="primary", width='stretch')

    if load_clicked:
        if not input_folder or not os.path.exists(input_folder):
            st.error("Please provide a valid local PDF folder path.")
            st.stop()
        if not base_title.strip():
            st.error("Please provide a valid Base Wiki Title.")
            st.stop()

        with st.spinner(f"🔍 Fetching `{base_title}` subpages, checking `{{{{ps|1}}}}` status, and matching local PDFs..."):
            issues, err = load_periodical_issues(input_folder.strip(), base_title.strip(), copyright_mode=copyright_mode)
            if err:
                st.error(err)
                st.stop()
            st.session_state["p08_issues"] = issues
            st.session_state["p08_loaded_title"] = base_title.strip()
            st.session_state["p08_editor_ver"] = st.session_state.get("p08_editor_ver", 0) + 1
            st.rerun()

    if "p08_issues" not in st.session_state:
        st.info("👆 Enter the **Local PDF Folder** and **Base Wiki Title** above, then click **📥 Load Periodical** to populate the issue checklist.")
        st.stop()

    issues_list = st.session_state["p08_issues"]

    # --- Step 2: Summary Metrics & Selection Toolbar ---
    total_issues = len(issues_list)
    unproofread_count = sum(1 for x in issues_list if x["status"] in ("⏳ Unproofread", "🆕 New Issue (Will Create 2 Pages)"))
    proofread_count = sum(1 for x in issues_list if x["is_proofread"])
    missing_count = total_issues - unproofread_count - proofread_count

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("📚 Total Issues", total_issues)
    m2.metric("⏳ Ready / New Issues", unproofread_count)
    m3.metric("✅ Already Proofread ({{ps|1}})", proofread_count)
    m4.metric("⚠️ Missing PDF / Tag", missing_count)

    b_col1, b_col2, b_col3, b_col4 = st.columns([1.5, 1.5, 1.5, 2.5])
    with b_col1:
        if st.button("☑️ Select All Unproofread / New", width='stretch'):
            for x in issues_list:
                x["selected"] = x["status"] in ("⏳ Unproofread", "🆕 New Issue (Will Create 2 Pages)")
            st.session_state["p08_editor_ver"] = st.session_state.get("p08_editor_ver", 0) + 1
            st.rerun()
    with b_col2:
        if st.button("✅ Select All (Valid PDF)", width='stretch'):
            for x in issues_list:
                x["selected"] = bool(x["pdf_path"])
            st.session_state["p08_editor_ver"] = st.session_state.get("p08_editor_ver", 0) + 1
            st.rerun()
    with b_col3:
        if st.button("⬜ Deselect All", width='stretch'):
            for x in issues_list:
                x["selected"] = False
            st.session_state["p08_editor_ver"] = st.session_state.get("p08_editor_ver", 0) + 1
            st.rerun()
    with b_col4:
        filter_text = st.text_input(
            "Filter by Issue / Filename",
            value="",
            placeholder="e.g. Volume 10 or Issue 5...",
            label_visibility="collapsed"
        )

    # Build table rows for st.data_editor
    editor_ver = st.session_state.get("p08_editor_ver", 0)
    visible_rows = []
    for item in issues_list:
        if filter_text.strip():
            q = filter_text.strip().lower()
            if q not in item["wiki_title"].lower() and q not in item["short_name"].lower():
                continue
        visible_rows.append({
            "#": item["idx"] + 1,
            "Select": item["selected"],
            "Wiki Page": item["wiki_title"],
            "Status": item["status"],
            "Local PDF": item["short_name"] or "—",
            "Pages": item["page_count"]
        })

    edited_rows = st.data_editor(
        visible_rows,
        width='stretch',
        height=380,
        hide_index=True,
        disabled=["#", "Wiki Page", "Status", "Local PDF", "Pages"],
        column_config={
            "#": st.column_config.NumberColumn("#", width="small"),
            "Select": st.column_config.CheckboxColumn("Select", default=False, width="small"),
            "Wiki Page": st.column_config.TextColumn("Wiki Page", width="large"),
            "Status": st.column_config.TextColumn("Status", width="medium"),
            "Local PDF": st.column_config.TextColumn("Local PDF", width="medium"),
            "Pages": st.column_config.NumberColumn("Pages", width="small"),
        },
        key=f"p08_issue_table_{editor_ver}"
    )

    # Sync user edits back to st.session_state["p08_issues"]
    edited_map = {row["Wiki Page"]: row["Select"] for row in edited_rows}
    for item in issues_list:
        if item["wiki_title"] in edited_map:
            item["selected"] = bool(edited_map[item["wiki_title"]])

    selected_items = [x for x in issues_list if x["selected"] and x["pdf_path"] and x["page_count"] > 0]
    total_selected_pages = sum(x["page_count"] for x in selected_items)

    # --- Step 3: Action Buttons ---
    status_container = st.container(border=True)
    log_area = st.empty()

    col1, col2 = st.columns(2)
    with col1:
        start_btn = st.button(
            f"🚀 Process Selected ({len(selected_items)} Issue(s), {total_selected_pages} Pages)",
            type="primary",
            width='stretch',
            disabled=(len(selected_items) == 0)
        )
    with col2:
        stop_info = st.button("🛑 Stop Execution", width='stretch')
        if stop_info:
            st.warning("Execution stopped.")
            st.stop()

    # --- Step 4: Continuous 100-Worker Cross-Issue Pool ---
    if start_btn:
        session = requests.Session()
        try:
            with st.spinner("🔐 Authenticating with MediaWiki..."):
                get_csrf_token(session)
        except Exception as e:
            st.error(f"Authentication Failed: {e}")
            st.stop()

        issue_jobs = []
        for item in selected_items:
            idx = item["idx"]
            wiki_title = item["wiki_title"]
            wiki_pdf_name = item["wiki_pdf_name"]
            pdf_path = item["pdf_path"]
            short_name = item["short_name"]

            # Regenerate skeleton with current copyright_mode if it's a new issue
            if item.get("is_new_issue"):
                base_page_wt = build_new_base_page_wikitext(
                    item.get("vol"), item.get("iss"), short_name, item.get("struct_info", {}), copyright_mode, base_title
                )
                live_wikitext = build_new_text_page_skeleton(
                    item.get("vol"), item.get("iss"), short_name, item["page_count"],
                    item.get("struct_info", {}), copyright_mode, base_title, lua_maps=item.get("lua_maps")
                )
            else:
                base_page_wt = ""
                live_wikitext = item["wikitext"]

            wip_file_path = os.path.join(project_root, f"wip_{short_name}.txt")
            if not os.path.exists(wip_file_path) or item.get("is_new_issue"):
                with open(wip_file_path, "w", encoding="utf-8") as f:
                    f.write(live_wikitext)

            pages_to_process = list(range(1, item["page_count"] + 1))
            temp_master_path = os.path.join(project_root, f"temp_master_{short_name}.json")
            shared_results = {}
            if os.path.exists(temp_master_path):
                try:
                    with open(temp_master_path, "r", encoding="utf-8") as f:
                        loaded_temp = json.load(f)
                        shared_results = {str(k): v for k, v in loaded_temp.items() if v}
                except json.JSONDecodeError:
                    shared_results = {}

            issue_jobs.append({
                "idx": idx,
                "wiki_title": wiki_title,
                "base_wiki_title": item.get("base_wiki_title"),
                "is_new_issue": item.get("is_new_issue", False),
                "base_page_wikitext": base_page_wt,
                "wiki_pdf_name": wiki_pdf_name,
                "pdf_path": pdf_path,
                "short_name": short_name,
                "wip_file_path": wip_file_path,
                "temp_master_path": temp_master_path,
                "pages_to_process": pages_to_process,
                "shared_results": shared_results,
                "shared_log_list": [],
                "futures": {},
                "pdf_doc": None,
                "uploaded": False,
                "offset_val": item.get("offset_val", 0),
                "double_val": item.get("double_val", 0),
            })

        status_container.markdown(
            f"### 🚀 Queued **{len(issue_jobs)}** Issue(s) (**{total_selected_pages}** total pages) into 100-Worker Pool"
        )

        ocr_box = st.container(border=True)
        ocr_progress_bar = ocr_box.progress(0)
        m_cols = ocr_box.columns(4)
        straggler_holder = ocr_box.empty()
        grid_holder = ocr_box.empty()
        with ocr_box.expander("⚠️ Warnings & DocAI Fallbacks Log", expanded=True):
            warnings_holder = st.empty()

        ui_holders = {
            "progress": ocr_progress_bar,
            "m_done": m_cols[0].empty(),
            "m_flight": m_cols[1].empty(),
            "m_docai": m_cols[2].empty(),
            "m_err": m_cols[3].empty(),
            "stragglers": straggler_holder,
            "grid": grid_holder,
            "warnings": warnings_holder,
        }

        for attempt in range(2):
            fatal_error = False
            fatal_error_msg = ""
            global_log_list = []

            executor = concurrent.futures.ThreadPoolExecutor(max_workers=100)
            for job in issue_jobs:
                if job["uploaded"]:
                    continue
                job["shared_log_list"] = []
                job["pdf_doc"] = fitz.open(job["pdf_path"])
                job["futures"] = {
                    page_num: executor.submit(
                        process_single_page,
                        page_num,
                        job["pdf_doc"],
                        ocr_strategy,
                        job["temp_master_path"],
                        job["shared_results"],
                        job["shared_log_list"],
                        f"[{job['short_name']}] ",
                        global_log_list
                    )
                    for page_num in job["pages_to_process"]
                }

            next_upload_ptr = 0
            while True:
                all_done = render_multi_issue_telemetry(issue_jobs, global_log_list, ui_holders)

                for log_msg in list(global_log_list):
                    if "FATAL_RATE_LIMIT" in log_msg:
                        fatal_error = True
                        fatal_error_msg = log_msg
                        break

                # Upload completed issues sequentially in order while workers continue on later issues
                while next_upload_ptr < len(issue_jobs) and not fatal_error:
                    candidate = issue_jobs[next_upload_ptr]
                    if candidate["uploaded"]:
                        next_upload_ptr += 1
                        continue
                    if all(f.done() for f in candidate["futures"].values()):
                        with _fitz_lock:
                            if candidate["pdf_doc"] is not None and not candidate["pdf_doc"].is_closed:
                                candidate["pdf_doc"].close()
                            candidate["pdf_doc"] = None
                        merge_and_upload_issue(candidate, session, log_area, status_container, copyright_mode=copyright_mode)
                        next_upload_ptr += 1
                    else:
                        break

                if fatal_error or (all_done and next_upload_ptr >= len(issue_jobs)):
                    break
                time.sleep(1)

            executor.shutdown(wait=False, cancel_futures=True)
            with _fitz_lock:
                for job in issue_jobs:
                    if job["pdf_doc"] is not None and not job["pdf_doc"].is_closed:
                        job["pdf_doc"].close()
                    job["pdf_doc"] = None

            if fatal_error:
                if attempt == 0:
                    ocr_box.warning(f"⚠️ Fatal API/Network error detected: `{fatal_error_msg}`. Sleeping for 15 minutes before retrying once...")
                    retry_countdown = ocr_box.empty()
                    for remaining in range(900, 0, -1):
                        mins, secs = divmod(remaining, 60)
                        retry_countdown.code(f"Retrying OCR in: {mins:02d}:{secs:02d}")
                        time.sleep(1)
                    retry_countdown.empty()
                else:
                    st.error(f"❌ FATAL (Failed twice): `{fatal_error_msg}`. Halting execution.")
                    st.stop()
            else:
                break

        st.session_state["p08_editor_ver"] = st.session_state.get("p08_editor_ver", 0) + 1
        st.success("🎉 All Selected Periodical Issues Processed and Uploaded!")
