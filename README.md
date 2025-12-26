# Bahai.works Digitization Dashboard

A comprehensive local Python application for managing the end-to-end digitization workflow for Bahai.works. This tool orchestrates file scanning, AI-driven prioritization, OCR processing, image extraction, and direct publishing to MediaWiki and Wikibase.

## Features

The application is structured into a main dashboard and several specialized workflow pages:

* **📊 Main Dashboard (`app.py`):** Central command for sorting, searching, and managing the document queue. View status (Pending, Digitized, Completed) and launch file system actions.
* **🤖 AI Analyst:** Uses Google Gemini to analyze PDF content, detect language, generate summaries, and assign priority scores (1-10) based on historical value.
* **🏭 OCR Assembly Line:** A robust pipeline to:
    * **Merge:** Detect and combine separated Cover/Content PDF pairs.
    * **Prep:** Detect double-page spreads and split them automatically.
    * **Execute:** Run OCR (Tesseract/PyMuPDF) and extract text/images.
* **🚀 Publication Pipeline:** Automates the creation of MediaWiki pages. Handles copyright headers, TOC extraction, and uploading text directly to Bahai.works.
* **🖼️ Image Import:** An interactive cropper to process illustrations extracted during OCR and upload them with captions.
* **📑 Chapter Manager:** Tools to define and link specific chapters to their authors in Wikibase (Bahaidata).
* **🛠️ Utilities:** Batch creation of Author pages, copyright sub-pages (`/AC-Message`), and system maintenance tasks.

## Requirements

* **Python 3.10+**
* **Google Gemini API Key**
* **Tesseract OCR Engine:**
    * *Linux:* `sudo apt install tesseract-ocr`
    * *Windows:* Download and install the [Tesseract binary](https://github.com/UB-Mannheim/tesseract/wiki). **Important:** You must add Tesseract to your system PATH environment variable during installation.
* **Operating System:** Cross-platform (Windows, Linux, MacOS).
    * *Note: Specific features like "Open Folder" are optimized for Linux (KDE Dolphin).*

## Installation

1.  **Clone the repository:**
    ```bash
    git clone [https://github.com/shaslip/bahaiworks-dashboard.git](https://github.com/shaslip/bahaiworks-dashboard.git) dashboard
    cd dashboard
    ```

2.  **Install Python dependencies:**
    ```bash
    pip install -r requirements.txt
    ```

3.  **Configure secrets:**
    Create a `.env` file in the root directory:
    ```env
    GEMINI_API_KEY=
    WIKI_USERNAME=
    WIKI_PASSWORD=
    ```

## Usage

### 1. Initialize Database
Scan your local directories to populate the SQLite database with new PDF files.

```bash
python -m src.crawler

```

### 2. Launch the Dashboard

Start the web interface. This acts as the central hub for all workflows.

```bash
streamlit run app.py

```

### 3. Standard Workflow

Once the app is running, navigate using the sidebar to move a document through the pipeline:

1. **AI Analyst:** Select "Pending" files to generate summaries and priority scores.
2. **OCR Pipeline:**
* *Merge* covers and content.
* *Prep* by splitting double pages and setting page offsets.
* *Execute* batch OCR processing.


3. **Publication Pipeline:** Select a digitized document to extract metadata/TOC and upload to Bahai.works.
4. **Post-Processing:** Use **Image Import** for illustrations or **Chapter Manager** to link specific sections to authors.

## Project Structure

```text
dashboard/
├── app.py                       # Main Dashboard (Queue view)
├── batch_process.py             # Headless script for bulk AI analysis
├── pages/                       # Streamlit multi-page workflows
│   ├── 01_ai_analysis.py        # AI scoring & summary generation
│   ├── 02_ocr_pipeline.py       # Merge, Split, & OCR execution
│   ├── 03_publication_pipeline.py # MediaWiki upload & text parsing
│   ├── 04_image_import.py       # Illustration cropping & processing
│   ├── 05_chapter_items.py      # Wikibase chapter item management
│   └── 06_misc_tasks.py         # Author creation & system maintenance
├── src/                         # Core logic modules
│   ├── config.py                # Paths and settings
│   ├── crawler.py               # File scanner
│   ├── database.py              # SQLAlchemy schema
│   ├── evaluator.py             # Gemini API integration
│   ├── ocr_engine.py            # OCR logic & image generation
│   ├── processor.py             # PDF manipulation (PyMuPDF)
│   ├── wikibase_importer.py     # Bahaidata API hooks
│   ├── mediawiki_uploader.py    # Bahai.works API hooks
│   └── ...
├── data/
│   └── files.db                 # SQLite database
└── requirements.txt

```

## Dependencies

Major libraries used:

* `streamlit` & `streamlit-cropper` (UI)
* `google-generativeai` (LLM analysis)
* `sqlalchemy` (Database ORM)
* `pymupdf` (PDF processing)
* `wikibaseintegrator` (Bahaidata sync)
* `pandas` (Data manipulation)

```
