#!/usr/bin/python3

import re, sys, requests, urllib.parse
from datetime import datetime

pagestring = "https://en.wikipedia.org/wiki/Wikipedia:Spoken_articles?action=raw"
wikipedia_api_base = "https://en.wikipedia.org/w/api.php" 
commons_api_base = "https://commons.wikimedia.org/w/api.php" 
owner_email = "joe@morris.cloud"
owner_name = "Joseph Morris"
feed_url = "https://wcast.me/"
feed_image = "cover.jpg"
feed_title = "Spoken Wikipedia"
feed_description = f"""These are Wikipedia articles as read by volunteers. It is generated as an RSS feed from the files in the Spoken Wikipedia project. The shownotes list the categories and authors, so you can search on, for example "Philosophy" to see articles in that category, or "thrownfootfalls" to see all recordings by that user. This feed is in beta and has not been tested on many devices.  The best way to provide feedback is by opening an issue at https://github.com/xenotropic/spoken-wikipedia-rss (that's also where the source code is to generate this RSS feed). Alternatively you can email me at joe@morris.cloud"""

def open_text_file(filename):
    try:
        with open(filename, 'r') as f:
            return f.read()
    except FileNotFoundError:
        return ""

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

def file_exists(path): 
    headers={'User-Agent': 'Spoken-wikipedia-rss-bot (https://github.com/xenotropic/spoken-wikipedia-rss/; joe@morris.cloud)'}
    r = requests.head(path,headers=headers)
    if r.status_code == requests.codes.ok:
        return True
    else:
        return False
 
def wiki_parser(text):

# split into headings and body text
    headings = []
    body_texts = []
    body = ""
    first = True
    lines = text.split('\n')
    for line in lines:
        if line.startswith('=='):
            headings.append(line)
            if ( not first ):
                body_texts.append(body)
                body = ""
            first = False
        else:
              body += line
    body_texts.append(body)
# create the dict
    wiki_dict = {}
    for h, b in zip(headings, body_texts):
        wiki_dict[h] = b

# return the dict
    return wiki_dict

rssheader = f"""<?xml version="1.0"?><rss xmlns:atom="http://www.w3.org/2005/Atom" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" xmlns:content="http://purl.org/rss/1.0/modules/content/" version="2.0">
<channel>
<title>{feed_title}</title>
<link>{feed_url}</link>
<language>en</language>
<itunes:category>Education</itunes:category>
<description>{feed_description} </description>
<itunes:image href="{feed_image}"/>
       <itunes:owner>
          <itunes:name>{owner_name}</itunes:name>
        <itunes:email>{owner_email}</itunes:email>
        </itunes:owner>
 """   

print (rssheader)

wikitext = get_web_page ( pagestring )
wikitext = "==" + wikitext.split('==', 1)[1]  # toss the intro text

wikisections = wiki_parser ( wikitext )

section_count = 0
test_start = 0
test_end = 100

apikey = open_text_file ("./apikey.txt")
useragent = 'Spoken-wikipedia-rss-bot (https://github.com/xenotropic/spoken-wikipedia-rss/; joe@morris.cloud)'

if ( len ( apikey) > 0 ): apikey = "Bearer " + apikey

for header, section in wikisections.items():
    section_count+=1
    # clean_section_text = clean_braces_and_comments (section )
    clean_section_text = section
    if ( section_count < test_start ) or ( section_count > test_end ): continue
    print ("* Processing heading: " + header , file=sys.stderr)
    if ( "=See also=" in header  ) or ( "=External links=" in header ): break
    records = re.findall( r'\[\[:File:[^\]]*\]\]', clean_section_text )
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
        # filename_normalized = urllib.parse.quote ( filename )
        # filename_normalized = re.sub(r"&20", "_", filename_normalized )
        filename_normalized = re.sub(r" ", "_", filename )
        filename_normalized = re.sub(r"&", "%26", filename_normalized )
        print("Processing: " + article + " | " + filename_normalized, file=sys.stderr)
        url = f'{commons_api_base}?action=query&titles=File:{filename_normalized}&prop=imageinfo&iiprop=timestamp|url|user|metadata|extmetadata&format=json'
        headers=""
        if ( len ( apikey ) > 0  ):
            headers={'User-Agent': useragent , 'Authorization': apikey }
        response = requests.get(url, headers=headers).json()
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
        if ( bad_listing ):
            print ("********* Error Cannot Process " + filename_normalized + " skipping" , file=sys.stderr)
            continue
        url = f'{wikipedia_api_base}?action=query&prop=extracts&titles={article_normalized}&exsentences=1&explaintext=1&format=json'
        response = requests.get(url, headers=headers).json()

        summary = "No summary available"
        if "pages" in response['query']:
            pages = response['query']['pages']
            for page_id in pages:
                articleid = pages[page_id]
                if 'extract' in articleid:
                    summary = articleid['extract']

        url = f'{wikipedia_api_base}?action=query&prop=pageimages&titles={article_normalized}&format=json&pithumbsize=500'
        response = requests.get(url, headers=headers).json()

        pages = response['query']['pages']
        for page_id in pages:
            articleid = pages[page_id]
            image_url = articleid.get('thumbnail', {}).get('source', '')
        sound_filename = re.findall(r'/([^/]+)$', sound_url)[0] # everything after the last slash
        # doing the substitutions for commons and wikipedia, since most files seem to be transcoded on
        # only one
        sound_url = re.sub(r"org/wikipedia/commons/", "org/wikipedia/commons/transcoded/", sound_url)
        sound_url = re.sub(r"org/wikipedia/en/", "org/wikipedia/en/transcoded/", sound_url)
        sound_url = sound_url + "/" + sound_filename + ".mp3"
        # some files just aren't transcoded yet
        if not file_exists ( sound_url ):
            print ("********* Error Missing File " + sound_url , file=sys.stderr)
            continue
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







