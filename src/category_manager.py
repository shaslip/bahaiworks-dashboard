import os
import json
import time
import re
import requests
from thefuzz import process, fuzz

CACHE_FILE = "category_cache.json"
EXCLUSIONS_FILE = "category_exclusions.txt"
ALIASES_FILE = "category_aliases.txt"
EXPIRY_SECONDS = 10 * 24 * 60 * 60  # 10 days

def fetch_all_categories(api_url="https://bahai.media/api.php"):
    """Downloads all categories from the MediaWiki API."""
    categories = []
    params = {
        "action": "query",
        "list": "allcategories",
        "aclimit": "max",
        "format": "json"
    }
    
    while True:
        response = requests.get(api_url, params=params).json()
        if "query" in response and "allcategories" in response["query"]:
            for cat in response["query"]["allcategories"]:
                categories.append(cat["*"])
                
        if "continue" in response:
            params.update(response["continue"])
            time.sleep(0.5)
        else:
            break
            
    return categories

def get_cached_categories():
    """Loads categories from cache, or fetches them if expired/missing."""
    if os.path.exists(CACHE_FILE):
        file_age = time.time() - os.path.getmtime(CACHE_FILE)
        if file_age < EXPIRY_SECONDS:
            with open(CACHE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
                
    # If missing or expired, fetch and save
    categories = fetch_all_categories()
    with open(CACHE_FILE, 'w', encoding='utf-8') as f:
        json.dump(categories, f, ensure_ascii=False, indent=2)
    return categories

def load_exclusions():
    """Loads regex patterns from the exclusions file."""
    if not os.path.exists(EXCLUSIONS_FILE):
        # Create an empty file with an example if it doesn't exist
        with open(EXCLUSIONS_FILE, 'w', encoding='utf-8') as f:
            f.write("# Add regex patterns to exclude from fuzzy search\n# BWNS \\d+\n# AB Volume\n")
        return []
        
    patterns = []
    with open(EXCLUSIONS_FILE, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#'):
                try:
                    patterns.append(re.compile(line, re.IGNORECASE))
                except re.error:
                    pass
    return patterns

def load_aliases():
    """Loads aliases from the text file (Alias = Target Category)."""
    if not os.path.exists(ALIASES_FILE):
        with open(ALIASES_FILE, 'w', encoding='utf-8') as f:
            f.write("# Add aliases using the equals sign: Alias = Target Category\n# Mr Khazeh = Jalál Kháḍih\n")
        return {}
        
    aliases = {}
    with open(ALIASES_FILE, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                alias, target = line.split('=', 1)
                aliases[alias.strip()] = target.strip()
    return aliases

def get_fuzzy_candidates(suggestions, limit_per_suggestion=5):
    """
    Takes a list of AI-generated suggestions, runs fuzzy matching on each, 
    and pools the top results.
    """
    if not suggestions:
        return []
        
    raw_categories = get_cached_categories()
    exclusions = load_exclusions()
    aliases = load_aliases()
    
    # 1. Filter raw categories
    filtered_cats = []
    for cat in raw_categories:
        if not any(pattern.search(cat) for pattern in exclusions):
            filtered_cats.append(cat)
            
    # 2. Build search dictionary: { "Search Term": "Actual Category" }
    search_dict = {cat: cat for cat in filtered_cats}
    for alias, target in aliases.items():
        search_dict[alias] = target
        
    search_terms = list(search_dict.keys())
    final_candidates = set()
    
    # 3. Run fuzzy search on EACH AI suggestion
    for suggestion in suggestions:
        matches = process.extract(suggestion, search_terms, limit=limit_per_suggestion, scorer=fuzz.token_set_ratio)
        for match_str, score in matches:
            # Only include if it's a reasonably good match (score out of 100)
            if score >= 60: 
                actual_cat = search_dict[match_str]
                final_candidates.add(actual_cat)
            
    return list(final_candidates)
