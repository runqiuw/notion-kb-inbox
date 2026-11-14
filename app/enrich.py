"""The only LLM step in ingestion: source title, Chinese summaries, type, topics.

Talks to an OpenAI-format endpoint over /v1/chat/completions only -- the
configured endpoint has no /v1/responses. Structured output is attempted but
never assumed: OpenAI-compatible gateways vary in what response_format they
accept, so we degrade to prompt-only JSON plus tolerant parsing.

Scope is deliberately narrow (plan section 21): mechanical tidying only. No
literature search, no judgement about the material, and nothing written in the
user's voice.
"""
import base64
import json
import logging
import re

import httpx
from pydantic import BaseModel, Field, ValidationError

from . import config, settings, notion
from .art_categories import ART_CATEGORIES
from . import topics as tag_rules

log = logging.getLogger(__name__)

KNOWN_TOPICS = tag_rules.TOPICS
MATERIAL_TYPES = ["帖子", "网页", "论文", "视频", "播客", "聊天记录", "图片", "其他"]

SYSTEM_IMAGES = """

这份材料附带了图片。逐张判断：

- **装饰性**：封面标题卡、纯配图、自拍、表情包、水印、界面截图边框 —— 不含实质信息
- **有信息**：正文截图、图表、代码、笔记、说明文字 —— 含实质信息

每张图输出 index、informative、summary、text、note：
- 对有信息的图，先直接根据图片写 `summary`：用 1-3 句中文总结这一张图的实质信息，
  保留关键数字、条件及作者观点，不重复整体概览，不输出逐行 OCR。
- `text` 单独保留能确认的文字转写，供折叠查阅；不要改写或补全。图表可有总结而无转写。
- 模糊、遮挡或无法辨认的内容标为「无法辨认」，在 summary 或 note 中说明不确定性，
  不猜测数字、专有名词或缺失内容。总结不能把不确定的识别结果写成确定事实。
- 对装饰性的图，`summary` 和 `text` 留空，在 `note` 里一句话说明为什么跳过。

图片按给出的顺序编号，`index` 从 0 开始。
总的中文概览要综合正文和有信息图片的内容。"""


SYSTEM = """你在为一个中文个人知识库做材料入库整理。

只做机械性整理，不做判断：
- 已有标题非空时原样保留，不翻译、不缩写、不改写。确实没有标题时才生成简洁、具体的中文标题
- 写 2-4 句中文概览，客观复述材料说了什么
- 判断材料类型
- 同时判断展示图片的内容类别 fallback_category，只用于没有可用内容图片时选择占位图：
  paper 学术论文；article 文章／博客；tutorial 技术文档／教程；document 报告／书籍／长文档；
  post 帖子／问答；image 图片／相册；video 视频；audio 音频／播客；other 无法确定。
  按正文的用途判断，不按网站名字判断；PDF 只是格式，不能因此判为 paper。
  技术教程即使来自知乎也可判为 tutorial；缺乏依据时用 other。
- 给最多 3 个主题标签，只从候选词表里选择，不新增标签
- 标签是可多选的检索线索，不是必须归属的目录。没有合适标签可以留空，
  不要为凑数打标签。复用同义标签的已有写法。
- 视频和播客的介绍不等于字幕或转录，不得声称已经读到未提供的内容

硬性要求：
- 概览只能复述材料内容，不得加入你自己的评价、建议或推测
- 不得以用户的口吻写任何东西（不要出现「我认为」「我觉得」）
- 如果正文残缺或看不懂，如实在概览里说明，不要编造内容

只输出一个 JSON 对象，不要输出任何其他文字、解释或代码围栏：
{"title": "原标题；没有原标题时才拟定标题",
 "overview": "2-4 句中文概览",
 "material_type": "帖子|网页|论文|视频|播客|聊天记录|图片|其他",
 "fallback_category": "paper|article|tutorial|document|post|image|video|audio|other",
 "topics": ["确定的标签"],
 "topic_evidence": [{"tag":"确定的标签", "basis":"主要内容为何符合范围", "quote":"所给来源的原文片段"}],
 "tag_review": {"needed":false, "reason":"", "suggestions":[]}}"""

WEB_SUMMARY_HINT = "网页阅读摘要："
POST_SUMMARY_HINT = "图文帖阅读摘要："
PDF_SUMMARY_HINT = "PDF 阅读摘要："
VIDEO_SUMMARY_HINT = "视频阅读摘要："
SUMMARY_INPUT_CHARS = 40000
SYSTEM_PDF = """

这是一份 PDF 阅读卡，Notion 只保存中文概览、要点和 PDF 原件，不保存提取全文。
key_points 保留重要论点、方法、关键数字和限制；不要逐页复述或输出长篇翻译。
若提供扫描页，逐图 summary 简要概括该页，text 留空，不逐字转写。
来源提示中若说明仅抽样或仅读取部分页面，概览必须明确这是部分内容总结，
不得声称已经阅读或总结完整文件；不得补猜未读部分。
"""


def pdf_summary_input(text: str) -> tuple[str, str]:
    if len(text) <= SUMMARY_INPUT_CHARS:
        return text, "总结范围：全部可提取正文"
    # Spread the input budget across the document, including its conclusion.
    width = 4800
    starts = [round(i * (len(text) - width) / 7) for i in range(8)]
    sampled = "\n\n".join(f"[正文字符 {start + 1}–{start + width}]\n{text[start:start + width]}"
                           for start in starts)
    return sampled, (f"总结范围：正文共 {len(text)} 字符，分布抽取 8 个片段、共 {width * 8} 字符；"
                     "这是抽样总结，未逐段阅读全文，完整内容见 PDF 原件")
SYSTEM_WEB = """

这是一份网页或图文帖阅读卡。页面优先展示总结，原始正文和图片转写折叠保存以供核对。
最终 JSON 必须在 title、overview 等字段之外包含 "key_points": ["第一条中文要点", "第二条中文要点"]，不可省略。
key_points 为字符串数组，写中文要点总结：
- 根据材料实际信息量写 3-8 条；极短文可以少于 3 条，不凑数。
- 概览回答“这是什么、主要说了什么”；要点补充关键论点、方法、证据、数字、限制或争议，避免重复概览。
- 每条 1-3 句，保留关键术语、条件和数字；作者的观点或预测明确归于作者，不能写成已证实的事实。
- 只总结已提供的正文及图片，不补造缺失信息，不输出整篇翻译，不写泛泛评价或阅读建议。
"""

SYSTEM_VIDEO = """

这是一份视频阅读卡。材料正文来自视频的完整字幕或转录，不是简介；
正文若是按时间顺序的章节要点，它们由完整转录逐段整理而来，覆盖整段视频，概览要概括整个视频而不只是开头。
最终 JSON 必须在 title、overview 等字段之外包含 "key_points": ["第一条中文要点", "第二条中文要点"]，不可省略。
key_points 写 3-8 条中文要点：关键论点、方法、例子、数字和限制；说话人的观点归于说话人，不写成已证实的事实。
转录可能有识别错误：不确定的人名、术语照录，不补猜；不要声称看到了画面、PPT、代码或图表。
"""

SYSTEM_ABSTRACT = """

已单独提供论文摘要原文。JSON 增加 abstract_zh，逐段完整翻译为中文，保留原文的术语、数字、
条件、限制和作者语气；不得缩写成概览，不得把项目网页中的结果或描述混进摘要译文。
原文为中文时忠实保留，不编造英文。材料类型固定为论文，fallback_category 固定为 paper。
"""

# Appended only when the message carried text alongside the link, so the
# ordinary call is not made to reason about a field that does not apply.
SYSTEM_NOTE = """

用户连同链接一起发来了一段文字。判断它是不是**用户自己的想法**，
写进 note_is_yours。

不是用户想法（false）：
- 平台分享模板、App 引导语、口令、「复制这段话打开…」
- 公众号的关注引导、转发语
- 只是把材料的标题或开头复述了一遍，没有加入任何自己的判断

是用户想法（true）：
- 任何评价、疑问、联想、反对、备忘，哪怕只有半句
- 提到了平台名字但在说自己的看法

拿不准时填 true。**漏掉一句用户写的话，比留下一条模板糟糕得多**——
模板他看得见也能删，被丢掉的想法他永远不知道存在过。

只判断，不要改写这段文字。"""

JSON_SCHEMA = {
    "name": "material_enrichment",
    "schema": {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "overview": {"type": "string"},
            "key_points": {"type": "array", "items": {"type": "string"}},
            "abstract_zh": {"type": "string"},
            "material_type": {"type": "string", "enum": MATERIAL_TYPES},
            "fallback_category": {"type": "string", "enum": list(ART_CATEGORIES)},
            "topics": {"type": "array", "items": {"type": "string"}},
            **tag_rules.RESULT_FIELDS,
            "note_is_yours": {"type": "boolean"},
            "images": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer"},
                        "informative": {"type": "boolean"},
                        "note": {"type": "string"},
                        "summary": {"type": "string"},
                        "text": {"type": "string"},
                    },
                    "required": ["index", "informative", "note", "summary", "text"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["title", "overview", "material_type", "topics", "topic_evidence", "tag_review"],
        # note_is_yours stays optional: the field is only asked for when the
        # message actually carried text, and a schema that demanded it would
        # make every ordinary call answer a question about nothing.
        "additionalProperties": False,
    },
}


class ImageRead(BaseModel):
    index: int = 0
    informative: bool = False
    note: str = ""
    summary: str = ""
    text: str = ""


class _Rejected(RuntimeError):
    """The endpoint answered and refused the request -- worth one retry without
    response_format. A timeout is not this: it means the endpoint is slow, and
    retrying only doubles the wait the user sits through."""

    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


class EndpointUnavailable(RuntimeError):
    """The LLM endpoint could not be reached, or answered 5xx.

    Raised rather than returned, because the caller's correct response is to
    produce nothing at all. A page written without the model is a page missing
    its title, overview and tags -- parking the message and retrying once the
    endpoint is back beats filing something unreadable. Everything that will
    not fix itself (4xx, an unparseable answer, no endpoint configured) keeps
    returning a degraded result instead, since parking those forever would make
    a mistyped key look like silence.
    """


class Enrichment(BaseModel):
    title: str
    overview: str = ""
    key_points: list[str] = Field(default_factory=list)
    abstract_zh: str = ""
    material_type: str = "其他"
    # Optional for older gateways/stubs. Missing or invalid display metadata
    # must not turn a valid title/overview into an enrichment failure.
    fallback_category: str = ""
    topics: list[str] = Field(default_factory=list)
    topic_evidence: list[dict] = Field(default_factory=list)
    tag_review: dict = Field(default_factory=dict)
    images: list[ImageRead] = Field(default_factory=list)
    comment_points: list[dict] = Field(default_factory=list)
    comment_corrections: list[dict] = Field(default_factory=list)
    # Whether the text the user sent alongside the link is their own thought.
    # Defaults to True so that a model that ignores the question, or a path that
    # never asks it, keeps what the user wrote.
    note_is_yours: bool = True


def _extract_json(text: str) -> dict:
    """Tolerant parse: strip code fences, then take the first balanced object."""
    s = (text or "").strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.I | re.M).strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    start = s.find("{")
    if start < 0:
        raise ValueError("响应里没有 JSON 对象")
    depth, in_str, esc = 0, False, False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(s[start:i + 1])
    raise ValueError("JSON 对象没有闭合")


def _chat(messages: list[dict], use_schema: bool, max_tokens: int = 1500,
          json_schema: dict | None = None, request_timeout: float | None = None,
          disable_thinking: bool = False) -> str:
    from . import work_budget
    provider = settings.active()
    payload: dict = {
        "model": provider["model"],
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": max_tokens,
    }
    model = provider["model"].rsplit("/", 1)[-1].lower()
    if disable_thinking and (model.startswith("deepseek-v4") or model == "deepseek-flash"):
        from urllib.parse import urlsplit
        # vLLM gateways take the template switch; the official API's toggle is ignored there.
        if urlsplit(provider['base_url']).hostname in config.LLM_VLLM_HOSTS:
            payload['chat_template_kwargs'] = {'thinking': False}
        else:
            payload["thinking"] = {"type": "disabled"}
    if use_schema:
        payload["response_format"] = {"type": "json_schema",
                                      "json_schema": json_schema or JSON_SCHEMA}
    r = httpx.post(
        provider["base_url"].rstrip("/") + "/chat/completions",
        json=payload,
        headers={"Authorization": f"Bearer {provider['api_key']}",
                 "Content-Type": "application/json"},
        timeout=work_budget.timeout(config.LLM_TIMEOUT if request_timeout is None else request_timeout,
                                    reserve=60),
    )
    if r.status_code >= 400:
        raise _Rejected(f"{r.status_code} {r.text[:300]}", r.status_code)
    content = (r.json()["choices"][0]["message"] or {}).get("content")
    if not content:
        # A 200 carrying a null content is not success. Reported as its own
        # failure because it used to fall through to `content is None` with an
        # empty `err`, which put a bare "整理失败: " on the page -- a warning
        # that tells you something broke and nothing about what.
        reason = r.json()["choices"][0].get("finish_reason") or "未知"
        raise _Rejected("模型返回了空内容（结束原因：" + reason + "）", r.status_code)
    return content


def endpoint_alive() -> bool:
    """One tiny call, used only to decide whether draining the parked queue is
    worth attempting. Any answer at all counts -- a 4xx means the endpoint is up
    and something else is wrong, which the real call will report properly."""
    provider = settings.active()
    if not (provider["base_url"] and provider["model"]):
        return False
    try:
        _chat([{"role": "user", "content": "ping"}], False, max_tokens=1)
        return True
    except _Rejected as e:
        return e.status < 500
    except Exception:
        return False


def _cap_topics(topics: list[str], vocabulary: list[str] | None = None) -> list[str]:
    """Canonicalize known suggestions; never grow the vocabulary from a model."""
    from . import tag_store
    data = tag_store.load()
    if vocabulary is None:
        return tag_store.normalize(topics, data)
    allowed = {tag_rules.key(t) for t in tag_store.normalize(vocabulary, data, limit=None)}
    return [t for t in tag_store.normalize(topics, data) if tag_rules.key(t) in allowed]


def _content_with_images(prompt: str, images: list[tuple[bytes, str]]) -> list[dict]:
    parts: list[dict] = [{"type": "text", "text": prompt}]
    for i, (blob, mime) in enumerate(images):
        parts.append({"type": "text", "text": f"[图片 {i}]"})
        parts.append({"type": "image_url", "image_url": {
            "url": f"data:{mime};base64," + base64.b64encode(blob).decode()}})
    return parts


def enrich(raw_title: str, text: str, source_hint: str = "",
           images: list[tuple[bytes, str]] | None = None,
           note: str = "", paper_abstract: str = "", request_timeout: float | None = None,
           disable_thinking: bool = False, evidence_text: str | None = None,
           comments: dict | None = None) -> tuple[Enrichment, str]:
    """Returns (enrichment, error). On failure the enrichment is a safe fallback
    built from what we already know, so the material still lands in Notion.

    With `images`, one call judges which are informative, transcribes those, and
    writes the overview -- the summary call happens anyway, so reading the
    pictures costs image tokens rather than a second round trip.
    """
    fallback = Enrichment(title=raw_title.strip() or "未命名材料")
    web_summary = source_hint.startswith(WEB_SUMMARY_HINT)
    pdf_summary = source_hint.startswith(PDF_SUMMARY_HINT)
    video_summary = source_hint.startswith(VIDEO_SUMMARY_HINT)
    reading_summary = web_summary or source_hint.startswith(POST_SUMMARY_HINT) or pdf_summary or video_summary
    images = images or []
    if not (text or raw_title or images):
        return fallback, "没有可整理的内容"
    provider = settings.active()
    if not (provider["base_url"] and provider["model"]):
        return fallback, "未配置 LLM 端点，跳过自动整理"

    from . import tag_store
    vocabulary_data = tag_store.load()
    vocabulary = tag_store.names(vocabulary_data)
    prompt = (
        f"已有标题：{raw_title or '（无）'}\n"
        f"来源提示：{source_hint or '（无）'}\n"
        f"候选主题标签：{', '.join(vocabulary)}\n\n"
        f"材料正文：\n{text[:SUMMARY_INPUT_CHARS] or '（正文为空，内容可能都在图片里）'}"
    )
    if note:
        prompt += f"\n\n用户连同链接发来的文字：\n{note[:2000]}"
    if paper_abstract:
        prompt += f"\n\n论文摘要原文（完整译为 abstract_zh）：\n{paper_abstract}"
    system = SYSTEM + tag_rules.prompt(vocabulary_data) + (SYSTEM_VIDEO if video_summary else SYSTEM_WEB if reading_summary else "") + (SYSTEM_ABSTRACT if paper_abstract else "") + (SYSTEM_IMAGES if images else "") + (SYSTEM_NOTE if note else "")
    from . import comments as comment_reader
    comment_input = comment_reader.prompt(comments)
    if comment_input:
        prompt += comment_input
        system += comment_reader.SYSTEM
    if pdf_summary:
        system += SYSTEM_PDF
    user_content = _content_with_images(prompt, images) if images else prompt
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": user_content}]
    budget = (6000 if reading_summary or paper_abstract else 2400) + 1500 * len(images)
    schema = None
    if reading_summary or paper_abstract or images or comment_input:
        import copy
        schema = copy.deepcopy(JSON_SCHEMA)
    if comment_input:
        schema['schema']['properties'].update(comment_reader.FIELDS)
        schema['schema']['required'].extend(comment_reader.FIELDS)
        budget += 3000
    if reading_summary:
        schema["schema"]["required"].append("key_points")
        schema["schema"]["properties"]["key_points"]["minItems"] = 1
    if images:
        schema["schema"]["required"].append("images")
        schema["schema"]["properties"]["images"].update(minItems=len(images), maxItems=len(images))
    if paper_abstract:
        schema["schema"]["required"].append("abstract_zh")
        schema["schema"]["properties"]["abstract_zh"]["minLength"] = 1

    content, err = None, ""
    for use_schema in (config.LLM_USE_SCHEMA, False):
        try:
            extra = ({'request_timeout': request_timeout, 'disable_thinking': disable_thinking}
                     if request_timeout is not None or disable_thinking else {})
            content = (_chat(messages, use_schema, budget, json_schema=schema, **extra)
                       if schema else _chat(messages, use_schema, budget))
            break
        except _Rejected as e:
            if e.status >= 500:
                # The endpoint is having a bad time, not refusing us. Dropping
                # response_format would not help, and the next minute might.
                raise EndpointUnavailable(str(e)) from None
            err = str(e)
            log.warning("chat/completions rejected (schema=%s): %s", use_schema, e)
            if not use_schema:
                break
        except httpx.HTTPError as e:
            # Timeout or connection error: the endpoint is unwell, not picky.
            raise EndpointUnavailable(f"{type(e).__name__}: {e}") from None
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            log.warning("chat/completions failed (schema=%s): %s", use_schema, err)
            break
    if content is None:
        return fallback, f"整理失败: {err}"

    try:
        data = _extract_json(content)
        if comment_input:
            for field in comment_reader.FIELDS:
                data[field] = comment_reader.validate(data.get(field), comments)
        else:
            for field in comment_reader.FIELDS:
                data.pop(field, None)
        data['topics'] = _cap_topics(data.get('topics') if isinstance(data.get('topics'), list) else [], vocabulary)
        data['topic_evidence'] = [item for item in (data.get('topic_evidence') or [])
                                  if isinstance(item, dict)] if isinstance(data.get('topic_evidence'), list) else []
        data['tag_review'] = data.get('tag_review') if isinstance(data.get('tag_review'), dict) else {}
        if raw_title.strip():
            data["title"] = raw_title.strip()
        data["images"] = [im for im in (data.get("images") or [])
                          if isinstance(im, dict)] if images else []
        if data.get("material_type") not in MATERIAL_TYPES:
            data["material_type"] = "其他"
        if data.get("fallback_category") not in ART_CATEGORIES:
            data["fallback_category"] = ""
        result = Enrichment.model_validate(data)
        source = '\n'.join([raw_title, evidence_text if evidence_text is not None else text[:SUMMARY_INPUT_CHARS], paper_abstract,
                            *[im.text for im in result.images if im.informative]])
        result.topics, result.topic_evidence, result.tag_review = tag_rules.audit(
            data, source, vocabulary_data, vocabulary)
        result.key_points = [point.strip() for point in result.key_points if point.strip()]
        if paper_abstract:
            result.material_type, result.fallback_category = "论文", "paper"
            if not result.abstract_zh.strip():
                return result, "论文摘要中文翻译缺失"
        if reading_summary and (not result.overview.strip() or not result.key_points):
            return result, ("PDF" if pdf_summary else "网页" if web_summary else "视频" if video_summary else "图文帖") + "中文概览或要点总结缺失"
        if images and reading_summary:
            indices = [im.index for im in result.images]
            if sorted(indices) != list(range(len(images))):
                return result, "逐图总结缺失或图片编号异常"
            if any(im.informative and not im.summary.strip() for im in result.images):
                return result, "有信息图片的逐图总结缺失"
        return result, ""
    except (ValidationError, ValueError, json.JSONDecodeError) as e:
        log.warning("enrich parse failed: %s | raw=%r", e, (content or "")[:300])
        return fallback, f"整理结果解析失败: {e}"
