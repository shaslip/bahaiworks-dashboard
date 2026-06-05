import os
# Suppress TensorFlow informational logs and oneDNN warnings
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '2'
os.environ['TF_ENABLE_ONEDNN_OPTS'] = '0'

import numpy as np
from mtcnn import MTCNN

def detect_faces(pil_image):
    """
    Takes a PIL image, converts it for MTCNN, and returns a list of bounding boxes.
    Boxes are padded to include the whole head/hair rather than just the facial features.
    """
    # Convert PIL Image to RGB numpy array
    img_array = np.array(pil_image.convert('RGB'))
    img_h, img_w, _ = img_array.shape
    
    # Initialize detector
    detector = MTCNN()
    
    # Detect faces
    faces = detector.detect_faces(img_array)
    
    results = []
    for i, face in enumerate(faces):
        x, y, w, h = face['box']
        
        # MTCNN boxes are tight on the features. Expand them to capture the whole head.
        pad_x = int(w * 0.25)       # 25% padding on left/right
        pad_top = int(h * 0.35)     # 35% padding on top for hair/hats
        pad_bottom = int(h * 0.15)  # 15% padding on bottom for chin/neck
        
        # Calculate new coordinates, ensuring we don't go out of image bounds
        new_x = max(0, x - pad_x)
        new_y = max(0, y - pad_top)
        new_w = min(img_w - new_x, w + pad_x * 2)
        new_h = min(img_h - new_y, h + pad_top + pad_bottom)
        
        results.append({
            "id": i + 1,
            "box": [new_x, new_y, new_w, new_h],
            "confidence": face['confidence']
        })
        
    return results
