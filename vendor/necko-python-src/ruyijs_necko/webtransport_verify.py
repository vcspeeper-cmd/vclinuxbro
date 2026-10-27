"""Explicit WebTransport server certificate hashes, separate from PKI verification."""
import datetime
import hashlib
import hmac
import ssl
import struct
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ec


class HashContext:
    def __init__(self, hashes):
        self.hashes = tuple(bytes(value) for algorithm,value in hashes
                            if isinstance(algorithm,str) and algorithm.lower() == 'sha-256' and len(value) == 32)
        if not self.hashes:
            raise ssl.SSLCertVerificationError('No supported WebTransport certificate hash')

    def verify(self, host, data):
        if len(data)<4:raise ssl.SSLCertVerificationError('Missing server certificate')
        length=struct.unpack('!I',data[:4])[0]
        der=data[4:4+length]
        if len(der)!=length:raise ssl.SSLCertVerificationError('Truncated server certificate')
        try:
            certificate=x509.load_der_x509_certificate(der)
        except ValueError as error:
            raise ssl.SSLCertVerificationError('Invalid WebTransport server certificate') from error
        now=datetime.datetime.now(datetime.timezone.utc)
        if not certificate.not_valid_before_utc<=now<=certificate.not_valid_after_utc:
            raise ssl.SSLCertVerificationError('WebTransport certificate is outside its validity interval')
        if certificate.not_valid_after_utc-certificate.not_valid_before_utc>datetime.timedelta(days=14):
            raise ssl.SSLCertVerificationError('WebTransport certificate validity exceeds 14 days')
        key=certificate.public_key()
        if not isinstance(key,ec.EllipticCurvePublicKey) or not isinstance(key.curve,ec.SECP256R1):
            raise ssl.SSLCertVerificationError('WebTransport certificate requires an ECDSA P-256 key')
        digest=hashlib.sha256(der).digest()
        if not any(hmac.compare_digest(digest,value) for value in self.hashes):
            raise ssl.SSLCertVerificationError('WebTransport server certificate hash mismatch')
