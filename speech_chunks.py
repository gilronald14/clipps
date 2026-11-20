"""PCM windows with shared speech context and exclusive timestamp ownership."""

def windows(blocks, rate=16000, context_seconds=2):
    size = 2 * rate * context_seconds
    iterator = iter(blocks)
    current = next(iterator,None)
    left = b''
    offset = 0
    while current:
        following = next(iterator,b'')
        stop = offset+len(current)
        yield (left+current+following[:size], (offset-len(left))/(rate*2),
               offset/(rate*2),stop/(rate*2))
        left,current,offset = current[-size:],following,stop


def owned_segment(seg, offset, left, right, duration):
    words=[]
    for word in seg.words or []:
        a,b=offset+word.start,offset+word.end
        if left<=(a+b)/2<right and min(duration,b)>max(0,a) and word.word.strip():
            words.append(dict(start=round(max(0,a),3),end=round(min(duration,b),3),text=word.word.strip()))
    if words:
        return dict(start=words[0]['start'],end=words[-1]['end'],
                    text=' '.join(w['text'] for w in words),words=words)
    if not seg.words and left <= offset+(seg.start+seg.end)/2 < right:
        a,b=max(left,offset+seg.start),min(right,duration,offset+seg.end)
        if b>a and seg.text.strip():
            return dict(start=a,end=b,text=seg.text.strip(),words=[])
    return None
