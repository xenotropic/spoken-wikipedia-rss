#!/usr/bin/python3

import re, sys, requests, urllib.parse
from datetime import datetime

def get_web_page(url):
    page = requests.get(url)
    return page.text

def clean_braces_and_comments (string):
    string = re.sub(r"{.*}", "", string)
    string = re.sub(r"<!--.*-->", "", string)
    return string

def clean_header (string):
    string = re.sub(r"=*", "", string )
    return string

def wiki_parser(text):

# split into headings and body text
    headings = []
    body_texts = []
    lines = text.split('\n')
    for line in lines:
        if line.startswith('=='):
            headings.append(line)
        else:
            body_texts.append(line)

# create the dict
    wiki_dict = {}
    for h, b in zip(headings, body_texts):
        wiki_dict[h] = b

# return the dict
    return wiki_dict

rssheader = """<?xml version="1.0"?><rss xmlns:atom="http://www.w3.org/2005/Atom" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" xmlns:content="http://purl.org/rss/1.0/modules/content/" version="2.0">
<channel>
<title>Spoken Wikipedia</title>
<link>https://morris.cloud/spoken-wikipedia/</link>
<description>These are Wikipedia articles as read by volunteers. It is generated as an RSS feed from the files in the Spoken Wikipedia project. The shownotes list the categories and authors, so you can search on, for example "Philosophy" to see articles in that category, or "thrownfootfalls" to see all recordings by that user. This feed uses the files hosted on Wikipedia, which are usually in ogg format, or in some rare cases wav. These ogg files typically do not work on Apple devices, certainly not iTunes. If you get it working on an iOS devices let me know. On Android, this feed is tested and working with Antennapod, should work with most other podcast apps (since Android includes ogg support at a system level). The best way to provide feedback is by opening an issue at https://github.com/xenotropic/spoken-wikipedia-rss (that's also where the source code is to generate this RSS feed). Alternatively you can email me at joe@morris.cloud </description>
<itunes:image href="https://morris.cloud/spokenwikipedia/cover.png"/>
 """   

print (rssheader)

basestring = "https://en.wikipedia.org/wiki/Wikipedia:Spoken_articles?action=raw"
wikitext = get_web_page ( basestring )
wikitext = wikitext.split('==', 1)[1] # toss the header
wikisections = wiki_parser ( wikitext )

section_count = 0
for header, sectiontext in wikisections.items():
    wikitext = clean_braces_and_comments (wikitext)
    print ( str ( section_count) +" "+ header ,  file=sys.stderr )
    if ( "=See also=" in header  ) or ( "=External links=" in header ): break
    # if section_count > 5: continue # for testing
    records = re.findall( r'\[\[:File:[^\]]*\]\]', wikitext)
    heading_count = 0
    output = []
    article_count = 0
    for record in records:
        article_count+=1
        items = record.split ("|")
        article = items [1][:-2]
        article = re.sub('\'\'', '', article)
        if (article[:5] == "(Part" ): continue # skipping the multiparts; there are few, they are mostly old, formatting is a special case 
        article_normalized = re.sub('&', '&amp;', article)
        filename = items [0][8:]
        filename_normalized = urllib.parse.quote ( filename )
        print("Processing: " + article + " | " + filename, file=sys.stderr)
        url = f'https://en.wikipedia.org/w/api.php?action=query&titles=File:{filename_normalized}&prop=imageinfo&iiprop=timestamp|url|user|metadata|extmetadata&format=json'
        response = requests.get(url).json()
        pages = response['query']['pages']
        bad_listing = True
        for page_id in pages:
            if "imageinfo" not in pages[page_id] : break 
            bad_listing = False
            image_info = pages[page_id]['imageinfo'][0]
            sound_url = image_info['url']
            author = image_info['user']
            filedate = image_info['timestamp']
            human_filedate = datetime.strptime(filedate, "%Y-%m-%dT%H:%M:%S%z").strftime("%B %d, %Y")
            metadata = image_info['metadata']
            extmetadata = image_info['extmetadata']
            human_license = "Unknown"
            if "LicenseShortName" in extmetadata:
                human_license = extmetadata["LicenseShortName"]["value"]
        if ( bad_listing ): continue
        url = f'https://en.wikipedia.org/w/api.php?action=query&prop=extracts&titles={article_normalized}&exsentences=1&explaintext=1&format=json'
        response = requests.get(url).json()
        
        pages = response['query']['pages']
        for page_id in pages:
            articleid = pages[page_id]
            if 'extract' in articleid:
                summary = articleid['extract']
            else: summary = "No summary available"

        url = f'https://en.wikipedia.org/w/api.php?action=query&prop=pageimages&titles={article_normalized}&format=json&pithumbsize=500'
        response = requests.get(url).json()

        pages = response['query']['pages']
        for page_id in pages:
            articleid = pages[page_id]
            image_url = articleid.get('thumbnail', {}).get('source', '')

        print ( "<item><title>" + article_normalized + "</title><link>" + sound_url + "</link>" )
        print ( "<enclosure url=\"" + sound_url + "\" />")
        print ( "<guid>" + sound_url + "</guid>" )
        print ( "<pubDate>" + filedate + "</pubDate>" )
        human_header = clean_header (header);
        print ( f"<description><![CDATA[ <p>{summary} <p> This is spoken word version of the Wikipedia article \"{article}\" recorded on {human_filedate} <p>Category: {human_header} <p>Read by: {author} <p>License:{human_license} ]]></description>" )
        print ( "<itunes:image href=\""+image_url+"\" />" )
        if ( "Human sexuality" in header ): print ( "<itunes:explicit>yes</itunes:explicit>" ) 
        print (" </item>" )


print ( " </channel> </rss> " )







