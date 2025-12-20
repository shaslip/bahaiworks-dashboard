import os
import json
import typing_extensions as typing
import google.generativeai as genai
from dotenv import load_dotenv

# Load API Key
load_dotenv()
api_key = os.getenv("GEMINI_API_KEY")

if not api_key:
    raise ValueError("GEMINI_API_KEY not found in .env file")

genai.configure(api_key=api_key)

# Define the response schema using strict typing
class EvaluationResult(typing.TypedDict):
    language: str
    summary: str
    priority_score: int
    ai_justification: str

def evaluate_document(images):
    """
    Sends images to Gemini 3 Flash Preview for analysis.
    Returns a dictionary matching the EvaluationResult schema.
    """
    model = genai.GenerativeModel('gemini-3-flash-preview')

    prompt = """
    You are an expert Historian and Archivist for 'Bahai.works', a repository of primary source materials. 
    Review these document pages. Your task is to prioritize them based on their value to **academic researchers**.

    1. **Language**: Identify the primary language (e.g., 'English', 'German', 'Persian').
    2. **Summary**: Write a concise English summary (2-3 sentences).
    3. **Priority Score (1-10)**: Assign a score based on this strict rubric:
       - **9-10 (Critical Source):** Original manuscripts, handwriting, primary source letters from central figures, unique pre-1930 documents.
       - **7-8 (High Value):** Rare early periodicals (e.g., Star of the West, Sonne der Wahrheit), out-of-print historical books, local community records/minutes.
       - **4-6 (Standard Reference):** Standard history books, biographies, study guides, substantive administrative reports.
       - **1-3 (Low Priority):** Mass-produced brochures, introductory pamphlets, modern reprints widely available elsewhere, or simple event programs.
    4. **Justification**: Briefly explain the score. **explicitly mention** if the item is a common brochure or pamphlet to justify a low score.

    Return the result in JSON format.
    """

    try:
        # Prepare content: Prompt + List of Images
        content = [prompt] + images

        # Generate response with forced JSON schema
        response = model.generate_content(
            content,
            generation_config=genai.GenerationConfig(
                response_mime_type="application/json",
                response_schema=EvaluationResult
            )
        )
        
        # Parse text response to dict
        return json.loads(response.text)

    except Exception as e:
        print(f"AI Evaluation Error: {e}")
        return None
