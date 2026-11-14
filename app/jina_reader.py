"""Anonymous Reader fallback for individual Zhihu articles and answers only."""
import datetime
import hashlib
import re
from urllib.parse import urlsplit

import httpx
import markdown
from bs4 import BeautifulSoup

from . import config, work_budget

ENDPOINT = 'https://r.jina.ai/'
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MIN_BODY_CHARACTERS = 80


def canonical_url(url):
    """Drop tracking parameters; never forward credentials or arbitrary URLs."""
    try:
        parts = urlsplit(url)
        if (parts.scheme not in ('http', 'https') or parts.username or parts.password
                or parts.port not in (None, 80, 443)):
            return ''
        host = (parts.hostname or '').lower()
        path = parts.path.rstrip('/')
        if host == 'zhuanlan.zhihu.com' and re.fullmatch(r'/p/\d+', path):
            return 'https://' + host + path
        if host in ('zhihu.com', 'www.zhihu.com') and re.fullmatch(r'/(?:question/\d+/)?answer/\d+', path):
            return 'https://www.zhihu.com' + path
    except (ValueError, TypeError):
        pass
    return ''


def _image_url(url):
    try:
        parts = urlsplit(url)
        host = (parts.hostname or '').lower()
        return (parts.scheme == 'https' and not parts.username and not parts.password
                and parts.port in (None, 443)
                and (host.endswith('.zhimg.com') or host == 'zhimg.com'
                     or (host == 'www.zhihu.com' and parts.path == '/equation')))
    except ValueError:
        return False


def parse_response(raw, source_url):
    """Validate the source envelope before rendering Markdown into model text."""
    header, marker, body = raw.partition('Markdown Content:')
    title = re.search(r'^Title:[ \t]*(\S[^\r\n]*)\r?$', header, re.M)
    source = re.search(r'^URL Source:[ \t]*(\S+)[ \t]*\r?$', header, re.M)
    if (not marker or not title or not source or not canonical_url(source[1])
            or canonical_url(source[1]) != canonical_url(source_url)):
        raise ValueError('返回内容缺少匹配的原始链接或标题')
    if re.search(r'^Warning:.*(?:error|403|401|429|captcha|blocked|login|truncat|partial|not fully loaded)', header, re.I | re.M):
        raise ValueError('来源返回访问限制或不完整页面')
    title = title[1].strip()
    if title in ('知乎', '知乎 - 有问题，就会有答案', '知乎 - 500', 'Just a moment...', 'Access Denied'):
        raise ValueError('返回的是站点提示页')
    soup = BeautifulSoup(markdown.markdown(body, extensions=['tables', 'fenced_code']), 'html.parser')
    for node in soup.select('script,style'):
        node.decompose()
    images = []
    for image in soup.find_all('img'):
        url = image.get('src', '')
        if not _image_url(url):
            raise ValueError('正文含无法安全处理的图片来源，保留待核查')
        if url not in images:
            images.append(url)
        image.replace_with(f'[图 {images.index(url) + 1}]')
    text = soup.get_text('\n', strip=True)
    if re.match(r'^(?:环境异常|当前环境异常|请您?登录后|你似乎来到了没有知识存在的荒原|Access Denied|Just a moment)', text, re.I):
        raise ValueError('返回的是登录或验证提示')
    # This fallback is conservative; a title or long URL is not article text.
    substance = text.removeprefix(title).strip()
    if len(re.findall(r'[^\W_]', substance, re.UNICODE)) < MIN_BODY_CHARACTERS:
        raise ValueError('正文内容不足，不能当作抓取成功')
    return dict(title=title, text=text, images=images)


def fetch(url):
    source = canonical_url(url)
    blank = dict(ok=False, title='', text='', images=[], error='')
    if not source:
        return {**blank, 'error': 'Jina 仅支持知乎单篇文章或回答'}
    try:
        timeout = work_budget.timeout(config.JINA_READER_TIMEOUT,
                                      reserve=max(120, config.LLM_TIMEOUT + 60))
        # An isolated anonymous request: no API key, account cookie, or paid mode.
        with httpx.stream('GET', ENDPOINT + source, headers={'Accept': 'text/plain'},
                          timeout=timeout, follow_redirects=False) as response:
            response.raise_for_status()
            data = bytearray()
            for chunk in response.iter_bytes():
                data.extend(chunk)
                if len(data) > MAX_RESPONSE_BYTES:
                    raise ValueError('阅读接口响应超过容量上限')
        raw = bytes(data).decode('utf-8')
        article = parse_response(raw, source)
        return dict(ok=True, error='', **article, resolved_url=source,
                    fetch_path='jina_reader', reader_response=raw,
                    capture={'method': 'jina_reader_anonymous', 'source_url': source,
                             'fetched_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                             'response_sha256': hashlib.sha256(data).hexdigest()},
                    art_candidates={'shares': [], 'body': article['images']})
    except httpx.HTTPStatusError as exc:
        return {**blank, 'error': f'Jina 免费阅读接口 HTTP {exc.response.status_code}，保留待重试'}
    except (httpx.HTTPError, ValueError, UnicodeError, work_budget.Exhausted) as exc:
        # Do not echo responses or request headers into user-facing errors.
        detail = str(exc) if isinstance(exc, (ValueError, work_budget.Exhausted)) else type(exc).__name__
        return {**blank, 'error': 'Jina 读取失败：' + detail}
