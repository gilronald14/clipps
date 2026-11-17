"""Persistent manual editing workspace. Stdlib + native FFmpeg; no AI required.
All editing times are source seconds; ordered segments define edited time.
"""
import copy
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'projects'
LOCK = threading.RLock()
ACTIVE = {}


def number(value, low, high):
    n = float(value)
    if not math.isfinite(n) or not low <= n <= high:
        raise ValueError(f'Expected a finite number between {low} and {high}')
    return n


def atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(value, f, ensure_ascii=False, indent=2, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def identity(path):
    p = Path(path).resolve()
    s = p.stat()
    return {'path': str(p), 'size': s.st_size, 'mtime_ns': s.st_mtime_ns}


def probe(path):
    path = str(Path(path).resolve(strict=True))
    r = subprocess.run(['ffprobe', '-v', 'error', '-show_format', '-show_streams',
                        '-of', 'json', path], capture_output=True, text=True, timeout=30)
    if r.returncode:
        raise ValueError('Cannot read this media. Check the codec or choose an undamaged file.')
    d = json.loads(r.stdout)
    v = next((s for s in d.get('streams', []) if s['codec_type'] == 'video'
              and not s.get('disposition', {}).get('attached_pic')), None)
    if not v:
        raise ValueError('The selected file has no video stream')
    duration = number(d.get('format', {}).get('duration') or v.get('duration'), .01, 604800)
    def rate(s):
        try:
            a, b = s.split('/')
            return float(a) / float(b)
        except (ValueError, ZeroDivisionError, AttributeError):
            return 0
    rotation = int(float(v.get('tags', {}).get('rotate', 0)))
    for side in v.get('side_data_list', []):
        rotation = int(side.get('rotation', rotation))
    w, h = v['width'], v['height']
    if abs(rotation) % 180 == 90:
        w, h = h, w
    return dict(identity(path), duration=duration, width=w, height=h,
                fps=rate(v.get('avg_frame_rate')) or 30, rotation=rotation,
                audio=any(s['codec_type'] == 'audio' for s in d.get('streams', [])),
                codec=v.get('codec_name'), timing='source seconds; export CFR 30 fps')


def folder(pid):
    if not re.fullmatch(r'[0-9a-f]{32}', str(pid)):
        raise ValueError('Invalid project ID')
    return DATA / pid


def load(pid):
    with LOCK:
        p = json.loads((folder(pid) / 'project.json').read_text(encoding='utf-8'))
        if p.get('schema') != 1:
            raise ValueError('Unsupported project schema; keep this file and use a compatible app version')
        return p


def validate(p):
    duration = number(p['source']['duration'], .01, 604800)
    if not isinstance(p['clips'], list) or len(p['clips']) > 100:
        raise ValueError('At most 100 clips per project')
    ids = set()
    for c in p['clips']:
        if not re.fullmatch(r'[0-9a-f]{32}', c['id']) or c['id'] in ids:
            raise ValueError('Invalid or duplicate clip ID')
        ids.add(c['id'])
        if not isinstance(c['segments'], list) or not 1 <= len(c['segments']) <= 100:
            raise ValueError('Each clip needs 1–100 ordered segments')
        for s in c['segments']:
            s['start'] = number(s['start'], 0, duration)
            s['end'] = number(s['end'], 0, duration)
            if s['end'] - s['start'] < 1 / 30:
                raise ValueError('Each segment must be at least one output frame')
        c['title'] = str(c.get('title', 'Clip'))[:160]
        if c.get('aspect') not in ('9:16', '16:9', '1:1'):
            raise ValueError('Choose 9:16, 16:9, or 1:1')
        if c.get('fit') not in ('fit', 'fill'):
            raise ValueError('Choose fit or fill')
        c['x'] = number(c.get('x', .5), 0, 1)
        c['y'] = number(c.get('y', .5), 0, 1)
        c['gain'] = number(c.get('gain', 0), -60, 12)
        c['font_size'] = number(c.get('font_size', 40), 16, 80)
        if not re.fullmatch(r'#[0-9A-Fa-f]{6}', c.get('color', '#ffffff')):
            raise ValueError('Invalid caption color')
    if len(p.get('transcript', [])) > 50000:
        raise ValueError('Transcript is too large')
    for s in p.get('transcript', []):
        s['start'] = number(s['start'], 0, duration)
        s['end'] = number(s['end'], 0, duration)
        if s['end'] <= s['start']:
            raise ValueError('Caption end must follow its start')
        s['text'] = str(s['text'])[:2000]
    return p


def new_clip(start, end, title='New clip'):
    return dict(id=uuid.uuid4().hex, title=title, segments=[dict(start=start, end=end)],
                aspect='9:16', fit='fit', x=.5, y=.5, gain=0, mute=False,
                normalize=False, captions=True, font_size=40, color='#ffffff')


def create(path):
    source = probe(path)
    p = dict(schema=1, id=uuid.uuid4().hex, revision=0, title=Path(path).stem,
             source=source, transcript=[], clips=[new_clip(0, min(30, source['duration']))],
             jobs=[], created=time.time(), updated=time.time())
    atomic(folder(p['id']) / 'project.json', p)
    return p


def save(pid, incoming):
    with LOCK:
        old = load(pid)
        if incoming.get('revision') != old['revision']:
            raise ValueError('Project changed in another tab. Reload before editing.')
        # Source, jobs and identity are server-owned.
        p = copy.deepcopy(old)
        for key in ('title', 'clips', 'transcript'):
            p[key] = incoming[key]
        validate(p)
        p['revision'] += 1
        p['updated'] = time.time()
        atomic(folder(pid) / 'project.json', p)
        return p


def cues(clip, transcript):
    """Intersect source captions with every EDL segment and remap to edited time."""
    out, offset = [], 0.0
    for seg in clip['segments']:
        for cue in transcript:
            a, b = max(seg['start'], cue['start']), min(seg['end'], cue['end'])
            if b > a:
                out.append(dict(start=offset + a - seg['start'],
                                end=offset + b - seg['start'], text=cue['text']))
        offset += seg['end'] - seg['start']
    return sorted(out, key=lambda x: x['start'])


def subtitles(clip, transcript, kind='srt'):
    def stamp(t):
        ms = round(t * 1000)
        h, ms = divmod(ms, 3600000)
        m, ms = divmod(ms, 60000)
        s, ms = divmod(ms, 1000)
        return f'{h:02}:{m:02}:{s:02}{"," if kind == "srt" else "."}{ms:03}'
    lines = ['WEBVTT\n'] if kind == 'vtt' else []
    for i, c in enumerate(cues(clip, transcript), 1):
        # SRT/VTT must not interpret user markup or timestamp delimiters.
        text = c['text'].replace('-->', '→').replace('<', '‹').replace('>', '›')
        lines.append(f'{i}\n{stamp(c["start"])} --> {stamp(c["end"])}\n{text}\n')
    return '\n'.join(lines)


def kill(proc):
    if proc.poll() is not None:
        return
    if os.name == 'nt':
        subprocess.run(['taskkill', '/F', '/T', '/PID', str(proc.pid)], capture_output=True)
    else:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


def update_job(pid, jid, **values):
    with LOCK:
        p = load(pid)
        j = next(j for j in p['jobs'] if j['id'] == jid)
        j.update(values)
        atomic(folder(pid) / 'project.json', p)


def start(pid, action, clip_ids=None, language='auto', model='base', count=7, min_sec=20, max_sec=45):
    with LOCK:
        legacy = __import__('sys').modules.get('webapp')
        if legacy and legacy.JOB.state == 'running':
            raise ValueError('The automatic generator is running. Wait or stop it first.')
        if ACTIVE:
            raise ValueError('A workspace job is already running. Wait or cancel it first.')
        p = load(pid)
        if identity(p['source']['path']) != {k: p['source'][k] for k in ('path', 'size', 'mtime_ns')}:
            raise ValueError('Source file changed. Import it as a new project to protect timing.')
        if action not in ('export', 'analyze', 'waveform'):
            raise ValueError('Unknown action')
        selected = [c for c in p['clips'] if c['id'] in (clip_ids or [])]
        if action == 'export' and not selected:
            raise ValueError('Select at least one clip')
        if action == 'waveform' and not p['source']['audio']:
            raise ValueError('This source has no audio waveform')
        if action == 'analyze' and p['transcript']:
            raise ValueError('Transcript already exists. Import a new project to re-transcribe without losing corrections.')
        if not re.fullmatch(r'auto|[a-z]{2,3}', language) or model not in ('tiny', 'base', 'small', 'medium'):
            raise ValueError('Invalid transcription settings')
        count = int(number(count, 1, 30))
        min_sec = number(min_sec, 1, 600)
        max_sec = number(max_sec, min_sec, 600)
        jid = uuid.uuid4().hex
        job = dict(id=jid, action=action, state='queued', stage='queued',
                   progress=None, files=[], error='', options=dict(count=count, min_sec=min_sec, max_sec=max_sec), revision=p['revision'], created=time.time())
        p['jobs'] = (p['jobs'] + [job])[-30:]
        atomic(folder(pid) / 'project.json', p)
        cancel = threading.Event()
        ACTIVE[jid] = dict(cancel=cancel, proc=None)
        threading.Thread(target=worker, args=(p, job, selected, language, model), daemon=True).start()
        return job


def worker(p, job, selected, language, model):
    pid, jid = p['id'], job['id']
    ctl = ACTIVE[jid]
    work = folder(pid) / jid
    work.mkdir()
    def run(args):
        if ctl['cancel'].is_set():
            raise InterruptedError('Cancelled')
        with open(work / 'process.log', 'w', encoding='utf-8') as log:
            proc = subprocess.Popen(args, cwd=work, stdout=log, stderr=subprocess.STDOUT,
                                    start_new_session=os.name != 'nt')
            with LOCK:
                ctl['proc'] = proc
            try:
                while proc.poll() is None:
                    if ctl['cancel'].wait(.15):
                        kill(proc)
                        raise InterruptedError('Cancelled')
                if proc.returncode:
                    raise RuntimeError((work / 'process.log').read_text(encoding='utf-8', errors='replace')[-2500:])
            finally:
                if proc.poll() is None:
                    kill(proc)
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                with LOCK:
                    ctl['proc'] = None
        if ctl['cancel'].is_set():
            raise InterruptedError('Cancelled')
    try:
        update_job(pid, jid, state='running', stage=job['action'])
        if job['action'] == 'waveform':
            run([os.sys.executable, str(ROOT / 'workspace.py'), '--waveform',
                 p['source']['path'], str(work / 'waveform.json'), str(p['source']['duration'])])
            with LOCK:
                current = load(pid)
                current['waveform'] = json.loads((work / 'waveform.json').read_text())
                atomic(folder(pid) / 'project.json', current)
            update_job(pid, jid, state='completed', stage='completed')
        elif job['action'] == 'analyze':
            run([os.sys.executable, str(ROOT / 'workspace.py'), '--transcribe',
                 p['source']['path'], str(work / 'transcript.json'), language, model])
            transcript = json.loads((work / 'transcript.json').read_text(encoding='utf-8'))
            # Reuse the existing offline selector; no external service is contacted.
            update_job(pid, jid, stage='ranking')
            run([os.sys.executable, str(ROOT / 'highlights_heuristic.py'),
                 '--transcript', str(work / 'transcript.json'), '--source', p['source']['path'],
                 '--clips', str(job['options']['count']), '--min', str(job['options']['min_sec']), '--max', str(job['options']['max_sec']), '--out', str(work / 'highlights.json')])
            result = json.loads((work / 'highlights.json').read_text(encoding='utf-8'))
            if ctl['cancel'].is_set():
                raise InterruptedError('Cancelled')
            with LOCK:
                current = load(pid)
                if current['revision'] != p['revision']:
                    raise ValueError('Edits changed during analysis. Results kept in job folder; project was not overwritten.')
                current['transcript'] = transcript['segments']
                current['analysis'] = {'provider': 'faster-whisper', 'language': transcript.get('language'),
                                       'ranker': 'heuristic', 'timing': 'segment', 'visual_analysis': False}
                for h in result.get('highlights', []):
                    c = new_clip(h['start_time'], h['end_time'], h.get('title', 'Suggestion'))
                    c['suggestion'] = dict(provider='heuristic', editorial_score=h.get('score'),
                        reason=h.get('virality_reason', 'Ranked using speech, audio energy and hook language.'),
                        excerpt=' '.join(s['text'] for s in transcript['segments']
                            if s['end'] > h['start_time'] and s['start'] < h['end_time']),
                        warning='Heuristic estimate; review context and boundaries. No visual analysis.')
                    current['clips'].append(c)
                validate(current)
                current['revision'] += 1
                atomic(folder(pid) / 'project.json', current)
            update_job(pid, jid, state='completed', stage='completed')
        else:
            files = []
            for idx, c in enumerate(selected):
                update_job(pid, jid, stage=f'Rendering {idx + 1}/{len(selected)}', progress=idx / len(selected))
                name = re.sub(r'[^\w-]+', '_', c['title']).strip('_.')[:70] or 'clip'
                dest = work / f'{idx+1:02}_{name}.mp4'
                render(p, c, dest, run)
                files.append(str(dest))
            update_job(pid, jid, state='completed', stage='completed', progress=1, files=files)
    except InterruptedError:
        update_job(pid, jid, state='cancelled', stage='cancelled')
    except Exception as e:
        update_job(pid, jid, state='failed', stage='failed', error=str(e)[-2500:])
    finally:
        # Delete only this job's intermediates, retain final MP4/caption files and diagnostic log.
        for item in work.iterdir():
            if item.name.endswith('.partial.mp4') or re.fullmatch(r'part_\d+\.mp4|concat.txt|captions.ass', item.name):
                item.unlink(missing_ok=True)
        with LOCK:
            ACTIVE.pop(jid, None)


def render(p, c, dest, run):
    """Same EDL drives render and UI playback. Accurate re-encoded cuts, 30fps output."""
    w, h = {'9:16': (720, 1280), '16:9': (1280, 720), '1:1': (720, 720)}[c['aspect']]
    work = dest.parent
    if c['fit'] == 'fit':
        vf = f'scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2'
    else:
        vf = (f'scale={w}:{h}:force_original_aspect_ratio=increase,'
              f'crop={w}:{h}:(iw-ow)*{c["x"]}:(ih-oh)*{c["y"]}')
    parts = []
    for i, seg in enumerate(c['segments']):
        part = work / f'part_{i}.mp4'
        cmd = ['ffmpeg', '-v', 'error', '-nostdin', '-y', '-ss', str(seg['start']),
               '-i', p['source']['path'], '-t', str(seg['end'] - seg['start']),
               '-map', '0:v:0', '-vf', vf + ',setsar=1', '-r', '30',
               '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20', '-pix_fmt', 'yuv420p']
        if p['source']['audio'] and not c['mute']:
            af = f'volume={c["gain"]}dB'
            if c['normalize']:
                af += ',loudnorm=I=-16:TP=-1.5:LRA=11'
            cmd += ['-map', '0:a:0', '-af', af, '-c:a', 'aac', '-ar', '48000', '-ac', '2']
        else:
            cmd += ['-an']
        run(cmd + [str(part)])
        parts.append(part)
    (work / 'concat.txt').write_text(''.join(f"file '{x.name}'\n" for x in parts), encoding='utf-8')
    temp = dest.with_suffix('.partial.mp4')
    cmd = ['ffmpeg', '-v', 'error', '-nostdin', '-y', '-f', 'concat', '-safe', '1', '-i', 'concat.txt']
    mapped = cues(c, p['transcript'])
    if c['captions'] and mapped:
        # ASS dialogue is plain text; strip override syntax and escape line breaks.
        def ts(t):
            cs = round(t * 100)
            hh, cs = divmod(cs, 360000)
            mm, cs = divmod(cs, 6000)
            ss, cs = divmod(cs, 100)
            return f'{hh}:{mm:02}:{ss:02}.{cs:02}'
        color = c['color'].lstrip('#')
        bgr = color[4:6] + color[2:4] + color[0:2]
        ass = (f'[Script Info]\nScriptType: v4.00+\nPlayResX: {w}\nPlayResY: {h}\n'
               '[V4+ Styles]\nFormat: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding\n'
               f'Style: Default,Arial,{c["font_size"]},&H00{bgr},&H00FFFFFF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,2,1,2,50,50,90,1\n'
               '[Events]\nFormat: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text\n')
        for cue in mapped:
            txt = cue['text'].replace('\\', '＼').replace('{', '｛').replace('}', '｝').replace('\r', '').replace('\n', r'\N')
            ass += f'Dialogue: 0,{ts(cue["start"])},{ts(cue["end"])},Default,,0,0,0,,{txt}\n'
        (work / 'captions.ass').write_text(ass, encoding='utf-8')
        cmd += ['-vf', 'subtitles=captions.ass', '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20', '-c:a', 'copy']
    else:
        cmd += ['-c', 'copy']
    run(cmd + ['-movflags', '+faststart', str(temp)])
    actual = probe(temp)
    expected = sum(s['end'] - s['start'] for s in c['segments'])
    if (actual['width'], actual['height']) != (w, h) or abs(actual['duration'] - expected) > max(.25, len(parts) / 30 + .1):
        raise ValueError('Export validation failed: dimensions or duration did not match the edit')
    if actual['audio'] != bool(p['source']['audio'] and not c['mute']):
        raise ValueError('Export validation failed: audio stream mismatch')
    run(['ffmpeg', '-v', 'error', '-xerror', '-i', str(temp), '-f', 'null', '-'])
    os.replace(temp, dest)
    for kind in ('srt', 'vtt'):
        dest.with_suffix('.' + kind).write_text(subtitles(c, p['transcript'], kind), encoding='utf-8')


def shutdown():
    with LOCK:
        for ctl in list(ACTIVE.values()):
            ctl['cancel'].set()
            if ctl.get('proc'):
                kill(ctl['proc'])


def recover():
    DATA.mkdir(exist_ok=True)
    for path in DATA.glob('*/project.json'):
        try:
            p = json.loads(path.read_text(encoding='utf-8'))
            changed = False
            for j in p.get('jobs', []):
                if j['state'] in ('queued', 'running'):
                    j.update(state='failed', stage='interrupted', error='Application stopped during this job. Retry when ready.')
                    changed = True
            if changed:
                atomic(path, p)
        except (ValueError, OSError):
            continue


def api(handler, method, route, query):
    """Return True when a workspace route was handled."""
    if route == '/editor' and method == 'GET':
        handler._send(200, (ROOT / 'editor.html').read_text(encoding='utf-8'), 'text/html; charset=utf-8')
        return True
    if not route.startswith('/api/workspace/'):
        return False
    try:
        op = route.rsplit('/', 1)[-1]
        pid = (query.get('id') or [''])[0]
        if method == 'GET':
            if op == 'list':
                result = []
                for path in DATA.glob('*/project.json'):
                    try:
                        p = load(path.parent.name)
                        result.append({k: p[k] for k in ('id', 'title', 'updated')})
                    except (ValueError, OSError, KeyError):
                        continue
                handler._send(200, sorted(result, key=lambda p: p['updated'], reverse=True))
            elif op == 'project':
                handler._send(200, load(pid))
            elif op == 'file':
                p = load(pid)
                path = (query.get('path') or [''])[0]
                allowed = {p['source']['path']}
                for j in p['jobs']:
                    for f in j.get('files', []):
                        allowed.update((f, str(Path(f).with_suffix('.srt')), str(Path(f).with_suffix('.vtt'))))
                if path not in allowed:
                    raise ValueError('File is not part of this project')
                handler._serve_file(path)
            else:
                raise ValueError('Unknown workspace operation')
        else:
            body = handler._json_body()
            if op == 'create':
                result = create(body['source'])
            elif op == 'save':
                result = save(pid, body)
            elif op in ('export', 'analyze', 'waveform'):
                result = start(pid, op, body.get('clips'), body.get('language', 'auto'), body.get('model', 'base'), body.get('count', 7), body.get('min_sec', 20), body.get('max_sec', 45))
            elif op == 'cancel':
                with LOCK:
                    j = ACTIVE.get(body['job'])
                    if not j or not any(x['id'] == body['job'] for x in load(pid)['jobs']):
                        raise ValueError('No active job found')
                    j['cancel'].set()
                result = {'ok': True}
            elif op == 'relink':
                with LOCK:
                    if ACTIVE:
                        raise ValueError('Wait for the active job before relinking')
                    p = load(pid)
                    src = probe(body['source'])
                    old = p['source']
                    if any(src[k] != old[k] for k in ('width', 'height', 'audio')) or abs(src['duration'] - old['duration']) > .05:
                        raise ValueError('Replacement media has different duration, dimensions, or audio. Import it as a new project.')
                    p['source'] = src
                    p['revision'] += 1
                    atomic(folder(pid) / 'project.json', p)
                    result = p
            else:
                raise ValueError('Unknown workspace operation')
            handler._send(200, result)
    except (ValueError, KeyError, TypeError, OSError, subprocess.TimeoutExpired) as e:
        handler._send(400, {'error': str(e)})
    return True


if __name__ == '__main__':
    import sys
    if len(sys.argv) == 5 and sys.argv[1] == '--waveform':
        import struct
        # Bound memory to 1500 bins regardless of recording length.
        hop = max(100, math.ceil(float(sys.argv[4]) * 1000 / 1500))
        proc = subprocess.Popen(['ffmpeg', '-v', 'error', '-nostdin', '-i', sys.argv[2],
            '-vn', '-ac', '1', '-ar', '1000', '-f', 's16le', '-'], stdout=subprocess.PIPE)
        bins = []
        while True:
            raw = proc.stdout.read(hop * 2)
            if not raw:
                break
            samples = [v[0] / 32768 for v in struct.iter_unpack('<h', raw)]
            bins.append(round(math.sqrt(sum(v*v for v in samples) / len(samples)), 5))
        if proc.wait():
            raise SystemExit('Audio decoding failed')
        atomic(sys.argv[3], {'hop': hop / 1000, 'rms': bins})
    elif len(sys.argv) == 6 and sys.argv[1] == '--transcribe':
        from transcribe_source import transcribe
        transcribe(sys.argv[2], sys.argv[3], language=None if sys.argv[4] == 'auto' else sys.argv[4], model=sys.argv[5], reuse=False)
