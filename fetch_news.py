import feedparser
import newspaper
import datetime
import time

# Categories for OceanFront Wealth
FEEDS = {
    "Market Themes": "https://news.google.com/rss/search?q=S%26P+500+TSX+market+analysis&hl=en-CA&gl=CA&ceid=CA:en",
    "International": "https://news.google.com/rss/search?q=XEF+XEC+emerging+markets+news&hl=en-CA&gl=CA&ceid=CA:en",
    "AI & Competitor Pulse": "https://news.google.com/rss/search?q=Wealthsimple+Questrade+AI+finance+news&hl=en-CA&gl=CA&ceid=CA:en"
}

def get_full_article(url):
    try:
        article = newspaper.article(url)
        article.download()
        article.parse()
        # Returns title + top 1500 characters of body to keep file size manageable for Claude
        return f"TITLE: {article.title}\nCONTENT: {article.text[:1500]}..." 
    except Exception:
        return "Full text extraction failed for this link."

if __name__ == "__main__":
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    report = f"FULL INTELLIGENCE BRIEF DATA - {today}\n"
    report += "="*30 + "\n\n"

    for category, url in FEEDS.items():
        report += f"### CATEGORY: {category} ###\n"
        feed = feedparser.parse(url)
        
        # Pull the top 2 articles per category for deep analysis
        for entry in feed.entries[:2]:
            report += f"SOURCE URL: {entry.link}\n"
            report += get_full_article(entry.link) + "\n"
            report += "-"*20 + "\n"
            time.sleep(1) # Polite delay to avoid blocks

    with open("latest_news.txt", "w", encoding="utf-8") as f:
        f.write(report)
