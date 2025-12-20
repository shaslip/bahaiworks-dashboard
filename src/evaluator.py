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
    Sends images to Gemini 3 Flash for analysis.
    Returns a dictionary matching the EvaluationResult schema.
    """
    # Use Flash for speed and high rate limits.
    model = genai.GenerativeModel('gemini-3-flash-preview')

    prompt = """
    You are an expert Historian and Archivist for Bahai.works. 
    Review these document pages. Your task is to categorize and prioritize them for digitization.

    1. **Language**: Identify the primary language (e.g., 'English', 'German', 'Persian').
    2. **Summary**: Write a concise English summary (2-3 sentences) of the content.
    3. **Priority Score (1-10)**: Rate the historical value for researchers.
       - 10: Rare/Original manuscripts, major historical letters, unique primary sources.
       - 7-9: Early periodicals, out-of-print books, community histories.
       - 4-6: Common reprints, general study guides, modern administrative circulars.
       - 1-3: Low value duplicates, blurry/unreadable scans, or irrelevant receipts.
    4. **Justification**: Briefly explain why you gave this score.

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
