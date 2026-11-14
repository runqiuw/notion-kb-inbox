"""Bounded, anonymous rendering of public pages in a disposable process group.

Chromium never fetches HTTP directly: intercepted requests use a connection to
the validated public IP, retaining the original TLS SNI and Host. This also
prevents DNS rebinding between validation and connection. Service workers,
WebSockets, popups, media and fonts are disabled. No browser state is reused.
"""
import http.client
import ipaddress
import json
import logging
import os
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

log = logging.getLogger(__name__)
_slot = threading.BoundedSemaphore(1)
MAX_HTML_BYTES = 4 * 1024 * 1024
MAX_RESOURCE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 24 * 1024 * 1024
MAX_REQUESTS = 180
MAX_INTERCEPTED_REQUESTS = 2000
MAX_REDIRECTS = 6
STABLE_SECONDS = 1.2


class FetchFailure(Exception):
    def __init__(self, kind, message):
        self.kind = kind
        super().__init__(message)


def public_target(url, resolver=socket.getaddrinfo):
    p = urlsplit(url)
    if p.scheme not in {"http", "https"} or not p.hostname or p.username or p.password:
        raise FetchFailure("blocked_url", "浏览器只接受无凭证的公开 HTTP(S) 地址")
    host = p.hostname.rstrip(".").lower()
    if host in {"localhost", "metadata.google.internal"} or host.endswith((".localhost", ".local", ".internal")):
        raise FetchFailure("blocked_url", "拒绝本机、内网或云元数据地址")
    port = p.port or (443 if p.scheme == "https" else 80)
    addresses = list(dict.fromkeys(r[4][0] for r in resolver(host, port, type=socket.SOCK_STREAM)))
    if not addresses or any(not ipaddress.ip_address(a).is_global for a in addresses):
        raise FetchFailure("blocked_url", "拒绝非公开网络地址")
    return p, host, port, addresses[0]


class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, ip, port, timeout):
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self.ip = ip

    def connect(self):
        sock = socket.create_connection((self.ip, self.port), self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


def _resource(request, remaining, targets):
    """No automatic redirects: each redirect goes back through the route guard."""
    url = request.url
    p = urlsplit(url)
    key = (p.scheme, p.hostname, p.port)
    target = targets.get(key)
    if target is None:
        target = targets[key] = public_target(url)
    else:
        # A cached host must not allow a URL with credentials or another scheme.
        if p.username or p.password or p.scheme not in {"http", "https"}:
            raise FetchFailure("blocked_url", "拒绝带凭证的子请求")
    p, host, port, ip = target
    # The path is per request, even when its DNS target was cached.
    path = urlsplit(url).path or "/"
    query = urlsplit(url).query
    if query:
        path += "?" + query
    timeout = max(.1, min(8, remaining))
    conn = (PinnedHTTPS(host, ip, port, timeout) if p.scheme == "https" else
            http.client.HTTPConnection(ip, port, timeout=timeout))
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in {"host", "connection", "accept-encoding", "authorization", "proxy-authorization", "content-length"}}
    headers.update({"Host": host if port in {80, 443} else f"{host}:{port}", "Accept-Encoding": "identity"})
    body = request.post_data_buffer
    if body and len(body) > 64 * 1024:
        raise FetchFailure("resource_limit", "页面请求体超过上限")
    try:
        conn.request(request.method, path, body=body, headers=headers)
        response = conn.getresponse()
        data = response.read(MAX_RESOURCE_BYTES + 1)
        if len(data) > MAX_RESOURCE_BYTES:
            raise FetchFailure("resource_limit", "页面资源超过大小上限")
        # Keep encoding for Chromium to decode the actual response bytes.
        out = {k: v for k, v in response.getheaders()
               if k.lower() not in {"transfer-encoding", "connection", "content-length"}}
        return response.status, out, data
    finally:
        conn.close()


# Wait for real semantic content, not a word count or networkidle (videos and
# live leaderboards never become idle). Remove browser chrome only in the
# stability signature; the shared HTML parser cleans the returned document.
_CONTENT = """() => {
  const root = document.querySelector('article, main, [role="main"]') || document.body;
  if (!root) return '';
  const copy = root.cloneNode(true);
  copy.querySelectorAll('nav,header,footer,aside,script,style,noscript,[aria-hidden="true"]').forEach(n=>n.remove());
  const text = (copy.textContent || '').replace(/\\s+/g,' ').trim();
  if (!text) return '';
  if (!copy.querySelector('p,table,h1,h2,h3,article,section') && !root.matches('article')) return '';
  return {text, headings: [document.title, ...Array.from(copy.querySelectorAll('h1,h2,h3,h4,h5,h6'), n => n.textContent || '')]};
}"""


def _wait_content(page, end, fatal):
    from .web_content import content_pending

    signature, changed = "", time.monotonic()
    while time.monotonic() < end:
        if fatal:
            raise fatal[0]
        snapshot = page.evaluate(_CONTENT)
        current = snapshot['text'] if snapshot and not content_pending(snapshot['text'], snapshot['headings']) else ''
        if current != signature:
            signature, changed = current, time.monotonic()
        if signature and time.monotonic() - changed >= STABLE_SECONDS:
            html = page.content()
            if len(html.encode()) > MAX_HTML_BYTES:
                raise FetchFailure("html_limit", "渲染后 HTML 超过大小上限")
            return html
        page.wait_for_timeout(200)
    raise FetchFailure("render_timeout", "动态网页正文未在时间上限内出现并稳定")


def _route_resource(route, request, end, targets, stats):
    stats["requests"] += 1
    if stats["requests"] > MAX_INTERCEPTED_REQUESTS:
        raise FetchFailure("resource_limit", "页面拦截请求次数超过上限")
    # Skipped media never opens a connection and must not consume the network
    # budget needed by the document, scripts, styles and article data.
    if request.resource_type in {"media", "font", "image"}:
        stats["skipped"] += 1
        route.abort()
        return
    if time.monotonic() >= end:
        raise FetchFailure("render_timeout", "动态网页加载超过时间上限")
    stats["fetched"] += 1
    if stats["fetched"] > MAX_REQUESTS:
        raise FetchFailure("resource_limit", "页面实际下载请求次数超过上限")
    previous, redirects = request.redirected_from, 0
    while previous:
        redirects += 1
        previous = previous.redirected_from
    if redirects > MAX_REDIRECTS:
        raise FetchFailure("redirect_limit", "页面重定向超过上限")
    status, headers, data = _resource(request, end-time.monotonic(), targets)
    stats["bytes"] += len(data)
    if stats["bytes"] > MAX_TOTAL_BYTES:
        raise FetchFailure("resource_limit", "页面资源总量超过上限")
    route.fulfill(status=status, headers=headers, body=data)


def _worker(url, seconds):
    from playwright.sync_api import sync_playwright
    end = time.monotonic() + seconds
    targets, stats = {}, {"requests": 0, "fetched": 0, "skipped": 0, "bytes": 0, "blocked": 0}
    public_target(url)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, timeout=max(1, int((end-time.monotonic())*1000)),
            args=["--disable-dev-shm-usage", "--disable-background-networking", "--disable-quic"])
        try:
            context = browser.new_context(service_workers="block", accept_downloads=False,
                viewport={"width": 1440, "height": 1000})
            context.route_web_socket("**/*", lambda ws: ws.close())
            fatal = []
            def route_request(route, request):
                try:
                    _route_resource(route, request, end, targets, stats)
                except Exception as exc:
                    stats["blocked"] += 1
                    if request.is_navigation_request() or isinstance(exc, FetchFailure) and exc.kind != "blocked_url":
                        fatal.append(exc)
                    route.abort()
            context.route("**/*", route_request)
            page = context.new_page()
            context.on("page", lambda new: new.close() if new != page else None)
            response = page.goto(url, wait_until="domcontentloaded", timeout=max(1, int((end-time.monotonic())*1000)))
            if response and response.status >= 400:
                raise FetchFailure("http_error", f"渲染页面 HTTP {response.status}")
            html = _wait_content(page, end, fatal)
            public_target(page.url)
            return {"ok": True, "html": html, "url": page.url, **stats}
        except Exception as exc:
            exc.render_stats = stats
            raise
        finally:
            browser.close()


def _group_rss(pgid):
    """Sample aggregate RSS of driver + Chromium on Linux; no extra dependency."""
    total = 0
    if not os.path.isdir('/proc'):
        return 0
    for pid in os.listdir('/proc'):
        if not pid.isdigit():
            continue
        try:
            stat = open(f'/proc/{pid}/stat').read().split(') ', 1)[1].split()
            if int(stat[2]) == pgid:
                total += int(stat[21]) * os.sysconf('SC_PAGE_SIZE')
        except (OSError, ValueError, IndexError):
            pass
    return total


def render(url, timeout=35, wait_timeout=2, *, mode='article'):
    started = time.monotonic()
    if timeout < 3:
        return {"ok": False, "failure_kind": "budget_exhausted", "error": "本次剩余时间不足，留待下次重试"}
    if not _slot.acquire(timeout=min(wait_timeout, timeout)):
        return {"ok": False, "failure_kind": "render_busy", "error": "动态网页浏览器忙，已保留待重试"}
    proc = None
    peak = 0
    try:
        end = started + timeout
        # File output avoids deadlocking on a pipe full of rendered HTML. The
        # parent bounds the file, and unlinks it even when a worker crashes.
        import tempfile
        with tempfile.TemporaryDirectory(prefix="kb-render-") as temp:
            output = os.path.join(temp, 'result.json')
            proc = subprocess.Popen([sys.executable, '-m', 'app.render_web', url,
                str(max(.1, end-time.monotonic())), output, mode], start_new_session=True,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            while proc.poll() is None and time.monotonic() < end:
                peak = max(peak, _group_rss(proc.pid))
                time.sleep(.05)
            if proc.poll() is None:
                return {"ok": False, "failure_kind": "render_timeout", "error": "动态网页加载超过时间上限"}
            if not os.path.exists(output) or os.path.getsize(output) > MAX_HTML_BYTES * 2:
                return {"ok": False, "failure_kind": "render_crash", "error": "动态网页浏览器异常退出"}
            result = json.load(open(output))
            result.update(elapsed_seconds=round(time.monotonic()-started, 3), peak_rss_bytes=peak)
            log.info("render-web ok=%s failure_kind=%s requests=%s fetched=%s skipped=%s bytes=%s blocked=%s",
                     result.get('ok'), result.get('failure_kind', ''), result.get('requests'),
                     result.get('fetched'), result.get('skipped'), result.get('bytes'), result.get('blocked'))
            return result
    except Exception as exc:
        return {"ok": False, "failure_kind": "render_crash", "error": f"动态网页浏览器异常：{str(exc)[:200]}"}
    finally:
        if proc:
            # Includes orphaned descendants after a crashed parent. SIGKILL is
            # last-resort cleanup after the worker's normal context close.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=3)
            finally:
                _slot.release()
        else:
            _slot.release()
        log.info("render-web seconds=%.3f peak_rss_bytes=%s", time.monotonic()-started, peak)


if __name__ == '__main__':
    try:
        if len(sys.argv) > 4 and sys.argv[4] == 'xhs_comments':
            from .comment_browser import worker
            result = worker(sys.argv[1], float(sys.argv[2]))
        else:
            result = _worker(sys.argv[1], float(sys.argv[2]))
    except Exception as exc:
        result = {"ok": False, "failure_kind": getattr(exc, 'kind', 'render_error'), "error": str(exc)[:300],
                  **getattr(exc, 'render_stats', {})}
    with open(sys.argv[3], 'w') as stream:
        json.dump(result, stream, ensure_ascii=False)
