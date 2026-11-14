"""Recognize a paper's own project page, not articles citing other papers.

HTML evidence selects a single explicitly linked paper. Its official title
must match the page before promoting it; a local Abstract is not required.
No project-host allowlist or extra LLM classification call.
"""
import re
import unicodedata
from urllib.parse import urljoin, urlsplit, urlunsplit

from lxml import html as lhtml


def _text(node) -> str:
    from copy import deepcopy
    copy = deepcopy(node)
    for br in copy.xpath('.//br'):
        br.tail = ' ' + (br.tail or '')
    return re.sub(r"\s+", " ", "".join(copy.itertext())).strip()


def page_title(html: str) -> str:
    """Prefer publisher metadata and the main heading to guessed sections."""
    try:
        root = lhtml.fromstring(html)
    except (ValueError, lhtml.etree.ParserError):
        return ''
    for xpath in ['//meta[@name="citation_title"]/@content', '//meta[@name="title"]/@content',
                  '//meta[@property="og:title"]/@content', '//meta[@name="twitter:title"]/@content']:
        # Client routers can append route metadata after the original site
        # shell. The latest value describes the page whose body was rendered.
        for value in reversed(root.xpath(xpath)):
            if value.strip():
                return re.sub(r'\s+', ' ', value).strip()
    for node in root.xpath('//h1|//title'):
        value = _text(node)
        if value and value.casefold() not in {'abstract', '摘要'}:
            # XPath union is document ordered: the main heading takes priority
            # over a short site/SEO title when no explicit title metadata exists.
            headings = root.xpath('//h1')
            if headings and _text(headings[0]).casefold() not in {'abstract', '摘要'}:
                return _text(headings[0])
            return value
    return ''


def cvf_landing_url(url: str) -> str:
    """Only CVF's canonical individual paper PDF/HTML paths qualify."""
    p = urlsplit(url)
    if p.scheme != 'https' or p.netloc != 'openaccess.thecvf.com':
        return ''
    m = re.fullmatch(r'/content/([^/]+)/(?:papers|html)/([^/]+)_paper\.(?:pdf|html)', p.path)
    if not m:
        return ''
    return urlunsplit(('https', p.netloc, f'/content/{m[1]}/html/{m[2]}_paper.html', '', ''))


def parse_cvf(html: str, url: str) -> dict:
    root = lhtml.fromstring(html)
    def values(name):
        return [v.strip() for v in root.xpath(f'//meta[@name="{name}"]/@content') if v.strip()]
    def first(name):
        return next(iter(values(name)), '')
    nodes = root.xpath('//*[@id="abstract"]')
    abstract = _text(nodes[0]) if nodes else ''
    pdf_url = first('citation_pdf_url')
    if not first('citation_title') or not abstract or cvf_landing_url(pdf_url) != url:
        return {'ok': False, 'error': 'CVF 官方页未返回完整论文标题、摘要与匹配的 PDF', 'title': '', 'text': ''}
    year = first('citation_publication_date')
    venue = first('citation_conference_title')
    if re.fullmatch(r'\d{4}', year):
        venue = f'{venue} ({year})'
    authors = []
    for author in values('citation_author'):
        family, comma, given = author.partition(',')
        authors.append((given.strip() + ' ' + family.strip()) if comma else author)
    return {'ok': True, 'error': '', 'title': first('citation_title'), 'text': abstract,
            'authors': authors, 'published': year if re.fullmatch(r'\d{4}-\d{2}-\d{2}', year) else '',
            'doi': '', 'journal_ref': venue, 'pdf_urls': [pdf_url], 'citations': None}


def fetch_cvf(url: str) -> dict:
    import httpx
    from . import config, work_budget
    try:
        response = httpx.get(url, headers={'User-Agent': 'kb-bot/1.0'},
                             timeout=work_budget.timeout(config.FETCH_TIMEOUT), follow_redirects=True)
        response.raise_for_status()
        if cvf_landing_url(str(response.url)) != url:
            raise ValueError('CVF 官方论文页重定向到了其他页面')
        return parse_cvf(response.text, url)
    except Exception as exc:
        return {'ok': False, 'error': f'CVF 官方摘要抓取失败：{exc}', 'title': '', 'text': ''}


def _title_key(text: str) -> str:
    # arXiv titles retain TeX delimiters that project headings often omit.
    text = re.sub(r"\\(?:[A-Za-z]+)", lambda m: m[0][1:], text)
    text = text.replace("π", "pi")
    return "".join(c for c in unicodedata.normalize("NFKC", text).casefold()
                   if c.isalnum())


def same_title(left: str, right: str) -> bool:
    return bool(_title_key(left)) and _title_key(left) == _title_key(right)


def _title_variants(root, project_title: str) -> list[str]:
    """A branded H1 may precede the paper title in a publication header.

    Accept only adjacent publication-title headings also present in BibTeX;
    body headings and arbitrary citations cannot supply an alternative title.
    Each alternative still requires an exact official title match in resolve.
    """
    titles = [project_title]
    headings = root.xpath('//h1')
    if not headings:
        return titles
    bib_titles = [m[1] for node in root.xpath('//pre | //code')
                  for m in re.finditer(r'\btitle\s*=\s*\{(.*?)\}\s*,', _text(node), re.I)]
    for sibling in headings[0].itersiblings():
        if not isinstance(sibling.tag, str):
            continue  # HTML comments are not headings or title evidence.
        if sibling.tag not in {'h2', 'h3', 'h4', 'h5', 'h6'}:
            break
        title = _text(sibling)
        if (sibling.tag in {'h2', 'h3'} and
                'publication-title' in sibling.get('class', '').split() and
                any(same_title(title, bib) for bib in bib_titles) and
                not any(same_title(title, previous) for previous in titles)):
            titles.append(title)
    return titles


def parse_arxiv_landing(html: str, pid: str) -> dict:
    """Read only official citation fields, never scraped page furniture."""
    from .extract import arxiv_id
    root = lhtml.fromstring(html)
    def first(name):
        return next((v.strip() for v in root.xpath(f'//meta[@name="{name}"]/@content')
                     if v.strip()), '')
    title, abstract = first('citation_title'), first('citation_abstract')
    identity = arxiv_id('https://arxiv.org/abs/' + first('citation_arxiv_id'))
    if not title or not abstract or identity != arxiv_id('https://arxiv.org/abs/' + pid):
        return {'ok': False, 'error': 'arXiv 官方页缺少完整 citation 摘要或论文身份不匹配'}
    authors = []
    for name in root.xpath('//meta[@name="citation_author"]/@content'):
        family, comma, given = name.strip().partition(',')
        authors.append((given.strip() + ' ' + family.strip()) if comma else family)
    published = first('citation_date').replace('/', '-')
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', published):
        published = ''
    return {'ok': True, 'error': '', 'title': title, 'text': abstract,
            'authors': authors, 'published': published, 'doi': '', 'journal_ref': '',
            'pdf_urls': ['https://arxiv.org/pdf/' + pid], 'citations': None}


def fetch_arxiv_landing(pid: str) -> dict:
    import httpx
    from . import config, extract, work_budget
    url = 'https://arxiv.org/abs/' + pid
    try:
        response = httpx.get(url, headers={'User-Agent': 'kb-bot/1.0'},
                             timeout=work_budget.timeout(config.FETCH_TIMEOUT), follow_redirects=True)
        response.raise_for_status()
        target = urlsplit(str(response.url))
        if (target.scheme != 'https' or target.netloc != 'arxiv.org' or
                not target.path.startswith('/abs/') or extract.arxiv_id(str(response.url)) != pid):
            raise ValueError('arXiv 官方论文页重定向到了其他页面')
        return parse_arxiv_landing(response.text, pid)
    except Exception as exc:
        return {'ok': False, 'error': f'arXiv 官方 citation 摘要抓取失败：{exc}'}


def detect(html: str, url: str, title: str) -> dict:
    from .extract import arxiv_id, doi_from_url

    try:
        root = lhtml.fromstring(html)
    except (ValueError, lhtml.etree.ParserError):
        return {}
    meta: dict[str, list[str]] = {}
    for node in root.xpath("//meta[@name][@content]"):
        meta.setdefault(node.get("name", "").lower(), []).append(node.get("content", "").strip())

    def first(key):
        return next((v for v in meta.get(key, []) if v), "")

    project_title = first("citation_title") or page_title(html) or title
    abstract = first("citation_abstract")
    if not abstract:
        # Do not take an ancestor's entire body: only the explicitly marked
        # section, or siblings up to the next section/heading.
        sections = root.xpath('//*[@id="abstract" and not(self::a)]')
        if sections:
            section = sections[0]
            paragraphs = section.xpath(".//p")
            abstract = "\n\n".join(_text(p) for p in paragraphs if _text(p))
        if not abstract:
            for heading in root.xpath("//h2 | //h3"):
                if _text(heading).casefold() not in {"abstract", "摘要"}:
                    continue
                parts = []
                for node in heading.itersiblings():
                    if node.tag in {"h1", "h2", "h3", "section"}:
                        break
                    if node.tag in {"p", "div"} and _text(node):
                        parts.append(_text(node))
                abstract = "\n\n".join(parts)
                break

    candidates: dict[tuple[str, str, str], str] = {}
    for key in ("citation_doi", "citation_arxiv_id", "citation_pdf_url"):
        value = first(key)
        if key == "citation_doi" and value:
            value = "https://doi.org/" + value.removeprefix("https://doi.org/")
        elif key == "citation_arxiv_id" and value:
            value = "https://arxiv.org/abs/" + value.removeprefix("arXiv:")
        pid, doi = arxiv_id(value), doi_from_url(value)
        cvf = cvf_landing_url(value)
        if pid or doi or cvf:
            candidates[(pid, doi, cvf)] = value
    metadata_evidence = bool(first("citation_title") and candidates)
    if not candidates:
        for a in root.xpath("//a[@href]"):
            label = _text(a).casefold()
            # Reference lists and "related work" links do not count.
            if not re.fullmatch(r"(?:paper|论文|publication|arxiv)(?:\s+(?:arxiv|pdf))?", label):
                continue
            target = urljoin(url, a.get("href", ""))
            pid, doi = arxiv_id(target), doi_from_url(target)
            cvf = cvf_landing_url(target)
            if pid or doi or cvf:
                candidates[(pid, doi, cvf)] = target
    if len(candidates) != 1 or not project_title:
        return {}
    (pid, doi, cvf), paper_url = next(iter(candidates.items()))
    # Matching self-citation permits a project Abstract fallback if a metadata
    # service is down; merely linking a paper does not permit that fallback.
    self_citation = metadata_evidence
    for pre in root.xpath("//pre | //code"):
        bib = _text(pre)
        m = re.search(r"\btitle\s*=\s*\{(.*?)\}\s*,", bib, re.I)
        if m and same_title(m[1], project_title) and (
                (pid and pid in bib) or (doi and doi.casefold() in bib.casefold())):
            self_citation = True
            break
    # Missing Abstract/BibTeX only removes the local fallback. The official
    # title check in resolve() is still required, including for linked reviews.
    return {"title": project_title, "title_variants": _title_variants(root, project_title),
            "arxiv_id": pid, "doi": doi,
            "cvf_url": cvf,
            "paper_url": paper_url, "abstract": abstract,
            "self_citation": self_citation,
            "authors": [v for v in meta.get("citation_author", []) if v]}


def resolve(project: dict) -> dict:
    """Prefer the full official abstract; never silently substitute a citation."""
    from . import extract

    pid, doi = project["arxiv_id"], project["doi"]
    cvf = project.get('cvf_url', '')
    meta = fetch_cvf(cvf) if cvf else (extract.fetch_arxiv(pid) if pid else extract.fetch_crossref(doi))
    if pid and not cvf and not meta['ok']:
        landing = fetch_arxiv_landing(pid)
        if landing['ok']:
            meta = landing
        else:
            meta = {**meta, 'error': meta.get('error', '') + '；' + landing['error']}
    variants = project.get('title_variants') or [project['title']]
    if not same_title(variants[0], project['title']):
        variants = [project['title']]  # Discard stale evidence after title edits.
    if meta["ok"] and not any(same_title(title, meta["title"]) for title in variants):
        return {"ok": False, "not_project": not project.get("self_citation"),
                "error": "项目页标题与关联论文不一致，需确认论文链接"}
    if not meta["ok"]:
        if not (project.get("self_citation") and project.get("abstract")):
            return meta
        meta = {"ok": True, "error": "", "title": project["title"],
                "text": "", "authors": project.get("authors") or [],
                "published": "", "doi": doi, "journal_ref": "", "pdf_urls": []}
    ident = 'CVF:' + cvf if cvf else ("arXiv:" + pid if pid else "DOI:" + doi)
    scholar = {} if cvf else extract.semantic_scholar(ident)
    official = meta.get("text") or scholar.get("abstract", "")
    abstract = official or project.get("abstract", "")
    if not abstract.strip():
        return {"ok": False, "error": "关联论文与项目页均没有取得摘要"}
    return {**meta, "text": abstract, "ident": ident,
            "abstract_source": (cvf or project["paper_url"]) if official else "项目页 Abstract",
            "journal_ref": meta.get("journal_ref") or scholar.get("venue", ""),
            "citations": meta.get("citations") if meta.get("citations") is not None else scholar.get("citations")}
