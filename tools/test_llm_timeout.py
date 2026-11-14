"""A slow endpoint must be waited on once, not twice -- and must not produce.

The retry exists for a gateway that rejects `response_format`; it answered, it
just refused. A timeout means the endpoint is unwell, and retrying only doubles
the wait the user sits through -- 2x120s once pushed one ingestion to 254s.

Unreachable now raises EndpointUnavailable rather than returning a fallback: a
page without a title, overview or tags is not worth filing, so the caller parks
the message instead. The one-attempt rule is unchanged and still the point.
"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import httpx
from app import config, enrich

fails = []
def check(label, cond, detail=""):
    print(("  ok  " if cond else "  FAIL") + f"  {label}" + (f"   {detail}" if detail and not cond else ""))
    if not cond: fails.append(label)

config.LLM_BASE_URL = "http://127.0.0.1:1/v1"
config.LLM_MODEL = "m"
config.LLM_API_KEY = "k"
config.LLM_USE_SCHEMA = True

print("超时：只尝试一次")
attempts = []
def timing_out(messages, use_schema, max_tokens=1500):
    attempts.append(use_schema)
    raise httpx.ReadTimeout("The read operation timed out")
enrich._chat = timing_out
raised = ""
try:
    enrich.enrich("原标题", "正文")
except enrich.EndpointUnavailable as e:
    raised = str(e)
check("只调用了 1 次", len(attempts) == 1, f"调用 {len(attempts)} 次: {attempts}")
check("抛 EndpointUnavailable 而不是产出降级页", bool(raised), "没有抛")
check("错误里说明了超时", "Timeout" in raised or "timed out" in raised, raised)

print("被拒绝（400）：重试一次，不带 schema")
attempts.clear()
def rejects_schema(messages, use_schema, max_tokens=1500):
    attempts.append(use_schema)
    if use_schema:
        raise enrich._Rejected("400 response_format not supported")
    return '{"title":"标题","overview":"概览","material_type":"网页","topics":[]}'
enrich._chat = rejects_schema
en, err = enrich.enrich("原标题", "正文")
check("调用了 2 次", len(attempts) == 2, str(attempts))
check("第二次不带 schema", attempts == [True, False], str(attempts))
check("最终成功且保留原标题", err == "" and en.title == "原标题", f"{err} {en.title}")

print("连接错误：也只试一次")
attempts.clear()
def refuses(messages, use_schema, max_tokens=1500):
    attempts.append(use_schema)
    raise httpx.ConnectError("connection refused")
enrich._chat = refuses
try:
    enrich.enrich("原标题", "正文")
    check("抛 EndpointUnavailable", False, "没有抛")
except enrich.EndpointUnavailable:
    check("抛 EndpointUnavailable", True)
check("只调用了 1 次", len(attempts) == 1, str(attempts))

print("超时上限已收紧")
check("LLM_TIMEOUT <= 60s", config.LLM_TIMEOUT <= 60, str(config.LLM_TIMEOUT))
check("最坏情况 < Cloud Run 300s 上限", config.LLM_TIMEOUT * 2 < 300, str(config.LLM_TIMEOUT))

print("\n" + ("PASS" if not fails else f"FAIL: {fails}"))
sys.exit(1 if fails else 0)
