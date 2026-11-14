"""Exercise the enrich fallbacks against a fake OpenAI-format gateway:
   1. gateway rejects response_format  -> must retry without it
   2. reply is JSON wrapped in a code fence -> must still parse
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
        if "response_format" in body:                      # gateway says no
            self.send_response(400); self.end_headers()
            self.wfile.write(b'{"error":"response_format not supported"}'); return
        reply = '```json\n' + json.dumps({"title":"知识管理的三个阶段","overview":"讲了采集、加工、回流。","material_type":"网页","topics":["知识管理","","阅读与思考","多余"], "topic_evidence":[{"tag":"Workflow","basis":"主要讨论知识管理流程","quote":"知识管理包括采集、加工和回流"}],"tag_review":{"needed":False,"reason":"","suggestions":[]}}, ensure_ascii=False) + '\n```'
        out = json.dumps({"choices":[{"message":{"content":reply}}]}).encode()
        self.send_response(200); self.send_header("Content-Type","application/json")
        self.send_header("Content-Length",str(len(out))); self.end_headers()
        self.wfile.write(out)

srv = HTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
port = srv.server_address[1]

from app import config
config.LLM_BASE_URL = f"http://127.0.0.1:{port}/v1"
config.LLM_MODEL = "my-model"
config.LLM_API_KEY = "x"
config.LLM_USE_SCHEMA = True
from app import enrich

en, err = enrich.enrich("原标题", "知识管理包括采集、加工和回流", "测试")
print("attempts:", len(seen), "(1st with schema, 2nd without)" if len(seen)==2 else "")
print("schema on 1st:", "response_format" in seen[0])
print("schema on 2nd:", "response_format" in seen[1])
print("error:", err or "(none)")
print("title:", en.title)
print("type:", en.material_type)
print("topics:", en.topics, "<- blanks dropped, capped at 3")
assert len(seen) == 2 and err == ""
assert en.title == "原标题"
assert en.topics == ["Workflow"]  # old synonym normalized; retired/unknown suggestions dropped

# no endpoint configured -> safe fallback, material still gets saved
config.LLM_BASE_URL = ""
en2, err2 = enrich.enrich("兜底标题", "正文")
assert en2.title == "兜底标题" and "未配置" in err2
print("unconfigured fallback:", en2.title, "|", err2)
print("PASS")
