from pathlib import Path
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import composition
import speech_chunks
import workspace as w
from test_offline_scan import OfflineTests as _OfflineTests


class CompositionTests(unittest.TestCase):
    wait = _OfflineTests.wait

    def test_stable_camera_and_two_face_selection(self):
        one=[(.1,.25,.2,.3)]
        two=one+[(.7,.25,.2,.3)]
        self.assertEqual(composition.decide([(0,one),(1,two),(2,two)],'interview',640,360)['fit'],'stack')
        self.assertEqual(composition.decide([(5,one),(6,one)],'gameplay',640,360)['fit'],'webcam')
        self.assertEqual(composition.decide([(5,one)],'tutorial',640,360),{})
        self.assertEqual(composition.decide([(5,[])],'interview',640,360),{})

    def test_overlap_owns_word_once(self):
        chunks=list(speech_chunks.windows([b'12345678',b'abcdefgh'],rate=2,context_seconds=1))
        self.assertEqual(chunks,[(b'12345678abcd',0,0,2),(b'5678abcdefgh',1,2,4)])
        for n,(offset,left,right) in enumerate([(0,0,2),(1,2,4)]):
            word=SimpleNamespace(word='hello',start=1.9-offset,end=2.1-offset)
            cue=speech_chunks.owned_segment(SimpleNamespace(words=[word]),offset,left,right,4)
            self.assertEqual(cue is not None, n==1)

    def test_project_layouts_real_decode(self):
        with tempfile.TemporaryDirectory() as root,patch.object(w,'DATA',Path(root)):
            source=Path(root)/'two-panels.mp4'
            subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i',
                'testsrc2=size=320x180:duration=1:rate=12','-threads','1',str(source)],check=True)
            p=w.create(source)
            p['clips']=[]
            for mode in ['stack','webcam','follow']:
                c=w.new_clip(0,1,mode)
                c.update(fit=mode,camera=[[0,.3,.5],[.5,.6,.5]],crf=25)
                p['clips'].append(c)
            p=w.save(p['id'],p)
            result=self.wait(p,w.start(p['id'],'export',[c['id'] for c in p['clips']]))
            self.assertEqual(len(result['jobs'][-1]['files']),3)
            for file in result['jobs'][-1]['files']:
                self.assertEqual(w.probe(file)['height'],1280)
del _OfflineTests
