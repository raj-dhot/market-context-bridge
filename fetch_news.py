import requests
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup
import datetime

# Broadened queries for high volume
FEEDS = {
    "North America (TSX & S&P 500)": "https://news.google.com/rss/search?q=TSX+OR+%22S%26P+500%22+market&hl=en-CA&gl=CA&ceid=CA:en",
    "International & Emerging (XEF/XEC)": "https://news.google.com/rss/search?q=%22emerging+markets%22+OR+%22international+equities%22&hl=en-CA&gl=CA&ceid=CA:en",
    "Competitor & AI Pulse": "https://news.google.com/rss/search?q=Wealthsimple+OR+Questrade+OR+%22AI+wealth%22&hl=en-CA&gl=CA&ceid=CA:en"
}

POLITICAL_KEYWORDS = ["election", "parliament", "congress", "trudeau", "biden", "conservative", "liberal", "democrat", "republican"]

def is_political(text):
    return any(keyword in text.lower() for keyword in POLITICAL_KEYWORDS)

def clean_html_snippet(raw_html):
    """Strips the HTML formatting out of the Google News description tag to get the raw text snippet."""
    if not raw_html:
        return "No summary available."
    soup = BeautifulSoup(raw_html, 'html.parser')
    text = soup.get_text(separator=" | ")
    # Clean up excess whitespace
    return " ".join(text.split())

def build_briefing_data():
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    output = f"RAW INTELLIGENCE DATA (SNIPPETS) - {today}\n"
    output += "="*50 + "\n\n"
    
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}

    for category, rss_url in FEEDS.items():
        output += f"### {category.upper()} ###\n"
        try:
            response = requests.get(rss_url, headers=headers, timeout=10)
            root = ET.fromstring(response.content)
            
            valid_articles_found = 0
            
            # Pull the top 5 valid snippets per category
            for item in root.findall('.//item'):
                if valid_articles_found >= 5: 
                    break
                    
                title = item.find('title').text
                
                if is_political(title):
                    continue
                
                # Grab the description directly from the RSS feed (no external site visits)
                desc_element = item.find('description')
                raw_snippet = desc_element.text if desc_element is not None else ""
                clean_snippet = clean_html_snippet(raw_snippet)
                
                output += f"TITLE: {title}\n"
                output += f"SUMMARY: {clean_snippet}\n"
                output += "-"*40 + "\n\n"
                
                valid_articles_found += 1
                
            if valid_articles_found == 0:
                output += "No non-political headlines found for this category today.\n\n"
                
        except Exception as e:
            output += f"Error fetching feed: {str(e)}\n\n"

    with open("latest_news.txt", "w", encoding="utf-8") as f:
        f.write(output)

if __name__ == "__main__":
    build_briefing_data()
