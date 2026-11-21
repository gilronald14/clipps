"""Source-time camera decisions shared by project renders and rendered previews."""
import json
import math
import statistics
import subprocess
import sys
from pathlib import Path


def decide(samples, profile, width, height):
    visible = [(t, sorted(boxes,key=lambda b:b[2]*b[3],reverse=True)) for t,boxes in samples if boxes]
    if width <= height or not samples or len(visible) < len(samples)*.5 or profile == 'tutorial':
        return {}
    def center(box):
        x,y,w,h = box
        return [min(1,max(0,x+w/2)), min(1,max(0,y+h/2))]
    pairs = [sorted([center(b) for b in boxes[:2]]) for _,boxes in visible if len(boxes)>=2]
    pairs = [pair for pair in pairs if pair[1][0]-pair[0][0]>.22]
    if len(pairs)>=len(samples)*.5 and profile!='gameplay':
        return dict(fit='stack', anchors=[[statistics.median(pair[i][k] for pair in pairs)
                    for k in range(2)] for i in range(2)])
    path = []
    for t,boxes in visible:
        x,y = center(boxes[0])
        if path:
            x,y = (.65*path[-1][1]+.35*x,.65*path[-1][2]+.35*y)
        path.append([round(t,3),round(x,4),round(y,4)])
    return dict(fit='webcam' if profile=='gameplay' else 'follow',camera=path)


def inspect(source, hits):
    try:
        import shortsmith as legacy
        legacy._load_detector()
    except (ImportError, RuntimeError, OSError) as exc:
        return [{} for _ in hits], str(exc)
    width=min(640,source['width'])
    height=max(2,round(width*source['height']/source['width']/2)*2)
    result=[]
    for hit in hits:
        length=hit['end']-hit['start']
        step=max(.5,length/32)
        cmd=['ffmpeg','-nostdin','-v','error','-ss',str(hit['start']),'-i',source['path'],
             '-t',str(length),'-an','-vf',f'fps={1/step},scale={width}:{height}',
             '-pix_fmt','bgr24','-f','rawvideo','-']
        samples=[]
        with subprocess.Popen(cmd,stdout=subprocess.PIPE) as proc:
            while data:=proc.stdout.read(width*height*3):
                frame=legacy.np.frombuffer(data,legacy.np.uint8).reshape(height,width,3)
                boxes=[]
                for candidate in legacy.detect_face_candidates(frame):
                    x,y,w,h=candidate['box']
                    boxes.append((x/width,y/height,w/width,h/height))
                samples.append((hit['start']+len(samples)*step,boxes))
            if proc.wait():
                raise ValueError('Camera analysis could not decode this clip')
        result.append(decide(samples,hit['profile_used'],width,height))
    return result,''


def validate(clip):
    path=clip.get('camera',[])
    if len(path)>128:
        raise ValueError('Too many camera positions')
    prev=-1
    for t,x,y in path:
        if not all(math.isfinite(v) for v in (t,x,y)) or t<=prev or not (0<=x<=1 and 0<=y<=1):
            raise ValueError('Invalid camera position')
        prev=t
    anchors=clip.get('anchors',[[.25,.5],[.75,.5]])
    if len(anchors)!=2 or any(len(a)!=2 or any(not 0<=v<=1 for v in a) for a in anchors):
        raise ValueError('Invalid stack positions')


def graph(clip, start, w, h):
    def axis(index,default):
        path=clip.get('camera',[])
        if not path:
            return str(default)
        expr=str(path[-1][index])
        for a,b in reversed(list(zip(path,path[1:]))):
            u,v=a[index],b[index]
            interp=f'{u}+({v}-{u})*clip((t+{start}-{a[0]})/{b[0]-a[0]},0,1)'
            expr=f'if(lt(t+{start},{b[0]}),{interp},{expr})'
        return expr
    def crop(ow,oh,x,y):
        return (f'scale={ow}:{oh}:force_original_aspect_ratio=increase,'
                f"crop={ow}:{oh}:'clip(iw*({x})-ow/2,0,iw-ow)':'clip(ih*({y})-oh/2,0,ih-oh)',setsar=1")
    if clip['fit']=='follow':
        return crop(w,h,axis(1,clip['x']),axis(2,clip['y']))
    if clip['fit']=='webcam':
        top=h//3//2*2
        return (f"split[a][b];[a]{crop(w,top,axis(1,clip['x']),axis(2,clip['y']))}[face];"
                f"[b]scale={w}:{h-top}:force_original_aspect_ratio=decrease,pad={w}:{h-top}:(ow-iw)/2:(oh-ih)/2,setsar=1[game];"
                '[face][game]vstack')
    a,b=clip.get('anchors',[[.25,.5],[.75,.5]])
    return f'split[a][b];[a]{crop(w,h//2,*a)}[left];[b]{crop(w,h//2,*b)}[right];[left][right]vstack'


if __name__=='__main__':
    from workspace import atomic
    source,hits=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    layouts,warning=inspect(source,hits)
    atomic(sys.argv[2],dict(layouts=layouts,warning=warning))
