import os
import sys
import tempfile
import urllib.parse
import re
import argparse
from datetime import datetime, timezone
import pymysql
import requests
from dateutil.parser import parse
from xml.sax.saxutils import escape
from email.utils import format_datetime

# ==========================================
# USER CONFIGURATION
# ==========================================

FEED_TITLE = "Spoken Wikipedia"
FEED_LINK = "https://sw.toolforge.org/"
FEED_LANGUAGE = "en-us"
FEED_CATEGORY = "Education"
FEED_DESCRIPTION = "These are Wikipedia articles as read by volunteers. It is generated as an RSS feed from the files in the Spoken Wikipedia project. Source code and issue tracking are available at https://github.com/xenotropic/spoken-wikipedia-rss."
FEED_IMAGE = "https://sw.toolforge.org/cover.jpg"
FEED_OWNER_NAME = "Joseph Morris"
FEED_OWNER_EMAIL = "joe@morris.cloud"

USER_AGENT = "SpokenWikipediaPodcaster/2.0 (https://github.com/xenotropic/spoken-wikipedia-rss; joe@morris.cloud)"
DB_HOST = "enwiki.analytics.db.svc.wikimedia.cloud"
DB_NAME = "enwiki_p"
MIN_FILE_SIZE_BYTES = 1500000

# ==========================================

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
    SELECT 
        CONVERT(p.page_title USING utf8mb4) AS article_title,
        CONVERT(lt_img.lt_title USING utf8mb4) AS audio_file
    FROM categorylinks cl
    JOIN linktarget lt_cat ON cl.cl_target_id = lt_cat.lt_id
    JOIN page p ON cl.cl_from = p.page_id
    JOIN imagelinks il ON il.il_from = p.page_id
    JOIN linktarget lt_img ON il.il_target_id = lt_img.lt_id
    WHERE lt_cat.lt_title = 'Spoken_articles'
      AND p.page_namespace = 0
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
        "format": "json"
    }
    all_whitelist = set()
    explicit_whitelist = set()
    try:
        res = session.get(url, params=params, headers={"User-Agent": USER_AGENT}, timeout=15)
        if res.status_code == 200:
            wikitext = res.json().get("parse", {}).get("wikitext", {}).get("*", "")
            
            is_explicit_section = False
            for line in wikitext.split('\n'):
                header_match = re.match(r'^={1,6}\s*(.*?)\s*={1,6}', line)
                if header_match:
                    header_title = header_match.group(1).lower()
                    is_explicit_section = ('human sexuality' in header_title)
                    
                matches = re.findall(r'\[\[:?(?:File|Image|Media):([^|\]]+\.(?:ogg|oga|mp3|flac|wav|opus))', line, re.IGNORECASE)
                for m in matches:
                    fname = m.strip().replace(' ', '_')
                    all_whitelist.add(fname)
                    if is_explicit_section:
                        explicit_whitelist.add(fname)
                        
    except Exception as e:
        print(f"Error fetching index whitelist: {e}", file=sys.stderr)
    return all_whitelist, explicit_whitelist

def load_explicit_list(filepath):
    explicit_titles = set()
    if os.path.exists(filepath):
        with open(filepath, 'r', encoding='utf-8') as f:
            for line in f:
                clean_line = line.strip()
                if clean_line and not clean_line.startswith('#'):
                    explicit_titles.add(clean_line.replace(' ', '_'))
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
            res = session.post(url, data=payload, headers={"User-Agent": USER_AGENT}, timeout=20)
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

def batch_fetch_article_images(articles, session):
    image_map = {}
    chunk_size = 50
    for i in range(0, len(articles), chunk_size):
        chunk = articles[i:i + chunk_size]
        titles = "|".join([a.replace('_', ' ') for a in chunk])
        url = "https://en.wikipedia.org/w/api.php"
        payload = {
            "action": "query",
            "titles": titles,
            "prop": "pageimages",
            "pithumbsize": "500",
            "format": "json",
            "formatversion": "2"
        }
        try:
            res = session.post(url, data=payload, headers={"User-Agent": USER_AGENT}, timeout=20)
            if res.status_code == 200:
                pages = res.json().get("query", {}).get("pages", [])
                for p in pages:
                    if "thumbnail" in p:
                        image_map[p["title"]] = p["thumbnail"].get("source")
        except requests.RequestException as e:
            print(f"Error fetching article images chunk: {e}", file=sys.stderr)
    return image_map

def filter_and_deduplicate(raw_records, metadata_map, whitelist_set):
    grouped = {}
    for article, audio in raw_records:
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
        in_whitelist = audio in whitelist_set
        
        if not (has_tag or has_title or in_whitelist):
            continue
            
        if article not in grouped:
            grouped[article] = []
        grouped[article].append((audio, info))
        
    final_items = []
    for article, files in grouped.items():
        versions = {}
        for audio_file, info in files:
            try:
                dt = parse(info.get("timestamp", ""))
                ts = dt.timestamp()
                date_str = dt.strftime("%Y-%m-%d")
            except Exception:
                ts = 0
                date_str = "1970-01-01"
                
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
                
        for audio_file, info in selected_version["files"]:
            final_items.append((article, audio_file, info))
        
    return final_items

def extract_metadata_fields(info):
    author = info.get("user", "Wikipedia Volunteers")
    extmetadata = info.get("extmetadata", {})
    if "Artist" in extmetadata and "value" in extmetadata["Artist"]:
        clean_author = re.sub(r"<[^<]+?>", "", extmetadata["Artist"]["value"]).strip()
        clean_author = clean_author.replace("Speaker: ", "").replace("User: ", "").replace("\nAuthors of the article", "").strip()
        if clean_author:
            author = clean_author

    license_name = extmetadata.get("LicenseShortName", {}).get("value", "Unknown")
    filedate_str = info.get("timestamp", "")
    if "DateTimeOriginal" in extmetadata and "value" in extmetadata["DateTimeOriginal"]:
        orig = re.sub(r"<[^<]+?>", "", extmetadata["DateTimeOriginal"]["value"]).strip()
        end_match = re.search(r"\d{4}", orig)
        if end_match:
            orig = orig[:end_match.end()]
        try:
            parse(orig)
            filedate_str = orig
        except (ValueError, TypeError):
            pass

    try:
        dt = parse(filedate_str)
        if not dt.tzinfo:
            dt = dt.replace(tzinfo=timezone.utc)
        pub_date = format_datetime(dt)
        human_date = dt.strftime("%B %d, %Y")
    except Exception:
        fallback_dt = datetime(2002, 10, 2, 8, 0, tzinfo=timezone.utc)
        pub_date = format_datetime(fallback_dt)
        human_date = "Unknown"

    return author, license_name, pub_date, human_date

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

def write_feed(items, output_path, is_explicit_feed, image_map):
    xml_items = []
    skipped_count = 0
    
    explicit_flag = "true" if is_explicit_feed else "false"
    feed_title = f"{FEED_TITLE} (Explicit)" if is_explicit_feed else FEED_TITLE

    for article, audio_file, file_info in items:
        mp3_url = resolve_verified_mp3(file_info)
        if not mp3_url:
            skipped_count += 1
            continue

        raw_author, license_name, pub_date, human_date = extract_metadata_fields(file_info)
        
        clean_article = escape(article.replace("_", " "))
        author = escape(raw_author)
        article_url_xml = escape(f"https://en.wikipedia.org/wiki/{urllib.parse.quote(article)}")
        mp3_url_xml = escape(mp3_url)
        
        image_url = image_map.get(article.replace("_", " "))
        image_tag = f'\n      <itunes:image href="{escape(image_url)}" />' if image_url else ""
        
        desc = (f"<![CDATA[<p>This is a spoken word version of the Wikipedia article "
                f"\"{clean_article}\" recorded on {human_date}.</p>"
                f"<p>Read by: {author}<br />License: {license_name}</p>]]>")

        item_xml = f"""    <item>
      <title>{clean_article}</title>
      <link>{article_url_xml}</link>
      <guid isPermaLink="false">{mp3_url_xml}</guid>
      <pubDate>{pub_date}</pubDate>
      <enclosure url="{mp3_url_xml}" type="audio/mpeg" />
      <description>{desc}</description>
      <itunes:author>{author}</itunes:author>{image_tag}
    </item>"""
        xml_items.append(item_xml)

    items_str = "\n".join(xml_items)
    feed_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss xmlns:atom="http://www.w3.org/2005/Atom" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" xmlns:content="http://purl.org/rss/1.0/modules/content/" version="2.0">
  <channel>
    <title>{escape(feed_title)}</title>
    <link>{escape(FEED_LINK)}</link>
    <language>{escape(FEED_LANGUAGE)}</language>
    <itunes:category text="{escape(FEED_CATEGORY)}" />
    <itunes:explicit>{explicit_flag}</itunes:explicit>
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
    print(f"[{'Explicit' if is_explicit_feed else 'Clean'}] Feed generation complete: {len(xml_items)} items written to {output_path} ({skipped_count} skipped).")

def generate_rss_feeds(out_clean, out_explicit, explicit_list_path, limit):
    print(f"Querying MariaDB replica for spoken articles (Limit: {limit})...")
    raw_records = fetch_spoken_records(limit)
    print(f"Retrieved {len(raw_records)} media transclusions from database.")

    session = requests.Session()
    
    print("Fetching whitelist from Wikipedia:Spoken_articles...")
    all_whitelist, explicit_whitelist = fetch_index_whitelist(session)
    print(f"Loaded {len(all_whitelist)} approved filenames ({len(explicit_whitelist)} explicitly flagged via section headers).")

    explicit_titles_set = load_explicit_list(explicit_list_path)
    if explicit_titles_set:
        print(f"Loaded {len(explicit_titles_set)} custom explicit titles from {explicit_list_path}.")

    unique_files = list(set([f for _, f in raw_records]))
    print(f"Batch fetching file metadata for {len(unique_files)} files...")
    metadata_map = batch_fetch_file_metadata(unique_files, session)
    
    print("Filtering and deduplicating records...")
    filtered_records = filter_and_deduplicate(raw_records, metadata_map, all_whitelist)
    
    unique_articles = list(set([r[0] for r in filtered_records]))
    print(f"Batch fetching page images for {len(unique_articles)} articles...")
    image_map = batch_fetch_article_images(unique_articles, session)

    clean_items = []
    explicit_items = []
    
    # Regex to catch explicit words anywhere in the article title
    profanity_pattern = re.compile(r'(?i)\b(bitch|shit|fuck|piss)')

    for article, audio_file, file_info in filtered_records:
        is_explicit = False
        
        if audio_file in explicit_whitelist:
            is_explicit = True
        elif article.replace(' ', '_') in explicit_titles_set:
            is_explicit = True
        elif profanity_pattern.search(article):
            is_explicit = True
            
        explicit_items.append((article, audio_file, file_info))
        if not is_explicit:
            clean_items.append((article, audio_file, file_info))

    write_feed(clean_items, out_clean, False, image_map)
    write_feed(explicit_items, out_explicit, True, image_map)

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
