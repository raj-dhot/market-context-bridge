import urllib.request
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup
import datetime
import time
import ssl

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
    text_lower = text.lower()
    return any(keyword in text_lower for keyword in POLITICAL_KEYWORDS)

def extract_article_body(url):
    try:
        # Bypass SSL verification issues sometimes present in automated environments
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, context=ctx, timeout=10) as response:
            html = response.read()
        
        soup = BeautifulSoup(html, 'html.parser')
        
        # Extract text from paragraphs
        paragraphs = soup.find_all('p')
        body_text = " ".join([p.get_text() for p in paragraphs])
        
        # Clean up extra whitespace
        body_text = " ".join(body_text.split())
        
        # Truncate to save agent context window space (approx. 500 words)
        if len(body_text) > 3000:
            body_text = body_text[:3000] + "... [Truncated]"
            
        return body_text if body_text else "No readable paragraph content found."
    except Exception as e:
        return f"Extraction failed: {str(e)}"

def build_briefing_data():
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    output = f"RAW INTELLIGENCE DATA - {today}\n"
    output += "="*50 + "\n\n"

    for category, rss_url in FEEDS.items():
        output += f"### {category.upper()} ###\n"
        try:
            req = urllib.request.Request(rss_url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=10) as response:
                xml_data = response.read()
            
            root = ET.fromstring(xml_data)
            
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
                
                if is_political(body):
                    continue 
                
                output += f"TITLE: {title}\n"
                output += f"LINK: {link}\n"
                output += f"CONTENT:\n{body}\n"
                output += "-"*40 + "\n\n"
                
                count += 1
                time.sleep(1.5) # Polite delay to prevent rate-limiting
                
        except Exception as e:
            output += f"Error fetching feed: {str(e)}\n\n"

    with open("latest_news.txt", "w", encoding="utf-8") as f:
        f.write(output)
    print("Briefing data generated successfully.")

if __name__ == "__main__":
    build_briefing_data()
