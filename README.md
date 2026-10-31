# Makes A Podcast Feed From The Spoken Wikipedia Project 
This repo has a python script that generates an RSS podcast feed out of the Spoken Wikipedia project files, using [this page](https://en.wikipedia.org/wiki/Wikipedia:Spoken_articles) as the data source. The generatepod.py script creates (to stdout) an RSS feed of the Spoken Wikimedia Project files. There are a few recorded files that currently don't make it -- multiparts, perhaps others. 

Originally I wrote this as a scraping script (in the legacy folder) but then refactored it using Gemini in order to be a MariaDB application that can run natively on Wikipedia's ToolForge. It runs there on a weekly basis and you can get the feeds there: https://sw.toolforge.org/ 

It should work as a standard RSS feed; most robust podcast apps will accept a RSS URL. So far I've only tested it in AntennaPod (Android only). Testing and feedback on other players is appreciated. If you have issues with a podcast app that is fairly common/widespread, please open an issue. 
