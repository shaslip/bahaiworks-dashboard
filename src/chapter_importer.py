import json
from wikibaseintegrator import wbi_helpers
from wikibaseintegrator.datatypes import String, Item
from wikibaseintegrator.wbi_enums import ActionIfExists
from src.wikibase_importer import get_wbi_instance, check_or_create_person

def link_chapter_to_author(wbi, chapter_item_id, author_item_id):
    author_item = wbi.item.get(entity_id=author_item_id)
    new_claim = Item(value=chapter_item_id, prop_nr='P11') 
    author_item.claims.add(new_claim, action_if_exists=ActionIfExists.APPEND_OR_REPLACE)
    author_item.write()

def link_chapter_to_book(wbi, chapter_item_id, book_item_id):
    book_item = wbi.item.get(entity_id=book_item_id)
    new_claim = Item(value=chapter_item_id, prop_nr='P59')  
    book_item.claims.add(new_claim, action_if_exists=ActionIfExists.APPEND_OR_REPLACE)
    book_item.write()

def create_chapter_item(wbi, title, page_range, author_item_ids, book_item_id):
    chapter_item = wbi.item.new()
    chapter_item.labels.set(language='en', value=title)
    chapter_item.claims.add(Item(value='Q4581', prop_nr='P12'))
    chapter_item.claims.add(Item(value=book_item_id, prop_nr='P9'))
    for author_id in author_item_ids:
        chapter_item.claims.add(Item(value=author_id, prop_nr='P10'), 
                                action_if_exists=ActionIfExists.APPEND_OR_REPLACE)
    chapter_item.claims.add(String(value=page_range, prop_nr='P6'))
    chapter_item.write()
    return chapter_item.id

def import_chapters_to_wikibase(book_qid, chapters_data):
    """
    Returns tuple: (logs, created_map)
    created_map = [{'title': 'Chapter 1', 'qid': 'Q123'}, ...]
    """
    wbi = get_wbi_instance()
    logs = []
    created_map = [] # NEW: Store structured data for the next pipeline step

    for chapter in chapters_data:
        title = chapter.get('title')
        page_range = chapter.get('page_range')
        authors = chapter.get('author', [])

        # 1. Resolve Authors
        author_ids = []
        for author_name in authors:
            aid = check_or_create_person(wbi, author_name, "author")
            author_ids.append(aid)

        # 2. Create Chapter
        chapter_qid = create_chapter_item(wbi, title, page_range, author_ids, book_qid)
        logs.append(f"Created chapter: '{title}' ({chapter_qid})")
        
        # 3. Link Authors & Book
        for aid in author_ids: link_chapter_to_author(wbi, chapter_qid, aid)
        link_chapter_to_book(wbi, chapter_qid, book_qid)
        
        # Add to map
        created_map.append({'title': title, 'qid': chapter_qid})

    return logs, created_map
