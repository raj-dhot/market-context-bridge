import requests
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup
import datetime
import time

# Target areas aligned with the firm's investment geographies and competitor pulse
FEEDS = {
    "TSX & S&P 500 (North America)": "https://news.google.com/rss/search?q=TSX+OR+%22S%26P+500%22+stock+market+retail+investors&hl=en-CA&gl=CA&ceid=CA:en",
    "XEF & XEC (International & Emerging)": "https://news.google.com/rss/search?q=international+developed+OR+emerging+markets+equities&hl=en-CA&gl=CA&ceid=CA:en",
    "Competitor & AI Pulse": "https://news.google.com/rss/search?q=Wealthsimple+OR+Questrade+OR+%22AI+wealth+management%22&hl=en-CA&gl=CA&ceid=CA:en"
}

# Strict filter to exclude political noise from the agent's context window
POLITICAL_KEYWORDS = [
    "election", "parliament", "congress", "trudeau", "biden", 
    "conservative", "liberal", "democrat", "republican", "campaign", "senate"
]

def is_political(text):
    return any(keyword in text.lower() for keyword in POLITICAL_KEYWORDS)

def get_real_url(google_url):
    """Intercepts the Google News redirect to find the actual publisher URL."""
    try:
        response = requests.get(google_url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
        soup = BeautifulSoup(response.text, 'html.parser')
        # Google often hides the real URL in a meta refresh tag
        meta = soup.find('meta', attrs={'http-equiv': 'refresh'})
        if meta:
            content = meta.get('content', '')
            if 'url=' in content.lower():
                return content.split('url=')[-1].strip("'\"")
        return response.url
    except:
        return google_url

def extract_article_body(url):
    try:
        real_url = get_real_url(url)
        # Using a highly specific User-Agent to avoid publisher bot-blocks
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
        response = requests.get(real_url, headers=headers, timeout=10)
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Extract text from paragraphs
        paragraphs = soup.find_all('p')
        body_text = " ".join([p.get_text() for p in paragraphs])
        body_text = " ".join(body_text.split()) # Clean up extra whitespace
        
        if len(body_text) < 150:
            return "No readable paragraph content found. Site may be protected by a strict paywall."
            
        # Truncate to save agent context window space
        if len(body_text) > 3000:
            return body_text[:3000] + "... [Truncated]"
            
        return body_text
    except Exception as e:
        return f"Extraction failed: {str(e)}"

def build_briefing_data():
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    output = f"RAW INTELLIGENCE DATA - {today}\n"
    output += "="*50 + "\n\n"

    for category, rss_url in FEEDS.items():
        output += f"### {category.upper()} ###\n"
        try:
            response = requests.get(rss_url, timeout=10)
            root = ET.fromstring(response.content)
            
            # Extract the top 3 highly relevant, non-political articles per category
            count = 0
            for item in root.findall('.//item'):
                if count >= 3:
                    break
                    
                title = item.find('title').text
                link = item.find('link').text
                
                if is_political(title):
                    continue
                    
                body = extract_article_body(link)
                
                # Skip articles that are political or blocked by heavy paywalls
                if is_political(body) or "No readable paragraph" in body:
                    continue 
                
                output += f"TITLE: {title}\n"
                output += f"LINK: {link}\n"
                output += f"CONTENT:\n{body}\n"
                output += "-"*40 + "\n\n"
                
                count += 1
                time.sleep(2) # Polite delay to prevent rate-limiting
                
        except Exception as e:
            output += f"Error fetching feed: {str(e)}\n\n"

    with open("latest_news.txt", "w", encoding="utf-8") as f:
        f.write(output)
    print("Briefing data generated successfully.")

if __name__ == "__main__":
    build_briefing_data()
