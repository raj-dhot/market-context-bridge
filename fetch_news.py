import requests
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup
import datetime
import time

# Broadened queries to ensure a healthy volume of articles are returned
FEEDS = {
    "North America (TSX & S&P 500)": "https://news.google.com/rss/search?q=TSX+OR+%22S%26P+500%22+market&hl=en-CA&gl=CA&ceid=CA:en",
    "International & Emerging (XEF/XEC)": "https://news.google.com/rss/search?q=%22emerging+markets%22+OR+%22international+equities%22&hl=en-CA&gl=CA&ceid=CA:en",
    "Competitor & AI Pulse": "https://news.google.com/rss/search?q=Wealthsimple+OR+Questrade+OR+%22AI+wealth%22&hl=en-CA&gl=CA&ceid=CA:en"
}

# Strict filter to exclude political noise from the agent's context window
POLITICAL_KEYWORDS = ["election", "parliament", "congress", "trudeau", "biden", "conservative", "liberal", "democrat", "republican"]

def is_political(text):
    return any(keyword in text.lower() for keyword in POLITICAL_KEYWORDS)

def extract_article_body(google_url):
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    try:
        # Step 1: Follow Google's redirect maze
        session = requests.Session()
        resp = session.get(google_url, headers=headers, timeout=15, allow_redirects=True)
        
        # Intercept JS/Meta redirects if Google tries to hide the real URL
        soup = BeautifulSoup(resp.text, 'html.parser')
        meta = soup.find('meta', attrs={'http-equiv': 'refresh'})
        if meta and 'url=' in meta.get('content', '').lower():
            real_url = meta.get('content').split('url=')[-1].strip("'\"")
            resp = session.get(real_url, headers=headers, timeout=15)
            soup = BeautifulSoup(resp.text, 'html.parser')
            
        # Step 2: Extract the actual reading material
        paragraphs = soup.find_all('p')
        body_text = " ".join([p.get_text() for p in paragraphs])
        body_text = " ".join(body_text.split())
        
        # If we hit a hard paywall, return None so the loop knows to skip to the next article
        if len(body_text) < 150:
            return None 
            
        # Truncate to save your Claude agent's context window
        if len(body_text) > 2500:
            return body_text[:2500] + "... [Truncated for AI context limit]"
            
        return body_text
    except Exception:
        return None

def build_briefing_data():
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    output = f"RAW INTELLIGENCE DATA - {today}\n"
    output += "="*50 + "\n\n"
    
    # We MUST identify as a browser here, or Google News returns an empty feed
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}

    for category, rss_url in FEEDS.items():
        output += f"### {category.upper()} ###\n"
        try:
            response = requests.get(rss_url, headers=headers, timeout=10)
            root = ET.fromstring(response.content)
            
            valid_articles_found = 0
            
            for item in root.findall('.//item'):
                if valid_articles_found >= 2: # Stop once we have 2 high-quality articles
                    break
                    
                title = item.find('title').text
                link = item.find('link').text
                
                if is_political(title):
                    continue
                    
                body = extract_article_body(link)
                
                # If extraction failed (paywall) or the article body turned political, skip it
                if not body or is_political(body):
                    continue 
                
                output += f"TITLE: {title}\n"
                output += f"CONTENT:\n{body}\n"
                output += "-"*40 + "\n\n"
                valid_articles_found += 1
                
                time.sleep(1) # Polite delay
                
            # If every article was a dud or political, log it so the file isn't mysteriously empty
            if valid_articles_found == 0:
                output += "No accessible, non-political articles found for this category today.\n\n"
                
        except Exception as e:
            output += f"Error fetching feed: {str(e)}\n\n"

    with open("latest_news.txt", "w", encoding="utf-8") as f:
        f.write(output)

if __name__ == "__main__":
    build_briefing_data()
