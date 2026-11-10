from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
import os
import re
import sys
import tempfile
import cv2
import fitz
import numpy as np
from PIL import Image
import pytesseract
import streamlit as st

# --- Path Setup ---
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
if project_root not in sys.path:
    sys.path.append(project_root)

st.set_page_config(
    page_title="Magazine Scan Stitcher", page_icon="🧩", layout="wide"
)


# ==============================================================================
# HELPER FUNCTIONS
# ==============================================================================
def infer_pdf_filename(folder_path):
    """Infers 'The_American_Bahá’í_Vol34_No1.pdf' from path if Vol/No exist."""
    vol_match = re.search(r"Vol(?:ume)?[_\s]*(\d+)", folder_path, re.IGNORECASE)
    no_match = re.search(
        r"(?:No|Issue)[_\s]*(\d+)", folder_path, re.IGNORECASE
    )
    if vol_match and no_match:
        vol = vol_match.group(1)
        no = no_match.group(1)
        return f"The_American_Bahá’í_Vol{vol}_No{no}.pdf"
    folder_name = os.path.basename(os.path.normpath(folder_path))
    return f"{folder_name}_Stitched.pdf"


def discover_scan_groups(folder_path):
    """Scans directory for part files like 1a.png, 1b.png, 5-6a.png, 6c.png.

    If 5-6a/b exists, combines 5*, 5-6*, and 6* into a single '5-6' spread
    group. Returns an ordered list of dicts: [{'target': '1.png', 'parts':
    ['1a.png', '1b.png'], 'sort_key': 1}, ...]
    """
    if not os.path.isdir(folder_path):
        return []

    part_pattern = re.compile(
        r"^(\d+(?:-\d+)?)([a-z])\.(png|jpg|jpeg|tif|tiff)$", re.IGNORECASE
    )
    raw_groups = {}
    ext_map = {}

    for fname in os.listdir(folder_path):
        m = part_pattern.match(fname)
        if m:
            base_id = m.group(1)  # e.g., '1' or '5-6'
            part_letter = m.group(2).lower()
            ext = m.group(3)
            raw_groups.setdefault(base_id, []).append((part_letter, fname))
            ext_map[base_id] = ext

    # Sort parts within each base_id by letter (a, b, c...)
    for base_id in raw_groups:
        raw_groups[base_id] = [
            f for _, f in sorted(raw_groups[base_id], key=lambda x: x[0])
        ]

    # Detect spreads like '5-6' and absorb '5' and '6' into '5-6'
    spread_keys = [k for k in raw_groups if "-" in k]
    absorbed = set()
    final_groups = {}

    for sp_key in spread_keys:
        pages = sp_key.split("-")
        combined_parts = []
        # Order: left page parts (5a, 5b), middle crease parts (5-6a, 5-6b), right page parts (6a, 6b)
        if len(pages) == 2:
            left_p, right_p = pages[0], pages[1]
            if left_p in raw_groups:
                combined_parts.extend(raw_groups[left_p])
                absorbed.add(left_p)
            combined_parts.extend(raw_groups[sp_key])
            if right_p in raw_groups:
                combined_parts.extend(raw_groups[right_p])
                absorbed.add(right_p)
        else:
            combined_parts.extend(raw_groups[sp_key])

        final_groups[sp_key] = combined_parts

    for k, parts in raw_groups.items():
        if k not in spread_keys and k not in absorbed:
            final_groups[k] = parts

    # Build sorted output list
    result = []
    for k, parts in final_groups.items():
        first_num = int(k.split("-")[0])
        result.append({
            "base_id": k,
            "target": f"{k}.png",
            "parts": parts,
            "sort_key": first_num,
        })

    return sorted(result, key=lambda x: x["sort_key"])


def discover_issue_folders_recursive(root_folder):
    """Recursively finds all folders under root_folder that contain scan parts."""
    issue_folders = []
    if not os.path.isdir(root_folder):
        return issue_folders

    for dirpath, dirnames, _ in os.walk(root_folder):
        # Natural sort subdirectories (so No1, No2, ..., No10 run in numeric order)
        dirnames.sort(
            key=lambda s: [
                int(t) if t.isdigit() else t.lower()
                for t in re.split(r"(\d+)", s)
            ]
        )
        groups = discover_scan_groups(dirpath)
        if groups:
            issue_folders.append((dirpath, groups))

    return issue_folders


def trim_scanner_whitespace(img):
    """Crops away ONLY the pure-white scanner bed (gray >= 254).

    Preserves the entire physical magazine page including its blank outer paper
    margins.
    """
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape

    # Scanner bed is pure 255 white. Any pixel < 254 is physical magazine paper.
    is_paper = gray < 254

    # A column is part of the physical page if at least 20% of its height is < 254
    col_paper_ratio = np.mean(is_paper, axis=0)
    page_cols = np.where(col_paper_ratio > 0.20)[0]

    if len(page_cols) > 0:
        left = int(page_cols[0])
        right = int(page_cols[-1]) + 1
    else:
        left, right = 0, w

    row_paper_ratio = np.mean(is_paper[:, left:right], axis=1)
    page_rows = np.where(row_paper_ratio > 0.20)[0]

    if len(page_rows) > 0:
        top = int(page_rows[0])
        bottom = int(page_rows[-1]) + 1
    else:
        top, bottom = 0, h

    return img[top:bottom, left:right]


def stitch_pair_fallback(img1, img2):
    """2D rigid stitcher using SIFT + RANSAC. Returns (blended_img, valid_page_mask)."""
    gray1 = cv2.cvtColor(img1, cv2.COLOR_BGR2GRAY)
    gray2 = cv2.cvtColor(img2, cv2.COLOR_BGR2GRAY)

    sift = cv2.SIFT_create()
    kp1, des1 = sift.detectAndCompute(gray1, None)
    kp2, des2 = sift.detectAndCompute(gray2, None)

    if des1 is None or des2 is None:
        raise ValueError("Could not find features for stitching.")

    bf = cv2.BFMatcher()
    matches = bf.knnMatch(des2, des1, k=2)
    good = [m for m, n in matches if m.distance < 0.75 * n.distance]

    if len(good) < 10:
        raise ValueError(f"Not enough feature matches found ({len(good)}).")

    src_pts = np.float32([kp2[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
    dst_pts = np.float32([kp1[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)

    M, _ = cv2.estimateAffinePartial2D(
        src_pts, dst_pts, method=cv2.RANSAC, ransacReprojThreshold=3.0
    )
    if M is None:
        raise ValueError("Could not estimate affine transform between scans.")

    # Lock scale to 1.0 so both halves stay the exact same physical width
    scale = np.sqrt(M[0, 0] ** 2 + M[1, 0] ** 2)
    if 0.95 < scale < 1.05:
        M[:2, :2] /= scale

    h1, w1 = img1.shape[:2]
    h2, w2 = img2.shape[:2]

    corners1 = np.float32([[0, 0], [0, h1], [w1, h1], [w1, 0]]).reshape(
        -1, 1, 2
    )
    corners2 = np.float32([[0, 0], [0, h2], [w2, h2], [w2, 0]]).reshape(
        -1, 1, 2
    )
    corners2_trans = cv2.transform(corners2, M)

    all_corners = np.concatenate((corners1, corners2_trans), axis=0)
    [xmin, ymin] = np.int32(all_corners.min(axis=0).ravel() - 0.5)
    [xmax, ymax] = np.int32(all_corners.max(axis=0).ravel() + 0.5)

    t_dist = [-xmin, -ymin]
    M_translated = M.copy()
    M_translated[0, 2] += t_dist[0]
    M_translated[1, 2] += t_dist[1]

    out_w = xmax - xmin
    out_h = ymax - ymin

    warped2 = cv2.warpAffine(
        img2,
        M_translated,
        (out_w, out_h),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=(255, 255, 255),
    )
    mask2 = np.full((h2, w2), 255, dtype=np.uint8)
    warped_mask2 = cv2.warpAffine(
        mask2,
        M_translated,
        (out_w, out_h),
        flags=cv2.INTER_NEAREST,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=0,
    )

    canvas1 = np.full((out_h, out_w, 3), 255, dtype=np.uint8)
    canvas1[t_dist[1] : t_dist[1] + h1, t_dist[0] : t_dist[0] + w1] = img1
    warped_mask1 = np.zeros((out_h, out_w), dtype=np.uint8)
    warped_mask1[t_dist[1] : t_dist[1] + h1, t_dist[0] : t_dist[0] + w1] = 255

    m1_bin = (warped_mask1 > 0).astype(np.uint8)
    m2_bin = (warped_mask2 > 0).astype(np.uint8)

    dist1 = cv2.distanceTransform(m1_bin, cv2.DIST_L2, 5)
    dist2 = cv2.distanceTransform(m2_bin, cv2.DIST_L2, 5)

    blended = np.full((out_h, out_w, 3), 255, dtype=np.uint8)
    only1 = (m1_bin == 1) & (m2_bin == 0)
    only2 = (m2_bin == 1) & (m1_bin == 0)
    overlap = (m1_bin == 1) & (m2_bin == 1)

    blended[only1] = canvas1[only1]
    blended[only2] = warped2[only2]

    if np.any(overlap):
        d1 = dist1[overlap]
        d2 = dist2[overlap]
        denom = d1 + d2
        w1_ov = np.where(denom > 0, d1 / np.maximum(denom, 1e-6), 0.5)[
            :, None
        ]
        w2_ov = 1.0 - w1_ov
        blended[overlap] = (
            canvas1[overlap].astype(np.float32) * w1_ov
            + warped2[overlap].astype(np.float32) * w2_ov
        ).astype(np.uint8)

    combined_mask = cv2.bitwise_or(warped_mask1, warped_mask2)
    return blended, combined_mask


def stitch_images_group(image_paths):
    """Pre-trims pure-white scanner bed and stitches image parts, returning (image, mask)."""
    raw_imgs = [cv2.imread(p) for p in image_paths]
    raw_imgs = [im for im in raw_imgs if im is not None]
    if not raw_imgs:
        raise ValueError("No valid images loaded.")

    imgs = [trim_scanner_whitespace(im) for im in raw_imgs]
    if len(imgs) == 1:
        h, w = imgs[0].shape[:2]
        return imgs[0], np.full((h, w), 255, dtype=np.uint8)

    current = imgs[0]
    current_mask = np.full(current.shape[:2], 255, dtype=np.uint8)
    for next_img in imgs[1:]:
        current, current_mask = stitch_pair_fallback(current, next_img)

    return current, current_mask


def deskew_and_crop_edges(stitched_data, extra_trim_px=0):
    """1.

    Straightens page using vertical column rules and horizontal dividers.
    2. Uses the geometric page mask (not ink brightness!) to crop only the empty
    canvas outside the paper, preserving the full paper margin on all four
    sides.
    """
    if isinstance(stitched_data, tuple):
        img, page_mask = stitched_data
    else:
        img = stitched_data
        page_mask = np.full(img.shape[:2], 255, dtype=np.uint8)

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape

    # 1. Measure page skew using vertical column rules (~90 deg) and horizontal lines (~0 deg)
    margin_y, margin_x = int(h * 0.05), int(w * 0.05)
    interior = gray[margin_y : h - margin_y, margin_x : w - margin_x]

    if interior.size > 0:
        edges = cv2.Canny(interior, 50, 150, apertureSize=3)
        lines = cv2.HoughLinesP(
            edges,
            1,
            np.pi / 1800,
            threshold=80,
            minLineLength=min(w, h) // 10,
            maxLineGap=15,
        )
        angles = []
        if lines is not None:
            for line in lines:
                x1, y1, x2, y2 = line[0]
                length = np.hypot(x2 - x1, y2 - y1)
                angle = np.degrees(np.arctan2(y2 - y1, x2 - x1))
                if -5.0 < angle < 5.0:
                    angles.extend([angle] * int(max(1, length // 50)))
                elif 85.0 < abs(angle) < 95.0:
                    vert_dev = angle - 90.0 if angle > 0 else angle + 90.0
                    angles.extend([vert_dev] * int(max(1, length // 50)))

        if angles:
            median_angle = float(np.median(angles))
            if abs(median_angle) > 0.03:
                center = (w // 2, h // 2)
                M = cv2.getRotationMatrix2D(center, median_angle, 1.0)
                img = cv2.warpAffine(
                    img,
                    M,
                    (w, h),
                    flags=cv2.INTER_CUBIC,
                    borderMode=cv2.BORDER_CONSTANT,
                    borderValue=(255, 255, 255),
                )
                page_mask = cv2.warpAffine(
                    page_mask,
                    M,
                    (w, h),
                    flags=cv2.INTER_NEAREST,
                    borderMode=cv2.BORDER_CONSTANT,
                    borderValue=0,
                )

    # 2. Crop using the geometric page_mask (where the physical paper actually exists),
    # NOT pixel darkness (which previously mistook blank paper margins for scanner bed).
    col_mask_ratio = np.mean(page_mask > 0, axis=0)
    valid_cols = np.where(col_mask_ratio >= 0.90)[0]

    if len(valid_cols) > 0:
        left = min(w - 1, int(valid_cols[0]) + extra_trim_px)
        right = max(left + 1, int(valid_cols[-1]) + 1 - extra_trim_px)
    else:
        left, right = 0, w

    row_mask_ratio = np.mean(page_mask[:, left:right] > 0, axis=1)
    valid_rows = np.where(row_mask_ratio >= 0.90)[0]

    if len(valid_rows) > 0:
        top = max(0, int(valid_rows[0]))
        bottom = min(h, int(valid_rows[-1]) + 1)
    else:
        top, bottom = 0, h

    return img[top:bottom, left:right]

def _stitch_single_group_worker(folder_path, g, extra_trim):
    """Worker to stitch, deskew, and save a single page group in parallel."""
    target_path = os.path.join(folder_path, g["target"])
    part_paths = [os.path.join(folder_path, p) for p in g["parts"]]
    raw_stitched = stitch_images_group(part_paths)
    final_img = deskew_and_crop_edges(raw_stitched, extra_trim_px=extra_trim)
    cv2.imwrite(target_path, final_img)
    return (
        g["target"],
        target_path,
        (final_img.shape[1], final_img.shape[0]),
    )

def _ocr_single_page_worker(idx, img_path, jpeg_quality):
    """Worker to JPEG-compress and run Tesseract OCR on a single page in parallel."""
    os.environ["OMP_THREAD_LIMIT"] = "1"
    pil_img = Image.open(img_path).convert("RGB")
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        tmp_jpg_path = tmp.name

    try:
        pil_img.save(
            tmp_jpg_path,
            format="JPEG",
            quality=jpeg_quality,
            optimize=True,
            dpi=(300, 300),
        )
        pdf_bytes = pytesseract.image_to_pdf_or_hocr(
            tmp_jpg_path, extension="pdf", config="--oem 1"
        )
        return idx, pdf_bytes
    finally:
        if os.path.exists(tmp_jpg_path):
            os.remove(tmp_jpg_path)


def build_ocr_optimized_pdf(
    image_paths,
    pdf_out_path,
    jpeg_quality=85,
    status_callback=None,
    progress_callback=None,
):
    """Runs JPEG compression + Tesseract OCR on all pages in parallel across physical CPU cores,

    then merges them in page order into `pdf_out_path`.
    """
    total_pages = len(image_paths)
    max_workers = min(total_pages, max(1, (os.cpu_count() or 8) // 2))
    if status_callback:
        status_callback(
            f"🚀 Running OCR & optimization on {total_pages} pages in parallel ({max_workers} workers)..."
        )
    if progress_callback:
        progress_callback(0.0)

    results = {}
    completed = 0
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                _ocr_single_page_worker, idx, img_path, jpeg_quality
            ): (idx, img_path)
            for idx, img_path in enumerate(image_paths)
        }
        for future in as_completed(futures):
            idx, img_path = futures[future]
            page_idx, pdf_bytes = future.result()
            results[page_idx] = pdf_bytes
            completed += 1
            if status_callback:
                status_callback(
                    f"🔍 OCR & optimizing pages ({completed}/{total_pages} finished — latest: `{os.path.basename(img_path)}`)..."
                )
            if progress_callback:
                progress_callback(completed / total_pages)

    if status_callback:
        status_callback("📦 Merging OCR'd pages and writing final PDF...")

    doc = fitz.open()
    for idx in range(total_pages):
        page_doc = fitz.open("pdf", results[idx])
        doc.insert_pdf(page_doc)
        page_doc.close()

    doc.save(pdf_out_path, garbage=4, deflate=True)
    doc.close()

# ==============================================================================
# UI & MAIN WORKFLOW
# ==============================================================================
st.title("🧩 Magazine Scan Stitcher & PDF Builder")

mode = st.radio(
    "Select Workflow Mode",
    [
        "🔍 Single Issue Mode (Interactive Review)",
        "🤖 Automatic Batch Mode (Recursive)",
    ],
    horizontal=True,
)

default_dir = (
    "/media/sarah/4TB/Projects/Bahai.works/English/American_Bahai/Vol34/No1/"
    if "Single" in mode
    else "/media/sarah/4TB/Projects/Bahai.works/English/American_Bahai/Vol34/"
)
input_dir = st.text_input("Input Directory", value=default_dir)

if not os.path.isdir(input_dir):
    st.warning("Please enter a valid directory path.")
    st.stop()

cols_cfg = st.columns([1, 1, 1])
with cols_cfg[0]:
    extra_trim = st.number_input(
        "Auto-Crop Edge Trim (px)", min_value=0, max_value=200, value=15, step=5
    )
with cols_cfg[1]:
    jpeg_quality = st.slider(
        "PDF JPEG Compression Quality",
        min_value=60,
        max_value=95,
        value=85,
        step=5,
    )
with cols_cfg[2]:
    overwrite_existing = st.checkbox(
        "Re-stitch even if target `.png` exists", value=False
    )

# ==============================================================================
# MODE 1: AUTOMATIC BATCH MODE (RECURSIVE)
# ==============================================================================
if "Automatic" in mode:
    issue_folders = discover_issue_folders_recursive(input_dir)

    if not issue_folders:
        st.info(f"No folders containing scan parts found under `{input_dir}`.")
        st.stop()

    st.subheader(f"📂 Discovered Issue Folders ({len(issue_folders)})")
    with st.expander("View Queued Issue Folders", expanded=True):
        for folder_path, f_groups in issue_folders:
            inferred_pdf = infer_pdf_filename(folder_path)
            pdf_exists = os.path.exists(os.path.join(folder_path, inferred_pdf))
            badge = "✅ PDF Exists" if pdf_exists else "⏳ Pending"
            st.markdown(
                f"- **`{folder_path}`** → `{inferred_pdf}` ({len(f_groups)} pages) [{badge}]"
            )

    skip_existing_pdfs = st.checkbox(
        "Skip folders where target `.pdf` already exists", value=True
    )

    if st.button(
        "🚀 Run Automatic Stitch + OCR PDF on All Folders", type="primary"
    ):
        overall_progress = st.progress(0)
        status_line = st.empty()
        log_box = st.container(border=True)

        for f_idx, (folder_path, f_groups) in enumerate(issue_folders):
            pdf_name = infer_pdf_filename(folder_path)
            pdf_out_path = os.path.join(folder_path, pdf_name)

            if os.path.exists(pdf_out_path) and skip_existing_pdfs:
                log_box.info(
                    f"⏭️ Skipping `{folder_path}` (`{pdf_name}` already exists)."
                )
                overall_progress.progress((f_idx + 1) / len(issue_folders))
                continue

            log_box.markdown(
                f"### 📂 Processing `{folder_path}` → `{pdf_name}`"
            )
            groups_to_stitch = [
                g
                for g in f_groups
                if overwrite_existing
                or not os.path.exists(os.path.join(folder_path, g["target"]))
            ]

            if groups_to_stitch:
                max_workers = min(len(groups_to_stitch), os.cpu_count() or 8)
                done_count = 0
                status_line.markdown(
                    f"**[{f_idx + 1}/{len(issue_folders)}] `{os.path.basename(folder_path)}`: Stitching {len(groups_to_stitch)} pages in parallel ({max_workers} workers)...**"
                )
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    futures = {
                        executor.submit(
                            _stitch_single_group_worker,
                            folder_path,
                            g,
                            extra_trim,
                        ): g
                        for g in groups_to_stitch
                    }
                    for future in as_completed(futures):
                        g = futures[future]
                        done_count += 1
                        try:
                            future.result()
                            status_line.markdown(
                                f"**[{f_idx + 1}/{len(issue_folders)}] `{os.path.basename(folder_path)}`: Stitched {done_count}/{len(groups_to_stitch)} pages...**"
                            )
                        except Exception as e:
                            log_box.error(
                                f"❌ Failed to stitch `{g['target']}` in `{folder_path}`: {e}"
                            )

            # Collect all valid stitched page paths in sorted order
            stitched_paths = [
                os.path.join(folder_path, g["target"])
                for g in f_groups
                if os.path.exists(os.path.join(folder_path, g["target"]))
            ]

            if stitched_paths:
                try:
                    build_ocr_optimized_pdf(
                        stitched_paths,
                        pdf_out_path,
                        jpeg_quality=jpeg_quality,
                        status_callback=lambda msg: status_line.markdown(
                            f"**[{f_idx + 1}/{len(issue_folders)}] `{os.path.basename(folder_path)}`: {msg}**"
                        ),
                    )
                    size_mb = os.path.getsize(pdf_out_path) / (1024 * 1024)
                    log_box.success(
                        f"✅ Created OCR'd PDF `{pdf_out_path}` ({len(stitched_paths)} pages, {size_mb:.1f} MB)"
                    )
                except Exception as e:
                    log_box.error(
                        f"❌ Failed to build PDF for `{folder_path}`: {e}"
                    )

            overall_progress.progress((f_idx + 1) / len(issue_folders))

        status_line.success("🎉 Automatic batch processing complete!")

# ==============================================================================
# MODE 2: SINGLE ISSUE MODE (INTERACTIVE REVIEW)
# ==============================================================================
else:
    groups = discover_scan_groups(input_dir)

    if not groups:
        st.info(
            "No scan parts (e.g., `1a.png`, `1b.png`, `5-6a.png`) found in this directory."
        )
        st.stop()

    st.subheader(f"📋 Discovered Page Groups ({len(groups)})")

    default_pdf_name = infer_pdf_filename(input_dir)
    pdf_filename = st.text_input("Output PDF Filename", value=default_pdf_name)

    with st.expander("View Detected Scan Parts Mapping", expanded=True):
        for g in groups:
            exists_badge = (
                "✅ Exists"
                if os.path.exists(os.path.join(input_dir, g["target"]))
                else "⏳ Pending"
            )
            st.markdown(
                f"- **`{g['target']}`** ({exists_badge}) ← `{', '.join(g['parts'])}`"
            )

    btn_col1, btn_col2, btn_col3 = st.columns([1, 1, 2])
    with btn_col1:
        stitch_all_clicked = st.button(
            "🚀 Stitch All Pages", type="primary", use_container_width=True
        )
    with btn_col2:
        stitch_one_clicked = st.button(
            "🧪 Stitch One Page (Test)",
            type="secondary",
            use_container_width=True,
        )
    with btn_col3:
        test_target_group = st.selectbox(
            "Select Page for Test Stitch",
            options=groups,
            format_func=lambda g: f"{g['target']} ({', '.join(g['parts'])})",
            label_visibility="collapsed",
        )

    if stitch_one_clicked and test_target_group:
        log_box = st.container(border=True)
        try:
            log_box.write(
                f"🔄 Test-stitching `{test_target_group['target']}` from `{', '.join(test_target_group['parts'])}`..."
            )
            t_name, _, (w, h) = _stitch_single_group_worker(
                input_dir, test_target_group, extra_trim
            )
            log_box.success(f"✅ Saved `{t_name}` ({w}x{h} px)")
            st.rerun()
        except Exception as e:
            log_box.error(
                f"❌ Failed to stitch `{test_target_group['target']}`: {e}"
            )

    if stitch_all_clicked:
        progress = st.progress(0)
        log_box = st.container(border=True)
        groups_to_stitch = [
            g
            for g in groups
            if overwrite_existing
            or not os.path.exists(os.path.join(input_dir, g["target"]))
        ]

        if not groups_to_stitch:
            log_box.info(
                "⏭️ All target `.png` files already exist. Check 'Re-stitch' to overwrite."
            )
        else:
            max_workers = min(len(groups_to_stitch), os.cpu_count() or 8)
            log_box.write(
                f"🚀 Stitching {len(groups_to_stitch)} pages in parallel across {max_workers} workers..."
            )
            done_count = 0
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {
                    executor.submit(
                        _stitch_single_group_worker, input_dir, g, extra_trim
                    ): g
                    for g in groups_to_stitch
                }
                for future in as_completed(futures):
                    g = futures[future]
                    done_count += 1
                    try:
                        t_name, _, (w, h) = future.result()
                        log_box.success(f"✅ Saved `{t_name}` ({w}x{h} px)")
                    except Exception as e:
                        log_box.error(f"❌ Failed to stitch `{g['target']}`: {e}")
                    progress.progress(done_count / len(groups_to_stitch))

        st.rerun()

    # ==============================================================================
    # HUMAN REVIEW & MANUAL CROP ADJUSTMENT
    # ==============================================================================
    existing_targets = [
        g for g in groups if os.path.exists(os.path.join(input_dir, g["target"]))
    ]

    if existing_targets:
        st.divider()
        st.subheader("👁️ Review Stitched Pages & Build PDF")

        review_cols = st.columns(3)
        for idx, g in enumerate(existing_targets):
            t_name = g["target"]
            t_path = os.path.join(input_dir, t_name)
            col = review_cols[idx % 3]

            with col:
                with st.container(border=True):
                    pil_img = Image.open(t_path)
                    w, h = pil_img.size
                    st.markdown(f"**`{t_name}`** ({w} × {h} px)")
                    st.image(pil_img, use_container_width=True)

                    with st.expander(f"✂️ Manual Trim `{t_name}`"):
                        t_top = st.number_input(
                            "Trim Top (px)",
                            0,
                            h // 2,
                            0,
                            step=10,
                            key=f"tt_{t_name}",
                        )
                        t_bot = st.number_input(
                            "Trim Bottom (px)",
                            0,
                            h // 2,
                            0,
                            step=10,
                            key=f"tb_{t_name}",
                        )
                        t_left = st.number_input(
                            "Trim Left (px)",
                            0,
                            w // 2,
                            0,
                            step=10,
                            key=f"tl_{t_name}",
                        )
                        t_right = st.number_input(
                            "Trim Right (px)",
                            0,
                            w // 2,
                            0,
                            step=10,
                            key=f"tr_{t_name}",
                        )
                        rot_deg = st.number_input(
                            "Fine Rotate (°)",
                            -5.0,
                            5.0,
                            0.0,
                            step=0.1,
                            key=f"rot_{t_name}",
                        )

                        if st.button(
                            "💾 Apply Adjustment", key=f"apply_{t_name}"
                        ):
                            img_cv = cv2.imread(t_path)
                            if abs(rot_deg) > 0.01:
                                ch, cw = img_cv.shape[:2]
                                M = cv2.getRotationMatrix2D(
                                    (cw // 2, ch // 2), rot_deg, 1.0
                                )
                                img_cv = cv2.warpAffine(
                                    img_cv,
                                    M,
                                    (cw, ch),
                                    flags=cv2.INTER_CUBIC,
                                    borderValue=(255, 255, 255),
                                )
                            ch, cw = img_cv.shape[:2]
                            cropped = img_cv[
                                t_top : ch - t_bot, t_left : cw - t_right
                            ]
                            cv2.imwrite(t_path, cropped)
                            st.success(f"Updated `{t_name}`!")
                            st.rerun()

        st.divider()
        if st.button("📕 Combine All Pages into OCR'd PDF", type="primary"):
            pdf_out_path = os.path.join(input_dir, pdf_filename)
            pdf_progress = st.progress(0.0)
            status_line = st.empty()
            try:
                img_paths = [
                    os.path.join(input_dir, g["target"])
                    for g in existing_targets
                ]
                build_ocr_optimized_pdf(
                    img_paths,
                    pdf_out_path,
                    jpeg_quality=jpeg_quality,
                    status_callback=lambda msg: status_line.info(msg),
                    progress_callback=lambda frac: pdf_progress.progress(frac),
                )
                size_mb = os.path.getsize(pdf_out_path) / (1024 * 1024)
                status_line.success(
                    f"🎉 Created OCR'd & optimized PDF with {len(existing_targets)} page(s) ({size_mb:.1f} MB): `{pdf_out_path}`"
                )
            except Exception as e:
                status_line.error(f"❌ Failed to build PDF: {e}")
