import datetime
import time
from duckduckgo_search import DDGS
import trafilatura

# Direct search queries
QUERIES = {
    "North America (TSX & S&P 500)": "TSX OR S&P 500 stock market news",
    "International & Emerging (XEF/XEC)": "emerging markets OR international equities market news",
    "Competitor & AI Pulse": "Wealthsimple OR Questrade OR AI wealth management news"
}

# Strict filter to keep the context clean
POLITICAL_KEYWORDS = [
    "election", "parliament", "congress", "trudeau", "biden", 
    "conservative", "liberal", "democrat", "republican", "campaign", "senate"
]

def is_political(text):
    if not text: return False
    return any(keyword in text.lower() for keyword in POLITICAL_KEYWORDS)

def fetch_intelligence():
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    output = f"OCEANFRONT RAW INTELLIGENCE - {today}\n"
    output += "="*50 + "\n\n"
    
    ddgs = DDGS()
    
    for category, query in QUERIES.items():
        output += f"### {category.upper()} ###\n"
        try:
           # Ask DuckDuckGo for 10 results instead of 5
            results = ddgs.news(query, max_results=10)
            valid_articles = 0
            
            for res in results:
                # Increase the cap to 4 valid articles per category
                if valid_articles >= 4: 
                    break
                    
                title = res.get('title', '')
                url = res.get('url', '')
                
                if is_political(title):
                    continue
                
                # Fetch the HTML and extract pure text, bypassing cookie banners
                downloaded = trafilatura.fetch_url(url)
                if not downloaded:
                    continue
                    
                body = trafilatura.extract(downloaded, include_comments=False, include_tables=False)
                
                # If it's a paywall stub (<300 chars), political, or empty, skip it
                if not body or len(body) < 300 or is_political(body):
                    continue
                    
                output += f"TITLE: {title}\n"
                output += f"SOURCE: {res.get('source', 'Unknown')}\n"
                output += f"LINK: {url}\n"
                
                # Truncate to 2500 characters so the agent isn't overwhelmed with noise
                output += f"CONTENT:\n{body[:2500]}...\n"
                output += "-"*50 + "\n\n"
                
                valid_articles += 1
                time.sleep(2) # Polite delay
                
            if valid_articles == 0:
                output += "No readable, non-political articles successfully extracted today.\n\n"
                
        except Exception as e:
            output += f"Error processing category: {str(e)}\n\n"

    with open("latest_news.txt", "w", encoding="utf-8") as f:
        f.write(output)

if __name__ == "__main__":
    fetch_intelligence()
