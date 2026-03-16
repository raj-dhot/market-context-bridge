import datetime
import time
import requests
import re
import os
import subprocess
import sys
from bs4 import BeautifulSoup
from duckduckgo_search import DDGS
import trafilatura
from youtube_transcript_api import YouTubeTranscriptApi

# --- DYNAMIC CONFIGURATION ---
PODCAST_FEEDS = {
    "The Loonie Hour (Vancouver/Canada Macro)": "https://www.youtube.com/feeds/videos.xml?channel_id=UCY7S99Xp4I_Vz65_AEPK7Aw",
    "All-In Podcast (Tech/Global Macro)": "https://www.youtube.com/feeds/videos.xml?channel_id=UCESLZhusAkFfsNsApnjF_Cg",
    "Rational Reminder (Canada Investing)": "https://rationalreminder.libsyn.com/rss",
    "The Compound & Friends (US Retail Sentiment)": "https://feeds.megaphone.fm/TCP4771071679"
}

QUERIES = {
    "North America (TSX & S&P 500)": "TSX S&P 500 stock market",
    "International & Emerging": "emerging markets international equities",
    "Competitor & AI Pulse": "Wealthsimple OR Questrade OR AI wealth management news"
}

POLITICAL_KEYWORDS = ["election", "parliament", "trudeau", "biden", "liberal", "conservative"]

# --- UTILITY FUNCTIONS ---
def clean_social_noise(text):
    """Nukes URLs, hashtags, and social handles to keep only the substance."""
    text = re.sub(r'http\S+', '', text) # Remove URLs
    text = re.sub(r'www\S+', '', text)  # Remove web addresses
    text = re.sub(r'[@#]\S+', '', text)  # Remove @handles and #hashtags
    text = re.sub(r'\S+@\S+', '', text) # Remove emails
    text = re.sub(r'\s+', ' ', text)    # Collapse whitespace
    return text.strip()

def get_transcript(url_or_id):
    """Tries to pull actual spoken words if it's a YouTube source."""
    try:
        # Extract ID from various YT URL formats
        video_id = None
        if "v=" in url_or_id: video_id = url_or_id.split("v=")[1].split("&")[0]
        elif "youtu.be/" in url_or_id: video_id = url_or_id.split("/")[-1]
        elif "v/" in url_or_id: video_id = url_or_id.split("v/")[-1].split("?")[0]
        
        if video_id:
            transcript = YouTubeTranscriptApi.get_transcript(video_id)
            full_text = " ".join([t['text'] for t in transcript])
            return clean_social_noise(full_text)
    except:
        return None
    return None

def fetch_content():
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    output = f"OCEANFRONT DEEP INTELLIGENCE - {today}\n" + "="*50 + "\n\n"
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}

    # 1. NEWS SCRAPER (Unchanged but efficient)
    ddgs = DDGS()
    for cat, query in QUERIES.items():
        output += f"### {cat.upper()} ###\n"
        try:
            results = ddgs.news(query, max_results=5)
            valid = 0
            for res in results:
                if valid >= 2: break
                downloaded = trafilatura.fetch_url(res['url'])
                body = trafilatura.extract(downloaded) if downloaded else None
                if body and len(body) > 400:
                    output += f"TITLE: {res['title']}\nCONTENT: {body[:2000]}...\n\n"
                    valid += 1
        except: pass

    # 2. PODCAST/SENTIMENT ENGINE (The Upgrade)
    output += "### SOCIAL & PODCAST TRANSCRIPTS ###\n"
    for name, url in PODCAST_FEEDS.items():
        try:
            resp = requests.get(url, headers=headers, timeout=15)
            soup = BeautifulSoup(resp.content, 'xml')
            item = soup.find('item') or soup.find('entry')
            
            if item:
                title = item.find('title').text
                link = item.find('link').get('href') if item.find('link').get('href') else item.find('link').text
                
                # Try transcript first, then fall back to cleaned show notes
                content = get_transcript(link)
                if not content:
                    raw_desc = item.find('description') or item.find('media:description')
                    content = clean_social_noise(raw_desc.text) if raw_desc else "No data available."
                
                output += f"SHOW: {name}\nEPISODE: {title}\nDATA:\n{content[:4000]}...\n"
                output += "-"*50 + "\n\n"
        except Exception as e:
            output += f"Error fetching {name}: {str(e)}\n\n"

    with open("latest_news.txt", "w", encoding="utf-8") as f:
        f.write(output)

if __name__ == "__main__":
    fetch_content()
