"""One-shot Cloud Run Job probe. No model, Notion or production state writes."""
import base64
import gzip
import json
import os
import time

from . import extract, render_web


def loading_boundaries():
    """Exercise the production wait loop in real Chromium, without network/state."""
    from playwright.sync_api import sync_playwright

    base = '<html><head><title>Robot Benchmark</title></head><body><main><h1>Robot <span>Benchmark</span></h1><p id="body">Loading…</p></main></body></html>'
    url = 'https://example.org/'
    shell = base.replace('</head>', '<script type="module" src="/main.js"></script></head>').replace('<main>', '<main id="root">')
    assert extract.parse_article_html(shell, url)['failure_kind'] == 'dynamic_shell'
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=['--disable-dev-shm-usage'])
        try:
            page = browser.new_page()
            page.route('**/*', lambda route: route.abort())
            page.set_content(base)
            started = time.monotonic()
            try:
                render_web._wait_content(page, started + 2.6, [])
            except render_web.FetchFailure as exc:
                assert exc.kind == 'render_timeout'
            else:
                raise AssertionError('Persistent placeholder returned as content')
            persistent_seconds = time.monotonic() - started
            delayed = base.replace('</body>', '<script>setTimeout(() => { document.getElementById("body").textContent = "Robot policies are evaluated in real environments."; }, 2200);</script></body>')
            page.set_content(delayed)
            started = time.monotonic()
            html = render_web._wait_content(page, started + 7, [])
            delayed_seconds = time.monotonic() - started
            assert delayed_seconds >= 3.2
            parsed = extract.parse_article_html(html, url)
            assert parsed['ok'] and 'real environments' in parsed['text']
            page.set_content('<article><h1>Loading</h1><p>Loading a model once saves time.</p></article>')
            short = render_web._wait_content(page, time.monotonic() + 4, [])
            assert extract.parse_article_html(short, url)['ok']
            print('BOUNDARY_PROBE ' + json.dumps({'persistent_placeholder': 'render_timeout', 'persistent_seconds': round(persistent_seconds, 3), 'delayed_body': 'ok', 'delayed_seconds': round(delayed_seconds, 3), 'short_article': 'ok'}), flush=True)
        finally:
            browser.close()


def main():
    if os.environ.get('RENDER_PROBE_MODE') == 'loading-boundaries':
        loading_boundaries()
        return
    url = os.environ.get('RENDER_PROBE_URL', 'https://robodojo-benchmark.com/')
    started = time.monotonic()
    static = extract.fetch_article(url)
    rendered = render_web.render(url)
    parsed = extract.parse_article_html(rendered['html'], rendered['url']) if rendered['ok'] else rendered
    report = {'url': url, 'static_ok': static['ok'], 'static_failure_kind': static.get('failure_kind'),
              'render': {k: v for k, v in rendered.items() if k != 'html'},
              'parsed': {k: v for k, v in parsed.items() if k != 'text'},
              'text': parsed.get('text', ''), 'elapsed_seconds': round(time.monotonic()-started, 3)}
    if rendered['ok']:
        report['html_gzip_base64'] = base64.b64encode(gzip.compress(rendered['html'].encode())).decode()
    print('DYNAMIC_PROBE ' + json.dumps(report, ensure_ascii=False), flush=True)
    if not parsed['ok']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
