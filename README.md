# Makes A Podcast Feed From The Spoken Wikipedia Project 
This repo has a python script that generates an RSS podcast feed out of the Spoken Wikipedia project files, using [this page](https://en.wikipedia.org/wiki/Wikipedia:Spoken_articles) as the data source. The generatepod.py script creates (to stdout) an RSS feed of the Spoken Wikimedia Project files. There are a few recorded files that currently don't make it -- some have not been transcoded from ogg to mp3, and a few I think I have some special characters that aren't being handled right in this script.  

A copy of the generated feed is at https://wcast.me/sw

It should work as a standard RSS feed in [any podcast app that will let you enter one](https://support.wondery.com/hc/en-us/articles/4405811301531-How-to-manually-add-an-RSS-feed-to-popular-podcast-apps-). So far I've only tested it in AntennaPod and Pocket Casts (Android). 

# To open the Spoken Wikimedia Project in AntennaPod
(0) Download (if necessary) and open AntennaPod (1) tap the three-line menu in the top left, (2) tap "Add Podcast", (3) look down under "Advanced" and tap on "Add Podcast by RSS address" and (4) paste or type in https://morris.cloud/spokenwikipedia/

It's a big podcast -- 1600+ "episodes" as of Feb 2023 -- so it will take a few seconds to open up. Click "subscribe" and you should be on your way. 
My next plan is to add to the script, or make another one, to download and convert the files to mp3, which be a more "normal" podcast that would work for most users and could be submitted to podcast feed listings. 
