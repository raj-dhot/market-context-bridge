import datetime
import time
import urllib.request
import xml.etree.ElementTree as ET
import re
from duckduckgo_search import DDGS
import trafilatura

# Direct search queries for news
QUERIES = {
    "North America (TSX & S&P 500)": "TSX OR S&P 500 stock market news",
    "International & Emerging (XEF/XEC)": "emerging markets OR international equities market news",
    "Competitor & AI Pulse": "Wealthsimple OR Questrade OR AI wealth management news"
}

# Top retail sentiment podcasts (Global + Vancouver/Canada specific)
PODCAST_FEEDS = {
    "The Loonie Hour (Vancouver Real Estate & Canadian Macro)": "https://feeds.buzzsprout.com/1879571.rss",
    "Rational Reminder (Canadian Retail & Evidence-Based Investing)": "https://rationalreminder.libsyn.com/rss",
    "The Compound & Friends (US/Global Retail Sentiment)": "https://feeds.megaphone.fm/thecompound",
    "All-In Podcast (Tech/Macro/VC Disruption)": "https://anchor.fm/s/2b0af938/podcast/rss"
}

# Strict filter to keep the context clean
POLITICAL_KEYWORDS = [
    "election", "parliament", "congress", "trudeau", "biden", 
    "conservative", "liberal", "democrat", "republican", "campaign", "senate"
]

def is_political(text):
    if not text: return False
    return any(keyword in text.lower() for keyword in POLITICAL_KEYWORDS)

def fetch_podcasts():
    output = "### SOCIAL & PODCAST SENTIMENT ###\n"
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    
    for name, url in PODCAST_FEEDS.items():
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as response:
                xml_data = response.read()
            
            root = ET.fromstring(xml_data)
            channel = root.find('channel')
            if channel is not None:
                # Grab the latest episode
                item = channel.find('item')
                if item is not None:
                    title = item.find('title')
                    title_text = title.text if title is not None else 'No Title'
                    
                    desc = item.find('description')
                    desc_text = desc.text if desc is not None else 'No Description'
                    
                    # Clean out the HTML tags to save tokens
                    clean_desc = re.sub('<[^<]+>', ' ', desc_text)
                    clean_desc = " ".join(clean_desc.split())
                    
                    output += f"SHOW: {name}\n"
                    output += f"LATEST EPISODE: {title_text}\n"
                    output += f"SHOW NOTES (SENTIMENT FUEL):\n{clean_desc[:1200]}...\n"
                    output += "-"*50 + "\n\n"
        except Exception as e:
            output += f"Error fetching {name}: {str(e)}\n\n"
            
    return output

def fetch_intelligence():
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    output = f"OCEANFRONT RAW INTELLIGENCE - {today}\n"
    output += "="*50 + "\n\n"
    
    # 1. Fetch News via DuckDuckGo & Trafilatura
    ddgs = DDGS()
    for category, query in QUERIES.items():
        output += f"### {category.upper()} ###\n"
        try:
            # Increased to 8 max results to find more quality hits
            results = ddgs.news(query, max_results=8)
            valid_articles = 0
            
            for res in results:
                if valid_articles >= 3: # Pulling up to 3 articles now
                    break
                    
                title = res.get('title', '')
                url = res.get('url', '')
                
                if is_political(title):
                    continue
                
                downloaded = trafilatura.fetch_url(url)
                if not downloaded:
                    continue
                    
                body = trafilatura.extract(downloaded, include_comments=False, include_tables=False)
                
                if not body or len(body) < 300 or is_political(body):
                    continue
                    
                output += f"TITLE: {title}\n"
                output += f"SOURCE: {res.get('source', 'Unknown')}\n"
                output += f"CONTENT:\n{body[:2500]}...\n"
                output += "-"*50 + "\n\n"
                
                valid_articles += 1
                time.sleep(1)
                
            if valid_articles == 0:
                output += "No readable, non-political articles successfully extracted today.\n\n"
                
        except Exception as e:
            output += f"Error processing category: {str(e)}\n\n"

    # 2. Fetch Podcast Sentiment
    output += fetch_podcasts()

    with open("latest_news.txt", "w", encoding="utf-8") as f:
        f.write(output)

if __name__ == "__main__":
    fetch_intelligence()
