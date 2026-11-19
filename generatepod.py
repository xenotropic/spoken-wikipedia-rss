import os
import sys
import tempfile
import urllib.parse
import html
import re
import argparse
from datetime import datetime, timezone
import pymysql
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from dateutil.parser import parse
from xml.sax.saxutils import escape
from email.utils import format_datetime

# ==========================================
# USER CONFIGURATION
# ==========================================

# AGENTS: Increment SCRIPT_VERSION on every modification using X.Y.Z criteria:
#   X (Major)  : Breaking changes to CLI flags, output filenames, or RSS schema architecture.
#   Y (Medium) : New features, new feed outputs, new API properties, or filtering logic changes.
#   Z (Minor)  : Bug fixes, regex tuning, metadata cleanup, or performance/query optimizations.
SCRIPT_VERSION = "2.2.0"

FEED_TITLE = "Spoken Wikipedia"
FEED_LINK = "https://sw.toolforge.org/"
FEED_LANGUAGE = "en-us"
FEED_CATEGORY = "Education"
FEED_DESCRIPTION = "These are Wikipedia articles as read by volunteers. It is generated as an RSS feed from the files in the Spoken Wikipedia project. Source code and issue tracking are available at https://github.com/xenotropic/spoken-wikipedia-rss."
FEED_IMAGE = "https://sw.toolforge.org/cover.jpg"
FEED_OWNER_NAME = "Joseph Morris"
FEED_OWNER_EMAIL = "joe@morris.cloud"

USER_AGENT = f"SpokenWikipediaPodcaster/{SCRIPT_VERSION} (https://github.com/xenotropic/spoken-wikipedia-rss; joe@morris.cloud)"
DB_HOST = "enwiki.analytics.db.svc.wikimedia.cloud"
DB_NAME = "enwiki_p"
MIN_FILE_SIZE_BYTES = 1500000

# ==========================================

def create_http_session():
    session = requests.Session()
    retry_strategy = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["GET", "POST"])
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({"User-Agent": USER_AGENT})
    return session

def normalize_wiki_title(title):
    if not title:
        return ""
    cleaned = urllib.parse.unquote(html.unescape(title))
    cleaned = cleaned.replace('\u200e', '').replace('\u200f', '').strip()
    cleaned = re.sub(r"'{2,5}", "", cleaned)
    cleaned = cleaned.replace(' ', '_')
    if not cleaned:
        return ""
    return cleaned[0].upper() + cleaned[1:]

def get_db_connection():
    return pymysql.connect(
        host=DB_HOST,
        database=DB_NAME,
        read_default_file=os.path.expanduser("~/replica.my.cnf"),
        cursorclass=pymysql.cursors.DictCursor
    )

def fetch_spoken_records(limit=0):
    limit_clause = f"LIMIT {int(limit)}" if limit and limit > 0 else ""
    sql = f"""
    SELECT DISTINCT
        CONVERT(p.page_title USING utf8mb4) AS article_title,
        CONVERT(lt_img.lt_title USING utf8mb4) AS audio_file
    FROM categorylinks cl
    JOIN linktarget lt_cat ON cl.cl_target_id = lt_cat.lt_id
    JOIN page p ON cl.cl_from = p.page_id
    JOIN imagelinks il ON il.il_from = p.page_id
    JOIN linktarget lt_img ON il.il_target_id = lt_img.lt_id
    WHERE lt_cat.lt_title = 'Spoken_articles'
      AND lt_cat.lt_namespace = 14
      AND p.page_namespace = 0
      AND lt_img.lt_namespace = 6
    {limit_clause};
    """
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
    finally:
        conn.close()
        
    allowed_exts = ('.ogg', '.oga', '.mp3', '.flac', '.wav', '.opus')
    return [(r["article_title"], r["audio_file"]) for r in rows if r["audio_file"].lower().endswith(allowed_exts)]

def fetch_index_whitelist(session):
    url = "https://en.wikipedia.org/w/api.php"
    params = {
        "action": "parse",
        "page": "Wikipedia:Spoken_articles",
        "prop": "wikitext",
        "format": "json",
        "formatversion": "2"
    }
    all_whitelist = set()
    explicit_files = set()
    explicit_section_titles = set()
    try:
        res = session.get(url, params=params, timeout=15)
        if res.status_code == 200:
            data = res.json().get("parse", {}).get("wikitext", "")
            wikitext = data.get("*", "") if isinstance(data, dict) else data
            
            is_explicit_section = False
            for line in wikitext.split('\n'):
                header_match = re.match(r'^={2,6}\s*(.*?)\s*={2,6}', line)
                if header_match:
                    header_title = header_match.group(1).strip().lower()
                    is_explicit_section = ('human sexuality' in header_title)
                
                if is_explicit_section and line.strip().startswith('*'):
                    prefix_match = re.match(r'^\*\s*(?:\{\{[^}]+\}\}\s*)*([^[\n]+?)\s*\[\[:?(?:File|Image|Media):', line, re.IGNORECASE)
                    if prefix_match and prefix_match.group(1).strip():
                        norm_prefix = normalize_wiki_title(prefix_match.group(1).strip())
                        if norm_prefix:
                            explicit_section_titles.add(norm_prefix.lower())

                matches = re.findall(
                    r'\[\[:?(?:File|Image|Media):([^|\]]+\.(?:ogg|oga|mp3|flac|wav|opus))(?:\|([^\]]*))?\]\]',
                    line,
                    re.IGNORECASE
                )
                for raw_fname, raw_label in matches:
                    fname = normalize_wiki_title(raw_fname)
                    if fname:
                        all_whitelist.add(fname)
                        if is_explicit_section:
                            explicit_files.add(fname)
                            explicit_files.add(fname.lower())
                    if is_explicit_section and raw_label:
                        label_clean = normalize_wiki_title(raw_label)
                        if label_clean and not re.match(r'^\(?part[_\s]\d+\)?$', label_clean, re.IGNORECASE):
                            explicit_section_titles.add(label_clean.lower())
                        
    except Exception as e:
        print(f"Error fetching index whitelist: {e}", file=sys.stderr)
    return all_whitelist, explicit_files, explicit_section_titles

def load_explicit_list(filepath):
    explicit_titles = set()
    if os.path.exists(filepath):
        with open(filepath, 'r', encoding='utf-8') as f:
            for line in f:
                clean_line = line.strip()
                if clean_line and not clean_line.startswith('#'):
                    explicit_titles.add(normalize_wiki_title(clean_line).lower())
    return explicit_titles

def batch_fetch_file_metadata(filenames, session):
    metadata_map = {}
    chunk_size = 50
    for i in range(0, len(filenames), chunk_size):
        chunk = filenames[i:i + chunk_size]
        titles = "|".join([f"File:{f.replace(' ', '_')}" for f in chunk])
        url = "https://en.wikipedia.org/w/api.php"
        payload = {
            "action": "query",
            "titles": titles,
            "prop": "imageinfo|videoinfo",
            "iiprop": "timestamp|user|url|size|extmetadata",
            "viprop": "timestamp|user|url|size|extmetadata|derivatives",
            "format": "json",
            "formatversion": "2"
        }
        try:
            res = session.post(url, data=payload, timeout=20)
            if res.status_code == 200:
                pages = res.json().get("query", {}).get("pages", [])
                for p in pages:
                    fname = p.get("title", "").replace("File:", "").replace(" ", "_")
                    media_info = p.get("videoinfo") or p.get("imageinfo")
                    if media_info:
                        info = media_info[0]
                        mp3_url = None
                        for deriv in info.get("derivatives", []):
                            src = deriv.get("src", "").split("?")[0]
                            if src.lower().endswith(".mp3") or deriv.get("type") == "audio/mpeg":
                                mp3_url = src
                                break
                        info["transcoded_mp3"] = mp3_url
                        metadata_map[fname] = info
        except requests.RequestException as e:
            print(f"Error fetching metadata chunk: {e}", file=sys.stderr)
    return metadata_map

def batch_fetch_article_extras(articles, session):
    extras_map = {}
    # MediaWiki TextExtracts enforces exlimit <= 20 per request
    chunk_size = 20
    for i in range(0, len(articles), chunk_size):
        chunk = articles[i:i + chunk_size]
        titles = "|".join([a.replace('_', ' ') for a in chunk])
        url = "https://en.wikipedia.org/w/api.php"
        payload = {
            "action": "query",
            "titles": titles,
            "prop": "pageimages|extracts",
            "pithumbsize": "500",
            "pilimit": "max",
            "exsentences": "2",
            "exlimit": "max",
            "exintro": "1",
            "explaintext": "1",
            "format": "json",
            "formatversion": "2"
        }
        try:
            res = session.post(url, data=payload, timeout=20)
            if res.status_code == 200:
                pages = res.json().get("query", {}).get("pages", [])
                for p in pages:
                    title = p.get("title")
                    raw_img = p.get("thumbnail", {}).get("source")
                    clean_img = raw_img.split("?")[0] if raw_img else None
                    extras_map[title] = {
                        "image": clean_img,
                        "extract": p.get("extract", "").strip()
                    }
        except requests.RequestException as e:
            print(f"Error fetching article extras chunk: {e}", file=sys.stderr)
    return extras_map

def filter_and_deduplicate(raw_records, metadata_map, whitelist_set):
    grouped = {}
    seen_pairs = set()
    for article, audio in raw_records:
        if (article, audio) in seen_pairs:
            continue
        seen_pairs.add((article, audio))

        if audio not in metadata_map:
            continue
        info = metadata_map[audio]
        
        try:
            size = int(info.get("size", 0))
        except (ValueError, TypeError):
            size = 0
            
        if size < MIN_FILE_SIZE_BYTES:
            continue
            
        audio_lower = audio.lower()
        article_lower = article.replace(' ', '_').lower()
        
        has_tag = bool(re.search(r'(?i)(article|spoken|wiki|^en-)', audio))
        has_title = article_lower in audio_lower
        in_whitelist = (audio in whitelist_set) or (normalize_wiki_title(audio) in whitelist_set)
        
        if not (has_tag or has_title or in_whitelist):
            continue
            
        if article not in grouped:
            grouped[article] = []
        grouped[article].append((audio, info))
        
    final_items = []
    for article, files in grouped.items():
        versions = {}
        for audio_file, info in files:
            ts = 0
            date_str = "1970-01-01"
            api_timestamp = info.get("timestamp", "")
            if api_timestamp:
                try:
                    dt = parse(api_timestamp, default=datetime(1970, 1, 1, tzinfo=timezone.utc))
                    ts = dt.timestamp()
                    date_str = dt.strftime("%Y-%m-%d")
                except Exception:
                    pass
                
            user = info.get("user", "Unknown")
            v_key = (user, date_str)
            
            if v_key not in versions:
                versions[v_key] = {"files": [], "total_size": 0, "max_ts": 0}
                
            versions[v_key]["files"].append((audio_file, info))
            try:
                versions[v_key]["total_size"] += int(info.get("size", 0))
            except (ValueError, TypeError):
                pass
            versions[v_key]["max_ts"] = max(versions[v_key]["max_ts"], ts)
            
        sorted_versions = sorted(versions.values(), key=lambda v: v["max_ts"], reverse=True)
        if not sorted_versions:
            continue
            
        selected_version = sorted_versions[0]
        for older_version in sorted_versions[1:]:
            if older_version["total_size"] > (selected_version["total_size"] * 1.3):
                selected_version = older_version
                break
                
        sorted_files = sorted(selected_version["files"], key=lambda x: x[0].lower())
        total_parts = len(sorted_files)
        for idx, (audio_file, info) in enumerate(sorted_files, start=1):
            final_items.append((article, audio_file, info, idx, total_parts))
        
    return final_items

def clean_author_string(raw_text, fallback_user):
    if not raw_text:
        return fallback_user
    text = re.sub(r"<[^<]+?>", "", raw_text)
    text = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]+)\]\]", r"\1", text)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return fallback_user
    first_line = lines[0]
    first_line = re.sub(r"^(?:Speaker|User|Read by|Narrated by)\s*:\s*", "", first_line, flags=re.IGNORECASE)
    first_line = re.sub(r"^en:User:", "", first_line, flags=re.IGNORECASE)
    first_line = re.sub(r"\s+", " ", first_line).strip()
    return first_line if first_line else fallback_user

def extract_metadata_fields(info):
    fallback_user = info.get("user", "Wikipedia Volunteers")
    author = fallback_user
    extmetadata = info.get("extmetadata", {})
    if "Artist" in extmetadata and "value" in extmetadata["Artist"]:
        author = clean_author_string(extmetadata["Artist"]["value"], fallback_user)

    license_name = extmetadata.get("LicenseShortName", {}).get("value", "Unknown")
    license_name = re.sub(r"<[^<]+?>", "", license_name).strip() or "Unknown"
    
    dt = None
    filedate_str = None
    if "DateTimeOriginal" in extmetadata and "value" in extmetadata["DateTimeOriginal"]:
        filedate_str = re.sub(r"<[^<]+?>", "", extmetadata["DateTimeOriginal"]["value"]).strip()
    elif "DateTime" in extmetadata and "value" in extmetadata["DateTime"]:
        filedate_str = re.sub(r"<[^<]+?>", "", extmetadata["DateTime"]["value"]).strip()
        
    if filedate_str:
        try:
            dt = parse(filedate_str, default=datetime(1970, 1, 1, tzinfo=timezone.utc))
        except (ValueError, TypeError):
            pass

    if not dt:
        api_timestamp = info.get("timestamp", "")
        if api_timestamp:
            try:
                dt = parse(api_timestamp, default=datetime(1970, 1, 1, tzinfo=timezone.utc))
            except (ValueError, TypeError):
                pass
                
    if not dt:
        dt = datetime(2002, 10, 2, 8, 0, tzinfo=timezone.utc)
        
    if not dt.tzinfo:
        dt = dt.replace(tzinfo=timezone.utc)
        
    pub_date = format_datetime(dt)
    human_date = dt.strftime("%B %d, %Y")

    return author, license_name, pub_date, human_date, dt.timestamp()

def resolve_verified_mp3(file_info):
    if not file_info:
        return None
    if file_info.get("transcoded_mp3"):
        return file_info["transcoded_mp3"].split("?")[0]
        
    raw_url = file_info.get("url", "").split("?")[0]
    if raw_url.lower().endswith(".mp3"):
        return raw_url
        
    if "upload.wikimedia.org" in raw_url:
        filename = raw_url.split("/")[-1]
        transcoded_url = raw_url.replace("/commons/", "/commons/transcoded/").replace("/en/", "/en/transcoded/")
        return f"{transcoded_url}/{filename}.mp3"
        
    return None

def write_feed(items, output_path, is_explicit_feed, extras_map):
    prepared_entries = []
    skipped_count = 0
    
    explicit_flag = "true" if is_explicit_feed else "false"
    feed_title = f"{FEED_TITLE} (Explicit)" if is_explicit_feed else FEED_TITLE

    for article, audio_file, file_info, part_idx, total_parts in items:
        mp3_url = resolve_verified_mp3(file_info)
        if not mp3_url:
            skipped_count += 1
            continue

        raw_author, raw_license, pub_date, human_date, sort_ts = extract_metadata_fields(file_info)
        
        base_article_title = article.replace("_", " ")
        display_title = f"{base_article_title} (Part {part_idx} of {total_parts})" if total_parts > 1 else base_article_title
        
        clean_title_xml = escape(display_title)
        clean_article_xml = escape(base_article_title)
        author_xml = escape(raw_author)
        license_xml = escape(raw_license)
        article_url_raw = f"https://en.wikipedia.org/wiki/{urllib.parse.quote(article)}"
        article_url_xml = escape(article_url_raw)
        mp3_url_xml = escape(mp3_url)
        
        try:
            file_size = max(0, int(file_info.get("size", 0)))
        except (ValueError, TypeError):
            file_size = 0
        
        extras = extras_map.get(base_article_title, {})
        image_url = extras.get("image")
        extract = extras.get("extract", "")
        
        image_tag = f'\n      <itunes:image href="{escape(image_url)}" />' if image_url else ""
        extract_html = f"<p>{escape(extract)}</p>" if extract else ""
        
        part_note = f" (Part {part_idx} of {total_parts})" if total_parts > 1 else ""
        inner_html = (
            f"{extract_html}<p>This is a spoken word version{escape(part_note)} of the Wikipedia article "
            f"<a href=\"{article_url_xml}\">{clean_article_xml}</a> recorded on {escape(human_date)}.</p>"
            f"<p>Read by: {author_xml}<br />License: {license_xml}</p>"
        ).replace("]]>", "]]]]><![CDATA[>")
        desc = f"<![CDATA[{inner_html}]]>"

        item_xml = f"""    <item>
      <title>{clean_title_xml}</title>
      <link>{article_url_xml}</link>
      <guid isPermaLink="false">{mp3_url_xml}</guid>
      <pubDate>{pub_date}</pubDate>
      <enclosure url="{mp3_url_xml}" length="{file_size}" type="audio/mpeg" />
      <description>{desc}</description>
      <itunes:author>{author_xml}</itunes:author>{image_tag}
    </item>"""
        prepared_entries.append((sort_ts, display_title, item_xml))

    # Sort newest episodes first; tie-break alphabetically by display title
    prepared_entries.sort(key=lambda x: (-x[0], x[1]))
    xml_items = [entry[2] for entry in prepared_entries]

    items_str = "\n".join(xml_items)
    feed_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss xmlns:atom="http://www.w3.org/2005/Atom" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" xmlns:content="http://purl.org/rss/1.0/modules/content/" version="2.0">
  <channel>
    <title>{escape(feed_title)}</title>
    <link>{escape(FEED_LINK)}</link>
    <language>{escape(FEED_LANGUAGE)}</language>
    <itunes:category text="{escape(FEED_CATEGORY)}" />
    <itunes:explicit>{explicit_flag}</itunes:explicit>
    <generator>Spoken Wikipedia Podcaster v{escape(SCRIPT_VERSION)}</generator>
    <description>{escape(FEED_DESCRIPTION)}</description>
    <itunes:image href="{escape(FEED_IMAGE)}" />
    <itunes:owner>
      <itunes:name>{escape(FEED_OWNER_NAME)}</itunes:name>
      <itunes:email>{escape(FEED_OWNER_EMAIL)}</itunes:email>
    </itunes:owner>
{items_str}
  </channel>
</rss>"""

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', delete=False, dir=os.path.dirname(output_path)) as tf:
        tf.write(feed_xml)
        temp_name = tf.name
    os.replace(temp_name, output_path)
    os.chmod(output_path, 0o644)
    print(f"[{'Explicit' if is_explicit_feed else 'Clean'}] Feed generation complete (v{SCRIPT_VERSION}): {len(xml_items)} items written to {output_path} ({skipped_count} skipped).")

def generate_rss_feeds(out_clean, out_explicit, explicit_list_path, limit):
    print(f"Querying MariaDB replica for spoken articles (Limit: {limit})...")
    raw_records = fetch_spoken_records(limit)
    print(f"Retrieved {len(raw_records)} distinct media transclusions from database.")

    session = create_http_session()
    
    print("Fetching whitelist from Wikipedia:Spoken_articles...")
    all_whitelist, explicit_files, explicit_section_titles = fetch_index_whitelist(session)
    print(
        f"Loaded {len(all_whitelist)} approved filenames "
        f"({len(explicit_files) // 2} files and {len(explicit_section_titles)} titles flagged via Human sexuality section)."
    )

    explicit_titles_set = load_explicit_list(explicit_list_path)
    if explicit_titles_set:
        print(f"Loaded {len(explicit_titles_set)} custom explicit titles from {explicit_list_path}.")

    unique_files = list(set([f for _, f in raw_records]))
    print(f"Batch fetching file metadata for {len(unique_files)} files...")
    metadata_map = batch_fetch_file_metadata(unique_files, session)
    
    print("Filtering and deduplicating records...")
    filtered_records = filter_and_deduplicate(raw_records, metadata_map, all_whitelist)
    
    unique_articles = list(set([r[0] for r in filtered_records]))
    print(f"Batch fetching page images and extracts for {len(unique_articles)} articles (in chunks of 20)...")
    extras_map = batch_fetch_article_extras(unique_articles, session)

    clean_items = []
    explicit_items = []
    
    profanity_pattern = re.compile(r'(?i)(bitch|shit|fuck|cunt|\bpiss)')
    
    sexual_topic_pattern = re.compile(
        r'(?i)(\b(penis|penile|vagina|vaginal|penectomy|intercourse|bdsm|bondage|'
        r'fetish|porn|pornography|erotic|masturbat\w*|orgasm|cunnilingus|fellatio|'
        r'sodomy|bukkake|dildo|vibrator|cleavage|prostitut\w*|brothel|incest|'
        r'bestiality|zoophilia|necrophilia|pedophil\w*|paedophil\w*|rape)\b|'
        r'urethral[_\s]+sounding)'
    )

    for item in filtered_records:
        article, audio_file, file_info, part_idx, total_parts = item
        is_explicit = False
        norm_audio = normalize_wiki_title(audio_file)
        norm_article_lower = normalize_wiki_title(article).lower()
        article_spaces = article.replace('_', ' ')
        
        if (
            audio_file in explicit_files
            or audio_file.lower() in explicit_files
            or norm_audio in explicit_files
            or norm_audio.lower() in explicit_files
            or norm_article_lower in explicit_section_titles
            or norm_article_lower in explicit_titles_set
            or profanity_pattern.search(article_spaces)
            or profanity_pattern.search(audio_file)
            or sexual_topic_pattern.search(article_spaces)
            or sexual_topic_pattern.search(audio_file)
        ):
            is_explicit = True
            
        explicit_items.append(item)
        if not is_explicit:
            clean_items.append(item)

    print(f"Classification summary: {len(clean_items)} clean items, {len(explicit_items) - len(clean_items)} explicit-only items excluded from clean feed.")
    write_feed(clean_items, out_clean, False, extras_map)
    write_feed(explicit_items, out_explicit, True, extras_map)

def main():
    parser = argparse.ArgumentParser(description="Generate Spoken Wikipedia RSS Feeds")
    parser.add_argument("--limit", type=int, default=0, help="Limit number of articles processed")
    parser.add_argument("--out-clean", type=str, default="/data/project/sw/public_html/sw.rss", help="Output path for the clean feed")
    parser.add_argument("--out-explicit", type=str, default="/data/project/sw/public_html/sw-explicit.rss", help="Output path for the explicit feed")
    parser.add_argument("--explicit-list", type=str, default="/data/project/sw/spoken-wikipedia-rss/explicit-only.txt", help="Path to text file containing article titles to exclude from clean feed")
    args = parser.parse_args()
    
    generate_rss_feeds(args.out_clean, args.out_explicit, args.explicit_list, args.limit)

if __name__ == "__main__":
    main()