"""Anonymous, bounded XHS comments; reuse the disposable renderer's network guard."""
import json
import re
import time
from urllib.parse import parse_qs, urlsplit
from . import render_web, config

STATE = """() => {
 const s=window.__INITIAL_STATE__?.note;
 const id=s?.firstNoteId || location.pathname.split('/').pop();
 const entry=s?.noteDetailMap?.[id];
 if(!entry?.note) return null;
 const c=entry.comments || {};
 return JSON.parse(JSON.stringify({id, comments:c,
   count:entry.note.interactInfo?.commentCount ?? null}));
}"""
SCROLL = """() => {
 const last=document.querySelector('.comments-container .parent-comment:last-child');
 if(last) last.scrollIntoView({block:'center'});
 const nodes=[...document.querySelectorAll('.note-scroller,.note-content,.comments-container')];
 for(const n of nodes) { n.scrollTop=n.scrollHeight; }
 window.scrollBy(0,900);
}"""


def session_cookies(value):
    """Only named XHS cookies, restricted to its domain; no login state exported."""
    allowed={'web_session','a1','webId','webIdV2','gid','abRequestId'}
    cookies=[]
    for piece in value.split(';'):
        name,sep,content=piece.strip().partition('=')
        if sep and name in allowed and content and len(content)<4096:
            cookies.append({'name':name,'value':content,'domain':'.xiaohongshu.com','path':'/','secure':True})
    return cookies


def comment_response(url, status, data, note_id):
    """Only same-note read endpoints; never collect recommendations or credentials."""
    p = urlsplit(url)
    if p.hostname != 'edith.xiaohongshu.com' or p.path not in {
        '/api/sns/web/v2/comment/page', '/api/sns/web/v2/comment/sub/page'}:
        return None
    if parse_qs(p.query).get('note_id') != [note_id] or status != 200 or len(data)>1024*1024:
        return None
    try:
        value=json.loads(data)
        if value.get('success') is True and isinstance(value.get('data', {}).get('comments'), list):
            return {'path':p.path, 'root':parse_qs(p.query).get('root_comment_id',[''])[0], 'data':value['data']}
    except (ValueError, AttributeError):
        pass
    return None


def worker(url, seconds):
    from playwright.sync_api import sync_playwright
    end=time.monotonic()+seconds
    render_web.public_target(url)
    targets={}; stats={'requests':0,'fetched':0,'skipped':0,'bytes':0,'blocked':0}
    pages=[]; note_id=['']; fatal=[]
    with sync_playwright() as pw:
        browser=pw.chromium.launch(headless=True, timeout=max(1,int(seconds*1000)),
            args=['--disable-dev-shm-usage','--disable-background-networking','--disable-quic'])
        try:
            context=browser.new_context(service_workers='block',accept_downloads=False,
                viewport={'width':1440,'height':1000})
            cookies=session_cookies(config.XHS_COMMENT_COOKIE)
            if cookies: context.add_cookies(cookies)
            context.route_web_socket('**/*',lambda ws:ws.close())
            # _route_resource keeps all DNS, byte, request and redirect bounds.
            class RecordingRoute:
                def __init__(self, route, request): self.route,self.request=route,request
                def abort(self): self.route.abort()
                def fulfill(self, **kw):
                    identifier=note_id[0]
                    if identifier:
                        value=comment_response(self.request.url, kw['status'],kw['body'],identifier)
                        if value and len(pages)<12: pages.append(value)
                    self.route.fulfill(**kw)
            def route_request(route, request):
                # Short links can redirect to the exact canonical note before state exists.
                m=re.search(r'/(?:explore|discovery/item)/([a-f0-9]{24})',urlsplit(request.url).path)
                if m and request.is_navigation_request() and urlsplit(request.url).hostname == 'www.xiaohongshu.com':
                    note_id[0]=m.group(1)
                try: render_web._route_resource(RecordingRoute(route,request),request,end,targets,stats)
                except Exception as exc:
                    stats['blocked']+=1
                    if request.is_navigation_request() or isinstance(exc,render_web.FetchFailure) and exc.kind!='blocked_url': fatal.append(exc)
                    route.abort()
            context.route('**/*',route_request)
            page=context.new_page()
            context.on('page',lambda new:new.close() if new!=page else None)
            response=page.goto(url,wait_until='domcontentloaded',timeout=max(1,int((end-time.monotonic())*1000)))
            if response and response.status>=400: raise render_web.FetchFailure('http_error',f'评论页 HTTP {response.status}')
            state=None; last=None; stable=0
            while time.monotonic()<end-2:
                if fatal: raise fatal[0]
                state=page.evaluate(STATE)
                if state:
                    note_id[0]=state['id']
                    signature=json.dumps(state['comments'],ensure_ascii=False)+str(len(pages))
                    stable=stable+1 if signature==last else 0
                    last=signature
                    if len(state['comments'].get('list',[]))>=100 or stable>=5: break
                    page.evaluate(SCROLL)
                    # Expand a small number of visible reply threads, read-only.
                    if stable==1:
                        for button in page.locator('.comments-container .show-more').all()[:3]:
                            if button.is_visible(): button.click(timeout=500)
                elif '/login' in page.url or '/404' in page.url:
                    break
                page.wait_for_timeout(600)
            render_web.public_target(page.url)
            return {'ok':bool(state),'state':state,'comment_pages':pages,'url':page.url,
                    'error':'' if state else '评论需要登录、验证或页面未提供评论数据',**stats}
        finally: browser.close()
