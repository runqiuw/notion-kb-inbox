"""Source-aware separation of platform share furniture and user comments."""
import re

VERSION = 2
TAIL = re.compile(r'(?:直达|跳转|保留这段|这篇笔记在|【小红书】里有|这段文字复制|发布了一篇小红书笔记|\d*\s*发布了一篇小红书笔记|复制打开抖音|长按复制|打开抖音搜索|点击链接直接打开|复制此链接|打开【小红书】).*', re.S)
PERSONAL = re.compile(r'我|认为|觉得|不同意|疑问|值得|需要|核实|改正|注意|想法|疑惑|为什么|怎么|是否|\?|？|\b(?:I|my|think|why|check|TODO)\b', re.I)
PLATFORM_ONLY = {'小红书', '哔哩哔哩', 'bilibili', '抖音', '知乎', '微博', '网页链接', '查看全文', '打开app'}
DESKTOP_XHS = re.compile(r'^\s*(?:\d+\s*)?【[^】\n]*\|\s*小红书\s*[-—]\s*你的生活兴趣社区】\s*(?:(?P<emoji>[^\w\s])\s*[A-Za-z0-9]{8,32}\s*(?P=emoji))?\s*')
SHARE_INTRO = re.compile(
    r'^(?:可以|大家可以|欢迎)?\s*(?:看下|看看|查看|阅读|阅读一下)\s*'
    r'(?:我们(?:的)?|这篇|这份|这个)?\s*(?:第[一二三四五六七八九十\d]+个)?\s*'
    r'(?:blog|博客|文章|帖子|笔记)(?:[，,\s]*(?:比较详细|更详细|详细介绍|详情在这里))?[：:。.!！\s]*$', re.I)


def compact(text):
    return re.sub(r'[\s…\.]+', '', text or '')


def echo(line, title, body):
    value = compact(line.strip('“”"「」'))
    if not value:
        return True
    sources = [compact(title), compact(body)]
    if any(s and value in s for s in sources):
        return True
    # Share sheets concatenate a title and an excerpt not adjacent in the body.
    t = compact(title)
    rest = value[len(t):] if t and value.startswith(t) else value
    return bool(t and value.startswith(t) and (not rest or rest in compact(body)))


def classify(text, title='', body=''):
    raw = (text or '').strip()
    lines, removed, desktop_removed = [], False, False
    for original in raw.splitlines():
        line = original.strip()
        if SHARE_INTRO.fullmatch(line):
            removed = True
            continue
        desktop = DESKTOP_XHS.match(line)
        if desktop:
            line = line[desktop.end():].strip()
            removed = desktop_removed = True
        match = TAIL.search(line)
        if not match and '【小红书】' in line and not PERSONAL.search(line) and re.search(r'逛|全文|精彩|探索|浏览|打开|分享|快来', line):
            removed = True
            continue
        if match:
            prefix = line[:match.start()].strip(' \t，,。.!！;；0123456789')
            # Preserve a comment preceding an inline platform instruction.
            line = prefix
            removed = True
        bare = re.sub(r'[\s\-—·、，,。.!！?？:：\[\]【】]+', '', line).lower()
        if not line or bare in PLATFORM_ONLY or echo(line, title, body):
            continue
        lines.append(line)
    candidate = '\n'.join(lines)
    if len(re.sub(r'\s', '', candidate)) < 4:
        candidate = ''
    # Without source context an excerpt next to a promotion isn't attributable.
    if removed and not desktop_removed and not (title or body) and candidate and (not PERSONAL.search(candidate) or '...' in candidate or '…' in candidate):
        state = 'pending'
    else:
        state = 'user' if candidate else 'template'
    return {'version': VERSION, 'raw': raw, 'candidate': candidate, 'state': state}
