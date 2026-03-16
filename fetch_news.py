import datetime
import time
import re
import os
import subprocess
import sys

# --- AUTO-INSTALLER (The Nuclear Option) ---
def install_and_import(package, import_name=None):
    if import_name is None: import_name = package
    try:
        __import__(import_name)
    except ImportError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", package])

# Force-check the essentials
install_and_import('requests')
install_and_import('beautifulsoup4', 'bs4')
install_and_import('trafilatura')
install_and_import('duckduckgo_search')
install_and_import('lxml')

from bs4 import BeautifulSoup
from duckduckgo_search import DDGS
import trafilatura
import requests

# --- CONFIGURATION ---
QUERIES = {
    "North America (TSX & S&P 500)": "TSX S&P 500 stock market",
    "International & Emerging (XEF/XEC)": "emerging markets international equities stock market",
    "Competitor & AI Pulse": "Wealthsimple Questrade AI wealth management news"
}

PODCAST_FEEDS = {
    "The Loonie Hour (Vancouver Real Estate & Canadian Macro)": "https://anchor.fm/s/103db19ac/podcast/rss",
    "Rational Reminder (Canadian Retail & Evidence-Based Investing)": "https://rationalreminder.libsyn.com/rss",
    "The Compound & Friends (US/Global Retail Sentiment)": "https://feeds.megaphone.fm/TCP4771071679",
    "All-In Podcast (Tech/Macro/VC Disruption)": "https://www.youtube.com/feeds/videos.xml?channel_id=UCESLZhusAkFfsNsApnjF_Cg"
}

POLITICAL_KEYWORDS = ["election", "parliament", "congress", "trudeau", "biden", "conservative", "liberal", "democrat", "republican"]

# --- FUNCTIONS ---
def is_political(text):
    if not text: return False
    return any(kw in text.lower() for kw in POLITICAL_KEYWORDS)

def fetch_podcasts():
    output = "### SOCIAL & PODCAST SENTIMENT ###\n"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
        'Accept': 'application/rss+xml, application/xml, text/xml, */*'
    }
    
    for name, url in PODCAST_FEEDS.items():
        try:
            resp = requests.get(url, headers=headers, timeout=15)
            # Use 'lxml-xml' specifically for speed and accuracy in GitHub Actions
            soup = BeautifulSoup(resp.content, 'xml')
            item = soup.find('item') or soup.find('entry')
            
            if item:
                title = item.find('title').text if item.find('title') else 'No Title'
                desc = item.find('media:description') or item.find('description')
                desc_text = desc.text if desc else 'No Description'
                
                clean_desc = re.sub('<[^<]+>', ' ', desc_text)
                clean_desc = " ".join(clean_desc.split())
                
                output += f"SHOW: {name}\nLATEST EPISODE: {title}\nSENTIMENT FUEL:\n{clean_desc[:1200]}...\n"
                output += "-"*50 + "\n\n"
        except Exception as e:
            output += f"Error fetching {name}: {str(e)}\n\n"
    return output

def fetch_intelligence():
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    output = f"OCEANFRONT RAW INTELLIGENCE - {today}\n" + "="*50 + "\n\n"
    
    ddgs = DDGS()
    for cat, query in QUERIES.items():
        output += f"### {cat.upper()} ###\n"
        try:
            results = ddgs.news(query, max_results=8)
            valid = 0
            for res in results:
                if valid >= 3: break
                url = res.get('url', '')
                if is_political(res.get('title', '')): continue
                
                downloaded = trafilatura.fetch_url(url)
                if not downloaded: continue
                body = trafilatura.extract(downloaded, include_comments=False)
                
                if not body or len(body) < 300 or is_political(body): continue
                
                output += f"TITLE: {res.get('title')}\nSOURCE: {res.get('source')}\nCONTENT:\n{body[:2500]}...\n"
                output += "-"*50 + "\n\n"
                valid += 1
                time.sleep(1)
        except Exception as e:
            output += f"Error: {str(e)}\n\n"

    output += fetch_podcasts()
    with open("latest_news.txt", "w", encoding="utf-8") as f:
        f.write(output)

if __name__ == "__main__":
    fetch_intelligence()
