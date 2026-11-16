"""Minimal RESP server for e2e: PING, LPUSH, BRPOP only."""
import socket
import threading
import time
import sys

store = {}
lock = threading.Lock()


def bulk(value):
    return f'${len(value)}\r\n{value}\r\n'.encode()


def handle(conn):
    buffer = b''
    try:
        while True:
            data = conn.recv(65536)
            if not data:
                return
            buffer += data
            while True:
                parsed = parse(buffer)
                if parsed is None:
                    break
                args, buffer = parsed
                name = args[0].upper()
                if name == 'PING':
                    conn.sendall(b'+PONG\r\n')
                elif name == 'LPUSH':
                    with lock:
                        store.setdefault(args[1], []).insert(0, args[2])
                        size = len(store[args[1]])
                    conn.sendall(f':{size}\r\n'.encode())
                elif name == 'BRPOP':
                    key, timeout = args[1], float(args[2])
                    end = time.time() + timeout
                    while True:
                        with lock:
                            if store.get(key):
                                value = store[key].pop()
                                conn.sendall(f'*2\r\n'.encode() + bulk(key) + bulk(value))
                                break
                        if time.time() >= end:
                            conn.sendall(b'*-1\r\n')
                            break
                        time.sleep(0.05)
                elif name == 'RPOP':
                    with lock:
                        values = store.get(args[1]) or []
                        value = values.pop() if values else None
                    conn.sendall(b'$-1\r\n' if value is None else bulk(value))
                elif name in ('SELECT',):
                    conn.sendall(b'+OK\r\n')
                else:
                    conn.sendall(b'-ERR unsupported\r\n')
    except OSError:
        pass
    finally:
        conn.close()


def parse(buf):
    if not buf.startswith(b'*'):
        return None
    lines = buf.split(b'\r\n')
    if len(lines) < 2:
        return None
    count = int(lines[0][1:])
    args = []
    index = 1
    for _ in range(count):
        if index >= len(lines):
            return None
        size = int(lines[index][1:])
        if index + 1 >= len(lines):
            return None
        args.append(lines[index + 1].decode())
        index += 2
    rest = b'\r\n'.join(lines[index:])
    return args, rest


def main(port):
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(('127.0.0.1', port))
    server.listen(64)
    print('fake redis on', port, flush=True)
    while True:
        conn, _ = server.accept()
        threading.Thread(target=handle, args=(conn,), daemon=True).start()


if __name__ == '__main__':
    main(int(sys.argv[1]))
