"""Complete multi-batch ingest, real checkpoint paths, resume and lost writes."""
import base64
import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import httpx
from app import config, enrich, image_batches as batch, notion, pipeline, settings, storage, store, tag_store, telegram, work_budget, tasks
from test_park_expiry import Database
from test_link_thoughts import NotionMemory

URL='https://xhslink.cn/o/batch-test'

class Memory(NotionMemory):
    lose_create=False
    lose_append=False
    def call(self,method,path,payload=None):
        if path.startswith('/databases/'):
            matches=[copy.deepcopy(p) for p in self.pages.values() if p.get('parent',{}).get('database_id')=='materials' and not p.get('archived')]
            return {'results':matches}
        result=super().call(method,path,payload)
        for page in [*self.pages.values(), result] if isinstance(result,dict) else []:
            for prop in page.get('properties',{}).values():
                prop.setdefault('type',next((k for k in prop if k in ('title','rich_text','select','multi_select','url','relation')),''))
                for text in prop.get('rich_text',[]): text.setdefault('plain_text',text.get('text',{}).get('content',''))
        if method=='POST' and path=='/pages' and self.lose_create:
            self.lose_create=False
            raise RuntimeError('POST response lost')
        if method=='PATCH' and path.endswith('/children') and self.lose_append:
            self.lose_append=False
            raise RuntimeError('PATCH response lost')
        return result

class Images(unittest.TestCase):
    def setUp(self):
        self.db=Database(); self.n=Memory(); self.files={}; self.downloaded=[]; self.calls=[]; self.left=float('inf'); self.fail_call=None
        self.p(store,'_db',self.db); self.p(store.firestore,'transactional',lambda f:f)
        self.p(config,'GCP_PROJECT','test'); self.p(config,'GCS_BUCKET','test'); self.p(config,'NOTION_MATERIAL_DB','materials'); self.p(config,'NOTION_THOUGHT_DB','thoughts')
        self.p(notion,'_call',self.n.call); self.p(notion,'_create_tagged_page',lambda payload:self.n.call('POST','/pages',payload))
        self.p(notion,'related_topics',lambda ids:[])
        self.p(pipeline.thumbnails,'material_art',lambda *a,**k:None)
        self.p(telegram,'note_deeplink',lambda pid:'https://t.me/test?start='+pid)
        self.p(settings,'active',lambda:{'base_url':'https://test/v1','model':'test','api_key':''})
        self.p(tag_store,'load',lambda *a,**k:{'version':1,'entries':[]})
        self.p(tag_store,'normalize',lambda names,*a,**k:[])
        self.p(storage,'archive',self.archive); self.p(storage,'read_archive',lambda uri,**k:self.files[uri])
        self.p(extract_httpx(),'get',self.download)
        self.original_chat=enrich._chat
        self.p(enrich,'_chat',self.model)
        self.p(work_budget,'remaining',lambda:self.left)
        self.enqueue=self.p(tasks,'enqueue_images',lambda *a:True)
        self.msg={'chat':{'id':7},'message_id':1}
    def p(self,obj,name,value):
        q=patch.object(obj,name,value);q.start();self.addCleanup(q.stop);return value
    def archive(self,data,name,mime='text/plain'):
        uri='gs://test/'+name;self.files[uri]=data;return uri
    def download(self,url,**kw):
        i=int(url.rsplit('/',1)[-1]);self.downloaded.append(i)
        return httpx.Response(200,content=b'\x89PNG\r\n\x1a\n'+str(i).encode(),request=httpx.Request('GET',url))
    def model(self,messages,schema,max_tokens=2400,**kw):
        content=messages[1]['content']
        if isinstance(content,list):
            ids=[int(base64.b64decode(p['image_url']['url'].split(',')[1])[8:]) for p in content if p.get('type')=='image_url']
            self.calls.append(ids)
            if len(self.calls)==self.fail_call: raise httpx.ReadTimeout('timeout')
            if len(self.calls)==2 and getattr(self,'exhaust',False): self.left=100
            return json.dumps({'images':[{'index':i,'informative':True,'summary':f'图片 {value} 的总结','text':f'原文 {value}','note':''} for i,value in enumerate(ids)]},ensure_ascii=False)
        return json.dumps({'title':'来源','overview':'材料整体概览','material_type':'帖子','key_points':['全部图片的要点'],'topics':[],'note_is_yours':False},ensure_ascii=False)
    def art(self,n):
        return {'ok':True,'title':'来源','text':'完整帖子原文','images':['https://cdn/'+str(i) for i in range(n)]}
    def run_import(self,n):
        return batch.handle(self.msg,URL,self.art(n),'小红书','来源\n保留这段，去【小红书】逛逛吧~',lambda s:None)
    def check_complete(self,n):
        job=store.get_image_job(URL)
        self.assertEqual(job['status'],'done');self.assertEqual(job['completed'],n)
        material=list(self.n.pages.values());self.assertEqual(len(material),1)
        text=self.n.text(material[0]['id'])
        for i in range(n): self.assertIn('原文 '+str(i),text)
        self.assertEqual(sorted(self.downloaded),list(range(n)))
        self.assertIsNone(store.get_staged(URL))
        self.assertEqual(notion.prop_text(material[0],'处理状态'),'完成')
        self.assertIn(notion.SOURCE_ARROW,str(material[0]['properties']['标题']))
        self.assertEqual(notion.prop_text(material[0],'卡片摘要'),'材料整体概览')
    def test_32_13_6_images_have_complete_originals_and_one_page(self):
        for n in (32,13,6):
            with self.subTest(n=n):
                self.db.data.clear();self.n=Memory();self.p(notion,'_call',self.n.call)
                self.p(notion,'_create_tagged_page',lambda payload:self.n.call('POST','/pages',payload))
                self.downloaded=[];self.calls=[]
                _,url=self.run_import(n);self.assertTrue(url);self.check_complete(n)
                self.assertLessEqual(max(map(len,self.calls)),4)
    def test_budget_continuation_reuses_confirmed_batches(self):
        self.exhaust=True
        _,url=self.run_import(13);self.assertFalse(url)
        self.assertEqual(store.get_image_job(URL)['completed'],8)
        first=copy.deepcopy(self.calls)
        self.exhaust=False;self.left=float('inf')
        _,url=self.run_import(13);self.assertTrue(url);self.check_complete(13)
        self.assertEqual(self.calls[:2],first)
        self.assertEqual(sum(i in first[0] for call in self.calls for i in call),4)
    def test_failed_second_batch_splits_and_keeps_first(self):
        self.fail_call=2
        self.run_import(13)
        self.assertEqual(store.get_image_job(URL)['completed'],4)
        self.assertEqual(store.get_image_job(URL)['batch_size'],2)
        self.fail_call=None
        self.run_import(13);self.check_complete(13)
        self.assertEqual(self.calls.count([0,1,2,3]),1)
    def test_lost_notion_create_or_append_resumes_without_duplicate(self):
        for flag in ('lose_create','lose_append'):
            with self.subTest(flag=flag):
                self.db.data.clear();self.n=Memory();self.p(notion,'_call',self.n.call)
                self.p(notion,'_create_tagged_page',lambda payload:self.n.call('POST','/pages',payload))
                self.downloaded=[];self.calls=[];setattr(self.n,flag,True)
                with self.assertRaises(RuntimeError):self.run_import(6)
                self.assertEqual(len(self.n.pages),1)
                self.run_import(6);self.check_complete(6)
    def test_reader_provenance_survives_snapshot_and_batch_finish(self):
        art = self.art(6)
        art['capture'] = {'method': 'jina_reader_anonymous', 'fetched_at': '2026-10-29T08:00:00+00:00',
                          'raw_archive': 'gs://test/jina-original.md'}
        _, url = batch.handle(self.msg, URL, art, '知乎', '', lambda s: None)
        self.assertTrue(url)
        job = store.get_image_job(URL)
        self.assertEqual(batch.get_json(job['snapshot_uri'])['capture'], art['capture'])
        text = self.n.text(job['page']['id'])
        self.assertIn('Jina Reader', text)
        self.assertIn('gs://test/jina-original.md', text)
    def test_lease_rejects_second_owner_and_wrong_release(self):
        first=store.claim_image_job(URL);self.assertTrue(first)
        self.assertIsNone(store.claim_image_job(URL));store.release_image_job(URL,'wrong')
        self.assertIsNone(store.claim_image_job(URL));store.release_image_job(URL,first)
        self.assertTrue(store.claim_image_job(URL))
    def test_missing_image_never_publishes_complete_page(self):
        self.p(extract_httpx(),'get',lambda url,**kw:httpx.Response(403,request=httpx.Request('GET',url)))
        _,url=self.run_import(13)
        self.assertFalse(url);self.assertFalse(self.n.pages)
        self.assertIn('图 1',store.get_image_job(URL)['error'])

    def test_no_progress_limit_stops_drain_and_already_queued_continuations(self):
        self.p(extract_httpx(), 'get', lambda url, **kw: httpx.Response(404, request=httpx.Request('GET', url)))
        self.run_import(6)
        for _ in range(batch.MAX_NO_PROGRESS_ATTEMPTS - 1):
            batch.resume(URL, notify=False)
        job = store.get_image_job(URL)
        self.assertEqual(job['failure_streak'], batch.MAX_NO_PROGRESS_ATTEMPTS)
        self.assertIn('已暂停自动重试', store.get_staged(URL)['error'])
        store.update_image_job(URL, next_retry_at=0)
        with patch.object(batch, 'schedule', side_effect=AssertionError('paused job requeued')):
            self.assertEqual(batch.drain(), 0)
            self.assertTrue(batch.resume(URL, notify=False)['skipped'])
        self.assertEqual(store.get_staged(URL)['attempts'], batch.MAX_NO_PROGRESS_ATTEMPTS)

    def test_daily_retry_leaves_paused_image_job_for_manual_retry(self):
        self.p(config, 'GCP_PROJECT', 'test')
        self.p(notion, 'find_materials', lambda states: [])
        self.p(extract_httpx(), 'get', lambda url, **kw: httpx.Response(404, request=httpx.Request('GET', url)))
        self.run_import(6)
        store.update_image_job(URL, failure_streak=209, next_retry_at=0)
        with patch.object(batch, 'resume', side_effect=AssertionError('daily retry resumed paused job')):
            self.assertIn('没有需要重跑', pipeline._retry_pending(lambda _: None, automatic=True)[0])
        self.p(extract_httpx(), 'get', self.download)
        self.assertIn('1 条成功', pipeline._retry_pending(lambda _: None)[0])
        self.check_complete(6)

    def test_manual_resume_repairs_legacy_rednote_snapshot(self):
        old = 'http://sns-img-hw.xhscdn.com/'
        cdn = 'https://sns-i11.rednotecdn.com/notes_pre_post/'
        art = self.art(6)
        art['images'] = [old + str(i) + '?sign=' + str(i) for i in range(6)]
        art['thumbnail'] = cdn + '0?sign=0'
        self.assertEqual(batch.extract.repair_xhs_image_urls(art['images'], cdn + '0?sign=wrong'),
                         art['images'])
        self.p(extract_httpx(), 'get', lambda url, **kw: httpx.Response(404, request=httpx.Request('GET', url)))
        batch.handle(self.msg, URL, art, '小红书', '', lambda _: None)
        store.update_image_job(URL, failure_streak=batch.MAX_NO_PROGRESS_ATTEMPTS)
        def download(url, **kw):
            self.assertTrue(url.startswith(cdn))
            return self.download(url.split('?')[0], **kw)
        self.p(extract_httpx(), 'get', download)
        self.assertTrue(batch.resume(URL, notify=False, manual=True)['completed'])
        self.check_complete(6)
        repaired = batch.get_json(store.get_image_job(URL)['snapshot_uri'])
        self.assertEqual(repaired['images'], [cdn + str(i) + '?sign=' + str(i) for i in range(6)])
    def test_batch_indices_are_validated_not_silently_relabelled(self):
        self.p(enrich,'_chat',lambda *a,**kw:json.dumps({'images':[{'index':0,'informative':True,'summary':'s'},{'index':0,'informative':True,'summary':'s'}]}))
        with self.assertRaises(batch.Pending):batch.read_batch('t','b',[4,5],[(b'i','image/png')]*2)
    def test_duplicate_share_ignores_all_attached_text(self):
        self.run_import(6)
        raw = '来源 完整帖子原文...\n我不认同这个判断'
        new = {'chat': {'id': 7}, 'message_id': 2}
        _, url = pipeline._handle_url(new, URL, raw, lambda s: None, update_id=2)
        self.assertTrue(url)
        self.assertIsNone(store.get_record(7, 2))
        self.assertFalse([p for p in self.n.pages.values() if p['parent'].get('database_id') == 'thoughts'])

    def test_waiting_duplicate_ignores_text_until_material_is_ready(self):
        self.exhaust = True
        self.run_import(13)
        new = {'chat': {'id': 7}, 'message_id': 2}
        _, url = pipeline._handle_url(new, URL, '我不认同这个判断', lambda s: None, update_id=2)
        self.assertFalse(url)
        self.assertIsNone(store.get_record(7, 2))
        self.exhaust = False
        self.left = float('inf')
        self.run_import(13)
        material = store.get_image_job(URL)['page']['id']
        self.assertNotIn('我不认同这个判断', self.n.text(material))
        self.assertIsNone(store.get_staged(URL))

    def test_deleted_material_can_be_imported_again_with_new_source(self):
        self.run_import(6);before=store.get_image_job(URL)
        original_body=self.files[before['body_uri']]
        self.n.pages[before['page']['id']]['archived']=True
        self.msg['message_id']=2
        art=self.art(7);art['text']='更新后的来源正文'
        self.downloaded=[]
        _,url=batch.handle(self.msg,URL,art,'小红书','',lambda s:None)
        self.assertTrue(url)
        after=store.get_image_job(URL)
        self.assertNotEqual(before['page']['id'],after['page']['id'])
        self.assertEqual(after['completed'],7)
        self.assertEqual(after['msg']['message_id'],2)
        self.assertEqual(self.files[before['body_uri']],original_body)
        self.assertEqual(self.files[after['body_uri']],art['text'].encode())
        self.assertTrue(after.get('previous_job_uri'))

    def test_busy_continuation_is_retryable_without_model_calls(self):
        store.claim_image_job(URL)
        store.update_image_job(URL,status='pending',snapshot_uri=batch.put_json(URL,'source',self.art(6)),msg=self.msg,platform='小红书')
        self.assertEqual(batch.resume(URL,notify=False),{'ok':False,'busy':True})
        self.assertFalse(self.calls)
        from fastapi.testclient import TestClient
        from app import main
        with patch.object(main,'_verify_oidc',lambda token:None),patch.object(batch,'resume',return_value={'ok':False,'busy':True}):
            response=TestClient(main.app).post('/tasks/images',json={'url':URL})
        self.assertEqual(response.status_code,503)

    def test_continuation_schedules_after_release_margin(self):
        seen=[]
        self.p(tasks,'enqueue_images',lambda url,round_id,delay:seen.append(delay) or True)
        job={};batch.schedule(URL,job,delay=1)
        self.assertGreaterEqual(seen[0],10)

    def test_corrupt_checkpoint_is_rejected_before_reading_or_writing(self):
        self.exhaust=True;self.run_import(13)
        job=store.get_image_job(URL);uri=job['steps'][0]['uri']
        value=json.loads(self.files[uri]);value[0]['text']='篡改后的原文'
        self.files[uri]=json.dumps(value,ensure_ascii=False).encode()
        self.exhaust=False;self.left=float('inf')
        self.run_import(13)
        self.assertFalse(self.n.pages)
        self.assertIn('SHA',store.get_image_job(URL)['error'])

    def test_summary_is_frozen_once_page_writing_starts(self):
        self.run_import(6);job=store.get_image_job(URL)
        with patch.object(tag_store,'load',side_effect=RuntimeError('词表已变化')):
            summary=batch.summarize(URL,job,self.art(6),[],'')
        self.assertEqual(summary.overview,'材料整体概览')

    def test_vllm_thinking_control_is_scoped_to_configured_hosts(self):
        response=httpx.Response(200,json={'choices':[{'message':{'content':'{}'}}]})
        for host,model in [('https://vllm.example/v1','deepseek-v4-flash'),('https://api.deepseek.com/v1','deepseek-v4-flash'),('https://vllm.example/v1','gemma4-12b')]:
            with self.subTest(host=host,model=model),patch.object(config,'LLM_VLLM_HOSTS',{'vllm.example'}),patch.object(settings,'active',return_value={'base_url':host,'model':model,'api_key':''}),patch.object(enrich.httpx,'post',return_value=response) as post:
                self.original_chat([{'role':'user','content':'test'}],False,disable_thinking=True)
                payload=post.call_args.kwargs['json']
                self.assertEqual(payload.get('chat_template_kwargs'),{'thinking':False} if 'vllm.example' in host and model.startswith('deepseek') else None)
                self.assertEqual(payload.get('thinking'),{'type':'disabled'} if 'api.deepseek' in host else None)

    def test_encoded_byte_budget_splits_batches(self):
        self.p(batch,'MAX_REQUEST_BYTES',100030)
        self.run_import(6);self.check_complete(6)
        self.assertTrue(all(len(call)<=2 for call in self.calls))

    def test_four_large_images_use_durable_batches_before_combined_enrichment(self):
        art=self.art(4)
        images=[(b'x'*1_320_000,'image/png')]*4
        self.p(pipeline,'fetch_for',lambda *a,**kw:art)
        self.p(pipeline,'_read_url_images',lambda *a:(images,[],[]))
        self.p(store,'find_by_url',lambda url:None)
        self.p(store,'pending_url_owner',lambda url:None)
        with patch.object(batch,'handle',return_value=('queued','')) as handle, \
             patch.object(enrich,'enrich',side_effect=AssertionError('oversized combined request')):
            result=pipeline._handle_url(self.msg,URL,'',lambda _:None,update_id=91)
        self.assertEqual(result,('queued',''))
        self.assertEqual(handle.call_args.args[2],art)
        self.assertEqual(handle.call_args.args[-1],91)

    def test_realistic_large_pngs_split_below_verified_gateway_budget(self):
        blob=b'x'*1_320_000
        job={'assets':{str(i):{'sha':str(i)} for i in range(4)}}
        self.p(batch,'asset',lambda *a:(blob,'image/png'))
        batches=[]
        def read(title,body,indices,images):
            self.assertLessEqual(batch.request_size(images),batch.MAX_REQUEST_BYTES)
            batches.append(list(indices))
            return [enrich.ImageRead(index=i,informative=True,summary='s',text='t') for i in indices]
        self.p(batch,'read_batch',read)
        reads=batch.read_all(URL,job,self.art(4),lambda _:None)
        self.assertEqual(batches,[[0],[1],[2],[3]])
        self.assertEqual([r.index for r in reads],list(range(4)))
        self.assertEqual(job['completed'],4)

    def test_desktop_share_never_becomes_thought_during_budget_continuation(self):
        note='38 【来源 - 作者 | 小红书 - 你的生活兴趣社区】 😆 R8qUAsbbLUbcUJw 😆'
        self.exhaust=True
        batch.handle(self.msg,URL,self.art(13),'小红书',note,lambda _:None)
        self.assertFalse((store.get_record(7,1) or {}).get('link_thought'))
        self.assertFalse(self.n.pages)
        self.exhaust=False;self.left=float('inf')
        batch.handle(self.msg,URL,self.art(13),'小红书',note,lambda _:None)
        self.check_complete(13)
        self.assertFalse((store.get_record(7,1) or {}).get('link_thought'))


def extract_httpx():
    from app import extract
    return extract.httpx

if __name__=='__main__':unittest.main()
