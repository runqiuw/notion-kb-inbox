"""Inline pickers: choose a provider or model by button, then tidy up.

Telegram caps callback_data at 64 bytes, so buttons carry an index and the real
values are looked up -- a model id can be arbitrarily long.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import config, pipeline, settings, telegram

fails = []
def check(label, cond, detail=""):
    print(("  ok  " if cond else "  FAIL") + f"  {label}" + (f"   {detail}" if detail and not cond else ""))
    if not cond: fails.append(label)

PICKS, sent, edited, deleted, answered = {}, [], [], [], []
settings.save_picks = lambda k, items, trigger=0: PICKS.__setitem__(k, (list(items), trigger))
settings.load_picks = lambda k: PICKS.get(k, ([], 0))
telegram.send = lambda cid, text, **kw: (sent.append((text, kw.get("markup"))),
                                         {"message_id": 500 + len(sent)})[1]
telegram.edit = lambda cid, mid, text, **kw: edited.append((mid, text, kw.get("markup")))
telegram.delete = lambda cid, mid: deleted.append(mid)
telegram.answer_callback = lambda cid, text="": answered.append(text)

DOC = {}
def _deep(d, s):
    for k, v in s.items():
        if isinstance(v, dict) and isinstance(d.get(k), dict): _deep(d[k], v)
        else: d[k] = v
settings._ref = lambda: type("R", (), {
    "get": lambda s: type("S", (), {"exists": bool(DOC), "to_dict": lambda x: dict(DOC)})(),
    "set": lambda s, d, merge=False: _deep(DOC, d),
    "update": lambda s, d: None})()
config.LLM_BASE_URL, config.LLM_MODEL = "https://llm.example/v1", "glm-5"
config.LLM_API_KEY = "sk-REALSECRET-9876543210"   # 必须一个字都不回显
settings._invalidate()
settings.add("deepseek", "https://api.deepseek.com/v1", "deepseek-chat", "sk-x")
settings.use("default")

print("1. /llm 弹出配置按钮")
pipeline.send_provider_picker(chat_id=1, trigger_message_id=42)
text, markup = sent[-1]
btns = [b["text"] for row in markup["inline_keyboard"] for b in row]
check("列出了两个配置", "✅ default" in btns and "deepseek" in btns, str(btns))
check("当前项打了勾", any(b.startswith("✅") for b in btns))
check("有换模型/试跑/关闭", {"换模型", "试跑", "关闭"} <= set(btns), str(btns))
# 「」 rather than <> -- the same strings go out with parse_mode="HTML",
# where a literal <名字> is an unsupported tag and the send 400s.
check("写明了 add 的写法", "/llm add 「名字」「base_url」「模型」「key」" in text, text[-200:])
check("给了可照抄的例子", "https://api.deepseek.com/v1" in text)
check("rm 和 model 也说了", "/llm rm" in text and "/llm model" in text)
check("完整 key 一个字都没回显", config.LLM_API_KEY not in text, text[:120])
check("显示的是脱敏形式", "…" in text, text[:120])
check("记下了触发消息 id", PICKS[pipeline._pick_key(1, 501)][1] == 42)

print("2. 点一个配置 → 切换并清理")
cb = {"id": "c1", "data": "lp:1", "message": {"message_id": 501, "chat": {"id": 1}}}
pipeline.handle_callback(cb)
check("切到了 deepseek", settings.active()["name"] == "deepseek", settings.active()["name"])
check("删掉了按钮消息", 501 in deleted, str(deleted))
check("也删掉了 /llm 那条", 42 in deleted, str(deleted))
check("回调已应答", any("deepseek" in a for a in answered), str(answered))

print("3. 模型选择器带翻页")
settings.list_models = lambda p=None: ([f"model-{i}" for i in range(20)], "")
sent.clear(); PICKS.clear()
pipeline.send_model_picker(chat_id=1, trigger_message_id=43, page=0)
mk = sent[-1][1]
labels = [b["text"] for row in mk["inline_keyboard"] for b in row]
check("每页 8 个", sum(1 for l in labels if l.startswith("model-")) == pipeline.PAGE_SIZE, str(labels))
check("有下一页", "下一页 ▶" in labels, str(labels))
check("第一页没有上一页", "◀ 上一页" not in labels)

print("4. 翻到第二页")
mid = 500 + len(sent)
pipeline.handle_callback({"id": "c2", "data": "lmp:1",
                          "message": {"message_id": mid, "chat": {"id": 1}}})
mk2 = edited[-1][2]
labels2 = [b["text"] for row in mk2["inline_keyboard"] for b in row]
check("两个方向都能翻", "◀ 上一页" in labels2 and "下一页 ▶" in labels2, str(labels2))
check("显示的是第二页内容", "model-8" in " ".join(labels2), str(labels2))

print("5. 选一个模型")
deleted.clear()
pipeline.handle_callback({"id": "c3", "data": "lm:9",
                          "message": {"message_id": mid, "chat": {"id": 1}}})
check("模型换成了 model-9", settings.active()["model"] == "model-9", settings.active()["model"])
check("清理了菜单", mid in deleted, str(deleted))

print("6. callback_data 全部在 64 字节内")
allb = [b for row in mk["inline_keyboard"] for b in row]
check("每个按钮都合法",
      all(len(b["callback_data"].encode()) <= telegram.CALLBACK_DATA_LIMIT for b in allb))

print("7. 过期按钮不崩")
answered.clear()
pipeline.handle_callback({"id": "c4", "data": "lp:99",
                          "message": {"message_id": 99999, "chat": {"id": 1}}})
check("给了过期提示", any("过期" in a for a in answered), str(answered))

print("8. 取不到模型列表时有兜底")
settings.list_models = lambda p=None: ([], "HTTP 503")
sent.clear()
pipeline.send_model_picker(chat_id=1, trigger_message_id=44)
check("提示了原因并给出手动办法",
      "503" in sent[-1][0] and "/llm model" in sent[-1][0], sent[-1][0])

print("\n" + ("PASS" if not fails else f"FAIL: {fails}"))
sys.exit(1 if fails else 0)
