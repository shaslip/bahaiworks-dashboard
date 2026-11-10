from io import BytesIO
import os
import re
import sys
import cv2
import fitz
import numpy as np
from PIL import Image
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
    return "Stitched_Output.pdf"


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


def stitch_pair_fallback(img1, img2):
    """Fallback 2D affine stitcher using SIFT + RANSAC when OpenCV Stitcher_SCANS fails."""
    gray1 = cv2.cvtColor(img1, cv2.COLOR_BGR2GRAY)
    gray2 = cv2.cvtColor(img2, cv2.COLOR_BGR2GRAY)

    sift = cv2.SIFT_create()
    kp1, des1 = sift.detectAndCompute(gray1, None)
    kp2, des2 = sift.detectAndCompute(gray2, None)

    if des1 is None or des2 is None:
        raise ValueError("Could not find features for fallback stitching.")

    bf = cv2.BFMatcher()
    matches = bf.knnMatch(des2, des1, k=2)  # Transform img2 into img1's space
    good = [m for m, n in matches if m.distance < 0.75 * n.distance]

    if len(good) < 10:
        raise ValueError(f"Not enough feature matches found ({len(good)}).")

    src_pts = np.float32([kp2[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
    dst_pts = np.float32([kp1[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)

    M, _ = cv2.estimateAffinePartial2D(
        src_pts, dst_pts, method=cv2.RANSAC, ransacReprojThreshold=5.0
    )
    if M is None:
        raise ValueError("Could not estimate affine transform between scans.")

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
        borderValue=(0, 0, 0),
    )
    canvas1 = np.zeros((out_h, out_w, 3), dtype=np.uint8)
    canvas1[t_dist[1] : t_dist[1] + h1, t_dist[0] : t_dist[0] + w1] = img1

    mask1 = (cv2.cvtColor(canvas1, cv2.COLOR_BGR2GRAY) > 0).astype(np.float32)
    mask2 = (cv2.cvtColor(warped2, cv2.COLOR_BGR2GRAY) > 0).astype(np.float32)

    # Distance transform feathering across the overlap region
    dist1 = cv2.distanceTransform((mask1 > 0).astype(np.uint8), cv2.DIST_L2, 5)
    dist2 = cv2.distanceTransform((mask2 > 0).astype(np.uint8), cv2.DIST_L2, 5)
    total_dist = dist1 + dist2 + 1e-6

    w_map1 = (dist1 / total_dist)[..., None]
    w_map2 = (dist2 / total_dist)[..., None]

    blended = (
        canvas1.astype(np.float32) * w_map1
        + warped2.astype(np.float32) * w_map2
    ).astype(np.uint8)
    return blended


def stitch_images_group(image_paths):
    """Stitches a list of image paths using OpenCV Stitcher_SCANS with pairwise fallback."""
    imgs = [cv2.imread(p) for p in image_paths]
    imgs = [im for im in imgs if im is not None]
    if not imgs:
        raise ValueError("No valid images loaded.")
    if len(imgs) == 1:
        return imgs[0]

    stitcher = cv2.Stitcher_create(cv2.Stitcher_SCANS)
    status, stitched = stitcher.stitch(imgs)
    if status == cv2.Stitcher_OK:
        return stitched

    # Fallback: Sequential pairwise affine stitching
    current = imgs[0]
    for next_img in imgs[1:]:
        status_pair, res_pair = stitcher.stitch([current, next_img])
        if status_pair == cv2.Stitcher_OK:
            current = res_pair
        else:
            current = stitch_pair_fallback(current, next_img)
    return current


def deskew_and_crop_edges(img, extra_trim_px=10):
    """1.

    Crops outer black bounding box.
    2. Detects text/line skew angle and straightens page.
    3. Crops away black rotation wedges along the outer edges.
    """
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    _, valid_mask = cv2.threshold(gray, 5, 255, cv2.THRESH_BINARY)

    coords = cv2.findNonZero(valid_mask)
    if coords is not None:
        x, y, w, h = cv2.boundingRect(coords)
        img = img[y : y + h, x : x + w]
        gray = gray[y : y + h, x : x + w]
        valid_mask = valid_mask[y : y + h, x : x + w]

    # Estimate skew angle using Hough lines on text/layout features inside the interior
    h, w = gray.shape
    margin_y, margin_x = int(h * 0.1), int(w * 0.1)
    interior = gray[margin_y : h - margin_y, margin_x : w - margin_x]

    if interior.size > 0:
        edges = cv2.Canny(interior, 50, 150, apertureSize=3)
        lines = cv2.HoughLinesP(
            edges,
            1,
            np.pi / 180,
            threshold=100,
            minLineLength=w // 8,
            maxLineGap=20,
        )
        angles = []
        if lines is not None:
            for line in lines:
                x1, y1, x2, y2 = line[0]
                angle = np.degrees(np.arctan2(y2 - y1, x2 - x1))
                if -5.0 < angle < 5.0:
                    angles.append(angle)

        if angles:
            median_angle = float(np.median(angles))
            if abs(median_angle) > 0.05:
                center = (w // 2, h // 2)
                M = cv2.getRotationMatrix2D(center, median_angle, 1.0)
                img = cv2.warpAffine(
                    img,
                    M,
                    (w, h),
                    flags=cv2.INTER_CUBIC,
                    borderMode=cv2.BORDER_CONSTANT,
                    borderValue=(0, 0, 0),
                )
                valid_mask = cv2.warpAffine(
                    valid_mask,
                    M,
                    (w, h),
                    flags=cv2.INTER_NEAREST,
                    borderMode=cv2.BORDER_CONSTANT,
                    borderValue=0,
                )

    # Trim black wedges along top, bottom, left, right until >=99% of the row/col is valid scan content
    h, w = valid_mask.shape
    row_Ratio = np.mean(valid_mask > 0, axis=1)
    col_Ratio = np.mean(valid_mask > 0, axis=0)

    valid_rows = np.where(row_Ratio >= 0.98)[0]
    valid_cols = np.where(col_Ratio >= 0.98)[0]

    if len(valid_rows) > 0 and len(valid_cols) > 0:
        top = min(h - 1, valid_rows[0] + extra_trim_px)
        bottom = max(top + 1, valid_rows[-1] - extra_trim_px)
        left = min(w - 1, valid_cols[0] + extra_trim_px)
        right = max(left + 1, valid_cols[-1] - extra_trim_px)
        img = img[top:bottom, left:right]

    return img


# ==============================================================================
# UI & MAIN WORKFLOW
# ==============================================================================
st.title("🧩 Magazine Scan Stitcher & PDF Builder")

default_dir = (
    "/media/sarah/4TB/Projects/Bahai.works/English/American_Bahai/Vol34/No1/"
)
input_dir = st.text_input("Input Issue Directory", value=default_dir)

if not os.path.isdir(input_dir):
    st.warning("Please enter a valid directory path.")
    st.stop()

groups = discover_scan_groups(input_dir)

if not groups:
    st.info(
        "No scan parts (e.g., `1a.png`, `1b.png`, `5-6a.png`) found in this directory."
    )
    st.stop()

st.subheader(f"📋 Discovered Page Groups ({len(groups)})")

cols_cfg = st.columns([2, 1, 1])
with cols_cfg[0]:
    default_pdf_name = infer_pdf_filename(input_dir)
    pdf_filename = st.text_input("Output PDF Filename", value=default_pdf_name)
with cols_cfg[1]:
    extra_trim = st.number_input(
        "Auto-Crop Edge Trim (px)", min_value=0, max_value=200, value=15, step=5
    )
with cols_cfg[2]:
    overwrite_existing = st.checkbox(
        "Re-stitch even if target `.png` exists", value=False
    )

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

if st.button("🚀 Stitch All Pages", type="primary"):
    progress = st.progress(0)
    log_box = st.container(border=True)

    for idx, g in enumerate(groups):
        target_path = os.path.join(input_dir, g["target"])
        if os.path.exists(target_path) and not overwrite_existing:
            log_box.info(
                f"⏭️ `{g['target']}` already exists. Skipping (check 'Re-stitch' to overwrite)."
            )
            progress.progress((idx + 1) / len(groups))
            continue

        part_paths = [os.path.join(input_dir, p) for p in g["parts"]]
        try:
            log_box.write(
                f"🔄 Stitching `{g['target']}` from `{', '.join(g['parts'])}`..."
            )
            raw_stitched = stitch_images_group(part_paths)
            final_img = deskew_and_crop_edges(
                raw_stitched, extra_trim_px=extra_trim
            )
            cv2.imwrite(target_path, final_img)
            log_box.success(
                f"✅ Saved `{g['target']}` ({final_img.shape[1]}x{final_img.shape[0]} px)"
            )
        except Exception as e:
            log_box.error(f"❌ Failed to stitch `{g['target']}`: {e}")

        progress.progress((idx + 1) / len(groups))

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

                    if st.button("💾 Apply Adjustment", key=f"apply_{t_name}"):
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
    if st.button("📕 Combine All Pages into PDF", type="primary"):
        pdf_out_path = os.path.join(input_dir, pdf_filename)
        try:
            doc = fitz.open()
            for g in existing_targets:
                img_path = os.path.join(input_dir, g["target"])
                img_doc = fitz.open(img_path)
                pdf_bytes = img_doc.convert_to_pdf()
                img_pdf = fitz.open("pdf", pdf_bytes)
                doc.insert_pdf(img_pdf)
                img_doc.close()
                img_pdf.close()

            doc.save(pdf_out_path)
            doc.close()
            st.success(
                f"🎉 Created PDF with {len(existing_targets)} page(s): `{pdf_out_path}`"
            )
        except Exception as e:
            st.error(f"❌ Failed to build PDF: {e}")
