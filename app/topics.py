"""Starter tag vocabulary and shared rules for article/chat suggestions.

This is only the initial seed written to Firestore on first run. Replace GROUPS with
your own fields of interest (or manage tags later in Telegram with /tags).
Tag names `ML`/`LLM` and `MLSys`/`Systems` have special rules in PRINCIPLES,
audit() and tag_store.normalize(); adjust those too if you drop them.
"""

GROUPS = {'研究方向与方法': {'Agent': '用于以目标驱动的多步任务执行为核心，实质讨论规划、工具调用、记忆、环境反馈或智能体协作机制的材料。单轮问答、一次 API 调用或普通脚本自动化，不自动使用此标签。',
             'RL': '用于以奖励信号和环境交互、离线轨迹或策略评估来学习决策策略，以及实质讨论强化学习算法、奖励、价值函数或训练机制的材料。只提及 RL、比较一个 RL '
                   '基线，或仅用“试错”“反馈”描述流程，不使用此标签。',
             'Multimodal': '用于实质研究多种模态之间的联合理解、融合、对应或生成，如视觉与语言、音频与文本的交互。分别在多个单模态任务上测试同一个骨干，或仅附带图片、视频和文字，不自动使用此标签。',
             'LLM': '用于以语言模型及以语言为核心的基础模型的能力、应用、评估、局限、发展或行业影响为主要内容的材料。主要贡献为模型内部算法或架构机制时选 ML，不因实验使用语言模型而同时添加 '
                    'LLM；仅调用 LLM 的工具或提及模型名称也不自动添加。',
             'ML': '仅用于以核心机器学习算法与模型机制（core-ML）为主要贡献或讨论对象的材料，包括模型架构、注意力、位置编码、状态空间模型、学习目标、优化方法和表示学习。LLM '
                   '的能力、应用、发展与影响，Agent 的工具调用与任务编排，以及计算执行、部署与资源优化，不因属于机器学习而归入 ML；有更贴切的方法标签时优先使用该标签，不将 ML 作为所有 '
                   'AI 材料的上位标签。',
             'MLSys': '用于以机器学习计算的执行与资源利用为主要贡献或讨论对象的材料，包括算子与内核、编译、并行训练、通信、显存与缓存管理及推理服务；硬件高效计算算法须实质解决执行或资源瓶颈。单纯改进模型结构、位置编码、学习目标或训练策略，即使参数更少、复杂度更低或速度更快，也不自动归入 '
                      'MLSys。'},
 '资源与用途': {'Benchmark': '用于以评测任务、基准、测试协议、指标或评测有效性为主要贡献或讨论对象的材料，包括基准资源及系统评测报告。普通算法论文仅在现有基准上报告结果，不自动使用此标签。',
           'Dataset': '用于介绍、发布、分析或提供可复用具体数据集的材料，数据内容、构建、质量或获取方式须为主要内容。普通论文仅使用某数据集、一般数据处理方法或泛谈数据规模，不自动使用此标签。',
           'Demo': '用于材料本身提供可观看或体验的演示，且演示行为、效果或交互是主要查阅用途的材料。普通结果图、截图、示例段落或论文附带演示链接，不自动使用此标签。',
           'Tool': '用于以可复用软件、库、服务、工具的功能、使用、选型或实现为主要内容的材料。论文仅开源代码、文章提到软件名称或算法附有实现，不自动使用此标签。',
           'Tutorial': '用于以教会读者理解或完成一项具体任务为主要用途的教程、操作指南和教学推导。研究论文有方法步骤或文章包含少量示例代码，不自动使用此标签；原 Guide 统一为 '
                       'Tutorial。',
           'Survey': '用于系统组织一个研究领域的文献、方法、发展、比较和开放问题的综述材料。论文常规相关工作、短新闻回顾或零散列举几个项目，不自动使用此标签。'},
 '技术与研究实践': {'Coding': '用于以编程、软件实现、调试、重构、测试或开发实践为主要内容的材料。算法论文包含伪代码、项目存在源码或文章出现编程语言名称，不自动使用此标签。',
             'Systems': '用于以操作系统、数据库、存储、网络、分布式系统或通用计算系统的机制、架构与性能为核心的材料。神经网络模型架构不属于这里；机器学习专用执行和资源优化优先使用 '
                        'MLSys。',
             'Data': '用于以数据采集、清洗、转换、存储、治理、质量管理或数据管线的方法与实践为核心的材料。仅使用训练数据或发布一个具体数据集不自动归入 Data；具体数据集资源使用 '
                     'Dataset。',
             'Infra': '用于以部署、云资源、容器、运维、监控、服务可靠性或计算基础设施配置为主要内容的材料。模型在 GPU 上训练、运行于云端或论文报告硬件型号，不自动使用此标签。',
             'Stats': '用于以统计推断、估计、假设检验、统计模型、实验设计或统计报告方法为核心的材料。普通论文报告均值、误差条、准确率或多个实验，不自动使用此标签。',
             'Research': '用于以选题、研究问题形成、科研方法、证据组织、研究流程或科研经验为主要内容的材料。材料本身是一项研究或介绍某个具体算法，不因此添加 Research。',
             'Workflow': '用于以可重复的工作步骤、流程组织、协作、自动化或知识管理为主要内容的材料。模型有多步计算、算法有训练流程或软件提供单一功能，不自动使用此标签。',
             'Writing': '用于以写作、论证组织、论文表达、编辑、语言润色或写作规范为主要内容的材料。材料本身是一篇文章或论文，不因此添加 Writing。'},
 '其他领域': {'Design': '用于以产品、交互、界面或视觉设计的方法、决策和实践为主要内容的材料。论文中的模型设计、实验设计或普通排版，不自动使用此标签。',
          'Product': '用于以用户需求、产品功能、体验、定位或产品案例分析为核心的材料。只介绍一个模型、软件技术或公司发布新闻，没有实质产品讨论，不自动使用此标签。',
          'Business': '用于以商业模式、公司战略、市场竞争、行业结构或商业机会分析为主要内容的材料。技术能力预测、机器人研究路线争论或偶尔提到公司名称，不自动使用此标签。',
          'Econ': '用于以经济机制、宏观或微观经济现象、劳动力、生产率、市场与政策影响为核心的材料。一般公司经营、商业机会或仅提及“成本”“价格”，不自动使用此标签。'}}
TOPICS = [name for group in GROUPS.values() for name in group]
_CANONICAL = {name.casefold(): name for name in TOPICS}
_ALIASES = {
    "agents": "Agent", "ai agent": "Agent", "智能体": "Agent",
    "强化学习": "RL", "reinforcement learning": "RL",
    "多模态": "Multimodal", "大模型": "LLM", "guide": "Tutorial", "教程": "Tutorial",
    "综述": "Survey", "知识管理": "Workflow", "科研方法": "Research",
}
_RETIRED = {name.casefold() for name in (
    "Opinion", "Case Study", "Experiment", "人工智能", "AI", "神经网络", "NN",
)}


def canonical(name: str) -> str:
    name = " ".join(name.split())
    key = name.casefold()
    if key in _RETIRED:
        return ""
    return _ALIASES.get(key, _CANONICAL.get(key, name))


def clean(name: str) -> str:
    return " ".join(name.split())


def key(name: str) -> str:
    return clean(name).casefold()


def vocabulary(live=None) -> list[str]:
    """Only defined formal tags are candidates; Notion options are display data."""
    from . import tag_store
    return tag_store.names()


def prompt(data=None) -> str:
    from . import tag_store
    data = data if data is not None else tag_store.load()
    definitions = "\n".join(f"{e['name']}：{e['scope']}" for e in data['entries'] if e['enabled'])
    return PRINCIPLES + "\n当前正式词表及范围（版本 " + str(data['version']) + "）：\n" + definitions


PRINCIPLES = """
先概括材料的核心问题或主要贡献，再按当前范围选 1–3 个标签，不凑数；无合适标签可留空。
每个标签都须有主要内容的依据；背景提及、常规使用、比较基线和实验指标不能自动成为标签。
ML 限于核心学习算法和模型机制；LLM 用于语言模型能力、应用、局限与影响。
按主要内容选择 ML 或 LLM，不能因算法在语言模型上实验就同时添加 LLM。
MLSys 需要计算执行或资源瓶颈方面的实质贡献，不能仅因模型更快、更省或复杂度更低而添加。
Agent 的工具调用、规划与任务编排不因属于 AI 就归入 ML。
只从当前正式启用词表选择，不创建标签；具体模型与项目名称留在标题、正文供搜索。
分组只方便阅读，实际只有同一个多选标签字段，不必每组都选。
普通操作系统、数据库、分布式系统选 Systems，不同时选择 MLSys 和 Systems。
Data 是数据工作的技术与方法，Dataset 是具体的数据集资源。
资源标签须有主要检索用途；用了数据集、开源代码、展示示例或报告评测分数，不自动加资源标签。
topic_evidence 为每个确定标签给出 tag、basis（主要内容为何符合范围）和 quote（来源中的原文片段）。
quote 必须来自所给标题、正文、摘要、对话原发言或有信息图片的原文，不得用自己的概览冒充来源。
难以归类时 tag_review.needed=true，reason 具体区分内容不足、范围歧义或词表缺项，
suggestions 给最多三个现有标签供确认。拿不准的标签只放 suggestions，不放 topics；
能确认的标签正常保留。没有合适的现有标签时 suggestions 留空，并在 reason 中说明缺项。
用户维护的当前名称与范围优先；别名仅用于规范化输出。
"""

# Compatibility for offline inspection; runtime prompts use prompt().
RULES = PRINCIPLES

RESULT_FIELDS = {
    'topic_evidence': {'type': 'array', 'maxItems': 3, 'items': {
        'type': 'object', 'additionalProperties': False,
        'properties': {'tag': {'type': 'string'}, 'basis': {'type': 'string'}, 'quote': {'type': 'string'}},
        'required': ['tag', 'basis', 'quote']}},
    'tag_review': {'type': 'object', 'additionalProperties': False,
        'properties': {'needed': {'type': 'boolean'}, 'reason': {'type': 'string'},
                       'suggestions': {'type': 'array', 'maxItems': 3, 'items': {'type': 'string'}}},
        'required': ['needed', 'reason', 'suggestions']},
}


def audit(data, source: str, vocabulary_data=None, vocabulary=None):
    """Keep only formal tags with a source-backed rationale; never fail ingestion."""
    import re
    from . import tag_store
    vocabulary_data = vocabulary_data if vocabulary_data is not None else tag_store.load()
    allowed = set(tag_store.names(vocabulary_data))
    if vocabulary is not None:
        allowed &= set(vocabulary)
    def normalize(values):
        return [t for t in tag_store.normalize(values if isinstance(values, list) else [],
                vocabulary_data, limit=None) if t in allowed][:3]
    requested = normalize(data.get('topics'))
    raw_review = data.get('tag_review')
    raw_review = raw_review if isinstance(raw_review, dict) else {}
    needed = raw_review.get('needed') is True
    reason = raw_review.get('reason')
    reason = reason.strip()[:400] if isinstance(reason, str) else ''
    suggestions = normalize(raw_review.get('suggestions')) if needed else []
    def compact(text):
        return re.sub(r'\s+', '', text).casefold()
    source_key = compact(source)
    evidence = {}
    raw_evidence = data.get('topic_evidence')
    for item in raw_evidence if isinstance(raw_evidence, list) else []:
        if not isinstance(item, dict):
            continue
        names = normalize([item.get('tag')])
        basis, quote = item.get('basis'), item.get('quote')
        if (names and names[0] in requested and isinstance(basis, str) and basis.strip()
                and isinstance(quote, str) and 6 <= len(compact(quote)) <= 500
                and compact(quote) in source_key):
            evidence[names[0]] = {'tag': names[0], 'basis': basis.strip()[:400], 'quote': quote.strip()}
    selected = [t for t in requested if t in evidence and t not in suggestions]
    removed = [t for t in requested if t not in selected]
    if 'ML' in selected and 'LLM' in selected:
        selected = [t for t in selected if t not in {'ML', 'LLM'}]
        removed.extend(['ML', 'LLM'])
        reason = '模型同时选择 ML 和 LLM，尚未明确主要贡献是算法机制还是语言模型能力或应用。'
    if removed:
        needed = True
        suggestions = normalize([*suggestions, *removed])
        reason = reason or '模型未为部分标签提供可核对的原文依据，暂未填写这些标签。'
    if not selected and not needed:
        needed = True
        reason = '模型未返回可确认的标签及未归类原因，暂未填写主题。'
    if needed and not reason:
        reason = '模型标记了分类待确认，但未说明具体原因，需核对原文与标签范围。'
    review = {'needed': needed, 'reason': reason if needed else '',
              'suggestions': [t for t in suggestions if t not in selected] if needed else []}
    return selected, [evidence[t] for t in selected], review
