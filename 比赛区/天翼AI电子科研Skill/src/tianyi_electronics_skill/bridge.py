"""跨网段反向 TCP 桥接：设备端主动连接中继，Agent 端使用本地代理。"""

from __future__ import annotations

import socket
import threading


def _addr(value: str) -> tuple[str, int]:
    host, sep, port = value.rpartition(":")
    if not sep or not port.isdigit():
        raise ValueError(f"地址必须是 host:port: {value}")
    return host or "127.0.0.1", int(port)


def _relay(left: socket.socket, right: socket.socket) -> None:
    def pump(source, target):
        try:
            while True:
                data = source.recv(65536)
                if not data:
                    break
                target.sendall(data)
        except OSError:
            pass
        finally:
            for sock in (source, target):
                try: sock.shutdown(socket.SHUT_RDWR)
                except OSError: pass
    a = threading.Thread(target=pump, args=(left, right), daemon=True)
    b = threading.Thread(target=pump, args=(right, left), daemon=True)
    a.start(); b.start(); a.join(); b.join()


class BridgeServer:
    def __init__(self, host: str, port: int):
        self.host, self.port, self.slots, self.lock = host, port, {}, threading.Lock()
    def serve_forever(self):
        with socket.create_server((self.host, self.port)) as listener:
            print(f"bridge server listening on {self.host}:{self.port}", flush=True)
            while True:
                conn, _ = listener.accept()
                threading.Thread(target=self._handle, args=(conn,), daemon=True).start()
    def _handle(self, conn):
        reader = conn.makefile("rb")
        try:
            first = reader.readline().decode("ascii", "replace").strip().split()
            if len(first) != 2:
                conn.close(); return
            role, token = first
            if role == "REGISTER":
                slot = {"conn": conn, "ready": threading.Event(), "start": threading.Event(), "peer": None}
                with self.lock: self.slots[token] = slot
                while not slot["start"].is_set():
                    line = reader.readline()
                    if not line: return
                    if line.strip() == b"READY":
                        slot["ready"].set(); slot["start"].wait(60); break
                if slot["peer"]: _relay(conn, slot["peer"])
            elif role == "CONNECT":
                with self.lock: slot = self.slots.get(token)
                if not slot:
                    conn.sendall(b"ERROR no-device\n"); return
                slot["peer"] = conn; slot["conn"].sendall(b"OPEN\n")
                if not slot["ready"].wait(60):
                    conn.sendall(b"ERROR not-ready\n"); return
                slot["conn"].sendall(b"START\n"); conn.sendall(b"START\n"); slot["start"].set()
        except OSError:
            pass
        finally:
            try: reader.close()
            except OSError: pass


def run_expose(server: str, token: str, local: str):
    sh, sp = _addr(server); lh, lp = _addr(local)
    with socket.create_connection((sh, sp), timeout=10) as upstream:
        upstream.sendall(f"REGISTER {token}\n".encode())
        reader = upstream.makefile("rb")
        while True:
            if reader.readline().strip() != b"OPEN": return
            device = socket.create_connection((lh, lp), timeout=10)
            upstream.sendall(b"READY\n")
            if reader.readline().strip() != b"START": device.close(); return
            _relay(upstream, device); return


def run_connect(server: str, token: str, listen: str):
    sh, sp = _addr(server); lh, lp = _addr(listen)
    with socket.create_server((lh, lp)) as listener:
        print(f"bridge proxy listening on {lh}:{lp}", flush=True)
        while True:
            local, _ = listener.accept()
            threading.Thread(target=_connect_one, args=(local, sh, sp, token), daemon=True).start()


def _connect_one(local, host, port, token):
    try:
        upstream = socket.create_connection((host, port), timeout=10)
        upstream.sendall(f"CONNECT {token}\n".encode())
        if upstream.makefile("rb").readline().strip() == b"START": _relay(local, upstream)
        else: local.close(); upstream.close()
    except OSError:
        local.close()
