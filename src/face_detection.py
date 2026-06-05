import numpy as np
from mtcnn import MTCNN

def detect_faces(pil_image):
    """
    Takes a PIL image, converts it for MTCNN, and returns a list of bounding boxes.
    """
    # Convert PIL Image to RGB numpy array
    img_array = np.array(pil_image.convert('RGB'))
    
    # Initialize detector (loads weights automatically)
    detector = MTCNN()
    
    # Detect faces
    faces = detector.detect_faces(img_array)
    
    results = []
    for i, face in enumerate(faces):
        x, y, w, h = face['box']
        # MTCNN can sometimes return negative coordinates if faces are cut off at the edge
        x = max(0, x)
        y = max(0, y)
        results.append({
            "id": i + 1,
            "box": [x, y, w, h],
            "confidence": face['confidence']
        })
        
    return results
