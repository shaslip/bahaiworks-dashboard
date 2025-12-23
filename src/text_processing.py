import re

def parse_text_file(file_path):
    """
    Parses the OCR text file into a dictionary mapping Page Labels to Content.
    
    Returns:
        page_map (dict): {'1': 'The Lost Hope...', 'vii': 'Preface...'}
        tag_order (list): List of page labels in the order they appear in the file.
    """
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            full_text = f.read()
    except FileNotFoundError:
        return {}, []

    # Regex to capture {{page|LABEL|...}}
    # We capture the label (group 1) and the full tag (group 0)
    # The content is everything between this tag and the next tag.
    pattern = re.compile(r'(\{\{page\|(.*?)\|.*?\}\})')
    
    matches = list(pattern.finditer(full_text))
    page_map = {}
    tag_order = []
    
    for i, match in enumerate(matches):
        full_tag = match.group(1)
        page_label = match.group(2) # e.g. "1", "vii", "66"
        
        start_index = match.start()
        
        # End index is the start of the next match, or end of file
        if i + 1 < len(matches):
            end_index = matches[i+1].start()
        else:
            end_index = len(full_text)
            
        # Extract content (keeping the tag at the top as requested)
        content = full_text[start_index:end_index]
        
        page_map[page_label] = content
        tag_order.append(page_label)
        
    return page_map, tag_order

def find_best_match_for_title(title, page_map, tag_order):
    """
    Simple heuristic to find Front Matter pages.
    Looks for the Title word in the first 100 chars of pages BEFORE '1'.
    """
    # 1. Identify where Page 1 is
    try:
        index_of_one = tag_order.index("1")
        front_matter_pages = tag_order[:index_of_one]
    except ValueError:
        # If no page '1' found, search everything
        front_matter_pages = tag_order

    # 2. Search for the title (case-insensitive)
    clean_title = title.lower().strip()
    
    for page in front_matter_pages:
        content = page_map[page]
        # Check first 200 chars to avoid false positives deep in text
        if clean_title in content[:200].lower():
            return page
            
    return None
