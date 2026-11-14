"""Switching LLM provider from chat, and never leaking the key back out.

The reason this exists: when the shared academic cluster stalls, moving to a
paid API must be a message, not a redeploy.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import config, enrich, pipeline, settings

fails = []
def check(label, cond, detail=""):
    print(("  ok  " if cond else "  FAIL") + f"  {label}" + (f"   {detail}" if detail and not cond else ""))
    if not cond: fails.append(label)

# in-memory stand-in for the Firestore document
DOC = {}
def _deep(dst, src):
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict): _deep(dst[k], v)
        else: dst[k] = v
class Ref:
    def get(self): return type("S", (), {"exists": bool(DOC), "to_dict": lambda s: dict(DOC)})()
    def set(self, d, merge=False):
        _deep(DOC, d) if merge else DOC.update(d)
    def update(self, d):
        for k in d:
            if k.startswith("providers."): DOC.get("providers", {}).pop(k.split(".", 1)[1], None)
settings._ref = lambda: Ref()
config.LLM_BASE_URL, config.LLM_MODEL, config.LLM_API_KEY = "https://llm.example/v1", "glm-5", "envkey123456"

print("1. 默认回落到部署时的配置")
settings._invalidate()
check("名字是 default", settings.active()["name"] == "default")
check("用的是 env 里的模型", settings.active()["model"] == "glm-5")

print("2. 登记商业 API 并自动切过去")
t, _ = pipeline._handle_llm(["add", "deepseek", "https://api.deepseek.com/v1",
                             "deepseek-chat", "sk-secret-abcdef123456"], lambda x: None)
check("提示删掉含 key 的消息", "建议现在就删掉" in t, t[:80])
check("已切换", settings.active()["name"] == "deepseek", settings.active()["name"])
check("端点正确", settings.active()["base_url"] == "https://api.deepseek.com/v1")
check("key 已保存", settings.active()["api_key"] == "sk-secret-abcdef123456")

print("3. 状态里绝不回显完整 key")
status = pipeline.llm_status_text()   # /llm 现在弹按钮，文本由这里产生
check("完整 key 没出现在回复里", "sk-secret-abcdef123456" not in status, status)
check("显示的是脱敏形式", "sk-s" in status and "3456" in status, status)

print("4. 只换模型，不用重填 key")
pipeline._handle_llm(["model", "deepseek-reasoner"], lambda x: None)
check("模型变了", settings.active()["model"] == "deepseek-reasoner")
check("key 没丢", settings.active()["api_key"] == "sk-secret-abcdef123456")

print("5. NRP 恢复后切回去")
t, _ = pipeline._handle_llm(["use", "default"], lambda x: None)
check("切回 default", settings.active()["name"] == "default", settings.active()["name"])
check("用回 env 配置", settings.active()["model"] == "glm-5")

print("6. enrich 真的读运行时配置")
pipeline._handle_llm(["use", "deepseek"], lambda x: None)
seen = {}
import httpx
class R:
    status_code = 200
    def json(self): return {"choices": [{"message": {"content": '{"title":"t","overview":"o","material_type":"网页","topics":[]}'}}]}
def fake_post(url, **kw):
    seen["url"] = url; seen["auth"] = kw["headers"]["Authorization"]; seen["model"] = kw["json"]["model"]
    return R()
httpx.post = fake_post
enrich.enrich("标题", "正文")
check("打到了 deepseek 端点", seen["url"].startswith("https://api.deepseek.com/v1"), seen["url"])
check("用的是切换后的模型", seen["model"] == "deepseek-reasoner", seen["model"])
check("带的是新 key", seen["auth"].endswith("sk-secret-abcdef123456"))

print("7. 错误输入不崩")
for bad in (["use"], ["use", "nope"], ["add", "只有一个参数"], ["rm", "nope"], ["乱七八糟"]):
    t, _ = pipeline._handle_llm(bad, lambda x: None)
    check(f"{bad} 有友好提示", isinstance(t, str) and len(t) > 0)

print("\n" + ("PASS" if not fails else f"FAIL: {fails}"))
sys.exit(1 if fails else 0)
