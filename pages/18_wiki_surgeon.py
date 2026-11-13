import streamlit as st
import pandas as pd
import requests
import re
import json
import concurrent.futures
import json_repair
import google.generativeai as genai
from google.generativeai.types import HarmCategory, HarmBlockThreshold

from src.mediawiki_uploader import (
    API_URL,
    get_csrf_token,
    fetch_wikitext,
    fetch_wikitext_batch,
    upload_to_bahaiworks,
)
from src.gemini_processor import MODEL_NAME, check_fatal_rate_limit

st.set_page_config(page_title="Wiki Surgeon", page_icon="🩺", layout="wide")
st.title("🩺 Wiki Surgeon (Pywikibot Regex & Surgical LLM Patcher)")
st.caption(
    "Scan `bahai.works` via pre-filtered `insource:` queries, book/periodical prefixes, or **Page 16 Tab 5 Invariant CSVs**. "
    "Fix mechanical template/tag typos instantly with **Tier 1 Python Regexes**, and resolve complex markup defects using "
    "**Tier 2 Surgical LLM Window Patching** (`find` ➔ `replace` on 600-char slices)."
)

# ==============================================================================
# 1. PRE-FILTERED CIRRUSSEARCH (`insource:`) PRESETS
# ==============================================================================
INSOURCE_PRESETS = {
    "Unclosed {{bwpage|...} (Single brace or missing brace)": (
        r'insource:"bwpage" insource:/\{\{bwpage\|[^}\n]+(\}(?!\})|$)/'
    ),
    "Unclosed {{u|...} Transliteration Underlines": (
        r'insource:"{{u|" insource:/\{\{u\|([a-zA-Z]{1,2}\}(?!\})|[cdgkstzCDGKSTZ][Hh][a-zA-Z]+)/'
    ),
    "Mangled {{center|...}} / {{right|...}} Braces ({center|, {[center|, {{center})": (
        r'insource:"center" insource:/(\{\[?center\||\{\{center\}|\{center\{\||\{~?center\|\{|\{\{center\|[^}\n]+\}\])/'
    ),
    "Unclosed Single-Brace {{SW-ML}": (
        r'insource:"{{SW-ML}"'
    ),
    "Empty {{}} or Anonymous {{|...}} Templates": (
        r'insource:/\{\{\s*\|?[A-Za-z0-9]/'
    ),
    "Broken Closing </small or </center Tags (Missing '>')": (
        r'insource:"</small" insource:/<\/small(?!>)/'
    ),
    "Scoped Unclosed {{page|...} (Requires Prefix below to avoid timeout!)": (
        r'insource:"{{page|" insource:/\{\{page\|[^}\n]+(\}(?!\})|$)/'
    ),
}

# ==============================================================================
# 2. TIER 1: DETERMINISTIC PYTHON REGEX RULE PACKS
# ==============================================================================
def _apply_pack_page_and_void(text: str) -> tuple[str, list[str]]:
    notes = []

    # 1a. {{page|...} followed by single } (not }}) -> {{page|...}}
    new_t, n = re.subn(
        r'(\{\{\s*(?:page|seite|p|swpage|bwpage)\s*\|[^\n{}]+)\}(?!\})',
        r'\1}}',
        text,
        flags=re.IGNORECASE,
    )
    if n:
        notes.append(f"Closed {n} single-brace page template(s)")
    text = new_t

    # 1b. {{page|... at end of line with NO closing brace at all
    new_t, n = re.subn(
        r'(\{\{\s*(?:page|seite|p|swpage|bwpage)\s*\|[^\n{}]+?)(?=\r?\n)',
        r'\1}}',
        text,
        flags=re.IGNORECASE,
    )
    if n:
        notes.append(f"Closed {n} EOL-unclosed page template(s)")
    text = new_t

    # 1c. {bwpage|8|711}} or {page|14}} (single opening brace, double closing)
    new_t, n = re.subn(
        r'(?<!\{)\{(\s*(?:page|seite|p|swpage|bwpage)\s*\|[^\n{}]+\}\})',
        r'{\1',
        text,
        flags=re.IGNORECASE,
    )
    if n:
        notes.append(f"Fixed {n} single-open-brace page template(s)")
    text = new_t

    # 1d. {{bwpage|5|288creation -> {{bwpage|5|288}}creation
    new_t, n = re.subn(
        r'(\{\{\s*bwpage\s*\|\s*\d+\s*\|\s*\d+)([A-Za-z])',
        r'\1}}\2',
        text,
        flags=re.IGNORECASE,
    )
    if n:
        notes.append(f"Closed {n} fused bwpage template(s)")
    text = new_t

    # 1e. {{SW-ML} -> {{SW-ML}}
    new_t, n = re.subn(
        r'(\{\{\s*SW-ML\s*)\}(?!\})',
        r'\1}}',
        text,
        flags=re.IGNORECASE,
    )
    if n:
        notes.append(f"Closed {n} {{{{SW-ML}}}} template(s)")
    text = new_t

    # 1f. Empty {{}} -> remove
    new_t, n = re.subn(r'\{\{\}\}', '', text)
    if n:
        notes.append(f"Removed {n} empty {{{{}}}} tag(s)")
    text = new_t

    return text, notes


def _apply_pack_underlines(text: str) -> tuple[str, list[str]]:
    notes = []

    # 2a. {{u|sh} -> {{u|sh}} (single closing brace)
    new_t, n = re.subn(
        r'(\{\{\s*u\s*\|\s*[A-Za-zÁáÍíÚúḤḥṢṣṬṭḌḍẒẓ]{1,3})\}(?!\})',
        r'\1}}',
        text,
    )
    if n:
        notes.append(f"Closed {n} single-brace {{{{u|...}}}} template(s)")
    text = new_t

    # 2b. {u|Kh}} -> {{u|Kh}} (single opening brace)
    new_t, n = re.subn(
        r'(?<!\{)\{(\s*u\s*\|\s*[A-Za-zÁáÍíÚúḤḥṢṣṬṭḌḍẒẓ]{1,3}\}\})',
        r'{\1',
        text,
    )
    if n:
        notes.append(f"Fixed {n} single-open-brace {{{{u|...}}}} template(s)")
    text = new_t

    # 2c. Unclosed digraphs inside words: e.g. Mua{{u|dhdhin -> Mua{{u|dh}}dhin
    # Standard Bahá'í transliteration digraphs: dh, sh, kh, gh, th, zh, ch, Dh, Sh, Kh, Gh, Th, Zh, Ch
    new_t, n = re.subn(
        r'\{\{\s*u\s*\|\s*([dskgtzcDSKGTZC][hH])([A-Za-zÁáÍíÚúḤḥṢṣṬṭḌḍẒẓ’‘]+)(?![^}\n]*\})',
        r'{{u|\1}}\2',
        text,
    )
    if n:
        notes.append(f"Closed {n} unclosed digraph {{{{u|..}}}} template(s)")
    text = new_t

    return text, notes


def _apply_pack_alignment_templates(text: str) -> tuple[str, list[str]]:
    notes = []

    # 3a. Mangled opening braces: {center|...}}, {[center|...}}, {center{|...}}, {center|{...}}
    new_t, n = re.subn(
        r'(?<!\{)\{\[?\s*(center|right|left)\s*\{\|\s*([^\n{}]+?)\}\}',
        r'{{\1|\2}}',
        text,
        flags=re.IGNORECASE,
    )
    if n:
        notes.append(f"Fixed {n} {{center{{|...}}}} template(s)")
    text = new_t

    new_t, n = re.subn(
        r'(?<!\{)\{\[?\s*(center|right|left)\s*\|\s*\{?\s*([^\n{}]+?)\}\}',
        r'{{\1|\2}}',
        text,
        flags=re.IGNORECASE,
    )
    if n:
        notes.append(f"Fixed {n} {{center|...}}}} opening brace(s)")
    text = new_t

    # 3b. {{center}Text}} -> {{center|Text}}
    new_t, n = re.subn(
        r'\{\{\s*(center|right|left)\s*\}([^\n{}]+?)\}\}',
        r'{{\1|\2}}',
        text,
        flags=re.IGNORECASE,
    )
    if n:
        notes.append(f"Fixed {n} {{{{center}}...}}}} pipe typo(s)")
    text = new_t

    # 3c. {{center|Text}] -> {{center|Text}}
    new_t, n = re.subn(
        r'(\{\{\s*(?:center|right|left)\s*\|[^\n{}]+?)\}\]',
        r'\1}}',
        text,
        flags=re.IGNORECASE,
    )
    if n:
        notes.append(f"Fixed {n} ...] closing bracket typo(s)")
    text = new_t

    # 3d. {{center|Text} (single closing brace at end of line) -> {{center|Text}}
    new_t, n = re.subn(
        r'(\{\{\s*(?:center|right|left)\s*\|[^\n{}]+?)\}(?!\})(?=\s*\r?\n|$)',
        r'\1}}',
        text,
        flags=re.IGNORECASE,
    )
    if n:
        notes.append(f"Closed {n} single-brace alignment template(s)")
    text = new_t

    return text, notes


def _apply_pack_single_line_autoclose(text: str) -> tuple[str, list[str]]:
    """
    Closes unclosed {{center|... or {{right|... at the end of the first line,
    provided there are no braces on that line and the template is not closed on the next line.
    """
    notes = []

    def _line_closer(m):
        full_match = m.group(0)
        return full_match + "}}"

    # Matches {{center|Text up to end of line where no } appears on the line
    # and the immediately following line does not end with }}
    pattern = re.compile(
        r'^([ \t]*\{\{\s*(?:center|right|left)\s*\|[^\n{}]+?)[ \t]*(?=\r?\n(?!.*\}\}))',
        flags=re.IGNORECASE | re.MULTILINE,
    )
    new_t, n = pattern.subn(_line_closer, text)
    if n:
        notes.append(f"Auto-closed {n} unclosed single-line {{{{center/right}}}} template(s) at EOL")
    return new_t, notes


def _apply_pack_html_and_seams(text: str) -> tuple[str, list[str]]:
    notes = []

    # 5a. Broken closing HTML tags missing '>': </small, </center, </div, </span
    new_t, n = re.subn(
        r'(<\/(?:small|center|div|span|big|font|em|i|b))(?![A-Za-z>])(\s*(?:\r?\n|$))',
        r'\1>\2',
        text,
        flags=re.IGNORECASE,
    )
    if n:
        notes.append(f"Closed {n} truncated HTML closing tag(s)")
    text = new_t

    # 5b. Hyphenated words split across {{page|...}} breaks: word-\n{{page|X}}\nsuffix -> {{page|X}}\nwordsuffix
    new_t, n = re.subn(
        r'([A-Za-zÁáÍíÚúḤḥṢṣṬṭḌḍẒẓ]{2,})-\s*\n\s*(\{\{\s*(?:page|seite|p|swpage|bwpage)\b[^}]+\}\})\s*\n\s*([a-záíúḥṣṭḍẓ]{2,})',
        r'\2\n\1\3',
        text,
        flags=re.IGNORECASE,
    )
    if n:
        notes.append(f"Rejoined {n} page-break hyphenated word(s)")
    text = new_t

    return text, notes


TIER1_RULE_PACKS = {
    "pack_page": ("1. Unclosed {{page}}, {{bwpage}}, {{SW-ML}}, & empty {{}}", _apply_pack_page_and_void),
    "pack_u": ("2. Unclosed / Mangled {{u|...}} Transliteration Underlines", _apply_pack_underlines),
    "pack_align": ("3. Mangled {center|...}}, {[center|...}}, {{center|...}] Braces", _apply_pack_alignment_templates),
    "pack_eol_align": ("4. Auto-Close Unclosed {{center|... / {{right|... at End of First Line", _apply_pack_single_line_autoclose),
    "pack_html": ("5. Truncated HTML Tags (</small) & Page-Break Hyphens", _apply_pack_html_and_seams),
}


def run_tier1_rules(wikitext: str, enabled_packs: list[str]) -> tuple[str, list[str]]:
    curr = wikitext
    all_notes = []
    for key in enabled_packs:
        if key in TIER1_RULE_PACKS:
            _, fn = TIER1_RULE_PACKS[key]
            curr, notes = fn(curr)
            all_notes.extend(notes)
    return curr, all_notes


def build_compact_diff_preview(old_text: str, new_text: str, context_chars: int = 75) -> list[dict]:
    """Finds changed lines between old_text and new_text for compact table preview."""
    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()
    diffs = []
    max_len = max(len(old_lines), len(new_lines))
    for i in range(max_len):
        o_ln = old_lines[i] if i < len(old_lines) else ""
        n_ln = new_lines[i] if i < len(new_lines) else ""
        if o_ln != n_ln:
            diffs.append({"line": i + 1, "before": o_ln[:220], "after": n_ln[:220]})
            if len(diffs) >= 6:
                break
    return diffs


# ==============================================================================
# 3. TIER 2: SURGICAL LLM WINDOW EXTRACTOR & PATCHER
# ==============================================================================
def locate_defect_window(wikitext: str, csv_snippet: str = "", window_radius: int = 300) -> tuple[str, str]:
    """
    Locates the defect in live wikitext and returns (window_text, anchor_method).
    Never returns the whole page—strictly slices a ~600-char local window.
    """
    if not wikitext:
        return "", "empty"

    anchor_pos = -1
    method = ""

    # Strategy 1: Anchor using clean prose runs from the Tab 5 CSV snippet
    if csv_snippet:
        clean_snip = re.sub(r'^\.\.\.|\.\.\.$', '', csv_snippet)
        clean_snip = clean_snip.replace(" ↵ ", "\n").replace("⏭️", "")
        # Extract 3-to-6 word runs of plain letters to bypass cleaned vs raw markup differences
        phrases = re.findall(r'[A-Za-zÁáÍíÚúḤḥṢṣṬṭḌḍẒẓ]{4,}(?:\s+[A-Za-zÁáÍíÚúḤḥṢṣṬṭḌḍẒẓ]{3,}){2,5}', clean_snip)
        # Try longest phrases first
        for phrase in sorted(phrases, key=len, reverse=True):
            pos = wikitext.find(phrase)
            if pos != -1:
                anchor_pos = pos
                method = f"CSV prose anchor ('{phrase[:28]}...')"
                break

    # Strategy 2: Scan raw wikitext directly for unclosed/mangled braces or broken tables/tags
    if anchor_pos == -1:
        defect_patterns = [
            (r'\{\{[^\n{}]+\}(?!\})', "Single-closed template"),
            (r'(?<!\{)\{[A-Za-z]+\|[^\n{}]+\}\}', "Single-opened template"),
            (r'\{\{\s*\|[^\n{}]+\}\}', "Anonymous {{|...}} template"),
            (r'^[^\n{}]*\}\}', "Orphan closing '}}'"),
            (r'\{\{\s*(?:center|right|left)\s*\|[^\n{}]+$', "Unclosed alignment template"),
            (r'<\/(?:small|center|div|span|table|ref)(?![A-Za-z>])', "Truncated HTML tag"),
            (r'<ref\b[^>]*>(?:(?!</ref>).){650,}', "Unclosed/oversized <ref>"),
        ]
        for pat, label in defect_patterns:
            m = re.search(pat, wikitext, flags=re.IGNORECASE | re.MULTILINE)
            if m:
                anchor_pos = m.start()
                method = f"Regex detector ({label})"
                break

    if anchor_pos == -1:
        return "", "not_found"

    start = max(0, anchor_pos - window_radius)
    end = min(len(wikitext), anchor_pos + window_radius)
    return wikitext[start:end], method


def propose_surgical_llm_patch(
    page_title: str,
    window_wikitext: str,
    invariant_type: str = "",
    csv_snippet: str = "",
) -> dict:
    """
    Sends ONLY the ~600-char local wikitext window to Gemini and requests a strict
    verbatim `find` -> `replace` JSON patch.
    """
    model = genai.GenerativeModel(MODEL_NAME)

    prompt = f"""You are a surgical MediaWiki syntax repair bot for Bahai.works.
You are given a ~600-character local window of raw MediaWiki wikitext from the page "{page_title}".

Reported Defect Type: {invariant_type or "Malformed MediaWiki template, table, or tag"}
Audit Scanner Hint: {csv_snippet or "(none)"}

LOCAL WIKITEXT WINDOW:
<<<WINDOW_START>>>
{window_wikitext}
<<<WINDOW_END>>>

YOUR TASK:
Identify the broken MediaWiki template (`{{{{...}}}}`), broken table syntax (`{{|`, `|-`, `|}}`), orphan braces (`}}}}`, `{{{{|...}}}}`), or unclosed `<ref>` / HTML tag inside the window, and produce a minimal, surgical `find` and `replace` patch.

STRICT RULES:
1. `find` MUST be an exact, character-for-character substring copied verbatim from inside `<<<WINDOW_START>>>` ... `<<<WINDOW_END>>>`. Include enough surrounding words (e.g. 25–120 chars) so `find` is 100% unique on the page.
2. Do NOT alter, modernize, or rewrite any author prose, spelling, or punctuation—ONLY fix the broken MediaWiki markup/templates/tags.
3. Common Bahai.works conventions:
   - Centered headings/lines use `{{{{center|...}}}}`. If `{{{{center|` is missing before an obvious heading ending with `}}}}`, add `{{{{center|` at the start of that heading block.
   - If an image caption or inscription is wrapped in anonymous braces like `{{{{|Caption text}}}}`, convert it to `{{{{center|Caption text}}}}` (or remove the stray braces if it's plain prose).
   - If a stray `}}}}` appears at the end of a regular paragraph or blockquote with no opening template, remove the stray `}}}}`.
   - Transliteration underlines `{{{{u|...}}}}` only wrap 1 or 2 transliterated letters (e.g. `{{{{u|dh}}}}`, `{{{{u|sh}}}}`, `{{{{u|kh}}}}`, `{{{{u|gh}}}}`, `{{{{u|th}}}}`, `{{{{u|zh}}}}`, `{{{{u|ch}}}}`).
4. If no markup error exists in the window, or if it cannot be safely fixed without seeing the physical book scan, set `"confidence": "SKIP"`.

Return ONLY a valid JSON object with these exact keys:
{{
  "find": "exact substring from window",
  "replace": "repaired substring",
  "summary": "Brief 5-10 word edit summary for MediaWiki history",
  "confidence": "HIGH" | "MEDIUM" | "SKIP"
}}"""

    safety_settings = {
        HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
        HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
        HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
        HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
    }

    try:
        resp = model.generate_content(prompt, safety_settings=safety_settings, request_options={"timeout": 60})
        parsed = json_repair.loads(resp.text)
        if not isinstance(parsed, dict):
            return {"confidence": "ERROR", "summary": "Invalid JSON returned by LLM", "find": "", "replace": ""}
        return {
            "find": str(parsed.get("find", "")),
            "replace": str(parsed.get("replace", "")),
            "summary": str(parsed.get("summary", "Bot: Fix malformed MediaWiki syntax")),
            "confidence": str(parsed.get("confidence", "MEDIUM")).upper(),
        }
    except Exception as e:
        check_fatal_rate_limit(e)
        return {"confidence": "ERROR", "summary": f"LLM Error: {e}", "find": "", "replace": ""}


# ==============================================================================
# 4. TARGET DISCOVERY PANEL (CSV / INSOURCE / PREFIX)
# ==============================================================================
st.subheader("1️⃣ Load Target Pages")

src_col1, src_col2 = st.columns([1.2, 2.8])
with src_col1:
    discovery_mode = st.radio(
        "Target Discovery Method",
        [
            "📄 Upload Tab 5 Invariant CSV (from Page 16)",
            "⚡ Fast CirrusSearch (`insource:` Query)",
            "📚 Book / Periodical Prefix or Title List",
        ],
    )

with src_col2:
    if discovery_mode.startswith("📄"):
        uploaded_csv = st.file_uploader("Upload CSV exported from Audit Studio Tab 5", type=["csv"])
        if uploaded_csv:
            df_csv = pd.read_csv(uploaded_csv)
            if "Source" in df_csv.columns:
                df_csv = df_csv[df_csv["Source"] == "bw"].copy()

            inv_col = "Invariant Violated"
            all_invs = sorted(df_csv[inv_col].dropna().unique().tolist()) if inv_col in df_csv.columns else []
            chosen_invs = st.multiselect(
                "Filter by Invariant Violated",
                options=all_invs,
                default=[x for x in all_invs if "1." in x or "2." in x] or all_invs,
            )
            if chosen_invs and inv_col in df_csv.columns:
                df_csv = df_csv[df_csv[inv_col].isin(chosen_invs)]

            st.caption(f"Matching rows in CSV: **{len(df_csv)}** across **{df_csv['Title'].nunique()}** unique pages.")
            if st.button("📥 Load Pages from CSV into Surgeon Queue", type="primary"):
                queue_items = {}
                for _, row in df_csv.iterrows():
                    t = str(row["Title"]).strip()
                    if t not in queue_items:
                        queue_items[t] = {
                            "title": t,
                            "invariant": str(row.get("Invariant Violated", "")),
                            "snippet": str(row.get("Snippet Preview", "")),
                            "citation": str(row.get("Citation ID / Segment", "")),
                        }
                st.session_state["surgeon_target_queue"] = list(queue_items.values())
                st.session_state.pop("tier1_proposals", None)
                st.session_state.pop("tier2_proposals", None)
                st.success(f"Loaded {len(queue_items)} unique pages from CSV!")

    elif discovery_mode.startswith("⚡"):
        preset_choice = st.selectbox("Select Pre-Filtered `insource:` Query", ["Custom Query..."] + list(INSOURCE_PRESETS.keys()))
        default_q = INSOURCE_PRESETS.get(preset_choice, r'insource:"bwpage" insource:/\{\{bwpage\|[^}\n]+\}(?!\})/')
        c_q1, c_q2, c_q3 = st.columns([2.5, 1.2, 0.8])
        with c_q1:
            srsearch_query = st.text_input("CirrusSearch Query (`srsearch`)", value=default_q)
        with c_q2:
            optional_prefix = st.text_input("Optional `prefix:` Scope", placeholder="e.g. Bahá’í World/")
        with c_q3:
            max_sr_results = st.number_input("Max Hits", min_value=10, max_value=2000, value=250, step=50)

        if st.button("🔍 Run `insource:` Search on Bahai.works", type="primary"):
            full_q = srsearch_query.strip()
            if optional_prefix.strip():
                full_q = f'prefix:"{optional_prefix.strip()}" {full_q}'

            session = requests.Session()
            try:
                get_csrf_token(session)
                found_titles = []
                sroffset = 0
                with st.spinner(f"Querying CirrusSearch: `{full_q}`..."):
                    while len(found_titles) < max_sr_results:
                        params = {
                            "action": "query",
                            "list": "search",
                            "srsearch": full_q,
                            "srnamespace": "0",
                            "srlimit": min(50, max_sr_results - len(found_titles)),
                            "sroffset": sroffset,
                            "format": "json",
                        }
                        resp = session.get(API_URL, params=params, timeout=30).json()
                        if "error" in resp:
                            st.error(f"MediaWiki Search Error: {resp['error'].get('info')}")
                            break
                        hits = resp.get("query", {}).get("search", [])
                        for h in hits:
                            found_titles.append({
                                "title": h["title"],
                                "invariant": preset_choice,
                                "snippet": re.sub(r"<[^>]+>", "", h.get("snippet", "")),
                                "citation": "",
                            })
                        if "continue" in resp and "sroffset" in resp["continue"]:
                            sroffset = resp["continue"]["sroffset"]
                        else:
                            break

                st.session_state["surgeon_target_queue"] = found_titles
                st.session_state.pop("tier1_proposals", None)
                st.session_state.pop("tier2_proposals", None)
                st.success(f"Found **{len(found_titles)}** matching pages on `bahai.works`!")
            finally:
                session.close()

    else:
        p_col1, p_col2 = st.columns(2)
        with p_col1:
            apprefix_input = st.text_input("Fetch All Subpages Under Prefix (`apprefix`)", placeholder="e.g. Bahá’í World/Volume 8/")
        with p_col2:
            manual_titles_input = st.text_area("Or Paste Page Titles (One per line)", height=90, placeholder="Star of the West/Volume 3/Issue 19/Text")

        if st.button("📥 Load Prefix / Title List", type="primary"):
            loaded = []
            if manual_titles_input.strip():
                for ln in manual_titles_input.splitlines():
                    if ln.strip():
                        loaded.append({"title": ln.strip().replace("_", " "), "invariant": "Manual", "snippet": "", "citation": ""})

            if apprefix_input.strip():
                session = requests.Session()
                try:
                    get_csrf_token(session)
                    params = {
                        "action": "query",
                        "list": "allpages",
                        "apprefix": apprefix_input.strip().replace("_", " "),
                        "aplimit": "max",
                        "format": "json",
                    }
                    while True:
                        resp = session.get(API_URL, params=params, timeout=20).json()
                        for p in resp.get("query", {}).get("allpages", []):
                            loaded.append({"title": p["title"], "invariant": "Prefix Scan", "snippet": "", "citation": ""})
                        if "continue" not in resp:
                            break
                        params["apcontinue"] = resp["continue"]["apcontinue"]
                finally:
                    session.close()

            st.session_state["surgeon_target_queue"] = loaded
            st.session_state.pop("tier1_proposals", None)
            st.session_state.pop("tier2_proposals", None)
            st.success(f"Loaded **{len(loaded)}** target pages!")

target_queue = st.session_state.get("surgeon_target_queue", [])
if target_queue:
    st.info(f"📋 **Active Target Queue:** `{len(target_queue)}` page(s) loaded and ready for inspection.")

st.divider()

# ==============================================================================
# 5. TWO-TIER EXECUTION TABS
# ==============================================================================
tab_tier1, tab_tier2 = st.tabs([
    "🤖 Tab 1: Tier 1 — Deterministic Python Regex Surgeon (Zero LLM)",
    "🧠 Tab 2: Tier 2 — Surgical LLM Window Patcher (600-char Context)",
])

# ------------------------------------------------------------------------------
# TAB 1: TIER 1 DETERMINISTIC PYTHON REGEX SURGEON
# ------------------------------------------------------------------------------
with tab_tier1:
    st.subheader("🤖 Tier 1: High-Confidence Python Regex Replacements")
    st.caption("Fetches live wikitext in batches of 50, runs deterministic regex fixes, shows exact line diffs, and batch-uploads approved changes.")

    enabled_packs = st.multiselect(
        "Enabled Deterministic Rule Packs",
        options=list(TIER1_RULE_PACKS.keys()),
        default=list(TIER1_RULE_PACKS.keys()),
        format_func=lambda k: TIER1_RULE_PACKS[k][0],
    )

    if st.button("🔍 Scan Loaded Queue with Tier 1 Regex Rules", type="primary", disabled=not target_queue):
        session = requests.Session()
        try:
            get_csrf_token(session)
            titles = [item["title"] for item in target_queue]
            meta_by_title = {item["title"]: item for item in target_queue}

            with st.spinner(f"Batch-fetching live wikitext for {len(titles)} page(s)..."):
                wikitext_map = fetch_wikitext_batch(titles, session=session)

            proposals = []
            unresolved_for_tier2 = []

            for t in titles:
                raw_wt = wikitext_map.get(t)
                if not raw_wt:
                    continue
                new_wt, notes = run_tier1_rules(raw_wt, enabled_packs)
                if new_wt != raw_wt:
                    diffs = build_compact_diff_preview(raw_wt, new_wt)
                    proposals.append({
                        "Approve": True,
                        "Title": t,
                        "Fixes Applied": "; ".join(notes),
                        "Before (Line Preview)": " | ".join(f"L{d['line']}: {d['before']}" for d in diffs),
                        "After (Line Preview)": " | ".join(f"L{d['line']}: {d['after']}" for d in diffs),
                        "_new_wikitext": new_wt,
                        "_summary": f"Bot: {', '.join(notes)}",
                    })
                else:
                    unresolved_for_tier2.append(meta_by_title[t])

            st.session_state["tier1_proposals"] = proposals
            st.session_state["tier1_unresolved"] = unresolved_for_tier2
        finally:
            session.close()

    t1_proposals = st.session_state.get("tier1_proposals")
    if t1_proposals is not None:
        unresolved_cnt = len(st.session_state.get("tier1_unresolved", []))
        st.write(
            f"✅ **Tier 1 matched `{len(t1_proposals)}` page(s)** with deterministic fixes. "
            f"(`{unresolved_cnt}` page(s) had no Tier 1 regex match and can be sent to **Tab 2 Surgical LLM**.)"
        )

        if unresolved_cnt > 0:
            if st.button(f"➡️ Send {unresolved_cnt} Unresolved Page(s) Only to Active Queue for Tab 2"):
                st.session_state["surgeon_target_queue"] = st.session_state["tier1_unresolved"]
                st.rerun()

        if t1_proposals:
            df_t1 = pd.DataFrame(t1_proposals)
            edited_t1 = st.data_editor(
                df_t1.drop(columns=["_new_wikitext", "_summary"]),
                width="stretch",
                hide_index=True,
                disabled=["Title", "Fixes Applied", "Before (Line Preview)", "After (Line Preview)"],
                key="tier1_editor",
            )

            approved_indices = edited_t1[edited_t1["Approve"] == True].index.tolist()
            if st.button(f"🚀 Upload {len(approved_indices)} Approved Tier 1 Fix(es) to Bahai.works", type="primary", disabled=not approved_indices):
                progress_bar = st.progress(0)
                status_box = st.empty()
                shared_session = requests.Session()
                try:
                    get_csrf_token(shared_session)
                    success_cnt = 0
                    errors = []

                    def _upload_t1(idx):
                        item = t1_proposals[idx]
                        try:
                            upload_to_bahaiworks(
                                title=item["Title"],
                                content=item["_new_wikitext"],
                                summary=item["_summary"],
                                check_exists=False,
                                session=shared_session,
                            )
                            return True, item["Title"], None
                        except Exception as e:
                            return False, item["Title"], str(e)

                    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
                        futs = [ex.submit(_upload_t1, idx) for idx in approved_indices]
                        for i, fut in enumerate(concurrent.futures.as_completed(futs), 1):
                            ok, title, err = fut.result()
                            if ok:
                                success_cnt += 1
                            else:
                                errors.append(f"{title}: {err}")
                            progress_bar.progress(i / len(approved_indices))
                            status_box.write(f"Uploaded {i}/{len(approved_indices)} (`{title}`)")

                    status_box.success(f"🎉 Successfully updated {success_cnt}/{len(approved_indices)} page(s) on Bahai.works!")
                    if errors:
                        with st.expander("⚠️ Upload Errors"):
                            for err in errors:
                                st.error(err)
                finally:
                    shared_session.close()

# ------------------------------------------------------------------------------
# TAB 2: TIER 2 SURGICAL LLM WINDOW PATCHER
# ------------------------------------------------------------------------------
with tab_tier2:
    st.subheader("🧠 Tier 2: Surgical LLM Window Patcher (`find` ➔ `replace`)")
    st.caption(
        "Slices a **~600-character local window** around the defect in the live wikitext and asks Gemini for a strict verbatim "
        "`find` ➔ `replace` JSON patch. Verifies that `find` appears **exactly once** in the page before allowing upload."
    )

    col_llm1, col_llm2 = st.columns([1, 2])
    with col_llm1:
        llm_batch_limit = st.number_input(
            "Max Pages to Process in This LLM Batch",
            min_value=1,
            max_value=200,
            value=min(25, max(1, len(target_queue))),
            step=5,
        )
    with col_llm2:
        window_radius = st.slider(
            "Local Wikitext Context Radius (chars before & after defect)",
            min_value=150,
            max_value=600,
            value=300,
            step=50,
            help="300 radius = ~600 total characters sent to Gemini per page.",
        )

    if st.button("🧠 Generate Surgical LLM Patches for Queue", type="primary", disabled=not target_queue):
        batch_items = target_queue[: int(llm_batch_limit)]
        titles = [x["title"] for x in batch_items]

        session = requests.Session()
        try:
            get_csrf_token(session)
            with st.spinner(f"Fetching live wikitext for {len(titles)} page(s)..."):
                wt_map = fetch_wikitext_batch(titles, session=session)
        finally:
            session.close()

        prog = st.progress(0)
        status = st.empty()
        llm_proposals = []

        for idx, item in enumerate(batch_items, 1):
            t = item["title"]
            raw_wt = wt_map.get(t, "")
            status.write(f"Analyzing ({idx}/{len(batch_items)}): `{t}`...")

            if not raw_wt:
                prog.progress(idx / len(batch_items))
                continue

            window_txt, anchor_method = locate_defect_window(
                raw_wt, csv_snippet=item.get("snippet", ""), window_radius=window_radius
            )
            if not window_txt:
                llm_proposals.append({
                    "Approve": False,
                    "Title": t,
                    "Confidence": "NO_ANCHOR",
                    "Anchor": "Could not locate snippet in live wikitext (already fixed?)",
                    "Find (Exact)": "",
                    "Replace (Surgical)": "",
                    "Summary": "",
                    "_raw_wikitext": raw_wt,
                })
                prog.progress(idx / len(batch_items))
                continue

            patch = propose_surgical_llm_patch(
                page_title=t,
                window_wikitext=window_txt,
                invariant_type=item.get("invariant", ""),
                csv_snippet=item.get("snippet", ""),
            )

            find_str = patch.get("find", "")
            replace_str = patch.get("replace", "")
            conf = patch.get("confidence", "SKIP")

            # Strict Uniqueness Verification in full page
            occ_count = raw_wt.count(find_str) if find_str else 0
            is_valid = (
                conf in ("HIGH", "MEDIUM")
                and bool(find_str)
                and find_str != replace_str
                and occ_count == 1
            )

            if occ_count == 0 and conf not in ("SKIP", "ERROR"):
                conf = "HALLUCINATED_FIND (0 matches)"
            elif occ_count > 1:
                conf = f"AMBIGUOUS ({occ_count} matches)"

            llm_proposals.append({
                "Approve": is_valid and conf == "HIGH",
                "Title": t,
                "Confidence": conf,
                "Anchor": anchor_method,
                "Find (Exact)": find_str,
                "Replace (Surgical)": replace_str,
                "Summary": patch.get("summary", "Bot: Surgical markup fix"),
                "_raw_wikitext": raw_wt,
            })
            prog.progress(idx / len(batch_items))

        status.success(f"Generated {len(llm_proposals)} surgical patch proposal(s)!")
        st.session_state["tier2_proposals"] = llm_proposals

    t2_proposals = st.session_state.get("tier2_proposals")
    if t2_proposals:
        df_t2 = pd.DataFrame(t2_proposals)
        edited_t2 = st.data_editor(
            df_t2.drop(columns=["_raw_wikitext"]),
            width="stretch",
            hide_index=True,
            disabled=["Title", "Confidence", "Anchor"],
            key="tier2_editor",
        )

        with st.expander("🔍 Side-by-Side Snippet Diff Cards", expanded=False):
            for idx, row in edited_t2.iterrows():
                if not row["Find (Exact)"]:
                    continue
                st.markdown(f"**`{row['Title']}`** — `{row['Confidence']}` ({row['Summary']})")
                c1, c2 = st.columns(2)
                with c1:
                    st.caption("BEFORE (`find`)")
                    st.code(row["Find (Exact)"], language="mediawiki")
                with c2:
                    st.caption("AFTER (`replace`)")
                    st.code(row["Replace (Surgical)"], language="mediawiki")
                st.divider()

        approved_t2_idxs = edited_t2[edited_t2["Approve"] == True].index.tolist()
        if st.button(
            f"🚀 Apply & Upload {len(approved_t2_idxs)} Approved Surgical LLM Patch(es)",
            type="primary",
            disabled=not approved_t2_idxs,
        ):
            progress_bar = st.progress(0)
            status_box = st.empty()
            shared_session = requests.Session()
            try:
                get_csrf_token(shared_session)
                success_cnt = 0
                errors = []

                for i, idx in enumerate(approved_t2_idxs, 1):
                    row = edited_t2.loc[idx]
                    orig_wt = t2_proposals[idx]["_raw_wikitext"]
                    find_s = row["Find (Exact)"]
                    rep_s = row["Replace (Surgical)"]
                    title = row["Title"]

                    if not find_s or orig_wt.count(find_s) != 1:
                        errors.append(f"{title}: `find` string did not match uniquely (count={orig_wt.count(find_s)}). Skipped.")
                        continue

                    patched_wt = orig_wt.replace(find_s, rep_s, 1)
                    status_box.write(f"Uploading ({i}/{len(approved_t2_idxs)}): `{title}`...")
                    try:
                        upload_to_bahaiworks(
                            title=title,
                            content=patched_wt,
                            summary=row["Summary"] or "Bot: Surgical markup fix",
                            check_exists=False,
                            session=shared_session,
                        )
                        success_cnt += 1
                    except Exception as e:
                        errors.append(f"{title}: {e}")

                    progress_bar.progress(i / len(approved_t2_idxs))

                status_box.success(f"🎉 Applied and uploaded {success_cnt}/{len(approved_t2_idxs)} surgical LLM patches!")
                if errors:
                    with st.expander("⚠️ Patch / Upload Warnings"):
                        for err in errors:
                            st.error(err)
            finally:
                shared_session.close()
