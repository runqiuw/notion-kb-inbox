"""Exercise enrich() itself with images -- no stubbing of the thing under test.

This exists because a NameError (SYSTEM_IMAGES was never defined) shipped to
production: test_images stubbed enrich.enrich wholesale, so the real function
was never run with images even once.
"""
import json, os, sys, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

seen = []

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        seen.append(body)
        reply = json.dumps({"title": "我为什么不做3D了", "overview": "作者复盘转向原因。",
                            "material_type": "帖子", "topics": ["人工智能"], "key_points": ["作者解释转向原因。"],
                            "images": [
                                {"index": 0, "informative": False, "note": "封面标题卡", "summary": "", "text": ""},
                                {"index": 1, "informative": True, "note": "", "summary": "作者复盘研究方向的变化。", "text": "正文截图的文字"}]})
        out = json.dumps({"choices": [{"message": {"content": reply}}]}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out))); self.end_headers()
        self.wfile.write(out)

srv = HTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()

from app import config, enrich, settings
config.LLM_BASE_URL = f"http://127.0.0.1:{srv.server_address[1]}/v1"
config.LLM_MODEL, config.LLM_API_KEY = "vision-model", "k"
settings._ref = lambda: type("R", (), {"get": lambda s: type("S", (), {"exists": False})()})()
settings._invalidate()

fails = []
def check(label, cond, detail=""):
    print(("  ok  " if cond else "  FAIL") + f"  {label}" + (f"   {detail}" if detail and not cond else ""))
    if not cond: fails.append(label)

print("带图调用")
en, err = enrich.enrich("原标题", "引言一句话。", enrich.POST_SUMMARY_HINT + "来自 小红书",
                        images=[(b"\xff\xd8fake0", "image/jpeg"), (b"\x89PNGfake1", "image/png")])
check("没有报错", err == "", err)
req = seen[-1]
content = req["messages"][1]["content"]
check("user content 是多段结构", isinstance(content, list), type(content).__name__)
check("两张图都发出去了",
      sum(1 for c in content if c.get("type") == "image_url") == 2, str(len(content)))
check("图片带各自真实的 mime",
      content[2]["image_url"]["url"].startswith("data:image/jpeg;base64,") and
      content[4]["image_url"]["url"].startswith("data:image/png;base64,"),
      content[4]["image_url"]["url"][:40])
check("system 里带了看图说明", "装饰性" in req["messages"][0]["content"])
image_budget = req['max_tokens']
check("max_tokens 随图片数放大", image_budget > 2400, str(image_budget))

print("解析结果")
check("原标题保持不变", en.title == "原标题", en.title)
check("两条图片判断都在", len(en.images) == 2, str(len(en.images)))
check("第 0 张判为装饰", en.images[0].informative is False)
check("第 1 张判为有信息且有文字", en.images[1].informative and en.images[1].text)
check("第 1 张总结与原文分别保留", en.images[1].summary == "作者复盘研究方向的变化。")
check("schema 要求逐图总结", "summary" in req["response_format"]["json_schema"]["schema"]["properties"]["images"]["items"]["required"])

print("不带图时不应出现看图说明")
seen.clear()
enrich.enrich("标题", "正文")
req = seen[-1]
check("user content 退回纯字符串", isinstance(req["messages"][1]["content"], str))
check("system 里没有看图说明", "装饰性" not in req["messages"][0]["content"])
check("文字与标签依据预算足够且小于带图预算", 2400 <= req["max_tokens"] < image_budget, str(req["max_tokens"]))

print("\n" + ("PASS" if not fails else f"FAIL: {fails}"))
sys.exit(1 if fails else 0)
