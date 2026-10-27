"""One system resolver call in a private disposable process."""
import json
import socket
import sys


def main():
    request = json.loads(sys.stdin.buffer.readline(4097))
    host, port, kind = request["host"], request["port"], request["kind"]
    if not isinstance(host, str) or not 1 <= len(host) <= 253 or "\0" in host:
        raise ValueError("Invalid DNS host")
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("Invalid DNS port")
    if kind not in (socket.SOCK_STREAM, socket.SOCK_DGRAM):
        raise ValueError("Invalid socket kind")
    rows, seen = [], set()
    for family, socktype, protocol, _, address in socket.getaddrinfo(host, port, type=kind):
        if family not in (socket.AF_INET, socket.AF_INET6):
            continue
        key = (family, socktype, protocol, address)
        if key in seen:
            continue
        seen.add(key)
        rows.append([family, socktype, protocol, list(address)])
        if len(rows) == 16:
            break
    if not rows:
        raise socket.gaierror("Resolver returned no IP addresses")
    return {"addresses": rows}


if __name__ == "__main__":
    try:
        result = main()
    except Exception as error:
        result = {"error": {"kind": type(error).__name__, "message": str(error)[:512]}}
    sys.stdout.write(json.dumps(result, separators=(",", ":")) + "\n")
