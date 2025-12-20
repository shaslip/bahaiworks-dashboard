# Bahai.works Digitization Dashboard

A local Python application for scanning, prioritizing, and managing a large queue of PDF documents using Google Gemini AI.

## Features

* **Crawler:** Recursively scans local directories for PDF files and tracks them in a SQLite database.
* **AI Analysis:** Uses Google Gemini (3-Flash) to generate summaries, detect language, and assign priority scores (1-10) based on historical value.
* **Dashboard:** Streamlit interface for sorting, searching, and manually reviewing documents.
* **Local Integration:** Direct file system hooks to open documents or containing folders (optimized for Linux/KDE Dolphin).

## Requirements

* Python 3.10+
* Google Gemini API Key
* Linux (Recommended for file system integration features)

## Installation

1. **Clone the repository:**
```bash
git clone <repository-url>
cd dashboard

```


2. **Install dependencies:**
```bash
pip install -r requirements.txt

```


3. **Configure API Key:**
Create a `.env` file in the root directory:
```env
GEMINI_API_KEY=your_actual_api_key_here

```



## Usage

### 1. Initialize Database

Scan your local directories to populate the database.

```bash
python -m src.crawler

```

*Note: Ensure source directories are defined in `src/config.py`.*

### 2. Run the Dashboard

Launch the interactive web interface.

```bash
streamlit run app.py

```

* **View:** Sorts by Priority (Highest first), then Filename (A-Z).
* **Action:** Click a row to view details. Sidebar actions (Open File/Folder) do not reload the table state.
* **Override:** AI scores can be manually adjusted in the sidebar.

### 3. Batch Processing (Optional)

Process all "Pending" documents in the background without the UI.

```bash
python batch_process.py

```

## Project Structure

```text
dashboard/
├── app.py                 # Main Streamlit dashboard
├── batch_process.py       # Headless script for bulk AI analysis
├── src/
│   ├── config.py          # Directory paths and settings
│   ├── crawler.py         # File scanner and database population
│   ├── database.py        # SQLAlchemy schema and connection
│   ├── evaluator.py       # Gemini API integration
│   └── processor.py       # PDF to Image extraction (PyMuPDF)
├── data/
│   └── files.db           # SQLite database (auto-generated)
└── requirements.txt       # Python dependencies

```

## Dependencies

* `streamlit`
* `google-generativeai`
* `sqlalchemy`
* `pymupdf` (fitz)
* `pandas`
* `python-dotenv`
* `tqdm`
