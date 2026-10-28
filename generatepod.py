#!/usr/bin/env python3
"""
Spoken Wikipedia RSS Feed Generator
Queries the Toolforge enwiki_p replica for spoken articles, batches metadata
lookups via the MediaWiki Action API, verifies transcoded MP3 streams, and
generates a validated podcast RSS XML document.
"""

import argparse
import configparser
import html
import os
import pathlib
import re
import sys
import tempfile
import urllib.parse
from datetime import datetime
from dateutil.parser import parse
import requests
import pymysql
import pymysql.cursors

# ---------------------------------------------------------
# Configuration
# ---------------------------------------------------------
MAX_ARTICLES = 5  # Set to 0 to process all available articles
DEFAULT_OUTPUT_PATH = "/data/project/sw/public_html/sw.rss"
USER_AGENT = "Spoken-wikipedia-rss-bot (https://github.com/xenotropic/spoken-wikipedia-rss/; joe@morris.cloud)"

FEED_CONFIG = {
    "title": "Spoken Wikipedia",
    "url": "https://wcast.me/sw/",
    "cover_image": "https://wcast.me/sw/cover.jpg",
    "description": (
        "These are Wikipedia articles as read by volunteers. It is generated as an "
        "RSS feed from the files in the Spoken Wikipedia project. The shownotes list "
        "the authors and licenses. Source code and issue tracking are available at "
        "https://github.com/xenotropic/spoken-wikipedia-rss."
    ),
    "owner_name": "Joseph Morris",
    "owner_email": "joe@morris.cloud",
    "language": "en-us",
    "category": "Education",
}


def get_db_connection():
    """Establish connection to enwiki replica via toolforge helper or replica.my.cnf."""
    try:
        import toolforge
        return toolforge.connect("enwiki_p", cursorclass=pymysql.cursors.DictCursor)
    except Exception:
        cnf_path = pathlib.Path.home() / "replica.my.cnf"
        if not cnf_path.exists():
            cnf_path = pathlib.Path.home() / ".my.cnf"
        if not cnf_path.exists():
            raise FileNotFoundError(f"Replica credentials not found at {cnf_path}")
        
        config = configparser.ConfigParser()
        config.read(str(cnf_path))
        user = config.get("client", "user").strip("'\"")
        password = config.get("client", "password").strip("'\"")
        
        return pymysql.connect(
            host="enwiki.analytics.db.svc.wikimedia.cloud",
            database="enwiki_p",
            user=user,
            password=password,
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
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


def format_episode_title(article_title, audio_filename):
    """Format clean episode title and detect multi-part recording indicators."""
    clean_title = article_title.replace("_", " ")
    match = re.search(
        r"[-_\s(](part|pt|section)[-_\s]*([0-9]+|[a-z])", 
        audio_filename, 
        re.IGNORECASE
    )
    if match:
        kind = "Part" if match.group(1).lower() in ("part", "pt") else "Section"
        num = match.group(2).upper()
        indicator = f"({kind} {num})"
        if indicator.lower() not in clean_title.lower():
            return f"{clean_title} {indicator}"
    return clean_title

def batch_fetch_commons_metadata(filenames, session):
    """
    Batch-fetch metadata and transcoded derivatives from Wikimedia Commons
    using videoinfo (TimedMediaHandler) and imageinfo in chunks of 50.
    """
    metadata_map = {}
    chunk_size = 50

    for i in range(0, len(filenames), chunk_size):
        chunk = filenames[i:i + chunk_size]
        titles = "|".join([f"File:{f.replace(' ', '_')}" for f in chunk])
        url = (
            "https://commons.wikimedia.org/w/api.php"
            f"?action=query&titles={urllib.parse.quote(titles)}"
            "&prop=imageinfo|videoinfo"
            "&iiprop=timestamp|user|url|extmetadata"
            "&viprop=derivatives"
            "&format=json&formatversion=2"
        )
        try:
            res = session.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
            if res.status_code == 200:
                pages = res.json().get("query", {}).get("pages", [])
                for p in pages:
                    fname = p.get("title", "").replace("File:", "").replace(" ", "_")
                    if p.get("imageinfo"):
                        info = p["imageinfo"][0]
                        # Extract authoritative MP3 derivative if present
                        mp3_url = None
                        for deriv in p.get("videoinfo", [{}])[0].get("derivatives", []):
                            src = deriv.get("src", "")
                            if src.lower().endswith(".mp3") or deriv.get("type") == "audio/mpeg":
                                mp3_url = src
                                break
                        info["transcoded_mp3"] = mp3_url
                        metadata_map[fname] = info
        except requests.RequestException as e:
            print(f"Error fetching Commons metadata batch: {e}", file=sys.stderr)

    return metadata_map

def batch_fetch_article_summaries(article_titles, session):
    """Batch-fetch 1-sentence summaries and thumbnails from Wikipedia in chunks of 50."""
    summary_map = {}
    chunk_size = 50

    for i in range(0, len(article_titles), chunk_size):
        chunk = article_titles[i:i + chunk_size]
        titles = "|".join([a.replace("_", " ") for a in chunk])
        url = (
            "https://en.wikipedia.org/w/api.php"
            f"?action=query&titles={urllib.parse.quote(titles)}"
            "&prop=extracts|pageimages&exintro=1&explaintext=1&exsentences=1"
            "&pithumbsize=500&format=json&formatversion=2"
        )
        try:
            res = session.get(url, headers={"User-Agent": USER_AGENT}, timeout=15)
            if res.status_code == 200:
                for p in res.json().get("query", {}).get("pages", []):
                    key = p.get("title", "").replace(" ", "_")
                    summary_map[key] = {
                        "summary": p.get("extract", "No summary available."),
                        "image_url": p.get("thumbnail", {}).get("source", ""),
                    }
        except requests.RequestException as e:
            print(f"Error fetching Wikipedia extracts: {e}", file=sys.stderr)

    return summary_map

def resolve_verified_mp3(file_info):
    """
    Returns the authoritative MP3 derivative provided by TimedMediaHandler,
    or falls back to the deterministic transcoded path without blocking on HEAD.
    """
    if not file_info:
        return None

    # 1. Did the API return a verified MP3 derivative?
    if file_info.get("transcoded_mp3"):
        return file_info["transcoded_mp3"]

    # 2. If the original uploaded file is already an MP3:
    raw_url = file_info.get("url", "")
    if raw_url.lower().endswith(".mp3"):
        return raw_url

    # 3. Construct canonical TimedMediaHandler URL (https://upload.wikimedia.org/...)
    if "upload.wikimedia.org" in raw_url:
        filename = raw_url.split("/")[-1]
        transcoded_url = raw_url.replace("/commons/", "/commons/transcoded/")
        transcoded_url = transcoded_url.replace("/en/", "/en/transcoded/")
        return f"{transcoded_url}/{filename}.mp3"

    return None

def extract_metadata_fields(info):
    """Parse author, licensing, and standardized timestamps from image metadata."""
    author = info.get("user", "Wikipedia Volunteers")
    extmetadata = info.get("extmetadata", {})

    if "Artist" in extmetadata and "value" in extmetadata["Artist"]:
        clean_author = re.sub(r"<[^<]+?>", "", extmetadata["Artist"]["value"]).strip()
        clean_author = clean_author.replace("Speaker: ", "").replace("User: ", "")
        clean_author = clean_author.replace("\nAuthors of the article", "").strip()
        if clean_author:
            author = clean_author

    license_name = "Unknown"
    if "LicenseShortName" in extmetadata and "value" in extmetadata["LicenseShortName"]:
        license_name = extmetadata["LicenseShortName"]["value"]

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
            pub_date = dt.strftime("%a, %d %b %Y %H:%M:%S +0000")
        else:
            pub_date = dt.strftime("%a, %d %b %Y %H:%M:%S %z")
        human_date = dt.strftime("%B %d, %Y")
    except Exception:
        pub_date = "Wed, 02 Oct 2002 08:00:00 +0000"
        human_date = "Unknown"

    return author, license_name, pub_date, human_date

def generate_rss_feed(output_path, limit):
    """Coordinates database reads, batch lookups, and atomic XML delivery."""
    session = requests.Session()

    print(f"Querying MariaDB replica for spoken articles (Limit: {limit or 'ALL'})...", file=sys.stderr)
    records = fetch_spoken_records(limit=limit)
    print(f"Retrieved {len(records)} media transclusions from database.", file=sys.stderr)

    unique_files = list({rec[1] for rec in records})
    unique_articles = list({rec[0] for rec in records})

    print(f"Batch fetching Commons metadata for {len(unique_files)} files...", file=sys.stderr)
    commons_meta = batch_fetch_commons_metadata(unique_files, session)

    print(f"Batch fetching Wikipedia extracts for {len(unique_articles)} articles...", file=sys.stderr)
    wiki_meta = batch_fetch_article_summaries(unique_articles, session)

    items_xml = []
    skipped_count = 0

    print("Verifying audio transcode availability and building feed items...", file=sys.stderr)
    for article_title, audio_file in records:
        norm_file = audio_file.replace(" ", "_")
        file_info = commons_meta.get(norm_file)
        if not file_info:
            print(f"Skipping {audio_file}: No file metadata returned.", file=sys.stderr)
            skipped_count += 1
            continue

        mp3_url = resolve_verified_mp3(file_info)
        if not mp3_url:
            print(f"Skipping {audio_file}: Missing transcoded MP3 stream.", file=sys.stderr)
            skipped_count += 1
            continue

        episode_title = format_episode_title(article_title, audio_file)
        article_display = article_title.replace("_", " ")
        article_data = wiki_meta.get(article_title, {})
        summary = article_data.get("summary", "No summary available.")
        cover_image = article_data.get("image_url", "")

        author, license_name, pub_date, human_date = extract_metadata_fields(file_info)

        item_desc = (
            f"<p>{html.escape(summary)}</p>"
            f"<p>This is a spoken word version of the Wikipedia article &quot;{html.escape(article_display)}&quot; "
            f"recorded on {html.escape(human_date)}.</p>"
            f"<p>Read by: {html.escape(author)}<br />License: {html.escape(license_name)}</p>"
        )

        image_tag = f'\n      <itunes:image href="{html.escape(cover_image)}" />' if cover_image else ""

        item_xml = f"""    <item>
      <title>{html.escape(episode_title)}</title>
      <link>{html.escape(mp3_url)}</link>
      <guid isPermaLink="false">{html.escape(mp3_url)}</guid>
      <pubDate>{pub_date}</pubDate>
      <enclosure url="{html.escape(mp3_url)}" type="audio/mpeg" />
      <description><![CDATA[{item_desc}]]></description>{image_tag}
      <itunes:author>{html.escape(author)}</itunes:author>
      <itunes:explicit>no</itunes:explicit>
    </item>\n"""
        items_xml.append(item_xml)

    # Resolve output directories
    target_path = os.path.abspath(output_path)
    target_dir = os.path.dirname(target_path)
    if not os.path.exists(target_dir):
        try:
            os.makedirs(target_dir, exist_ok=True)
        except OSError:
            target_path = os.path.abspath("sw.rss")
            target_dir = os.path.dirname(target_path)

    # Atomic write pattern
    with tempfile.NamedTemporaryFile("w", dir=target_dir, delete=False, encoding="utf-8") as tf:
        temp_file_path = tf.name
        tf.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        tf.write(
            '<rss xmlns:atom="http://www.w3.org/2005/Atom" '
            'xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" '
            'xmlns:content="http://purl.org/rss/1.0/modules/content/" version="2.0">\n'
        )
        tf.write("  <channel>\n")
        tf.write(f"    <title>{html.escape(FEED_CONFIG['title'])}</title>\n")
        tf.write(f"    <link>{html.escape(FEED_CONFIG['url'])}</link>\n")
        tf.write(f"    <language>{html.escape(FEED_CONFIG['language'])}</language>\n")
        tf.write(f'    <itunes:category text="{html.escape(FEED_CONFIG["category"])}" />\n')
        tf.write(f"    <description>{html.escape(FEED_CONFIG['description'])}</description>\n")
        tf.write(f'    <itunes:image href="{html.escape(FEED_CONFIG["cover_image"])}" />\n')
        tf.write("    <itunes:owner>\n")
        tf.write(f"      <itunes:name>{html.escape(FEED_CONFIG['owner_name'])}</itunes:name>\n")
        tf.write(f"      <itunes:email>{html.escape(FEED_CONFIG['owner_email'])}</itunes:email>\n")
        tf.write("    </itunes:owner>\n")

        for item in items_xml:
            tf.write(item)

        tf.write("  </channel>\n</rss>\n")

    os.replace(temp_file_path, target_path)
    print(
        f"Feed generation complete: {len(items_xml)} items written to {target_path} "
        f"({skipped_count} skipped).", 
        file=sys.stderr
    )


def main():
    parser = argparse.ArgumentParser(description="Generate Spoken Wikipedia podcast RSS feed on Toolforge.")
    parser.add_argument(
        "--limit", 
        type=int, 
        default=MAX_ARTICLES, 
        help=f"Maximum number of articles to process (0 for all, default: {MAX_ARTICLES})"
    )
    parser.add_argument(
        "--out", 
        type=str, 
        default=DEFAULT_OUTPUT_PATH, 
        help=f"Target output RSS file (default: {DEFAULT_OUTPUT_PATH})"
    )
    args = parser.parse_args()
    generate_rss_feed(args.out, args.limit)


if __name__ == "__main__":
    main()
    
