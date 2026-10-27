"""Bounded typed helper frames; no browser objects or stream queues."""
import base64
import io
import struct
import sys

LIMIT=2*1024*1024

class Reader:
    def __init__(self,data):self.data=io.BytesIO(data)
    def number(self,kind='I'):
        size=struct.calcsize('<'+kind);data=self.data.read(size)
        if len(data)!=size:raise ValueError('Truncated WebTransport number')
        return struct.unpack('<'+kind,data)[0]
    def blob(self):
        size=self.number()
        if size>LIMIT:raise ValueError('WebTransport field limit')
        data=self.data.read(size)
        if len(data)!=size:raise ValueError('Truncated WebTransport field')
        return data
    def text(self):return self.blob().decode('utf8')
    def end(self):
        if self.data.read(1):raise ValueError('Trailing WebTransport frame data')

def receive():
    head=sys.stdin.buffer.read(4)
    if not head:return None
    if len(head)!=4:raise ValueError('Truncated WebTransport frame')
    size=struct.unpack('<I',head)[0]
    if size>LIMIT:raise ValueError('WebTransport frame limit')
    data=sys.stdin.buffer.read(size)
    if len(data)!=size:raise ValueError('Truncated WebTransport frame')
    return Reader(data)

def emit(events):
    if len(events)>257:raise ValueError('WebTransport event count limit')
    out=io.BytesIO()
    def number(value,kind='I'):out.write(struct.pack('<'+kind,value))
    def blob(value):number(len(value));out.write(value)
    def text(value):blob(value.encode('utf8'))
    number(len(events))
    for event in events:
        number(event['kind'])
        for key in ('ticket','stream','group','consumed'):number(event.get(key,0),'Q')
        for key in ('success','bidirectional','pending','fin','clean'):number(int(event.get(key,key=='success')))
        number(event.get('code',0));number(event.get('maxDatagramSize',1024))
        blob(event.get('bytes',b''));text(event.get('reason',''));text(event.get('protocol') or '')
        stats=event.get('statistics',{})
        if len(stats)>64:raise ValueError('WebTransport statistics limit')
        number(len(stats))
        for name,value in stats.items():text(name);number(float(value),'d')
    data=out.getvalue()
    if len(data)>LIMIT:raise ValueError('WebTransport event frame limit')
    sys.stdout.buffer.write(struct.pack('<I',len(data))+data);sys.stdout.buffer.flush()

def begin(reader):
    url,origin,congestion=reader.text(),reader.text(),reader.text()
    pooling,unreliable=reader.number(),reader.number()
    count=reader.number()
    if count>256:raise ValueError('WebTransport certificate hash count limit')
    hashes=[(reader.text(),reader.blob()) for _ in range(count)]
    count=reader.number()
    if count>256:raise ValueError('WebTransport protocol count limit')
    protocols=[reader.text() for _ in range(count)];reader.end()
    return dict(url=url,origin=origin,hashes=hashes,protocols=protocols)

def command(reader):
    value={'ticket':reader.number('Q'),'action':reader.number()}
    for key in ('stream','group'):value[key]=reader.number('Q')
    value['order']=reader.number('q')
    for key in ('bidirectional','hasGroup','hasOrder','sending'):value[key]=bool(reader.number())
    value['code']=reader.number();value['size']=reader.number()
    value['data']=base64.b64encode(reader.blob()).decode()
    value['label']=base64.b64encode(reader.blob()).decode()
    value['context']=base64.b64encode(reader.blob()).decode()
    value['reason']=reader.text();reader.end();return value

def stats(info):return {k:v for k,v in info.items() if isinstance(v,(int,float))}

def events(session):
    result=[]
    for event in session.poll():
        kind=event['type']
        if kind=='wt_stream':result.append(dict(kind=2,stream=event['stream'],bidirectional=event['bidirectional']))
        elif kind=='wt_datagram':result.append(dict(kind=3,bytes=base64.b64decode(event['data'])))
        elif kind=='wt_draining':result.append(dict(kind=4))
        elif kind in ('wt_reset','wt_stop_sending'):
            result.append(dict(kind=7 if kind=='wt_reset' else 8,stream=event['stream'],
                               code=event['code'],reason=event['reason'],clean=True))
        elif kind=='wt_closed':result.append(dict(kind=5,clean=event.get('clean',False),code=event.get('code',0),
            reason=event.get('reason','WebTransport peer closed'),success=event.get('clean',False),
            statistics=stats(event.get('statistics',{}))))
    return result
