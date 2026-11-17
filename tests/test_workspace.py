import copy
import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import workspace as w
import webapp


class WorkspaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.source = cls.root / '源 video with spaces.mp4'
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i',
                        'testsrc2=size=320x240:rate=30:duration=4', '-f', 'lavfi', '-i',
                        'sine=frequency=440:duration=4', '-c:v', 'libx264', '-pix_fmt',
                        'yuv420p', '-c:a', 'aac', '-shortest', str(cls.source)], check=True)
        cls.noaudio = cls.root / 'portrait.mp4'
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i',
                        'color=c=blue:size=180x320:rate=24:duration=2', '-c:v', 'libx264',
                        str(cls.noaudio)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.data = patch.object(w, 'DATA', Path(self.dir.name))
        self.data.start()

    def tearDown(self):
        self.data.stop()
        self.dir.cleanup()

    def test_invalid_trims(self):
        for a,b in [(-1,1),(2,1),(0,0),(0,5),(float('nan'),2)]:
            p=w.create(self.source)
            p['clips'][0]['segments']=[{'start':a,'end':b}]
            with self.assertRaises(ValueError):w.validate(p)

    def test_caption_remapping_reordered_and_removed_pause(self):
        c=w.new_clip(0,1)
        c['segments']=[{'start':3,'end':4},{'start':0,'end':1}]
        actual=w.cues(c,[{'start':.5,'end':1.5,'text':'early'}, {'start':2.5,'end':3.5,'text':'late'}])
        self.assertEqual(actual,[{'start':0,'end':.5,'text':'late'}, {'start':1.5,'end':2,'text':'early'}])
        self.assertIn('00:00:01,500 --> 00:00:02,000',w.subtitles(c,[{'start':.5,'end':1.5,'text':'early'}]))
        self.assertTrue(w.subtitles(c,[], 'vtt').startswith('WEBVTT'))

    def test_save_reopen_conflict_and_schema(self):
        p=w.create(self.source)
        p['clips'][0]['title']='Corrected'
        saved=w.save(p['id'],p)
        self.assertEqual(w.load(p['id'])['clips'][0]['title'],'Corrected')
        with self.assertRaises(ValueError):w.save(p['id'],p)
        saved['schema']=999
        w.atomic(w.folder(p['id'])/'project.json',saved)
        with self.assertRaises(ValueError):w.load(p['id'])

    def test_recovery(self):
        p=w.create(self.source)
        p['jobs']=[{'id':'abc','state':'running'}]
        w.atomic(w.folder(p['id'])/'project.json',p)
        w.recover()
        self.assertEqual(w.load(p['id'])['jobs'][0]['stage'],'interrupted')

    def wait_job(self,p,j):
        limit=time.time()+40
        while time.time()<limit:
            state=w.load(p['id'])['jobs'][-1]
            if state['state'] in ('completed','failed','cancelled'):
                while w.ACTIVE and time.time()<limit:time.sleep(.02)
                return state
            time.sleep(.05)
        self.fail('job timed out')

    def test_real_export_caption_crop_edl(self):
        p=w.create(self.source)
        c=p['clips'][0]
        c.update(aspect='1:1',fit='fill',x=.25)
        c['segments']=[{'start':2,'end':3},{'start':0,'end':1}]
        p['transcript']=[{'start':.1,'end':.8,'text':'Hello {unsafe} \\N → test'}, {'start':2,'end':2.8,'text':'Kumusta!'}]
        p=w.save(p['id'],p)
        j=w.start(p['id'],'export',[c['id']])
        result=self.wait_job(p,j)
        self.assertEqual(result['state'],'completed',result.get('error'))
        dest=Path(result['files'][0])
        self.assertTrue(dest.exists())
        d=w.probe(dest)
        self.assertEqual((d['width'],d['height']),(720,720))
        self.assertAlmostEqual(d['duration'],2,delta=.15)
        self.assertTrue(d['audio'])
        self.assertIn('00:00:01,100',dest.with_suffix('.srt').read_text())
        # Keep one evidence file outside the temporary project tree.
        import shutil
        evidence=Path(__file__).resolve().parents[1]/'verification'
        evidence.mkdir(exist_ok=True)
        shutil.copyfile(dest,evidence/'smoke-edited.mp4')
        (evidence/'smoke-ffprobe.json').write_text(json.dumps(d,indent=2))

    def test_real_noaudio_portrait_export(self):
        p=w.create(self.noaudio)
        p['clips'][0]['segments']=[{'start':0,'end':1}]
        p=w.save(p['id'],p)
        result=self.wait_job(p,w.start(p['id'],'export',[p['clips'][0]['id']]))
        self.assertEqual(result['state'],'completed',result.get('error'))
        self.assertFalse(w.probe(result['files'][0])['audio'])

    def test_waveform_real_audio(self):
        p=w.create(self.source)
        result=self.wait_job(p,w.start(p['id'],'waveform'))
        self.assertEqual(result['state'],'completed',result.get('error'))
        waveform=w.load(p['id'])['waveform']
        self.assertGreater(len(waveform['rms']),1)
        self.assertLessEqual(len(waveform['rms']),1501)
        self.assertGreater(max(waveform['rms']),0)

    def test_cancellation(self):
        p=w.create(self.source)
        entered=threading.Event()
        def slow(*args):
            entered.set()
            while not next(iter(w.ACTIVE.values()))['cancel'].wait(.02):pass
            raise InterruptedError()
        with patch.object(w,'render',slow):
            j=w.start(p['id'],'export',[p['clips'][0]['id']])
            self.assertTrue(entered.wait(2))
            w.ACTIVE[j['id']]['cancel'].set()
            self.assertEqual(self.wait_job(p,j)['state'],'cancelled')

    def test_changed_source_rejected(self):
        p=w.create(self.source)
        p['source']['size']-=1
        w.atomic(w.folder(p['id'])/'project.json',p)
        with self.assertRaises(ValueError):w.start(p['id'],'export',[p['clips'][0]['id']])

    def test_corrupt_import(self):
        bad=Path(self.dir.name)/'bad.mp4';bad.write_text('not video')
        with self.assertRaises(ValueError):w.create(bad)

    def test_http_ranges_upload_and_project_api(self):
        srv=webapp.ThreadingHTTPServer(('127.0.0.1',0),webapp.Handler)
        thread=threading.Thread(target=srv.serve_forever,daemon=True);thread.start()
        upload=patch.object(webapp,'UPLOAD_DIR',str(Path(self.dir.name)/'uploads'));upload.start()
        def request(method,path,body=None,headers=None):
            conn=http.client.HTTPConnection('127.0.0.1',srv.server_port)
            conn.request(method,path,body,headers or {})
            res=conn.getresponse();result=(res.status,res.read(),dict(res.getheaders()));conn.close();return result
        try:
            body=self.source.read_bytes()
            status,data,_=request('POST','/api/upload?name=video.mp4',body)
            self.assertEqual(status,200,data)
            source=json.loads(data)['path']
            status,data,_=request('POST','/api/workspace/create',json.dumps({'source':source}),{'Content-Type':'application/json'})
            self.assertEqual(status,200,data)
            p=json.loads(data)
            from urllib.parse import urlencode
            url='/api/workspace/file?'+urlencode({'id':p['id'],'path':source})
            status,data,headers=request('GET',url,headers={'Range':'bytes=-10'})
            self.assertEqual(status,206);self.assertEqual(data,body[-10:])
            self.assertEqual(request('GET',url,headers={'Range':'bytes=999999999-'})[0],416)
            self.assertEqual(request('POST','/api/upload?name=x.mp4',b'broken')[0],400)
            self.assertEqual(request('GET','/editor',headers={'Host':'evil.example'})[0],403)
            self.assertEqual(request('GET','/editor')[0],200)
        finally:
            srv.shutdown();srv.server_close();upload.stop()


if __name__=='__main__':unittest.main()

class ProviderValidationTests(unittest.TestCase):
    def test_invalid_provider_intervals(self):
        from highlights_llm import _sanitize_highlights
        data=[{'start_time':a,'end_time':b} for a,b in [(0,1),(2,1),(-1,1),(0,11),(float('nan'),2),(0,float('inf'))]]
        cleaned=_sanitize_highlights(data,10)
        self.assertEqual(len(cleaned),1)
        self.assertEqual(cleaned[0]['end_time'],1)

    def test_duplicate_suggestions(self):
        from highlights_llm import dedupe_highlights
        data=[{'start_time':0,'end_time':30,'score':90},{'start_time':1,'end_time':29,'score':70},{'start_time':40,'end_time':60,'score':50}]
        self.assertEqual(len(dedupe_highlights(data)),2)
