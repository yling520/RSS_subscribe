import requests
from bs4 import BeautifulSoup
from feedgen.feed import FeedGenerator
from datetime import datetime, timezone
import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(SCRIPT_DIR, 'output')
os.makedirs(OUTPUT_DIR, exist_ok=True)

URL = 'https://www.wuzao.com/projects/trends/monthly/'
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36'
}


def fetch_page():
    resp = requests.get(URL, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    resp.encoding = 'utf-8'
    return resp.text


def parse_projects(html):
    soup = BeautifulSoup(html, 'html.parser')
    container = soup.select_one('div.divide-y')
    if not container:
        return []

    projects = []
    for item in container.find_all('div', recursive=False):
        repo_el = item.select_one('h3 a')
        if not repo_el:
            continue

        repo_name = repo_el.get_text(strip=True)
        repo_url = repo_el.get('href', '')

        rank_el = item.select_one('[class*="rounded-full"][class*="w-8"]')
        rank = rank_el.get_text(strip=True) if rank_el else ''

        lang_el = item.select_one('[class*="bg-primary-100"]')
        language = lang_el.get_text(strip=True) if lang_el else ''

        desc_el = item.select_one('p[class*="line-clamp"]')
        description = desc_el.get_text(strip=True) if desc_el else ''

        star_el = item.select_one('[class*="items-center"][class*="gap-3"] span')
        stars = star_el.get_text(strip=True) if star_el else ''

        growth_el = item.select_one('[class*="text-2xl"][class*="font-bold"]')
        growth = growth_el.get_text(strip=True).replace('+', '').strip() if growth_el else ''

        topics = [t.get_text(strip=True) for t in item.select('a[href*="?topic="]')]

        projects.append({
            'rank': rank,
            'name': repo_name,
            'url': repo_url,
            'language': language,
            'description': description,
            'stars': stars,
            'growth': growth,
            'topics': topics,
        })

    return projects


def generate_rss(projects):
    fg = FeedGenerator()
    fg.title('GitHub 30日趋势榜')
    fg.link(href=URL, rel='alternate')
    fg.description('GitHub 30日星标增长榜单 — 来自无噪(wuzao.com)')
    fg.language('zh-CN')

    now = datetime.now(timezone.utc)

    for proj in projects:
        fe = fg.add_entry()
        fe.title(proj['name'])
        fe.link(href=proj['url'], rel='alternate')
        fe.guid(proj['url'], permalink=True)

        if proj['language']:
            fe.category(term=proj['language'])
        for topic in proj['topics']:
            fe.category(term=topic)

        parts = [f'<h3>#{proj["rank"]} <a href="{proj["url"]}">{proj["name"]}</a></h3>']
        if proj['description']:
            parts.append(f'<p>{proj["description"]}</p>')
        lang = proj['language'] or '未知'
        parts.append(f'<p>语言: {lang} | ⭐ {proj["stars"]} | 30日增长: <strong>+{proj["growth"]}</strong></p>')
        if proj['topics']:
            parts.append(f'<p>标签: {", ".join(proj["topics"])}</p>')

        fe.description('\n'.join(parts))
        fe.pubDate(now)

    rss_file = os.path.join(OUTPUT_DIR, 'github-trends-monthly.xml')
    fg.rss_file(rss_file)
    return rss_file


def main():
    print('Fetching https://www.wuzao.com/projects/trends/monthly/...')
    html = fetch_page()
    print('Parsing...')
    projects = parse_projects(html)
    print(f'Found {len(projects)} projects')
    if not projects:
        print('WARNING: no projects found, page structure may have changed')
    rss_file = generate_rss(projects)
    print(f'RSS saved: {rss_file}')


if __name__ == '__main__':
    main()
