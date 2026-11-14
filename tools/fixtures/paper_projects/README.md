# 论文项目页样本

都是知名论文的公开项目页，用普通 HTTP GET 抓取，无登录信息，用于离线测试项目页识别。

| 文件 | 论文 | 测什么 |
|---|---|---|
| `voyager.html` | Voyager（arXiv 2305.16291） | 有 Abstract 小节和匹配的 BibTeX：识别为论文、自身引用成立、摘要截取不越界 |
| `dust3r.html`、`dust3r-cvf.html` | DUSt3R（CVPR 2024） | 论文链接指向 CVF：从 CVF 官方页取摘要和会议，只有年份时不编造日期 |
| `pix2pix.html`、`taming-transformers.html` | pix2pix（1611.07004）、Taming Transformers（2012.09841） | 项目页没有 Abstract，BibTeX 里没有 arXiv 编号：必须用官方标题核对 |
| `1611.07004.json`、`2012.09841.json` | 同上 | 对应的 arXiv API 解析结果 |
| `llava.html`、`llava-arxiv.html` | LLaVA（2304.08485） | 品牌名做主标题、副标题才是论文标题；arXiv 页面 `citation_*` 字段作为 API 限流时的备用 |

测试另外会向页面追加一个 Attention Is All You Need（1706.03762）链接，验证出现多篇候选论文时不升级为论文。

页面链接只提供候选；必须通过官方论文标题核对，不能把引用他人论文的博客或多论文页面直接升级为论文。
