"""Comment evidence, rank selection, thread scope, failures and both ingest paths."""
import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import httpx
from app import comments, comment_browser, config, enrich, pipeline, render_web, storage, store, image_batches
from test_image_batches import Images


def row(cid,text,likes=1,**kw):
    return {'id':cid,'content':text,'like_count':likes,'user_info':{'nickname':'读者'},**kw}


def fixture():
    rows=comments.normalize([row('a','正文中的成功率不是总体成功率，是筛选后的子集。',999,
        sub_comments=[row('b','作者补充：确实只统计了筛选后的结果。',80,is_author=True)])],'小红书','https://xhslink.cn/o/test')
    all_rows,selected=comments.select(rows)
    return {'platform':'小红书','source_url':'https://xhslink.cn/o/test','captured_at':'2026-10-12T00:00:00Z',
            'rows':all_rows,'selected':selected,'fetched':len(all_rows),'ranking':'样本赞数排序','scope':'有限评论样本',
            'status':'partial','archive_uri':'gs://test/comments.json'}


class Comments(unittest.TestCase):
    def test_likes_counts_html_and_author_reply(self):
        self.assertEqual(comments.count('1.2万'),12000);self.assertIsNone(comments.count('赞'))
        values=comments.normalize([row('a','<p>纠正：不是 10%</p>',200,sub_comments=[row('b','<p>已更正。</p>',15,show_tags=['作者'])])],'小红书','url')
        self.assertEqual(values[1]['parent_id'],'a');self.assertTrue(values[1]['is_author'])
        self.assertEqual(values[0]['text'],'纠正：不是 10%')

    def test_sample_high_likes_dedupe_lower_rank_correction_and_parent(self):
        source=[row(str(i),'高赞观点 '+str(i),1000-i) for i in range(35)]
        source += [row('fix','纠正：使用范围仅限模拟器',2),row('fix','纠正：使用范围仅限模拟器',2),row('parent','请解释分母',0,sub_comments=[row('reply','补充统计条件',1200)])]
        normalized=comments.normalize(source,'小红书','url')
        rows,selected=comments.select(normalized)
        ids=[r['id'] for r in selected]
        self.assertIn('fix',ids);self.assertIn('parent',ids);self.assertLess(ids.index('parent'),ids.index('reply'))
        self.assertEqual(sum(r['id']=='fix' for r in rows),1)
        self.assertLessEqual(len(json.dumps(selected,ensure_ascii=False)),comments.INPUT_CHARS+300)

    def test_unknown_like_does_not_become_zero_or_global_highest(self):
        values=comments.normalize([row('a','观点','赞')],'知乎','url')
        self.assertIsNone(values[0]['likes'])
        capture=fixture();capture['rows'][0]['likes']=None;capture['selected'][0]['likes']=None
        en=enrich.Enrichment(title='标题',comment_points=[{'text':'评论者指出统计范围有限。','comment_ids':['a']}])
        text=str(comments.blocks(en,capture));self.assertIn('赞数未知',text)

    def test_cookie_scope_and_no_unrelated_session_material(self):
        cookies=comment_browser.session_cookies('web_session=hidden; a1=device; z_c0=private; csrf=private')
        self.assertEqual({c['name'] for c in cookies},{'web_session','a1'})
        self.assertTrue(all(c['domain']=='.xiaohongshu.com' and c['secure'] for c in cookies))

    def test_question_link_never_expands_answers(self):
        with patch.object(config,'ZHIHU_DC0','device'),patch.object(comments.httpx,'get') as get:
            with self.assertRaisesRegex(ValueError,'问题链接不展开回答'):
                comments.zhihu('https://www.zhihu.com/question/123',25)
        get.assert_not_called()

    def test_response_collection_rejects_other_notes_and_non_read_paths(self):
        data=json.dumps({'success':True,'data':{'comments':[row('a','test')]}}).encode()
        url='https://edith.xiaohongshu.com/api/sns/web/v2/comment/page?note_id=note'
        self.assertIsNotNone(comment_browser.comment_response(url,200,data,'note'))
        self.assertIsNone(comment_browser.comment_response(url,200,data,'other'))
        self.assertIsNone(comment_browser.comment_response(url.replace('/page?','/post?'),200,data,'note'))
        self.assertIsNone(comment_browser.comment_response(url.replace('edith.xiaohongshu.com','evil.example'),200,data,'note'))

    def test_xhs_state_and_network_comments_keep_reply_context_no_recommendations(self):
        out={'ok':True,'url':'https://www.xiaohongshu.com/explore/note','state':{'id':'note','count':5,'comments':{'list':[row('a','主评论',20)],'hasMore':True}},
             'comment_pages':[{'root':'a','data':{'comments':[row('b','作者更正',10,is_author=True),row('c','别的帖子',10,note_id='other')],'has_more':False}}]}
        with patch.object(render_web,'render',return_value=out): result=comments.xhs('url',20)
        self.assertEqual([r['id'] for r in result['rows']],['a','b']);self.assertEqual(result['rows'][1]['parent_id'],'a')
        self.assertFalse(result['complete'])

    def test_zhihu_answer_comments_use_fixed_pagination_host(self):
        urls=[]
        answers={'data':[{'id':456}]}
        root={'data':[{'id':1,'content':'<p>错误数字</p>','like_count':40,'author':{'member':{'name':'读者'}}}],
              'paging':{'is_end':False,'next':'http://169.254.169.254/path?offset=next'}}
        empty={'data':[],'paging':{'is_end':True}}
        def get(url,**kw):
            urls.append((url,kw))
            data=answers if url.endswith('/answers') else root if kw['params']['offset']=='' and url.endswith('/root_comment') else empty
            return httpx.Response(200,json=data,request=httpx.Request('GET',url))
        with patch.object(config,'ZHIHU_DC0','device'),patch.object(comments.httpx,'get',side_effect=get):
            result=comments.zhihu('https://www.zhihu.com/question/123/answer/456',25)
        self.assertEqual(result['source_url'],'https://www.zhihu.com/question/123/answer/456')
        self.assertTrue(all(u.startswith('https://www.zhihu.com/api/') for u,_ in urls))
        self.assertTrue(any('/answers/456/root_comment' in u for u,_ in urls))
        self.assertTrue(any(k['params'].get('offset')=='next' for _,k in urls))
        self.assertEqual(result['rows'][0]['author'],'读者')
        self.assertTrue(all(k['cookies']=={'d_c0':'device'} for _,k in urls))

    def test_partial_network_failure_preserves_fetched_comments(self):
        root={'data':[{'id':1,'content':'补充正文','like_count':40}],'paging':{'is_end':False,'next':'https://www.zhihu.com/api/?offset=20'}}
        with patch.object(config,'ZHIHU_DC0','device'),patch.object(comments.httpx,'get',side_effect=[httpx.Response(200,json=root,request=httpx.Request('GET','https://www.zhihu.com')),httpx.ReadTimeout('test')]):
            result=comments.zhihu('https://www.zhihu.com/question/123/answer/456',25)
        self.assertEqual(len(result['rows']),1);self.assertIn('ReadTimeout',result['error'])

    def test_xhs_disabled_before_browser_network_or_archive(self):
        with patch.object(comments,'xhs') as xhs,patch.object(storage,'archive') as archive:
            self.assertIsNone(comments.capture('https://xhslink.cn/o/test','小红书'))
        xhs.assert_not_called();archive.assert_not_called()

    def test_capture_failure_and_empty_are_distinct_and_archive_is_complete(self):
        with patch.object(comments,'zhihu',side_effect=ValueError('设备 Cookie 未配置')),patch.object(comments.work_budget,'remaining',return_value=260):
            result=comments.capture('https://zhuanlan.zhihu.com/p/123','知乎')
        self.assertEqual(result['status'],'unavailable');self.assertIn('设备 Cookie 未配置',str(comments.blocks(enrich.Enrichment(title='标题'),result)))
        with patch.object(comments,'zhihu',return_value={'rows':[],'raw':{'comments':[]},'source_url':'url','ranking':'页','complete':True}),patch.object(comments.work_budget,'remaining',return_value=260),patch.object(storage,'archive',return_value='gs://test/archive') as archive:
            result=comments.capture('https://zhuanlan.zhihu.com/p/123','知乎')
        self.assertEqual(result['status'],'empty');self.assertEqual(json.loads(archive.call_args.args[0])['raw'],{'comments':[]})

    def test_model_refs_required_and_post_summary_separate(self):
        answer={'title':'原标题','overview':'帖子作者介绍测试方法。','key_points':['帖子结论。'],'topics':[],
                'comment_points':[{'text':'评论者指出统计范围有限。','comment_ids':['a','b']}],
                'comment_corrections':[{'text':'评论者质疑分母；页面标注作者承认统计子集。','comment_ids':['a','b']}]}
        prompts=[]
        def model(messages,*a,**kw): prompts.append((messages,kw));return json.dumps(answer,ensure_ascii=False)
        with patch.object(enrich.settings,'active',return_value={'base_url':'test','model':'test'}),patch.object(enrich,'_chat',side_effect=model):
            en,error=enrich.enrich('原标题','帖子原文',enrich.POST_SUMMARY_HINT,comments=fixture())
            self.assertFalse(error);self.assertEqual(en.overview,'帖子作者介绍测试方法。')
            text=str(pipeline._web_blocks('url',en,[],'帖子原文',0,fixture()))
            self.assertIn('评论中的纠错与争议',text);self.assertIn('999 赞',text);self.assertIn('尚未进行外部事实核验',text)
            self.assertIn('所采集评论原文',text)
            self.assertIn('comment_corrections',prompts[0][1]['json_schema']['schema']['required'])
            self.assertIn('主题标签仍只总结正文/图片',prompts[0][0][0]['content'])
            answer['comment_points'][0]['comment_ids']=['invented']
            _,error=enrich.enrich('原标题','帖子原文',enrich.POST_SUMMARY_HINT,comments=fixture());self.assertTrue(error)
            answer['comment_points']=[];answer.pop('comment_corrections')
            _,error=enrich.enrich('原标题','帖子原文',enrich.POST_SUMMARY_HINT,comments=fixture());self.assertTrue(error)
            en,error=enrich.enrich('原标题','帖子原文',enrich.POST_SUMMARY_HINT)
            self.assertFalse(error);self.assertFalse(en.comment_points)

    def test_unavailable_comments_do_not_block_body(self):
        en=enrich.Enrichment(title='标题',overview='正文概览',key_points=['正文要点'])
        capture={'status':'unavailable','error':'需要登录'}
        with patch.object(store,'find_by_url',return_value=None),patch.object(store,'pending_url_owner',return_value=None),patch.object(config,'GCP_PROJECT',''),patch.object(pipeline,'fetch_for',return_value={'ok':True,'title':'标题','text':'正文','images':[]}),patch.object(comments,'capture',return_value=capture),patch.object(enrich,'enrich',return_value=(en,'')),patch.object(storage,'archive_text',return_value='gs://test/body'),patch.object(pipeline,'_create_material',return_value={'id':'page','url':'https://notion/page'}) as create,patch.object(pipeline,'_claim_staged'):
            _,url=pipeline._handle_url({'chat':{'id':1},'message_id':2},'https://zhuanlan.zhihu.com/p/123','')
        self.assertTrue(url);self.assertIn('评论未采集：需要登录',str(create.call_args.kwargs['blocks']))


class CommentImages(Images):
    # Test only new integration; inherited image tests are run in their own script.
    def test_comments_frozen_across_image_continuation(self):
        art=self.art(13);art['comments']=fixture();self.exhaust=True
        original=self.model
        def model(messages,*a,**kw):
            if isinstance(messages[1]['content'],list): return original(messages,*a,**kw)
            self.assertIn('评论区样本',messages[1]['content'])
            return json.dumps({'title':'来源','overview':'正文概览','key_points':['正文要点'],'material_type':'帖子',
                'comment_points':[{'text':'评论者补充统计条件。','comment_ids':['a']}],'comment_corrections':[{'text':'评论者质疑分母。','comment_ids':['a','b']}],'topics':[]})
        self.p(enrich,'_chat',model)
        image_batches.handle(self.msg,'https://xhslink.cn/o/batch-test',art,'小红书','',lambda _:None)
        self.exhaust=False;self.left=float('inf')
        # Frozen snapshot must win over a changed fetch on the next turn.
        _,url=image_batches.handle(self.msg,'https://xhslink.cn/o/batch-test',self.art(13),'小红书','',lambda _:None)
        self.assertTrue(url)
        text=self.n.text(next(iter(self.n.pages)))
        self.assertIn('评论者质疑分母',text);self.assertIn('作者补充：确实只统计了筛选后的结果',text)

# Keep image integration independent without executing the inherited suite twice.
for name in list(vars(Images)):
    if name.startswith('test_'): setattr(CommentImages,name,None)

del Images

if __name__=='__main__':unittest.main()
