# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import io
from urllib.parse import unquote

from requests.exceptions import InvalidSchema, ProxyError


def negotiate(connection, proxy, target):
    try:
        import socks
    except ImportError as error:
        raise InvalidSchema(
            "Install necko-standalone[socks] for SOCKS proxy support"
        ) from error

    class Stream(io.RawIOBase):
        def readable(self):
            return True

        def writable(self):
            return True

        def readinto(self, buffer):
            data = connection.read(len(buffer))
            buffer[: len(data)] = data
            return len(data)

        def write(self, buffer):
            connection.write(bytes(buffer))
            return len(buffer)

    class Negotiator(socks.socksocket):
        def makefile(self, mode="r", buffering=-1, **kwargs):
            stream = Stream()
            return io.BufferedWriter(stream) if "w" in mode else stream

    with Negotiator() as negotiator:
        version = socks.SOCKS5 if proxy.scheme.startswith("socks5") else socks.SOCKS4
        negotiator.set_proxy(
            version,
            proxy.hostname,
            proxy.port or 1080,
            rdns=proxy.scheme in ("socks5h", "socks4a"),
            username=unquote(proxy.username) if proxy.username else None,
            password=unquote(proxy.password) if proxy.password else None,
        )
        port = target.port or (443 if target.scheme == "https" else 80)
        try:
            if version == socks.SOCKS5:
                negotiator._negotiate_SOCKS5(target.hostname, port)
            else:
                negotiator._negotiate_SOCKS4(target.hostname, port)
        except socks.ProxyError as error:
            raise ProxyError(error) from error
