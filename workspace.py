"""Persistent manual editing workspace. Stdlib + native FFmpeg; no AI required.
All editing times are source seconds; ordered segments define edited time.
"""
import copy
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid
import offline_scan
import composition

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
                        '-of', 'json', path], capture_output=True, encoding="utf-8", timeout=30)
    if r.returncode:
        raise ValueError('Cannot read this media. Check the codec or choose an undamaged file.')
    d = json.loads(r.stdout)
    v = next((s for s in d.get('streams', []) if s['codec_type'] == 'video'
              and not s.get('disposition', {}).get('attached_pic')), None)
    audio = next((s for s in d['streams'] if s['codec_type'] == 'audio'), None)
    if not v and not audio:
        raise ValueError('No playable audio or video stream')
    has_video = bool(v)
    v = v or {}
    duration = number(d.get('format', {}).get('duration') or (v or audio).get('duration'), .01, 604800)
    def rate(s):
        try:
            a, b = s.split('/')
            return float(a) / float(b)
        except (ValueError, ZeroDivisionError, AttributeError):
            return 0
    rotation = int(float(v.get('tags', {}).get('rotate', 0)))
    for side in v.get('side_data_list', []):
        rotation = int(side.get('rotation', rotation))
    w, h = v.get('width', 720), v.get('height', 1280)
    if abs(rotation) % 180 == 90:
        w, h = h, w
    return dict(identity(path), duration=duration, width=w, height=h,
                fps=rate(v.get('avg_frame_rate')) or 30, rotation=rotation,
                audio=bool(audio), video=has_video, channels=(audio or {}).get('channels'),
                sample_rate=(audio or {}).get('sample_rate'), video_index=v.get('index'),
                codec=v.get('codec_name'), timing='source seconds; export CFR 30 fps')


def folder(pid):
    if not re.fullmatch(r'[0-9a-f]{32}', str(pid)):
        raise ValueError('Invalid project ID')
    return DATA / pid


def load(pid):
    with LOCK:
        p = json.loads((folder(pid) / 'project.json').read_text(encoding='utf-8'))
        if p.get('schema') == 1:
            p['source'].setdefault('video', True)
            p['schema'] = 2
        if p.get('schema') != 2:
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
        if c.get('caption_style','plain') not in ('plain','word'):
            raise ValueError('Unknown caption style')
        if c.get('resolution','720p') not in ('720p','1080p'):
            raise ValueError('Choose 720p or 1080p')
        c['title'] = str(c.get('title', 'Clip'))[:160]
        if c.get('aspect') not in ('9:16', '16:9', '1:1'):
            raise ValueError('Choose 9:16, 16:9, or 1:1')
        if c.get('fit') not in ('fit', 'fill', 'blur','follow','stack','webcam'):
            raise ValueError('Choose fit, fill or blurred background')
        composition.validate(c)
        c['crf'] = int(number(c.get('crf',20),16,28))
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
                normalize=False, captions=True, font_size=40, color='#ffffff',
                platform='YouTube Shorts', resolution='720p')


def create(path):
    source = probe(path)
    first_clip = new_clip(0, min(30, source['duration']), 'Manual starting clip')
    if source['video'] and source['width'] > source['height']:
        # Preserve the entire widescreen image without black sidebars.
        first_clip['fit'] = 'blur'
    p = dict(schema=2, id=uuid.uuid4().hex, revision=0, title=Path(path).stem,
             source=source, transcript=[], clips=[first_clip],
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


def cues(clip, transcript, emphasize=False):
    """Intersect source captions with every EDL segment and remap to edited time."""
    out, offset = [], 0.0
    for seg in clip['segments']:
        for cue in transcript:
            a, b = max(seg['start'], cue['start']), min(seg['end'], cue['end'])
            if b > a:
                text = cue['text']
                words = cue.get('words', [])
                if words and ''.join(text.split()) == ''.join(w['text'].strip() for w in words):
                    kept = [w for w in words if w['end'] > a and w['start'] < b]
                    if not kept:
                        continue
                    if emphasize:
                        for w in kept:
                            begin,finish=max(a,w['start']),min(b,w['end'])
                            if finish>begin:
                                out.append(dict(start=offset+begin-seg['start'],end=offset+finish-seg['start'],
                                                text=w['text'],emphasized=True))
                        continue
                    text = ' '.join(w['text'] for w in kept)
                    a, b = max(a, kept[0]['start']), min(b, kept[-1]['end'])
                out.append(dict(start=offset + a - seg['start'],
                                end=offset + b - seg['start'], text=text))
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


def start(pid, action, clip_ids=None, language='auto', model='base', count=7, min_sec=20, max_sec=45, profile='auto', platform='YouTube Shorts', resolution='720p', out_dir=None):
    with LOCK:
        legacy = __import__('sys').modules.get('webapp')
        if legacy and legacy.JOB.state == 'running':
            raise ValueError('The automatic generator is running. Wait or stop it first.')
        if ACTIVE:
            raise ValueError('A workspace job is already running. Wait or cancel it first.')
        p = load(pid)
        if identity(p['source']['path']) != {k: p['source'][k] for k in ('path', 'size', 'mtime_ns')}:
            raise ValueError('Source file changed. Import it as a new project to protect timing.')
        if profile not in offline_scan.PROFILES:
            raise ValueError('Unknown content profile')
        if platform not in ('custom', 'YouTube Shorts', 'TikTok', 'Instagram Reels', 'Facebook Reels', 'Vertical', 'Square', 'Landscape'):
            raise ValueError('Unknown export platform')
        if resolution not in ('720p', '1080p'):
            raise ValueError('Unknown export resolution')
        if action not in ('export', 'preview', 'analyze', 'scan', 'proxy', 'waveform'):
            raise ValueError('Unknown action')
        selected = [c for c in p['clips'] if c['id'] in (clip_ids or [])]
        if action in ('export','preview') and not selected:
            raise ValueError('Select at least one clip')
        if action == 'waveform' and not p['source']['audio']:
            raise ValueError('This source has no audio waveform')
        if action == 'analyze' and p['source']['audio'] and not p['transcript']:
            # Smart transcription is optional. Detect missing packages before
            # creating a background job so the UI shows actionable advice.
            from importlib.util import find_spec
            missing = [name for name in ('numpy', 'faster_whisper')
                       if find_spec(name) is None]
            if missing:
                raise ValueError(
                    'Offline Smart needs local Python packages: ' + ', '.join(missing)
                    + '. Install them in the Python environment launching Clipps'
                    + ' (prefer a Python 3.11 virtual environment).'
                    + ' Or choose Pure offline (no models) to analyze without AI.')
        if not re.fullmatch(r'auto|[a-z]{2,3}', language) or model not in ('tiny', 'base', 'small', 'medium'):
            raise ValueError('Invalid transcription settings')
        count = int(number(count, 1, 30))
        min_sec = number(min_sec, 1, 600)
        max_sec = number(max_sec, min_sec, 600)
        export_dir = None
        if action == 'export' and out_dir:
            export_dir = str(Path(out_dir))
            try:
                Path(export_dir).mkdir(parents=True, exist_ok=True)
            except OSError as e:
                raise ValueError(f'Cannot use that export folder: {e}')
        jid = uuid.uuid4().hex
        job = dict(id=jid, action=action, state='queued', stage='queued',
                   progress=None, files=[], error='', options=dict(count=count, min_sec=min_sec, max_sec=max_sec, profile=profile, platform=platform, resolution=resolution, out_dir=export_dir), revision=p['revision'], created=time.time())
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
                                    start_new_session=os.name != 'nt',
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
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
        if job['action'] == 'proxy':
            source = p['source']
            partial = work / 'preview.partial.mp4'
            cmd = ['ffmpeg','-nostdin','-v','error','-y','-i',source['path']]
            if source.get('video', True):
                cmd += ['-map', f"0:{source['video_index']}" if source.get('video_index') is not None else '0:v:0',
                        '-vf', "scale=640:640:force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1"]
            else:
                cmd += ['-filter_complex','[0:a]showwaves=s=360x640:mode=cline:rate=30[v]','-map','[v]']
            run(cmd + ['-map','0:a:0?','-c:a','aac','-c:v','libx264','-threads','2','-r','30',
                       '-preset','veryfast','-pix_fmt','yuv420p','-movflags','+faststart',str(partial)])
            if abs(probe(partial)['duration'] - source['duration']) > .3:
                raise ValueError('Preview duration differs from source')
            with LOCK:
                if ctl['cancel'].is_set():
                    raise InterruptedError('Cancelled')
                current = load(pid)
                if identity(source['path']) != {k: source[k] for k in ('path','size','mtime_ns')}:
                    raise ValueError('Source changed during preview generation')
                proxy = folder(pid) / 'preview.mp4'
                os.replace(partial,proxy)
                current['proxy'] = str(proxy)
                atomic(folder(pid) / 'project.json', current)
            update_job(pid,jid,state='completed',stage='Preview ready',progress=1)
        elif job['action'] == 'waveform':
            run([os.sys.executable, str(ROOT / 'workspace.py'), '--waveform',
                 p['source']['path'], str(work / 'waveform.json'), str(p['source']['duration'])])
            with LOCK:
                current = load(pid)
                current['waveform'] = json.loads((work / 'waveform.json').read_text())
                atomic(folder(pid) / 'project.json', current)
            update_job(pid, jid, state='completed', stage='completed')
        elif job['action'] in ('scan','analyze'):
            transcript = None
            cues_for_ranking = p['transcript']
            if job['action'] == 'analyze' and not cues_for_ranking and p['source']['audio']:
                update_job(pid,jid,stage='Local transcription')
                run([os.sys.executable, str(ROOT / 'workspace.py'), '--transcribe',
                     p['source']['path'], str(work / 'transcript.json'), language, model])
                transcript = json.loads((work / 'transcript.json').read_text(encoding='utf-8'))
                cues_for_ranking = transcript['segments']
            update_job(pid,jid,stage='Measuring audio and motion')
            cache = folder(pid) / 'signals-v2.json'
            data = json.loads(cache.read_text(encoding='utf-8')) if cache.exists() else {}
            source_id = identity(p['source']['path'])
            if data.get('source') != source_id:
                atomic(work / 'source.json', p['source'])
                run([os.sys.executable, str(ROOT / 'offline_scan.py'), str(work / 'source.json'),
                     str(work / 'signals.json')])
                data = dict(source=source_id, signals=json.loads((work / 'signals.json').read_text()))
                atomic(cache, data)
            opts = job['options']
            hits = offline_scan.rank(p['source'], data['signals'], cues_for_ranking, opts['count'],
                                     opts['min_sec'], opts['max_sec'], opts['profile'])
            layouts = [{} for _ in hits]
            if job['action']=='analyze' and hits and p['source'].get('video',True):
                update_job(pid,jid,stage='Checking local face layouts')
                atomic(work/'camera-input.json',[p['source'],hits])
                run([os.sys.executable,str(ROOT/'composition.py'),str(work/'camera-input.json'),str(work/'camera.json')])
                camera=json.loads((work/'camera.json').read_text(encoding='utf8'))
                layouts=camera['layouts']
                if camera['warning']:
                    update_job(pid,jid,camera_warning=camera['warning'])
            with LOCK:
                current = load(pid)
                if ctl['cancel'].is_set():
                    raise InterruptedError('Cancelled')
                if current['revision'] != p['revision'] or source_id != identity(p['source']['path']):
                    raise ValueError('Project or media changed during analysis. Run the scan again.')
                if transcript is not None:
                    current['transcript'] = cues_for_ranking
                    current['analysis'] = {key: transcript.get(key) for key in ('provider','timing','model','language')}
                known = {c.get('suggestion', {}).get('origin') for c in current['clips']}
                added = 0
                for hit,layout in zip(hits,layouts):
                    origin = f"{opts['profile']}:{hit['start']:.3f}:{hit['end']:.3f}"
                    if origin in known:
                        continue
                    clip = new_clip(hit['start'], hit['end'], f"Highlight at {hit['peak']}s")
                    clip['platform'] = opts['platform']
                    clip['resolution'] = opts['resolution']
                    if opts['platform'] == 'Square':
                        clip['aspect'] = '1:1'
                    elif opts['platform'] == 'Landscape':
                        clip['aspect'] = '16:9'
                    if p['source'].get('video', True) and p['source']['width'] > p['source']['height']:
                        # Safe default for a landscape clip until face/screen
                        # analysis supplies a more specific layout.
                        clip['fit'] = 'blur'
                    clip['suggestion'] = dict(hit, origin=origin, profile=opts['profile'], version=1,
                        warning='Review context and payoff.', excerpt='', provider='offline-signals')
                    clip.update(layout)
                    clip['normalize'] = p['source']['audio']
                    clip['suggestion']['excerpt'] = ' '.join(c['text'] for c in cues_for_ranking
                        if c['start'] < hit['end'] and c['end'] > hit['start'])[:800]
                    current['clips'].append(clip)
                    added += 1
                validate(current)
                current['revision'] += 1
                current['updated'] = time.time()
                atomic(folder(pid) / 'project.json', current)
            update_job(pid, jid, state='completed', stage=f'{added} new suggestions', progress=1)
        else:
            files = []
            for idx, c in enumerate(selected):
                update_job(pid, jid, stage=f'Rendering {idx + 1}/{len(selected)}', progress=idx / len(selected))
                name = re.sub(r'[^\w-]+', '_', c['title']).strip('_.')[:70] or 'clip'
                dest = work / f'{idx+1:02}_{name}.mp4'
                render(p, c, dest, run)
                files.append(str(dest))
            # Copy finished clips and their sidecars to the requested export
            # folder, so they are not buried in projects/<id>/<job>/. Sidecars
            # keep the MP4's final stem, so subtitle files still auto-load.
            export_dir = job['options'].get('out_dir')
            copied = []
            if export_dir:
                root = Path(export_dir)
                for f in files:
                    src = Path(f)
                    target = root / src.name
                    if target.exists():
                        target = root / f'{src.stem} ({uuid.uuid4().hex[:4]}){src.suffix}'
                    shutil.copy2(src, target)
                    copied.append(str(target))
                    for ext in ('.srt', '.vtt', '.json'):
                        side = src.with_suffix(ext)
                        if side.exists():
                            shutil.copy2(side, root / (target.stem + ext))
            update_job(pid, jid, state='completed', stage='completed', progress=1, files=files,
                       exported_to=export_dir or '', copied=copied)
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
    if c.get('resolution') == '1080p':
        w, h = w * 3 // 2, h * 3 // 2
    work = dest.parent
    if c['fit'] == 'fit':
        vf = f'scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2'
    elif c['fit'] == 'blur':
        vf = (f'split[base][subject];[base]scale={w//4}:{h//4}:force_original_aspect_ratio=increase,'
              f'crop={w//4}:{h//4},boxblur=8,scale={w}:{h},setsar=1[bg];'
              f'[subject]scale={w}:{h}:force_original_aspect_ratio=decrease,setsar=1[fg];'
              '[bg][fg]overlay=(W-w)/2:(H-h)/2')
    else:
        vf = (f'scale={w}:{h}:force_original_aspect_ratio=increase,'
              f'crop={w}:{h}:(iw-ow)*{c["x"]}:(ih-oh)*{c["y"]}')
    parts = []
    for i, seg in enumerate(c['segments']):
        part = work / f'part_{i}.mp4'
        if c['fit'] in ('stack','follow','webcam'):
            vf = composition.graph(c,seg['start'],w,h)
        cmd = ['ffmpeg', '-v', 'error', '-nostdin', '-y', '-ss', str(seg['start']),
               '-i', p['source']['path'], '-t', str(seg['end'] - seg['start']),
               '-map', '0:v:0', '-vf', vf + ',setsar=1', '-r', '30',
               '-c:v', 'libx264', '-preset', 'veryfast', '-crf', str(c.get('crf',20)), '-pix_fmt', 'yuv420p']
        if not p['source'].get('video', True):
            i_map = cmd.index('-map')
            cmd[i_map:i_map+4] = ['-filter_complex',
                f'[0:a:0]showwaves=s={w}x{h}:mode=cline:rate=30:colors=0x81c8ff,setsar=1[wave]',
                '-map', '[wave]']
        elif p['source'].get('video_index') is not None:
            cmd[cmd.index('-map')+1] = f"0:{p['source']['video_index']}"
        if p['source']['audio'] and not c['mute']:
            af = f'volume={c["gain"]}dB'
            cmd += ['-map', '0:a:0', '-af', af, '-c:a', 'aac', '-ar', '48000', '-ac', '2']
        else:
            cmd += ['-an']
        run(cmd + [str(part)])
        parts.append(part)
    (work / 'concat.txt').write_text(''.join(f"file '{x.name}'\n" for x in parts), encoding='utf-8')
    temp = dest.with_suffix('.partial.mp4')
    cmd = ['ffmpeg', '-v', 'error', '-nostdin', '-y', '-f', 'concat', '-safe', '1', '-i', 'concat.txt']
    mapped = cues(c, p['transcript'], emphasize=c.get('caption_style')=='word')
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
            if cue.get('emphasized'):
                txt = r'{\fscx108\fscy108\t(0,90,\fscx100\fscy100)}' + txt
            ass += f'Dialogue: 0,{ts(cue["start"])},{ts(cue["end"])},Default,,0,0,0,,{txt}\n'
        (work / 'captions.ass').write_text(ass, encoding='utf-8')
        cmd += ['-vf', 'subtitles=captions.ass', '-c:v', 'libx264', '-preset', 'veryfast', '-crf', str(c.get('crf',20)), '-c:a', 'copy']
    else:
        cmd += ['-c', 'copy']
    if c['normalize'] and not c['mute'] and p['source']['audio']:
        norm = 'loudnorm=I=-16:TP=-1.5:LRA=11'
        run(['ffmpeg','-nostdin','-v','info','-f','concat','-safe','1','-i','concat.txt',
             '-vn','-af',norm+':print_format=json','-f','null','-'])
        log = (work / 'process.log').read_text(encoding='utf-8',errors='replace')
        stats = re.search(r'\{\s*"input_i".*?\}',log,re.S)
        if stats:
            values = json.loads(stats.group())
            fields = {'measured_I':'input_i','measured_TP':'input_tp','measured_LRA':'input_lra',
                      'measured_thresh':'input_thresh','offset':'target_offset'}
            if all(math.isfinite(float(values[k])) for k in fields.values()):
                norm += ''.join(f':{k}={values[v]}' for k,v in fields.items()) + ':linear=true'
        cmd += ['-c:a','aac','-af',norm,'-ar','48000']
        if '-c' in cmd:
            cmd[cmd.index('-c')] = '-c:v'
    run(cmd + ['-movflags', '+faststart', str(temp)])
    actual = probe(temp)
    expected = sum(s['end'] - s['start'] for s in c['segments'])
    if (actual['width'], actual['height']) != (w, h) or abs(actual['duration'] - expected) > max(.25, len(parts) / 30 + .1):
        raise ValueError('Export validation failed: dimensions or duration did not match the edit')
    if actual['audio'] != bool(p['source']['audio'] and not c['mute']):
        raise ValueError('Export validation failed: audio stream mismatch')
    run(['ffmpeg', '-v', 'error', '-xerror', '-i', str(temp), '-f', 'null', '-'])
    os.replace(temp, dest)
    seconds = actual['duration']
    if w > h or seconds > 180:
        shorts = 'Outside the square/vertical or three-minute Shorts limits.'
    elif seconds > 60:
        shorts = 'An active copyright claim can block this Short worldwide.'
    else:
        shorts = 'Review rights before publishing.'
    atomic(dest.with_suffix('.json'), dict(source=identity(p['source']['path']), segments=c['segments'],
        dimensions=[w,h], duration=actual['duration'], fps=actual['fps'], codec=actual['codec'],
        audio=actual['audio'], review_rights=True, youtube_shorts_note=shorts, suggestion=c.get('suggestion')))
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
            if p.get('schema') not in (1, 2):
                continue
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
                if p.get('proxy'):
                    allowed.add(p['proxy'])
                for j in p['jobs']:
                    for f in j.get('files', []):
                        allowed.update((f, str(Path(f).with_suffix('.srt')), str(Path(f).with_suffix('.vtt')), str(Path(f).with_suffix('.json'))))
                # Windows may canonicalize the drive/path spelling during import.
                # Compare resolved paths instead of raw strings, but serve only
                # the already approved project path, never an arbitrary request.
                canonical = lambda candidate: os.path.normcase(os.path.realpath(candidate))
                approved = {canonical(candidate): candidate for candidate in allowed}
                selected = approved.get(canonical(path)) if path else None
                if selected is None:
                    raise ValueError('File is not part of this project')
                handler._serve_file(selected)
            else:
                raise ValueError('Unknown workspace operation')
        else:
            body = handler._json_body()
            if op == 'create':
                result = create(body['source'])
            elif op == 'save':
                result = save(pid, body)
            elif op in ('export', 'preview', 'analyze', 'scan', 'proxy', 'waveform'):
                result = start(pid, op, body.get('clips'), body.get('language', 'auto'), body.get('model', 'base'), body.get('count', 7), body.get('min_sec', 20), body.get('max_sec', 45), body.get('profile','auto'), body.get('platform','YouTube Shorts'), body.get('resolution','720p'), body.get('out_dir'))
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
                    p.pop('proxy',None)
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
        # A raw traceback here would surface in the UI; explain instead. The
        # scheduler pre-checks most cases, this covers anything it missed.
        try:
            from transcribe_source import transcribe
        except ImportError as missing:
            raise SystemExit(
                'Offline smart needs local transcription libraries that are not installed '
                f'(missing: {missing.name}). Use Pure offline mode — it needs no models — '
                'or run setup.bat on a connected PC to install them.')
        transcribe(sys.argv[2], sys.argv[3], language=None if sys.argv[4] == 'auto' else sys.argv[4], model=sys.argv[5], reuse=False)
