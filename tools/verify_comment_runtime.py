"""Exercise real current model on labelled synthetic comments; never writes Notion/Telegram."""
import os
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from import_chat import configure_local
from test_comments import fixture
from app import enrich

def main():
    configure_local(os.environ['GCP_PROJECT'])
    capture=fixture()
    en,error=enrich.enrich('评论总结验证（合成样本）',
        '这是一份合成测试材料。帖子作者介绍机器人测试，并声称报告中的成功率反映总体表现。',
        enrich.POST_SUMMARY_HINT+'合成样本，仅验证观点归属和评论引用，不入库',
        comments=capture,request_timeout=120,disable_thinking=True)
    if error: raise RuntimeError(error)
    assert en.comment_points or en.comment_corrections,'Substantive comments were omitted'
    output={'synthetic_fixture':True,'notion_writes':0,'telegram_messages_sent':0,
            'overview':en.overview,'key_points':en.key_points,'comment_points':en.comment_points,
            'comment_corrections':en.comment_corrections,'citations_validated':True}
    print(json.dumps(output,ensure_ascii=False))
if __name__=='__main__':main()
