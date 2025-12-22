import re
from wikibaseintegrator import wbi_login, WikibaseIntegrator, wbi_helpers
from wikibaseintegrator.datatypes import String, Item, MonolingualText, Time
from wikibaseintegrator.wbi_config import config as wbi_config
from wikibaseintegrator.wbi_enums import ActionIfExists

# --- Configuration (Hardcoded as per your script) ---
wbi_config['MEDIAWIKI_API_URL'] = 'https://bahaidata.org/api.php'
wbi_config['USER_AGENT'] = 'MyWikibaseBot/1.0 (https://bahaidata.org/User:Sarah)'

# Note: In production, consider moving credentials to environment variables
LOGIN_USER = 'Username'
LOGIN_PASS = 'password' 

def get_wbi_instance():
    login_instance = wbi_login.Clientlogin(user=LOGIN_USER, password=LOGIN_PASS)
    return WikibaseIntegrator(login=login_instance)

def check_or_create_person(wbi, person_name, role):
    """Generic function to check or create a person entity"""
    if not person_name: return None
    
    search_result = wbi_helpers.search_entities(person_name)
    if search_result:
        return search_result[0]
    else:
        person_item = wbi.item.new()
        person_item.labels.set(language='en', value=person_name)
        person_item.claims.add(Item(value='Q100', prop_nr='P12')) # Instance of Human
        person_item.write()
        print(f"Created {role} {person_name} ({person_item.id})")
        return person_item.id

def check_or_create_publisher(wbi, publisher_name):
    if not publisher_name: return None
    search_result = wbi_helpers.search_entities(publisher_name)
    if search_result:
        return search_result[0]
    else:
        item = wbi.item.new()
        item.labels.set(language='en', value=publisher_name)
        item.claims.add(Item(value='Q3118', prop_nr='P12')) # Instance of Publisher
        item.write()
        return item.id

def check_or_create_country(wbi, country_name):
    if not country_name: return None
    search_result = wbi_helpers.search_entities(country_name)
    if search_result:
        return search_result[0]
    else:
        item = wbi.item.new()
        item.labels.set(language='en', value=country_name)
        item.write()
        return item.id

def link_book_to_person(wbi, book_id, person_id, prop_id):
    if not person_id: return
    person_item = wbi.item.get(entity_id=person_id)
    new_claim = Item(value=book_id, prop_nr=prop_id)
    person_item.claims.add(new_claim, action_if_exists=ActionIfExists.APPEND_OR_REPLACE)
    person_item.write()

def import_book_to_wikibase(data):
    """
    Accepts a dictionary 'data' with keys:
    TITLE, FULL_TITLE, AUTHOR, EDITOR, TRANSLATOR, COMPILER, 
    COVER_IMAGE, PUBLISHER, COUNTRY, PUBYEAR, PAGES, ISBN10, ISBN13
    """
    wbi = get_wbi_instance()
    
    label = data.get('TITLE', '').strip()
    title = data.get('FULL_TITLE', '').strip() or label
    
    # Process Creators
    authors = [x.strip() for x in data.get('AUTHOR', '').split(',') if x.strip()]
    author_ids = [check_or_create_person(wbi, a, "author") for a in authors]

    editors = [x.strip() for x in data.get('EDITOR', '').split(',') if x.strip()]
    editor_ids = [check_or_create_person(wbi, e, "editor") for e in editors]

    translators = [x.strip() for x in data.get('TRANSLATOR', '').split(',') if x.strip()]
    translator_ids = [check_or_create_person(wbi, t, "translator") for t in translators]

    compilers = [x.strip() for x in data.get('COMPILER', '').split(',') if x.strip()]
    compiler_ids = [check_or_create_person(wbi, c, "compiler") for c in compilers]

    # Process Metadata
    pub_id = check_or_create_publisher(wbi, data.get('PUBLISHER'))
    country_id = check_or_create_country(wbi, data.get('COUNTRY'))
    
    isbn10 = re.sub(r'\D', '', data.get('ISBN10', ''))
    isbn13 = re.sub(r'\D', '', data.get('ISBN13', ''))

    # Create Book Item
    book = wbi.item.new()
    book.labels.set(language='en', value=label)
    book.claims.add(Item(value='Q4581', prop_nr='P12')) # Instance of Written Work
    book.claims.add(MonolingualText(text=title, language='en', prop_nr='P47'))

    # Add Claims
    for aid in author_ids: book.claims.add(Item(value=aid, prop_nr='P10'))
    for eid in editor_ids: book.claims.add(Item(value=eid, prop_nr='P14'))
    for tid in translator_ids: book.claims.add(Item(value=tid, prop_nr='P32'))
    for cid in compiler_ids: book.claims.add(Item(value=cid, prop_nr='P43'))

    if data.get('COVER_IMAGE'):
        book.claims.add(String(value=data['COVER_IMAGE'], prop_nr='P35'))
    
    if data.get('PUBYEAR'):
        year = int(data['PUBYEAR'])
        book.claims.add(Time(time=f'+{year:04}-00-00T00:00:00Z', prop_nr='P29', precision=9))

    if pub_id: book.claims.add(Item(value=pub_id, prop_nr='P26'))
    if country_id: book.claims.add(Item(value=country_id, prop_nr='P48'))
    if data.get('PAGES'): book.claims.add(String(value=data['PAGES'], prop_nr='P6'))
    if isbn10: book.claims.add(String(value=isbn10, prop_nr='P31'))
    if isbn13: book.claims.add(String(value=isbn13, prop_nr='P49'))

    book.write()
    
    # Back-link people to book
    for aid in author_ids: link_book_to_person(wbi, book.id, aid, 'P11')
    for eid in editor_ids: link_book_to_person(wbi, book.id, eid, 'P15')
    for tid in translator_ids: link_book_to_person(wbi, book.id, tid, 'P33')
    for cid in compiler_ids: link_book_to_person(wbi, book.id, cid, 'P44')

    return book.id
