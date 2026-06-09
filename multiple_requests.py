from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium_stealth import stealth
from selenium.webdriver.common.by import By
from feedgen.feed import FeedGenerator
from datetime import datetime, timezone
import os
import time
import random

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(SCRIPT_DIR, 'output')
os.makedirs(OUTPUT_DIR, exist_ok=True)

# 拦截 fetch 请求，捕获 arc/search API 的响应数据
# 用 __arcEpoch 防止上一个 UID 的响应覆盖当前结果
FETCH_INTERCEPTOR = '''
window.__arcSearchResult = null;
window.__arcEpoch = 0;
window.__arcSearchUrl = '';
const _origFetch = window.fetch;
window.fetch = function() {
    const url = typeof arguments[0] === 'string' ? arguments[0] : (arguments[0] ? arguments[0].url : '');
    const capturedEpoch = window.__arcEpoch;
    return _origFetch.apply(this, arguments).then(function(response) {
        if (url.indexOf('arc/search') !== -1) {
            response.clone().json().then(function(data) {
                if (window.__arcEpoch === capturedEpoch) {
                    window.__arcSearchResult = data;
                    window.__arcSearchUrl = url;
                }
            }).catch(function() {
                if (window.__arcEpoch === capturedEpoch) {
                    window.__arcSearchResult = {__error: true, status: response.status};
                    window.__arcSearchUrl = url;
                }
            });
        }
        return response;
    });
};
'''

# 每200ms轮询 __arcSearchResult，超时3秒后返回
WAIT_API_JS = '''
var done = arguments[arguments.length - 1];
var elapsed = 0;
function check() {
    if (window.__arcSearchResult !== null) {
        done({found: true, data: window.__arcSearchResult});
        return;
    }
    elapsed += 200;
    if (elapsed >= 3000) {
        done({found: false});
        return;
    }
    setTimeout(check, 200);
}
check();
'''

RSS_FILES = []


def create_driver():
    # 创建带反检测的 headless Chrome 实例
    options = Options()
    options.add_argument('--headless=new')
    options.add_argument('--disable-blink-features=AutomationControlled')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    options.add_argument('--window-size=1920,1080')
    options.add_argument('--disable-gpu')
    options.add_argument(
        '--user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36'
    )

    service = Service(os.path.join(SCRIPT_DIR, 'chromedriver'))
    driver = webdriver.Chrome(service=service, options=options)

    # 伪装浏览器指纹：语言、平台、WebGL渲染器
    stealth(driver,
            languages=["zh-CN", "zh"],
            vendor="Google Inc.",
            platform="Win32",
            webgl_vendor="Intel Inc.",
            renderer="Intel Iris OpenGL Engine",
            fix_hairline=True,
            )
    driver.execute_cdp_cmd('Emulation.setTimezoneOverride',
                           {'timezoneId': 'Asia/Shanghai'})

    # 在每个新页面加载时注入 fetch 拦截器
    driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument', {
        'source': FETCH_INTERCEPTOR
    })

    driver.set_script_timeout(5)
    return driver


def wait_for_api(driver):
    result = driver.execute_async_script(WAIT_API_JS)
    if result and result.get('found'):
        return result['data']
    return None


# SPA 内导航：通过 pushState 切换 UID，不刷新页面
def navigate_to_uid(driver, uid):
    # 递增 epoch 使上一个 UID 的慢响应失效
    driver.execute_script('window.__arcEpoch++; window.__arcSearchResult = null;')
    driver.execute_script(f'''
        window.history.pushState({{}}, '', '/{uid}/video');
        window.dispatchEvent(new PopStateEvent('popstate', {{state: {{}}}}));
    ''')


# 全页面导航：打开 UP 主主页并等待 API 响应
def load_uid_page(driver, uid):
    driver.get(f'https://space.bilibili.com/{uid}/video')
    time.sleep(4)
    if '/upload/video' not in driver.current_url:
        try:
            driver.find_element(By.CSS_SELECTOR, 'a[href$="/upload"]').click()
            time.sleep(2)
        except Exception:
            pass
    return wait_for_api(driver)


def try_uid_spa(driver, uid):
    navigate_to_uid(driver, uid)
    result = wait_for_api(driver)
    # 校验响应 URL 的 mid 参数是否匹配请求的 UID
    if result and not result.get('__error'):
        search_url = driver.execute_script('return window.__arcSearchUrl || ""')
        if not search_url or f'mid={uid}' not in search_url:
            return None
    return result


def try_uid_full(driver, uid):
    return load_uid_page(driver, uid)


def try_uid(driver, uid, is_first=False, use_full=False):
    if is_first or use_full:
        result = load_uid_page(driver, uid)
    else:
        result = try_uid_spa(driver, uid)

    # 状态分类：timeout / http错误 / 412限流 / API错误 / 空 / 成功
    if result is None:
        return 'timeout', None

    if result.get('__error'):
        status = result.get('status')
        return 'http_' + str(status), None

    code = result.get('code')
    if code == 412 or code == -412:
        return '412', None

    if code != 0:
        return 'api_err_' + str(code), None

    vlist = result.get('data', {}).get('list', {}).get('vlist', [])
    if not vlist:
        return 'empty', None

    return 'ok', vlist


def generate_rss(uid, vlist):
    fg = FeedGenerator()
    fg.title(f'{uid} RSS feed')
    fg.link(href=f'https://space.bilibili.com/{uid}/video', rel='alternate')
    fg.description(f'RSS feed for Bilibili user {uid}')

    for video in vlist:
        fe = fg.add_entry()
        fe.title(video['title'])
        fe.link(href=f"https://www.bilibili.com/video/{video['bvid']}")
        fe.guid(video['bvid'], permalink=False)

        if video.get('author'):
            fe.author({'name': video['author']})
        if video.get('typename'):
            fe.category(term=video['typename'])

        pic = video.get('pic', '')
        if pic.startswith('http://'):
            pic = 'https://' + pic[7:]
        elif pic.startswith('//'):
            pic = 'https:' + pic

        pub_date = datetime.fromtimestamp(video['created'], tz=timezone.utc)
        date_str = pub_date.strftime('%Y-%m-%d %H:%M')

        length = video.get('length', '')
        desc_text = video.get('description', '').strip()[:200]
        play = video.get('play', 0)
        review = video.get('review', 0)

        parts = [f'<img src="{pic}" alt="{video["title"]}">']
        parts.append(f'<p><strong>{video["title"]}</strong></p>')
        parts.append(f'<p>UP主: {video.get("author", "未知")} | 分类: {video.get("typename", "未知")}</p>')
        if length:
            parts.append(f'<p>时长: {length} | 播放: {play:,} | 弹幕: {review:,}</p>')
        else:
            parts.append(f'<p>播放: {play:,} | 弹幕: {review:,}</p>')
        if desc_text:
            parts.append(f'<p>{desc_text}</p>')
        parts.append(f'<p>发布时间: {date_str}</p>')

        fe.description('\n'.join(parts))
        fe.pubDate(pub_date)

    rss_file = os.path.join(OUTPUT_DIR, f'{uid}.xml')
    fg.rss_file(rss_file)
    return rss_file


with open(os.path.join(SCRIPT_DIR, 'list_of_UID.txt'), 'r') as f:
    UIDs = [line.strip() for line in f if line.strip()]
print("UIDs:", UIDs)

# 全部使用全页面导航，B站 SPA 不支持 pushState 触发路由
FULL_THRESHOLD = 0

_t0 = time.time()
driver = create_driver()

print(f"[{time.time()-_t0:.0f}s] 预热...")
# 预热：先访问B站首页，建立 cookie 和会话状态
driver.get('https://www.bilibili.com')
time.sleep(4)

for i, uid in enumerate(UIDs):
    is_first = (i == 0)
    attempts = 0
    max_attempts = 10

    while attempts < max_attempts:
        attempts += 1

        if attempts > 1:
            # 重试策略：0.5-1s随机延迟，避免触发限流
            wait = random.uniform(0.5, 1)
            mode = "full" if attempts > FULL_THRESHOLD else "spa"
            print(f"[{time.time()-_t0:.0f}s] {uid} 第{attempts}次({mode}) 等{wait:.1f}s...", end='', flush=True)
            time.sleep(wait)
        else:
            print(f"[{time.time()-_t0:.0f}s] {uid}...", end='', flush=True)

        use_full = (attempts > FULL_THRESHOLD)
        status, data = try_uid(driver, uid, is_first=is_first, use_full=use_full)

        if status == 'ok':
            rss_file = generate_rss(uid, data)
            RSS_FILES.append(uid)
            print(f" OK ({len(data)}视频)")
            break
        elif status == '412':
            print(f" 412")
            continue
        elif status.startswith('http_'):
            print(f" HTTP {status.split('_')[1]}")
            continue
        elif status == 'timeout':
            print(f" 超时")
            continue
        else:
            print(f" {status}")
            continue

    if attempts >= max_attempts:
        print(f" 放弃: {uid}")

driver.quit()
elapsed = time.time() - _t0
print(f'\n完成: {len(RSS_FILES)}/{len(UIDs)} 成功，耗时 {elapsed:.0f}s')
