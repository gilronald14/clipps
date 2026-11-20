"""Low-resolution, one-second evidence for model-free project suggestions."""
from array import array
from bisect import bisect_left
from collections import deque
import json
import math
import subprocess
import sys


PROFILES = {
    'auto': (.40, .35, .25),
    'gameplay': (.30, .60, .10),
    'interview': (.30, .10, .60),
    'podcast': (.40, .05, .55),
    'tutorial': (.25, .35, .40),
    'livestream': (.40, .40, .20),
}


def blocks(path, options, size):
    cmd = ['ffmpeg', '-nostdin', '-v', 'error', '-threads', '1', '-i', path,
           *options, 'pipe:1']
    with subprocess.Popen(cmd, stdout=subprocess.PIPE) as proc:
        try:
            while data := proc.stdout.read(size):
                yield data
        finally:
            proc.stdout.close()
        if proc.wait():
            raise ValueError('FFmpeg could not decode the analysis stream')


def measure(source):
    path = source['path']
    audio, motion = [], []
    if source['audio']:
        history = deque(maxlen=20)
        for block in blocks(path, ['-vn', '-ac', '1', '-ar', '4000', '-f', 's16le'], 8000):
            samples = array('h', block)
            rms = math.sqrt(sum(v*v for v in samples) / len(samples)) / 32768
            baseline = sum(history) / len(history) if history else rms
            audio.append(min(1., rms + max(0., rms-baseline) / max(.035, baseline)))
            history.append(rms)
    if source.get('video', True):
        previous = None
        for frame in blocks(path, ['-an', '-vf', 'fps=1,scale=32:18,format=gray',
                                    '-f', 'rawvideo'], 576):
            if len(frame) != 576:
                raise ValueError('Incomplete analysis frame')
            delta = sum(abs(a-b) for a,b in zip(previous, frame)) if previous else 0
            motion.append(min(1., delta / (576*48)))
            previous = frame
    return {'audio': audio, 'motion': motion}


def rank(source, signals, cues, count, minimum, maximum, profile):
    duration = source['duration']
    if duration < 1/30:
        return []
    length = min(duration, maximum, max(minimum, (minimum + maximum)/2))
    cues = sorted(cues, key=lambda c: c['start'])
    starts = [c['start'] for c in cues]
    weights = PROFILES[profile]
    peaks = []
    for second in range(math.ceil(duration)):
        audio, motion = (signals[k][second] if second < len(signals[k]) else 0
                         for k in ('audio', 'motion'))
        index = bisect_left(starts, second)
        nearby = cues[max(0,index-1):bisect_left(starts,second+1)]
        speech = max((.45 + .25*('?' in c['text']) + .30*c['text'].endswith(('.', '!'))
                      for c in nearby if c['end'] > second and c['text']), default=0)
        score = sum(w*v for w,v in zip(weights,(audio,motion,speech)))
        if score > .001:
            peaks.append((score, second, audio, motion, speech))
    selected = []
    for score, second, audio, motion, speech in sorted(peaks, key=lambda p: -p[0]):
        start = min(max(0, second-length*.55), duration-length)
        end = start + length
        # Include the action following a scene change if the surrounding frames
        # form a bounded burst. A hard cut alone can outrank the action itself.
        frames = signals['motion']
        if motion >= .12 and profile in ('auto', 'gameplay', 'tutorial', 'livestream'):
            left = right = second
            while left > 0 and second-left < maximum and frames[left-1] >= .08:
                left -= 1
            while right+1 < len(frames) and right-left < maximum and frames[right+1] >= .08:
                right += 1
            if 2 <= right-left+1 <= maximum-1:
                span = min(duration, maximum, max(minimum, right-left+3))
                start = max(0, min(left-1, duration-span))
                end = start + span
        i = bisect_left(starts, start)
        if i < len(cues) and abs(starts[i]-start) <= 2 and end-starts[i] >= minimum:
            start = starts[i]
        i = bisect_left(starts, end)
        if i and minimum <= cues[i-1]['end']-start <= maximum and abs(cues[i-1]['end']-end)<=3:
            end = min(duration, cues[i-1]['end'])
        if any(min(end,h['end']) > max(start,h['start']) for h in selected):
            continue
        evidence = dict(audio=round(audio,3), motion=round(motion,3), speech=round(speech,3))
        strongest = max(zip(weights,(audio,motion,speech),evidence),key=lambda t:t[0]*t[1])[2]
        selected.append(dict(start=start,end=end,peak=second,score=round(score,4),
                             components=evidence,reason=f'Strongest signal: {strongest} near {second}s.'))
        if len(selected)==count:
            break
    return selected


if __name__ == '__main__':
    from workspace import atomic
    with open(sys.argv[1], encoding='utf-8') as f:
        source = json.load(f)
    atomic(sys.argv[2], measure(source))
