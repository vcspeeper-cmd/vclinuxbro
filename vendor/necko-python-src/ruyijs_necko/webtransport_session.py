"""WebTransport session projections using the original H3 Connection worker."""
import base64
from collections import deque
from pathlib import Path
import threading
from urllib.parse import urlsplit
from necko._http3 import Connection
from necko._nss import Context
from urllib3.exceptions import ProtocolError


class StreamError(ProtocolError):
    def __init__(self, sending, wire_code):
        shifted = wire_code - 0x52e4a40fa8db
        self.code = shifted - shifted // 31 if 0 <= shifted <= 263 else 0
        self.sending = sending
        super().__init__('WebTransportStream ' + ('StopSending' if sending else 'Reset'))


class Endpoint:
    def __init__(self, stream=None):
        self.stream_id = stream
        self.error = None
        self.read_error = self.write_error = None
        self.closed = self.ended = self.readable = self.send_stopped = False
        self.received_data = False

    def fail(self, error):
        if self.error is None:
            self.error = error


class Session:
    MAX_EVENTS = 256
    MAX_EVENT_BYTES = 1024 * 1024

    def __init__(self, url, origin, hashes=(), protocols=(), timeout=10, verify=True):
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.fragment or parsed.username:
            raise ValueError('WebTransport requires an absolute HTTPS URL without credentials or fragment')
        self._lock = threading.Lock()
        self._events, self._event_bytes = deque(), 0
        self._negotiated = threading.Event()
        self._session_ready = threading.Event()
        self._negotiation_ok = False
        self._timeout = timeout
        self._closed = False
        self.info = {}
        self.session = self.connection = None
        self._control = Endpoint()
        context = Context(verify, None)
        if hashes:
            from ruyijs_necko.webtransport_verify import HashContext
            context = HashContext(hashes)
        self.connection = Connection(parsed.hostname, parsed.port or 443, context,
            str(Path(__import__('necko').__file__).parent/'necko_core.dll'), timeout,
            webtransport=self)
        try:
            if not self._negotiated.wait(timeout):
                raise TimeoutError('WebTransport negotiation timed out')
            if self._control.error:
                raise self._control.error
            if not self._negotiation_ok:
                raise ProtocolError('Peer did not negotiate WebTransport')
            fields=[('origin',base64.b64encode(origin.encode()).decode())]
            if protocols:
                quoted=', '.join('"'+p.replace('\\','\\\\').replace('"','\\"')+'"' for p in protocols)
                fields.append(('wt-available-protocols',base64.b64encode(quoted.encode('ascii')).decode()))
            path=(parsed.path or '/')+('?' + parsed.query if parsed.query else '')
            self.session=self.command('open',authority=parsed.netloc,path=path,headers=fields)['session']
            if not self._session_ready.wait(timeout):
                raise TimeoutError('WebTransport session response timed out')
            if self._control.error:
                raise self._control.error
            info=self.command('info',session=self.session)
            if info.get('protocol') and info['protocol'] not in protocols:
                raise ProtocolError('Peer selected an unoffered WebTransport protocol')
            self.info.update(info)
        except BaseException:
            self.connection.close()
            raise

    def event(self, connection, event):
        kind=event['type']
        if kind=='wt_negotiated':
            self._negotiation_ok=event['available'];self._negotiated.set();return
        if kind=='wt_session':
            if event['status'] != 200:self._control.fail(ProtocolError('WebTransport HTTP '+str(event['status'])))
            self._session_ready.set()
        if kind=='wt_stream':
            self.add_endpoint(connection,event['stream'])
        if kind=='wt_closed':
            # Already on the sole protocol thread: read counters directly,
            # before connection retirement, without submitting to ourselves.
            snapshot=connection._call('webtransport',action='info',session=event['session'])
            self.info.update((k,v) for k,v in snapshot.items() if v is not None)
            event['statistics']=dict(self.info)
            self._closed=True
            self._control.fail(ProtocolError('WebTransport session closed: '+str(event.get('reason',''))))
            self._session_ready.set()
        cost=len(event.get('data',''))+len(str(event.get('reason','')))+256
        with self._lock:
            if len(self._events)>=self.MAX_EVENTS or self._event_bytes+cost>self.MAX_EVENT_BYTES:
                raise ProtocolError('WebTransport event budget exceeded')
            self._events.append((event,cost));self._event_bytes+=cost

    def add_endpoint(self, connection, stream):
        # Called exclusively by the original Connection protocol thread.
        if stream not in connection.streams:
            connection.streams[stream]=Endpoint(stream)

    def stream_event(self, connection, event):
        endpoint = connection.streams.get(event.get('stream'))
        if not isinstance(endpoint, Endpoint):
            return False
        sending = event['type'] == 'stop_sending'
        error = StreamError(sending, event['error'])
        if sending:
            endpoint.write_error = endpoint.write_error or error
        else:
            endpoint.read_error = endpoint.read_error or error
        self.event(connection, dict(type='wt_stop_sending' if sending else 'wt_reset',
                                    stream=event['stream'], code=error.code, reason=str(error)))
        return True

    @staticmethod
    def check_operation(endpoint, operation):
        if isinstance(endpoint, Endpoint):
            error = endpoint.read_error if operation == 'wt_read' else (
                endpoint.write_error if operation in ('write', 'finish') else None)
            if error:
                raise error

    def connection_closed(self, error):
        self._control.fail(error)
        self._negotiated.set()
        self._session_ready.set()

    def command(self, action, **arguments):
        return self.connection.submit('webtransport',self._control,self._timeout,action=action,**arguments)

    def poll(self):
        with self._lock:
            values=[item[0] for item in self._events]
            self._events.clear();self._event_bytes=0
        if self.connection.error:
            raise self.connection.error
        return values

    def stream(self, bidirectional, group=None, order=0):
        stream=self.command('stream',session=self.session,bidirectional=bidirectional,group=group,order=order)['stream']
        return stream

    def read(self, stream, size=65536):
        endpoint=self.connection.streams[stream]
        if endpoint.error:raise endpoint.error
        if not isinstance(size,int) or not 1<=size<=65536:raise ValueError('WebTransport read size must be 1..65536')
        return self.connection.submit('wt_read',endpoint,self._timeout,size=size)

    def write(self, stream, data):
        if len(data)>87384:raise ValueError('WebTransport write exceeds 64 KiB')
        decoded=base64.b64decode(data,validate=True)
        if len(decoded)>65536:raise ValueError('WebTransport write exceeds 64 KiB')
        if not decoded:return 0
        return self.connection.submit('write',self.connection.streams[stream],self._timeout,data=data)

    def finish(self, stream):
        return self.connection.submit('finish',self.connection.streams[stream],self._timeout)

    def close(self, code=0, reason=''):
        try:
            if self.session is not None and not self.connection.closed and not self._closed:
                self.command('close',session=self.session,code=code,reason=reason)
        finally:
            self._closed=True
            if self.connection:self.connection.close()
