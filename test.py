import os
from google.cloud import documentai
from google.api_core.client_options import ClientOptions
from dotenv import load_dotenv

load_dotenv()

def test_ocr():
    project_id = os.getenv("GCP_PROJECT_ID")
    location = os.getenv("GCP_LOCATION")
    processor_id = os.getenv("GCP_PROCESSOR_ID")
    
    print(f"Testing connection to: {project_id} / {location} / {processor_id}")

    opts = ClientOptions(api_endpoint=f"{location}-documentai.googleapis.com")
    client = documentai.DocumentProcessorServiceClient(client_options=opts)
    
    name = client.processor_path(project_id, location, processor_id)
    print(f"Successfully created client for: {name}")

test_ocr()
