import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

import offline_scan
import workspace as w


class OfflineTests(unittest.TestCase):
    def test_interview_keeps_question_with_answer(self):
        cues = [dict(start=5,end=7,text='What changed?'),dict(start=8,end=14,text='We replaced the sensor.')]
        hit = offline_scan.rank({'duration':30}, {'audio':[], 'motion':[]}, cues,1,8,18,'interview')[0]
        self.assertEqual((hit['start'],hit['end']),(5,14))

    def test_bounded_motion_keeps_setup_and_recovery(self):
        motion = [0]*12 + [1]+[.25]*7+[1]+[0]*9
        hits = offline_scan.rank({'duration':30}, {'audio':[],'motion':motion}, [], 2, 7, 12, 'gameplay')
        self.assertEqual([(h['start'],h['end']) for h in hits],[(11,22)])

    def test_corrected_caption_takes_priority_over_cached_words(self):
        words = [dict(start=0,end=1,text='Hello'),dict(start=1,end=2,text='there')]
        cue = dict(start=0,end=2,text='Hello there',words=words)
        clip = w.new_clip(1,2)
        self.assertEqual(w.cues(clip,[cue])[0]['text'],'there')
        cue['text'] = 'My corrected caption'
        self.assertEqual(w.cues(clip,[cue])[0]['text'],cue['text'])

    def test_flat_silent_source(self):
        self.assertEqual(offline_scan.rank({'duration':4}, {'audio':[], 'motion':[0]*4},
                                          [], 3, 1, 3, 'gameplay'), [])

    def wait(self, project, job):
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            with w.LOCK:
                state = w.load(project['id'])['jobs'][-1]
                if job['id'] not in w.ACTIVE:
                    self.assertEqual(state['state'], 'completed', state['error'])
                    return w.load(project['id'])
            time.sleep(.05)
        self.fail('Timed out waiting for ' + job['action'])

    def test_silent_gameplay_rescan_preserves_edits(self):
        with tempfile.TemporaryDirectory() as root, patch.object(w,'DATA',Path(root)):
            media = Path(root)/'silent.mp4'
            subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i',
                            'testsrc2=size=160x90:rate=10:duration=6',str(media)],check=True)
            p = w.create(media)
            def scan():
                return self.wait(p,w.start(p['id'],'scan',count=1,min_sec=2,max_sec=4,profile='gameplay'))
            result = scan()
            self.assertEqual(len(result['clips']),2)
            self.assertGreater(result['clips'][1]['suggestion']['components']['motion'],0)
            result['clips'][1]['title'] = 'My edit'
            w.save(p['id'],result)
            self.assertEqual([c['title'] for c in scan()['clips']], ['Manual starting clip','My edit'])

    def test_missing_smart_packages_fails_before_job_is_queued(self):
        with tempfile.TemporaryDirectory() as root, patch.object(w, 'DATA', Path(root)):
            media = Path(root) / 'podcast.wav'
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                            'sine=duration=1', str(media)], check=True)
            project = w.create(media)
            with patch('importlib.util.find_spec', return_value=None):
                with self.assertRaisesRegex(ValueError, 'Offline Smart needs local Python packages'):
                    w.start(project['id'], 'analyze', profile='podcast')
            self.assertEqual(w.load(project['id'])['jobs'], [])

    def test_new_landscape_projects_preserve_full_frame_by_default(self):
        with tempfile.TemporaryDirectory() as root, patch.object(w, 'DATA', Path(root)):
            media = Path(root) / 'wide.mp4'
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                            'testsrc2=size=320x180:duration=1:rate=10',
                            str(media)], check=True)
            project = w.create(media)
            self.assertEqual(project['clips'][0]['fit'], 'blur')
            self.assertEqual(project['clips'][0]['title'], 'Manual starting clip')

    def test_waveform_export_for_audio_only(self):
        with tempfile.TemporaryDirectory() as root, patch.object(w,'DATA',Path(root)):
            media = Path(root)/'audio.wav'
            subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','sine=duration=2',str(media)],check=True)
            p = w.create(media)
            self.assertFalse(p['source']['video'])
            preview = self.wait(p,w.start(p['id'],'proxy'))
            self.assertEqual(w.probe(preview['proxy'])['width'],360)
            p['clips'][0].update(normalize=True,resolution='1080p')
            p = w.save(p['id'],p)
            result = self.wait(p,w.start(p['id'],'export',[p['clips'][0]['id']]))
            output = w.probe(result['jobs'][-1]['files'][0])
            self.assertTrue(output['audio'] and output['video'])
            self.assertEqual((output['width'], output['height']),(1080,1920))
            self.assertAlmostEqual(output['duration'],2,delta=.2)

    def test_legacy_project_migration(self):
        with tempfile.TemporaryDirectory() as root, patch.object(w,'DATA',Path(root)):
            pid='a'*32
            original={'schema':1,'source':{},'clips':[{'title':'My edit'}]}
            path=w.folder(pid)/'project.json'
            w.atomic(path,original)
            self.assertEqual(w.load(pid),dict(original,schema=2,source={'video':True}))
            self.assertEqual(json.loads(path.read_text()),original)
