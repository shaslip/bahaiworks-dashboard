import streamlit as st
import os
import sys
import re
import json
import tempfile
import requests
import concurrent.futures
from urllib.parse import urlparse
import google.generativeai as genai

# --- Path Setup ---
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
if project_root not in sys.path:
    sys.path.append(project_root)

from src.mediawiki_uploader import (
    fetch_wikitext,
    upload_to_mediawiki,
    upload_file_binary,
    get_csrf_token,
)
from src.gemini_processor import MODEL_NAME, check_fatal_rate_limit

MEDIA_API_URL = "https://bahai.media/api.php"

st.set_page_config(page_title="Update BWNS", page_icon="📰", layout="wide")

# ==============================================================================
# HELPER FUNCTIONS
# ==============================================================================

def get_latest_bwns_id_from_module():
    """Fetches Module:BWNS from bahai.media and returns (max_id, raw_wikitext)."""
    text, err = fetch_wikitext("Module:BWNS", api_url=MEDIA_API_URL)
    if err or not text:
        return None, None

    map_match = re.search(r'publicationDate_map\s*=\s*\{([^}]+)\}', text)
    if not map_match:
        return None, text

    ids = [int(m) for m in re.findall(r'\[\s*(\d+)\s*\]\s*=', map_match.group(1))]
    max_id = max(ids) if ids else None
    return max_id, text


def update_bwns_module_date(story_id, pub_date, session):
    """Adds or updates [story_id] = "pub_date" in Module:BWNS on bahai.media."""
    text, err = fetch_wikitext("Module:BWNS", session=session, api_url=MEDIA_API_URL)
    if err or not text:
        raise Exception(f"Could not fetch Module:BWNS: {err}")

    # Check if entry already exists
    existing_pattern = re.compile(rf'(\[\s*{story_id}\s*\]\s*=\s*")[^"]*(")')
    if existing_pattern.search(text):
        new_text = existing_pattern.sub(rf'\g<1>{pub_date}\g<2>', text)
    else:
        # Insert right before the closing brace of publicationDate_map
        map_pattern = re.compile(r'(local\s+publicationDate_map\s*=\s*\{.*?)(\n\})', re.DOTALL)
        match = map_pattern.search(text)
        if not match:
            raise Exception("Could not locate publicationDate_map block in Module:BWNS.")

        prefix = match.group(1).rstrip()
        if not prefix.endswith(","):
            prefix += ","
        new_entry = f'\n    [{story_id}] = "{pub_date}",'
        new_text = text[:match.start()] + prefix + new_entry + match.group(2) + text[match.end():]

    if new_text != text:
        upload_to_mediawiki(
            title="Module:BWNS",
            content=new_text,
            summary=f"Add BWNS {story_id} publication date ({pub_date})",
            session=session,
            api_url=MEDIA_API_URL,
        )


def sanitize_filename(text, max_length=55):
    """Sanitize filename by keeping only alphanumeric and common punctuation."""
    text = re.sub(r'[^a-zA-Z0-9 _.,()\'"-]', '', text)
    text = text.replace("'", "").replace("’", "")
    text = text.replace(".", ",")
    text = text.replace(' ', '_')

    extension_length = 4
    available_length = max_length - extension_length

    if len(text) > available_length:
        last_space = text[:available_length].rfind('_')
        if last_space > 0:
            text = text[:last_space]
        else:
            text = text[:available_length]

    text = text.rstrip(',_ ')
    return text or "BWNS_image"


def generate_filename_with_gemini(caption):
    """Uses Gemini to generate a descriptive filename base (without extension) from a caption."""
    try:
        model = genai.GenerativeModel(MODEL_NAME)
        prompt = f"""
        Your task is to create a concise, descriptive, and clean filename from an image caption.

        Rules:
        1. The filename should be in English, retain existing capitalization, remove diacritical or accent marks.
        2. It should be 5-10 words long, focusing on the main subject, people, or location.
        3. Replace all spaces with underscores (_).
        4. Do NOT include any file extension (like .jpg).
        5. Do NOT include any characters that are unsafe for filenames (no apostrophes).
        6. Return ONLY the filename string and nothing else.

        Example 1:
        Caption: "A view of the Shrine of the Báb on Mount Carmel in Haifa, Israel, surrounded by terraced gardens."
        Filename: View_of_Shrine_of_the_Bab_Mount_Carmel_Haifa

        Example 2:
        Caption: "Barney Leith (right), secretary for external affairs of the Baha'i community of the United Kingdom, talks to Dr. Anna Cummins."
        Filename: Barney_Leith_and_Anna_Cummins_UK

        Now, generate a filename for this caption:
        Caption: "{caption}"
        Filename:
        """
        response = model.generate_content(prompt)
        ai_filename = response.text.strip().replace(' ', '_')
        return sanitize_filename(ai_filename)
    except Exception as e:
        check_fatal_rate_limit(e)
        return sanitize_filename(caption)


def build_file_wikitext(caption, story_id):
    return f"""== File info ==
{{{{cs
| caption = {caption}
| source = {{{{bwn|{story_id}}}}}
}}}}

== File license ==
{{{{Baha'i World News Service}}}}"""


def scrape_bwns_story(story_id):
    """
    Fetches a single BWNS story page and extracts metadata + images.
    Returns a dict with status: 'ok', 'podcast', 'not_found', or 'error'.
    """
    url = f"https://news.bahai.org/story/{story_id}/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
    }

    try:
        response = requests.get(url, allow_redirects=True, headers=headers, timeout=20)
        if response.status_code == 404:
            return {"status": "not_found", "final_url": response.url}
        if response.status_code != 200:
            return {"status": "error", "message": f"HTTP {response.status_code}", "final_url": response.url}

        final_url = response.url
        is_podcast = "/podcasts/" in final_url

        # Reconstruct Next.js flight stream
        chunks = re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', response.text)
        full_payload = "".join(json.loads(f'"{c}"') for c in chunks)

        target_marker = f'"_locales":["en"],"storyNumber":{story_id}'
        idx = full_payload.find(target_marker)
        if idx == -1:
            target_marker = f'"storyNumber":{story_id}'
            idx = full_payload.find(target_marker)
        if idx == -1:
            return {"status": "error", "message": "Could not find storyNumber in page JSON.", "final_url": final_url}

        start = full_payload.rfind('{"data":', 0, idx)
        if start == -1:
            return {"status": "error", "message": "Could not find start of data JSON object.", "final_url": final_url}

        story_data, _ = json.JSONDecoder().raw_decode(full_payload[start + 8:])

        pub_date = story_data.get("date", "")
        title = story_data.get("title", f"Story {story_id}")

        images_and_captions = []

        if story_data.get("thumbnail"):
            thumb_url = story_data["thumbnail"].get("responsiveImage", {}).get("src")
            thumb_desc = (story_data.get("thumbnailDescription") or "").strip()
            if not thumb_desc and is_podcast:
                thumb_desc = (story_data.get("description") or "").strip()
            if thumb_url:
                clean_url = thumb_url.split("?")[0]
                images_and_captions.append({
                    "url": clean_url,
                    "caption": thumb_desc,
                    "naming_hint": thumb_desc or story_data.get("description") or title,
                })

        for item in story_data.get("storyContent", []):
            typename = item.get("__typename")

            if typename in ("InlineImageRecord", "FullWidthImageRecord") and item.get("image"):
                img_url = item["image"].get("responsiveImage", {}).get("src")
                caption = item.get("caption") or item.get("inlineImageAltText") or item.get("altText")
                if img_url and caption:
                    clean_url = img_url.split("?")[0]
                    images_and_captions.append({"url": clean_url, "caption": caption.strip()})

            elif typename == "SideBySideImageRecord" and item.get("images"):
                imgs = item.get("images", [])
                if len(imgs) >= 1 and imgs[0].get("responsiveImage"):
                    img_url_1 = imgs[0]["responsiveImage"].get("src")
                    cap_1 = item.get("captionStart") or item.get("altTextStart")
                    if img_url_1 and cap_1:
                        images_and_captions.append({"url": img_url_1.split("?")[0], "caption": cap_1.strip()})

                if len(imgs) >= 2 and imgs[1].get("responsiveImage"):
                    img_url_2 = imgs[1]["responsiveImage"].get("src")
                    cap_2 = item.get("captionEnd") or item.get("altTextEnd")
                    if img_url_2 and cap_2:
                        images_and_captions.append({"url": img_url_2.split("?")[0], "caption": cap_2.strip()})

        # Deduplicate by URL
        seen_urls = set()
        unique_items = []
        for item in images_and_captions:
            if item["url"] not in seen_urls:
                seen_urls.add(item["url"])
                unique_items.append(item)

        return {
            "status": "ok",
            "story_id": story_id,
            "final_url": final_url,
            "title": title,
            "date": pub_date,
            "images": unique_items,
        }

    except Exception as e:
        return {"status": "error", "message": str(e), "final_url": url}


def get_unique_wiki_filename(base_filename, api_url=MEDIA_API_URL):
    """Checks bahai.media if File:<base_filename> exists; appends -2, -3 if needed."""
    if not base_filename.lower().startswith("file:"):
        base_filename = f"File:{base_filename}"

    current_name = base_filename
    increment = 2
    while True:
        try:
            resp = requests.get(
                api_url,
                params={"action": "query", "titles": current_name, "format": "json"},
                timeout=10,
            ).json()
            pages = resp.get("query", {}).get("pages", {})
            exists = not any(int(pid) < 0 for pid in pages)
        except Exception:
            exists = False

        if not exists:
            return current_name

        name_part, ext = os.path.splitext(base_filename)
        current_name = f"{name_part}-{increment}{ext}"
        increment += 1
        if increment > 25:
            return current_name


# ==============================================================================
# STATE INITIALIZATION
# ==============================================================================

if "bwns_next_id_after_upload" in st.session_state:
    st.session_state.bwns_target_id = st.session_state.pop("bwns_next_id_after_upload")

if "bwns_latest_in_module" not in st.session_state:
    max_id, _ = get_latest_bwns_id_from_module()
    st.session_state.bwns_latest_in_module = max_id or 1824
    st.session_state.bwns_target_id = (max_id + 1) if max_id else 1825

if "bwns_current_story" not in st.session_state:
    st.session_state.bwns_current_story = None

# ==============================================================================
# UI
# ==============================================================================

st.title("📰 Update Bahá’í World News Service (BWNS)")

def refresh_max_id():
    max_id, _ = get_latest_bwns_id_from_module()
    if max_id:
        st.session_state.bwns_latest_in_module = max_id
        st.session_state.bwns_target_id = max_id + 1

def skip_to_next_story():
    st.session_state.bwns_target_id += 1
    st.session_state.bwns_current_story = None
    st.session_state.bwns_auto_fetch = True

col_info, col_refresh = st.columns([4, 1])
with col_info:
    st.info(f"📌 Highest Story ID currently in `Module:BWNS`: **{st.session_state.bwns_latest_in_module}**")
with col_refresh:
    st.button("🔄 Refresh Module Max ID", on_click=refresh_max_id, use_container_width=True)

# --- Top Controls: Select & Fetch Story ---
col_id, col_fetch, col_next = st.columns([2, 2, 2])
with col_id:
    story_id_input = st.number_input(
        "Story ID to Process",
        min_value=1,
        step=1,
        key="bwns_target_id",
    )

def load_story(sid):
    with st.spinner(f"Fetching story {sid} from news.bahai.org..."):
        result = scrape_bwns_story(sid)

    if result["status"] == "ok" and result["images"]:
        with st.spinner(f"Generating {len(result['images'])} filenames with Gemini..."):
            images = result["images"]
            with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
                future_to_idx = {
                    executor.submit(
                        generate_filename_with_gemini,
                        img.get("naming_hint") or img["caption"]
                    ): i
                    for i, img in enumerate(images)
                }
                for future in concurrent.futures.as_completed(future_to_idx):
                    i = future_to_idx[future]
                    base_name = future.result()
                    parsed_url = urlparse(images[i]["url"])
                    _, ext = os.path.splitext(parsed_url.path)
                    if not ext:
                        ext = ".jpg"
                    images[i]["proposed_filename"] = f"{base_name}{ext}"
                    images[i]["wikitext"] = build_file_wikitext(images[i]["caption"], sid)

    st.session_state.bwns_current_story = result

if st.session_state.pop("bwns_auto_fetch", False):
    load_story(st.session_state.bwns_target_id)

with col_fetch:
    st.write("")  # Vertical alignment spacer
    if st.button("🔍 Fetch Story", type="primary", use_container_width=True):
        load_story(story_id_input)
        st.rerun()

with col_next:
    st.write("")
    st.button("⏭️ Skip / Next Story ID", on_click=skip_to_next_story, use_container_width=True)

st.divider()

# ==============================================================================
# DISPLAY FETCHED STORY
# ==============================================================================

story = st.session_state.bwns_current_story

if story:
    status = story["status"]

    if status == "not_found":
        st.warning(f"⚠️ Story **{story_id_input}** returned **404 Not Found** (`{story['final_url']}`).")
    elif status == "podcast":
        st.warning(
            f"🎙️ **Podcast URL detected:** [{story['final_url']}]({story['final_url']})\n\n"
            "We can't handle podcast pages yet. Click **Skip / Next Story ID** above to continue."
        )
    elif status == "error":
        st.error(f"❌ Error fetching story: {story.get('message')} (`{story.get('final_url')}`)")
    elif status == "ok":
        sid = story["story_id"]
        st.subheader(f"Story {sid}: {story['title']}")
        st.markdown(f"🔗 **Redirected URL:** [{story['final_url']}]({story['final_url']})")

        col_date, col_count = st.columns([2, 4])
        with col_date:
            edited_date = st.text_input(
                "Publication Date (YYYY-MM-DD for Module:BWNS)",
                value=story.get("date", ""),
                key=f"date_{sid}",
            )
        with col_count:
            st.write("")
            st.write(f"🖼️ Found **{len(story['images'])}** unique image(s) with captions.")

        if not story["images"]:
            st.info("No images with captions were found on this story page.")
        else:
            st.divider()
            for idx, img in enumerate(story["images"]):
                col_preview, col_details = st.columns([1.5, 3.5])

                with col_preview:
                    st.image(img["url"], use_container_width=True)
                    st.checkbox(
                        "Include in upload",
                        value=True,
                        key=f"inc_{sid}_{idx}",
                    )

                with col_details:
                    st.text_input(
                        "Filename (without File: prefix)",
                        value=img["proposed_filename"],
                        key=f"fname_{sid}_{idx}",
                    )
                    st.text_area(
                        "File Description Wikitext",
                        value=img["wikitext"],
                        height=180,
                        key=f"wtext_{sid}_{idx}",
                    )
                st.divider()

            if st.button(f"🚀 Upload Story {sid} to Bahai.media & Update Module:BWNS", type="primary", use_container_width=True):
                session = requests.Session()
                progress_bar = st.progress(0)
                log_container = st.container(border=True)

                try:
                    log_container.write("🔐 Authenticating with bahai.media...")
                    get_csrf_token(session, api_url=MEDIA_API_URL)

                    selected_indices = [
                        i for i in range(len(story["images"]))
                        if st.session_state.get(f"inc_{sid}_{i}", True)
                    ]

                    total_steps = len(selected_indices) + 2  # images + category + Module:BWNS
                    current_step = 0
                    uploaded_count = 0

                    # 1. Download & Upload Each Selected Image
                    for i in selected_indices:
                        img_data = story["images"][i]
                        raw_fname = st.session_state[f"fname_{sid}_{i}"].strip()
                        wikitext = st.session_state[f"wtext_{sid}_{i}"].strip()

                        target_file_title = get_unique_wiki_filename(raw_fname, api_url=MEDIA_API_URL)
                        log_container.write(f"⬇️ Downloading & uploading `{target_file_title}`...")

                        download_url = img_data["url"]
                        head_resp = requests.head(download_url, timeout=10)
                        content_len = int(head_resp.headers.get("Content-Length", 0))
                        if content_len > 10 * 1024 * 1024:  # Over 10 MB -> fetch smaller version
                            download_url = f"{download_url}?w=3000&auto=format&q=85"

                        img_resp = requests.get(download_url, timeout=30)
                        img_resp.raise_for_status()

                        _, ext = os.path.splitext(target_file_title)
                        with tempfile.NamedTemporaryFile(delete=False, suffix=ext or ".jpg") as tmp_file:
                            tmp_file.write(img_resp.content)
                            tmp_path = tmp_file.name

                        try:
                            upload_file_binary(
                                filename=target_file_title,
                                file_path=tmp_path,
                                summary=f"Uploaded from BWNS story {sid}",
                                session=session,
                                api_url=MEDIA_API_URL,
                            )
                            upload_to_mediawiki(
                                title=target_file_title,
                                content=wikitext,
                                summary=f"Added file description for BWNS story {sid}",
                                session=session,
                                api_url=MEDIA_API_URL,
                            )
                            uploaded_count += 1
                            log_container.success(f"✅ Uploaded `{target_file_title}`")
                        finally:
                            if os.path.exists(tmp_path):
                                os.remove(tmp_path)

                        current_step += 1
                        progress_bar.progress(current_step / total_steps)

                    # 2. Ensure Category:BWNS <sid> exists
                    cat_title = f"Category:BWNS {sid}"
                    log_container.write(f"📁 Checking `{cat_title}`...")
                    existing_cat, err = fetch_wikitext(cat_title, session=session, api_url=MEDIA_API_URL)
                    if not existing_cat:
                        cat_content = f"{{{{bwns_view|{sid}}}}}{{{{CatIndicator|0}}}}"
                        upload_to_mediawiki(
                            title=cat_title,
                            content=cat_content,
                            summary=f"Created category for BWNS {sid}",
                            session=session,
                            api_url=MEDIA_API_URL,
                        )
                    current_step += 1
                    progress_bar.progress(current_step / total_steps)

                    # 3. Update Module:BWNS with publication date
                    if edited_date.strip():
                        status_box.info("📅 Updating `Module:BWNS` publicationDate_map...")
                        update_bwns_module_date(sid, edited_date.strip(), session=session)
                        st.session_state.bwns_latest_in_module = max(
                            st.session_state.bwns_latest_in_module, sid
                        )

                    progress_bar.progress(1.0)
                    log_container.success(
                        f"🎉 Done! Uploaded {uploaded_count} image(s), verified `{cat_title}`, and updated `Module:BWNS`. "
                        f"Click **⏭️ Skip / Next Story ID** at the top when ready for Story {sid + 1}."
                    )

                    # Advance to next story automatically
                    st.session_state.bwns_next_id_after_upload = sid + 1
                    st.session_state.bwns_current_story = None
                    st.rerun()

                except Exception as e:
                    st.error(f"❌ Upload failed: {e}")
                finally:
                    session.close()
