# ChatGPT / Gemini 分享会话导入

把公开分享链接发给 bot，会在想法与笔记库建一条 `类型=聊天提炼` 的笔记，不建资料页。

也可以不经过 bot，在 chatbot 里直接用「知识库：聊天提炼」skill 保存当前聊天（支持私人会话和 Claude），
见 [chat-capture-skill.md](chat-capture-skill.md)。两条路写同一个库但不共享去重，同一场聊天只用其中一条。

## 支持范围

- **ChatGPT**：`chatgpt.com/share/<UUID>`（兼容 `chat.openai.com`）。解码公开 HTML 里的
  React Router 数据，只取分享中可见的用户 / 助手发言。工具调用、analysis、thoughts 等内部节点不算正文。
- **Gemini**：`gemini.google.com/share/<ID>`、`g.co/gemini/share/<ID>`、`share.gemini.google`，
  读 `ujx1Bf` 分享 RPC，校验返回 ID 和完整问答。
- 只导入**纯文字**（含代码、列表、文字表格）。图片、附件、空发言、未知结构都进暂存区，不假装导入完整。
- 不支持 Claude 分享、私人会话、登录后内容、增量同步。

格式依据是公开页面 / RPC 的实际结构，不是稳定的官方 API，随时可能变化。参考了
[ShareXtract](https://github.com/wuaishare/sharextract)。

## 提炼与页面

1. 先把完整有序会话 JSON 和原始响应存进 GCS，再调用 LLM。
2. 模型输出中文标题、最多 3 个标签、「讨论主题」「核心结论」「待确认问题」，另外分开列出
   「用户明确表达」和「AI 建议（未确认）」。
3. 每个条目必须引用真实发言编号，代码检查：编号存在、用户观点只引用用户发言、AI 建议只引用 AI 发言。
   这防止角色串位，但不代替对内容本身的判断。

页面结构：

```
原讨论时间范围
讨论主题 / 核心结论 / 待确认问题
▸ 观点与讨论细节      ← 用户观点、AI 建议、依据
你的补充              ← Telegram 消息里除链接外的原文，不改写
▸ 完整对话            ← 灰底「用户：」、蓝底「AI：」，保留原发言时间
▸ 来源与归档          ← 分享链接、导入时间、GCS 原件地址
```

「用户」指分享对话的发言者，不推定为转发者。ChatGPT 取可见消息的 `create_time`；
Gemini 只有轮次时间，不宣称每条发言的独立时间。缺失的时间标为未知，不猜。

## 长会话

- 单次模型输入上限 60000 字符；超过时按发言边界分段（每段约 30000 字符），逐段提炼再合并，原文不截断。
- 每段、每次合并都存检查点，中断后从断点继续。
- 全部分段完成但合并失败时，把已通过校验的分段结果按栏目汇集，页面注明「未做全会话综合提炼」。
- 推理模型需要足够的输出预算（分段 8000、合并 16000 token），太小会返回空正文。

## 暂存、重试、去重

- 失败进 `staged`（`ingest_kind=chat_share`），每日自动重试和 `/retry` 都会继续，已归档的原件不重复抓取。
- 每条分享有 Firestore 租约，避免并发建页。完整内容读回成功后才把类型设为「聊天提炼」。
- 原文逐批写入，不受 Notion 每次 100 个块的限制。重试先比较已有正文前缀，发现人工修改就停，不覆盖。
- 成功后 URL 去重：重复分享只返回已有笔记，不刷新内容。编辑 Telegram 原消息也不覆盖已有聊天笔记。

## 手动导入

不经过 Telegram，直接用服务配置导入一条：

```bash
.venv/bin/python tools/import_chat.py --local-gcloud --project=<GCP_PROJECT> \
  --url='https://gemini.google.com/share/<分享ID>' --public-demo
```

`--public-demo` 会把页面标为 `样例来源=公开材料演示`。

代码：`app/chat_share.py`（解析）、`app/chat_notes.py`（提炼与写入）。
