"""Real platform templates, genuine comments and failure branches."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import pipeline, notion, share_text, store
import staging_stub

class Shares(unittest.TestCase):
    def setUp(self):
        self.msg={'chat':{'id':7},'message_id':1}
        self.url='https://xhslink.cn/o/test'
    def test_actual_templates_never_create_thought_on_missing_images(self):
        cases=[('2026具身真正的主线是“自进化机器人”','导读 过去两年','直达【小红书】探索笔记全文~'),
               ('具身智能主流benchmark汇总','具身智能的 benchmark 越','保留这段，去【小红书】逛逛吧~')]
        for title,body,tail in cases:
            raw=title+' '+body+'...\n'+tail
            with self.subTest(title=title),patch.multiple(store, get_staged=store.get_staged, stage_url=store.stage_url, update_chat_stage=store.update_chat_stage):
                rows=staging_stub.install(store)
                with patch.object(notion,'create_thought') as create:
                    pipeline._stage(self.msg,self.url,'缺图',raw,'图片',material_title=title,material_text=body)
                create.assert_not_called();self.assertFalse(rows[store.normalize_url(self.url)].get('attached_note'))
    def test_mixed_share_preserves_real_comment_only(self):
        raw='具身智能主流benchmark汇总 具身智能的 benchmark 越...\n保留这段，去【小红书】逛逛吧~\n我觉得需要比较真实环境与仿真的差距'
        result=pipeline.user_note(raw,'具身智能主流benchmark汇总','具身智能的 benchmark 越来越重要')
        self.assertEqual(result,'我觉得需要比较真实环境与仿真的差距')
    def test_unresolved_share_excerpt_is_durable_not_a_thought(self):
        raw='我为什么不做3D了 今天面了某大厂...\n快来【小红书】探索笔记！'
        value=share_text.classify(raw)
        self.assertEqual(value['state'],'pending');self.assertEqual(value['raw'],raw)
    def test_real_comment_before_platform_instruction_is_preserved(self):
        self.assertEqual(pipeline.user_note('我觉得这个结论反了，打开【小红书】浏览笔记。'),'我觉得这个结论反了')
    def test_long_quoted_title_does_not_erase_short_real_comment(self):
        title='一个很长的科研材料标题'+('标题细节'*15)
        note=title+'，我不认同'
        self.assertEqual(pipeline.user_note(note,title,'原文内容'),note)
        self.assertEqual(pipeline.user_note('“'+title+'”',title,'原文内容'),'')

    def test_retry_recovers_raw_pending_note_instead_of_dropping_it(self):
        row={'url':self.url,'chat_id':7,'message_id':1,'attached_note':{'state':'pending','raw':'原始附带文字'}}
        with patch.object(pipeline,'_handle_url',return_value=('ok','p')) as handle,patch.object(store,'get_staged',return_value=None):
            pipeline._retry_staged(row,lambda s:None)
        self.assertEqual(handle.call_args.args[2],'原始附带文字')

if __name__=='__main__':unittest.main()
