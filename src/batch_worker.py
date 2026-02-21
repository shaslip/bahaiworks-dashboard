import os
import logging

# 1. Force Streamlit's internal config to only log true errors in the background
os.environ["STREAMLIT_LOGGER_LEVEL"] = "error"

# 2. Import Streamlit FIRST so it builds its default loggers, then permanently disable the noisy one
import streamlit as st
noisy_logger = logging.getLogger("streamlit.runtime.scriptrunner_utils.script_run_context")
noisy_logger.setLevel(logging.ERROR)
noisy_logger.disabled = True

# 3. Now do the rest of the imports safely
import json
import io
import time
import gc
import fitz  # PyMuPDF
from PIL import Image
from src.gemini_processor import proofread_with_formatting, transcribe_with_document_ai, reformat_raw_text

def mute_streamlit_in_worker():
    """Runs the exact second a background process wakes up to permanently kill the Streamlit warning"""
    import logging
    import os
    
    os.environ["STREAMLIT_LOGGER_LEVEL"] = "error"
    
    class MuteScriptRunContext(logging.Filter):
        def filter(self, record):
            # Drop the log record completely if it contains the annoying string
            return "missing ScriptRunContext" not in record.getMessage()

    # 1. Target the specific Streamlit loggers
    loggers_to_mute = [
        "streamlit.runtime.scriptrunner_utils.script_run_context",
        "streamlit",
        "streamlit.runtime"
    ]
    
    for logger_name in loggers_to_mute:
        logger = logging.getLogger(logger_name)
        logger.setLevel(logging.ERROR)
        logger.disabled = True
        logger.propagate = False
        logger.addFilter(MuteScriptRunContext())

    # 2. Add the filter to the root logger and all its active handlers to catch leaks
    root_logger = logging.getLogger()
    root_logger.addFilter(MuteScriptRunContext())
    for handler in root_logger.handlers:
        handler.addFilter(MuteScriptRunContext())

def get_page_image_data(pdf_path, page_num_1_based):
    doc = fitz.open(pdf_path)
    if page_num_1_based > len(doc):
        doc.close()
        return None  
    
    page = doc.load_page(page_num_1_based - 1)
    pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
    img = Image.open(io.BytesIO(pix.tobytes("png")))
    doc.close()
    return img

def process_pdf_batch(batch_id, page_list, pdf_path, ocr_strategy, short_name, project_root, shared_log_list):
    gemini_consecutive_failures = 0
    docai_cooldown_pages = 0
    permanent_docai = False
    
    batch_file_path = os.path.join(project_root, f"temp_{short_name}_batch_{batch_id}.json")
    batch_results = {}
    
    if os.path.exists(batch_file_path):
        try:
            with open(batch_file_path, "r", encoding="utf-8") as f:
                saved_data = json.load(f)
                batch_results = {int(k): v for k, v in saved_data.items()}
        except json.JSONDecodeError:
            pass

    for page_num in page_list:
        if page_num in batch_results and batch_results[page_num] and "ERROR" not in batch_results[page_num]:
            shared_log_list.append(f"⏩ Skipping Page {page_num} (Already processed)")
            continue

        img = get_page_image_data(pdf_path, page_num)
        if img is None:
            continue
            
        final_text = ""
        force_docai = (ocr_strategy == "DocAI Only") or permanent_docai or (docai_cooldown_pages > 0)

        if force_docai:
            mode_label = "Permanent DocAI" if permanent_docai else f"Cooldown DocAI ({docai_cooldown_pages} left)"
            shared_log_list.append(f"🤖 [{mode_label}] Processing Page {page_num}...")
            
            raw_ocr = transcribe_with_document_ai(img)
            if "DOCAI_ERROR" in raw_ocr:
                shared_log_list.append(f"⚠️ DocAI Failed. Attempting Gemini Rescue...")
                final_text = proofread_with_formatting(img)
            else:
                final_text = reformat_raw_text(raw_ocr)
                if "FORMATTING_ERROR" in final_text:
                    shared_log_list.append(f"⚠️ DocAI Formatting Failed. Attempting Gemini Rescue...")
                    rescue_text = proofread_with_formatting(img)
                    if "GEMINI_ERROR" in rescue_text or "Recitation" in rescue_text:
                        shared_log_list.append(f"⚠️ Rescue also failed. Saving RAW OCR text.")
                        final_text = raw_ocr + "\n\n"
                    else:
                        final_text = rescue_text

        if docai_cooldown_pages > 0:
            docai_cooldown_pages -= 1
            if docai_cooldown_pages == 0:
                shared_log_list.append(f"🟢 Cooldown complete. Re-enabling Gemini.")

        else:
            final_text = proofread_with_formatting(img)
            is_gemini_error = "GEMINI_ERROR" in final_text or "Recitation" in final_text or "Copyright" in final_text

            if is_gemini_error:
                gemini_consecutive_failures += 1
                if gemini_consecutive_failures == 2:
                    docai_cooldown_pages = 5
                    shared_log_list.append(f"⚠️ 2 Consecutive Failures. Switching to DocAI for next 5 pages.")
                elif gemini_consecutive_failures >= 3:
                    permanent_docai = True
                    shared_log_list.append(f"⛔ 3rd Strike. Switching to DocAI for remainder of batch.")
                else:
                    shared_log_list.append(f"⚠️ Gemini Error ({gemini_consecutive_failures}/2). Retrying with DocAI...")

                raw_ocr = transcribe_with_document_ai(img)
                if "DOCAI_ERROR" in raw_ocr:
                    final_text = "DOCAI_ERROR" 
                else:
                    formatted_text = reformat_raw_text(raw_ocr)
                    if "FORMATTING_ERROR" in formatted_text:
                        shared_log_list.append(f"⚠️ Formatting failed. Saving RAW OCR text.")
                        final_text = raw_ocr + "\n\n"
                    else:
                        final_text = formatted_text
            else:
                gemini_consecutive_failures = 0

        system_error_flags = ["GEMINI_ERROR", "DOCAI_ERROR", "FORMATTING_ERROR"]
        if not final_text or any(flag in final_text for flag in system_error_flags):
            error_summary = final_text if final_text else "Empty Response"
            shared_log_list.append(f"❌ SKIPPING Page {page_num} due to failure: {error_summary}")
            batch_results[page_num] = "" 
        else:
            batch_results[page_num] = final_text
            shared_log_list.append(f"✅ Saved Page {page_num} locally")

        with open(batch_file_path, "w", encoding="utf-8") as f:
            json.dump(batch_results, f)

    return True
