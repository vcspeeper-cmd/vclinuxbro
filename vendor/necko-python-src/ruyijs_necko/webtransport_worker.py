"""WebTransport mode of the original Necko helper process."""
import base64
from .webtransport_session import Session, StreamError
from . import webtransport_wire as wire

class Handles:
    """Translate caller group handles to this helper's original Neqo handles."""
    def __init__(self):
        self.groups={}
        self.members={}

    def group(self,local):
        if local not in self.groups:raise ValueError('Unknown WebTransport send group')
        return self.groups[local]

    def statistics(self,session,local):
        self.group(local)
        result=dict(bytesWritten=0,bytesSent=0,bytesAcknowledged=0)
        for stream,group in self.members.items():
            if group!=local:continue
            values=session.command('stream_stats',stream=stream,sending=True)
            for key in result:result[key]+=values[key]
        return result


def execute(session,value,handles):
    action=value['action'];stream=value['stream'];sid=session.session
    result=dict(kind=1,ticket=value['ticket'])
    local=value['group'] if value['hasGroup'] else None
    group=handles.group(local) if local is not None and action in (0,11) else None
    order=value['order'] if value['hasOrder'] else None
    if action==0:
        stream=session.stream(value['bidirectional'],group,order)
        if local is not None:handles.members[stream]=local
        result.update(stream=stream,bidirectional=value['bidirectional'])
    elif action==1:
        data=session.read(stream,value['size'])
        result.update(bytes=base64.b64decode(data.get('data','')),pending=data.get('pending',False),fin=data.get('fin',False))
    elif action==2:result['consumed']=session.write(stream,value['data'])
    elif action==3:session.finish(stream)
    elif action in (4,5):session.command('reset' if action==4 else 'stop',stream=stream,code=value['code'])
    elif action==6:session.command('datagram',session=sid,data=value['data'])
    elif action==7:
        if local is not None:result['statistics']=handles.statistics(session,local)
        else:
            info=session.command('info',session=sid)
            result.update(statistics=wire.stats(info),maxDatagramSize=info['maxDatagramSize'],protocol=info.get('protocol'))
    elif action==8:result['statistics']=session.command('stream_stats',stream=stream,sending=value['sending'])
    elif action==9:
        local=value['group']
        if local in handles.groups:raise ValueError('Duplicate WebTransport send group')
        handles.groups[local]=session.command('group',session=sid)['group']
        result['group']=local
    elif action==10:session.command('order',stream=stream,order=order)
    elif action==11:
        session.command('set_group',session=sid,stream=stream,group=group)
        if local is None:handles.members.pop(stream,None)
        else:handles.members[stream]=local
    elif action==12:
        info=session.command('export',session=sid,label=value['label'],context=value['context'])
        result['bytes']=base64.b64decode(info['data'])
    elif action==13:
        session.close(value['code'],value['reason'])
        return [result,dict(kind=5,clean=True,code=value['code'],reason=value['reason'],
                            statistics=wire.stats(session.info))]
    else:raise ValueError('Unknown WebTransport helper command')
    return [result]

def main():
    session=None;handles=Handles()
    try:
        while True:
            reader=wire.receive()
            if reader is None:return
            operation=reader.number()
            if operation==1:
                if session is not None:raise ValueError('WebTransport session already initialized')
                session=Session(**wire.begin(reader));info=session.info
                wire.emit([dict(kind=0,maxDatagramSize=info['maxDatagramSize'],protocol=info.get('protocol'),
                                statistics=wire.stats(info))]+wire.events(session))
            elif operation==2:
                value=wire.command(reader)
                try:result=execute(session,value,handles)
                except Exception as error:
                    result=[dict(kind=1,ticket=value['ticket'],success=False,reason=str(error)[:1024],
                                 clean=isinstance(error,StreamError),code=error.code if isinstance(error,StreamError) else 0)]
                if value['action']!=13:
                    events=wire.events(session)
                    # A typed stream failure must error the original controller
                    # before its failed operation settles, sharing one JS error.
                    # Successful creation keeps Result first so endpoints exist.
                    failed_stream=not result[0].get('success',True) and result[0].get('clean',False)
                    result=events+result if failed_stream else result+events
                wire.emit(result)
                if value['action']==13:return
            elif operation==3:
                reader.end();wire.emit(wire.events(session))
            else:raise ValueError('Unknown WebTransport helper operation')
    except Exception as error:wire.emit([dict(kind=6,success=False,reason=str(error)[:1024])])
    finally:
        if session:session.close()
