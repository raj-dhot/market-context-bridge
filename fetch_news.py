import urllib.request
import xml.etree.ElementTree as ET
import datetime

# Define the target RSS feeds tailored to your prompt
FEEDS = {
    "S&P 500 & US Markets": "https://news.google.com/rss/search?q=S%26P+500+stock+market&hl=en-CA&gl=CA&ceid=CA:en",
    "TSX & Canadian Markets": "https://news.google.com/rss/search?q=TSX+Toronto+Stock+Exchange+markets&hl=en-CA&gl=CA&ceid=CA:en",
    "XEF & XEC (International & Emerging)": "https://news.google.com/rss/search?q=International+developed+emerging+markets+stocks&hl=en-CA&gl=CA&ceid=CA:en",
    "Competitor & AI Pulse (Wealthsimple, Questrade, AI)": "https://news.google.com/rss/search?q=Wealthsimple+OR+Questrade+OR+AI+finance+disruption&hl=en-CA&gl=CA&ceid=CA:en"
}

def fetch_feed(name, url):
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req) as response:
            xml_data = response.read()
        root = ET.fromstring(xml_data)
        
        output = f"--- {name} ---\n"
        # Grab the top 5 headlines and publish dates for each category
        for item in root.findall('.//item')[:5]:
            title = item.find('title').text
            pubDate = item.find('pubDate').text
            output += f"- {title} ({pubDate})\n"
        return output + "\n"
    except Exception as e:
        return f"--- {name} ---\nError fetching data: {str(e)}\n\n"

if __name__ == "__main__":
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    final_report = f"MARKET CONTEXT FOR WEEK OF {today}\n\n"
    
    for name, url in FEEDS.items():
        final_report += fetch_feed(name, url)
        
    with open("latest_news.txt", "w", encoding="utf-8") as f:
        f.write(final_report)
        
    print("News successfully fetched and written to latest_news.txt")
