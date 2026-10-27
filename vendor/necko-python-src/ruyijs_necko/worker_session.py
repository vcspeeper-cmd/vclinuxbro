"""Worker connection epoch, identity and sequence owner; no HTTP/ticket state."""
import struct
import threading
from .worker_frames import decode_preface, encode_preface
from .worker_json import U64_MAX, encode_control_frame
from .worker_schema import decode_message, validate_payload
from .worker_session_identity import local_identity
from .worker_channel_win import stall


class Session:
    def __init__(self, control, body, epoch, parent_pid, nonce, expected_manifest):
        self._control, self._body = control, body
        self._read, self._write = threading.Lock(), threading.Lock()
        self._dead = threading.Event()
        self._started = self._ready = False
        self._receive_seq = self._send_seq = 0
        try:
            self._identity = local_identity(expected_manifest)
            self._preface = encode_preface(2, epoch, self._identity["pid"], nonce)
            encode_preface(1, epoch, parent_pid, nonce)
            self._epoch, self._parent_pid, self._nonce = epoch, parent_pid, nonce
        except BaseException:
            self.stop()
            raise

    def stop(self):
        self._dead.set()
        self._control.stop()
        self._body.stop()

    def _check(self):
        if self._dead.is_set():
            raise ValueError("Worker session stopped")

    def _require_ready(self):
        self._check()
        if not self._ready:
            raise ValueError("Worker session not ready")

    def _make(self, kind, payload, sequence):
        return dict(version=1, type=kind, epoch=str(self._epoch),
                    channelSeq=str(sequence), payload=payload)

    def _read_message(self, deadline, stall_ms=0):
        if stall_ms:
            prefix, deadline = self._control.read_start(4, stall_ms)
        else:
            prefix = self._control.read_exact(4, deadline)
        size = struct.unpack("<I", prefix)[0]
        if not 1 <= size <= 262144:
            raise ValueError("Worker control prefix out of range")
        frame = prefix + self._control.read_exact(size, deadline)
        return decode_message(frame, "parent")

    def handshake(self, deadline_ms):
        if not self._read.acquire(False):
            raise ValueError("Worker session concurrent handshake/read")
        writing = False
        try:
            writing = self._write.acquire(False)
            if not writing:
                raise ValueError("Worker session concurrent handshake/write")
            self._check()
            if self._started:
                raise ValueError("Worker session handshake already attempted")
            self._started = True
            try:
                self._handshake(deadline_ms)
            except BaseException:
                self.stop()
                raise
        finally:
            if writing:
                self._write.release()
            self._read.release()

    def _handshake(self, deadline_ms):
        for channel in (self._control, self._body):
            channel.write_exact(self._preface, deadline_ms)
            peer = decode_preface(channel.read_exact(40, deadline_ms))
            if peer != dict(role=1, epoch=self._epoch, pid=self._parent_pid, nonce=self._nonce):
                raise ValueError("Worker preface peer identity differs")
        payload = dict(role="worker", nonce=self._nonce.hex(), **self._identity)
        envelope = self._make("hello", payload, 0)
        validate_payload(envelope, "worker")
        self._control.write_exact(encode_control_frame(envelope), deadline_ms)
        peer = self._read_message(deadline_ms)
        expected = self._make("hello", dict(role="parent", pid=self._parent_pid,
            nonce=self._nonce.hex(), manifest=self._identity["manifest"],
            qpcFrequency=self._identity["qpcFrequency"]), 0)
        if peer != expected:
            raise ValueError("Worker hello peer identity differs")
        self._send_seq = self._receive_seq = 1
        self._ready = True

    def send(self, kind, payload, deadline_ms):
        if not self._write.acquire(False):
            raise ValueError("Worker session concurrent writer")
        try:
            self._require_ready()
            try:
                if kind == "hello" or self._send_seq >= U64_MAX:
                    raise ValueError("Worker session send state/sequence")
                envelope = self._make(kind, payload, self._send_seq)
                validate_payload(envelope, "worker")
                frame = encode_control_frame(envelope)
                self._send_seq += 1
                self._control.write_exact(frame, deadline_ms)
            except BaseException:
                self.stop()
                raise
        finally:
            self._write.release()

    def receive(self, deadline_ms):
        return self._receive(deadline_ms)

    def receive_next(self, stall_ms=30000):
        return self._receive(None, stall(stall_ms))

    def _receive(self, deadline_ms, stall_ms=0):
        if not self._read.acquire(False):
            raise ValueError("Worker session concurrent reader")
        try:
            self._require_ready()
            try:
                if self._receive_seq >= U64_MAX:
                    raise ValueError("Worker session receive state/sequence")
                envelope = self._read_message(deadline_ms, stall_ms)
                if (envelope["type"] == "hello" or envelope["epoch"] != str(self._epoch) or
                        envelope["channelSeq"] != str(self._receive_seq)):
                    raise ValueError("Worker session peer sequence/epoch")
                self._receive_seq += 1
                return envelope
            except BaseException:
                self.stop()
                raise
        finally:
            self._read.release()
