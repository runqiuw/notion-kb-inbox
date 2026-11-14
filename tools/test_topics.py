"""Candidate labels stay precise across articles, chats and offline fallback."""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import enrich, topics

assert len(topics.TOPICS) == len(set(topics.TOPICS)) == 24
assert [len(group) for group in topics.GROUPS.values()] == [6, 6, 8, 4]
assert enrich._cap_topics(["agent", " AI Agent ", "智能体", "rl"]) == ["Agent", "RL"]
assert enrich._cap_topics(["Guide", "Tutorial", "综述", "大模型"]) == [
    "Tutorial", "Survey", "LLM"]
assert enrich._cap_topics(["Opinion", "Case Study", "Experiment", "AI", "NN"]) == []
assert enrich._cap_topics(["Systems", "MLSys", "Infra", "Data"]) == ["MLSys", "Infra", "Data"]
assert enrich._cap_topics(["Systems", "Data"]) == ["Systems", "Data"]
assert enrich._cap_topics(["Data", "Dataset"]) == ["Data", "Dataset"]
assert enrich._cap_topics(["Agent", "RL", "Multimodal", "Tool"]) == ["Agent", "RL", "Multimodal"]
assert enrich._cap_topics(["unknown", None, " ", "Agent"]) == ["Agent"]
assert enrich._cap_topics(["unknown", "Agent"], []) == []
assert topics.vocabulary([]) == topics.TOPICS
assert topics.vocabulary(["强化学习", "知识管理", "TTA", "AI"]) == topics.TOPICS
assert enrich._cap_topics(["MY TAG", "unknown"], topics.vocabulary(["MY TAG"])) == []
assert "Data 是数据工作的技术与方法" in topics.prompt()
assert "Survey：用于系统组织一个研究领域的文献" in topics.prompt()
assert 'ML 限于核心学习算法' in topics.prompt()
print("PASS: 24 candidates, aliases, exclusions, specificity and offline fallback")
