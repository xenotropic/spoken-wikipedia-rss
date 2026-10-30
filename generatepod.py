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
FEED_EXPLICIT = "false"

USER_AGENT = "SpokenWikipediaPodcaster/1.1 (https://github.com/xenotropic/spoken-wikipedia-rss; joe@morris.cloud)"
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
      AND (
          lt_img.lt_title LIKE '%.ogg' 
          OR lt_img.lt_title LIKE '%.oga' 
          OR lt_img.lt_title LIKE '%.mp3' 
          OR lt_img.lt_title LIKE '%.flac'
      )
    {limit_clause};
    """
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
    finally:
        conn.close()
    return [(r["article_title"], r["audio_file"]) for r in rows]

def batch_fetch_commons_metadata(filenames, session):
    metadata_map = {}
    chunk_size = 50
    for i in range(0, len(filenames), chunk_size):
        chunk = filenames[i:i + chunk_size]
        titles = "|".join([f"File:{f.replace(' ', '_')}" for f in chunk])
        url = "https://commons.wikimedia.org/w/api.php"
        payload = {
            "action": "query",
            "titles": titles,
            "prop": "imageinfo|videoinfo",
            "iiprop": "timestamp|user|url|size|extmetadata",
            "viprop": "derivatives",
            "format": "json",
            "formatversion": "2"
        }
        try:
            res = session.post(url, data=payload, headers={"User-Agent": USER_AGENT}, timeout=20)
            if res.status_code == 200:
                pages = res.json().get("query", {}).get("pages", [])
                for p in pages:
                    fname = p.get("title", "").replace("File:", "").replace(" ", "_")
                    if p.get("imageinfo"):
                        info = p["imageinfo"][0]
                        mp3_url = None
                        for deriv in p.get("videoinfo", [{}])[0].get("derivatives", []):
                            src = deriv.get("src", "")
                            if src.lower().endswith(".mp3") or deriv.get("type") == "audio/mpeg":
                                mp3_url = src
                                break
                        info["transcoded_mp3"] = mp3_url
                        metadata_map[fname] = info
        except requests.RequestException as e:
            print(f"Error fetching metadata chunk: {e}", file=sys.stderr)
    return metadata_map

def filter_and_deduplicate(raw_records, metadata_map):
    grouped = {}
    for article, audio in raw_records:
        if audio not in metadata_map:
            continue
        info = metadata_map[audio]
        size = int(info.get("size", 0))
        
        if size < MIN_FILE_SIZE_BYTES:
            continue
            
        if not re.search(r'(?i)(article|spoken|wiki|^en-)', audio):
            continue
            
        if article not in grouped:
            grouped[article] = []
        grouped[article].append((audio, info))
        
    final_items = []
    for article, files in grouped.items():
        is_multipart = any(re.search(r'(?i)(part\s*\d+|\d+\s*of\s*\d+)', f[0]) for f in files)
        if is_multipart:
            final_items.extend([(article, f[0], f[1]) for f in files])
            continue
            
        def get_ts(f_tuple):
            try:
                return parse(f_tuple[1].get("timestamp", "")).timestamp()
            except Exception:
                return 0
                
        files_sorted = sorted(files, key=get_ts, reverse=True)
        newest_audio, newest_info = files_sorted[0]
        newest_size = int(newest_info.get("size", 0))
        
        selected_audio, selected_info = newest_audio, newest_info
        for older_audio, older_info in files_sorted[1:]:
            if int(older_info.get("size", 0)) > (newest_size * 1.3):
                selected_audio, selected_info = older_audio, older_info
                break
                
        final_items.append((article, selected_audio, selected_info))
        
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
        return file_info["transcoded_mp3"]
    raw_url = file_info.get("url", "")
    if raw_url.lower().endswith(".mp3"):
        return raw_url
    if "upload.wikimedia.org" in raw_url:
        filename = raw_url.split("/")[-1]
        transcoded_url = raw_url.replace("/commons/", "/commons/transcoded/").replace("/en/", "/en/transcoded/")
        return f"{transcoded_url}/{filename}.mp3"
    return None

def generate_rss_feed(output_path, limit):
    print(f"Querying MariaDB replica for spoken articles (Limit: {limit})...")
    raw_records = fetch_spoken_records(limit)
    print(f"Retrieved {len(raw_records)} media transclusions from database.")

    session = requests.Session()
    unique_files = list(set([f for _, f in raw_records]))
    print(f"Batch fetching Commons metadata for {len(unique_files)} files...")
    metadata_map = batch_fetch_commons_metadata(unique_files, session)
    
    print("Filtering and deduplicating records...")
    filtered_records = filter_and_deduplicate(raw_records, metadata_map)

    xml_items = []
    skipped_count = 0

    print("Verifying audio transcode availability and building feed items...")
    for article, audio_file, file_info in filtered_records:
        mp3_url = resolve_verified_mp3(file_info)
        if not mp3_url:
            skipped_count += 1
            continue

        raw_author, license_name, pub_date, human_date = extract_metadata_fields(file_info)
        
        # XML Escape critical string fields
        clean_article = escape(article.replace("_", " "))
        author = escape(raw_author)
        article_url = f"https://en.wikipedia.org/wiki/{urllib.parse.quote(article)}"
        
        desc = (f"<![CDATA[<p>This is a spoken word version of the Wikipedia article "
                f"\"{clean_article}\" recorded on {human_date}.</p>"
                f"<p>Read by: {author}<br />License: {license_name}</p>]]>")

        item_xml = f"""    <item>
      <title>{clean_article}</title>
      <link>{article_url}</link>
      <guid isPermaLink="false">{mp3_url}</guid>
      <pubDate>{pub_date}</pubDate>
      <enclosure url="{mp3_url}" type="audio/mpeg" />
      <description>{desc}</description>
      <itunes:author>{author}</itunes:author>
    </item>"""
        xml_items.append(item_xml)

    items_str = "\n".join(xml_items)
    feed_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss xmlns:atom="http://www.w3.org/2005/Atom" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" xmlns:content="http://purl.org/rss/1.0/modules/content/" version="2.0">
  <channel>
    <title>{escape(FEED_TITLE)}</title>
    <link>{FEED_LINK}</link>
    <language>{FEED_LANGUAGE}</language>
    <itunes:category text="{escape(FEED_CATEGORY)}" />
    <itunes:explicit>{FEED_EXPLICIT}</itunes:explicit>
    <description>{escape(FEED_DESCRIPTION)}</description>
    <itunes:image href="{FEED_IMAGE}" />
    <itunes:owner>
      <itunes:name>{escape(FEED_OWNER_NAME)}</itunes:name>
      <itunes:email>{FEED_OWNER_EMAIL}</itunes:email>
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
    print(f"Feed generation complete: {len(xml_items)} items written to {output_path} ({skipped_count} skipped).")

def main():
    parser = argparse.ArgumentParser(description="Generate Spoken Wikipedia RSS Feed")
    parser.add_argument("--limit", type=int, default=0, help="Limit number of articles processed")
    parser.add_argument("--out", type=str, default="/data/project/sw/public_html/sw.rss", help="Output path")
    args = parser.parse_args()
    generate_rss_feed(args.out, args.limit)

if __name__ == "__main__":
    main()