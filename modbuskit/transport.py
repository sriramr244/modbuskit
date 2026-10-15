"""
Byte transports. A transport only moves bytes; it knows nothing about Modbus.

    SerialTransport  RS-485/RS-232 via pyserial      -> used with RTU framing
    TcpTransport     raw TCP socket                  -> used with TCP (MBAP) framing,
                                                        or RTU framing for "RTU over TCP"
                                                        (Lantronix / Moxa raw-socket gateways)
"""
from __future__ import annotations

import socket


class Transport:
    gap_time = 0.05        # silence (s) that marks end of an RTU frame
    pre_tx_delay = 0.0     # silence (s) required before sending
    strip_echo = False     # some RS-485 converters echo what they transmit

    def open(self) -> None: raise NotImplementedError
    def close(self) -> None: raise NotImplementedError
    @property
    def is_open(self) -> bool: raise NotImplementedError
    def write(self, data: bytes) -> None: raise NotImplementedError
    def read(self, max_bytes: int) -> bytes:
        """Return whatever arrives within ~10 ms (may be b'')."""
        raise NotImplementedError
    def flush_input(self) -> None: raise NotImplementedError
    def describe(self) -> str: return self.__class__.__name__


class SerialTransport(Transport):
    def __init__(self, port: str, baudrate: int = 9600, parity: str = "N",
                 bytesize: int = 8, stopbits: int = 1, strip_echo: bool = False,
                 min_gap: float = 0.02):
        self.port, self.baudrate, self.parity = port, int(baudrate), parity.upper()
        self.bytesize, self.stopbits, self.strip_echo = int(bytesize), int(stopbits), strip_echo
        bits = 1 + self.bytesize + (0 if self.parity == "N" else 1) + self.stopbits
        self.char_time = bits / self.baudrate
        # 10 char-times is generous vs the 3.5 in the spec: USB adapters deliver in chunks
        self.gap_time = max(10 * self.char_time, min_gap)
        self.pre_tx_delay = max(3.5 * self.char_time, 0.002)
        self._ser = None

    def open(self) -> None:
        import serial  # lazy: TCP-only users do not need pyserial
        self._ser = serial.Serial(port=self.port, baudrate=self.baudrate, bytesize=self.bytesize,
                                  parity=self.parity, stopbits=self.stopbits,
                                  timeout=0.01, write_timeout=2)

    def close(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            finally:
                self._ser = None

    @property
    def is_open(self) -> bool:
        return self._ser is not None and self._ser.is_open

    def write(self, data: bytes) -> None:
        self._ser.write(data)
        self._ser.flush()

    def read(self, max_bytes: int) -> bytes:
        n = self._ser.in_waiting
        return self._ser.read(min(n, max_bytes) if n else 1)

    def flush_input(self) -> None:
        self._ser.reset_input_buffer()

    def describe(self) -> str:
        return f"{self.port} {self.baudrate} {self.bytesize}{self.parity}{self.stopbits}"


class TcpTransport(Transport):
    def __init__(self, host: str, port: int = 502, connect_timeout: float = 3.0):
        self.host, self.port, self.connect_timeout = host, int(port), connect_timeout
        self._sock: socket.socket | None = None

    def open(self) -> None:
        s = socket.create_connection((self.host, self.port), timeout=self.connect_timeout)
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        s.settimeout(0.01)
        self._sock = s

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    @property
    def is_open(self) -> bool:
        return self._sock is not None

    def write(self, data: bytes) -> None:
        self._sock.sendall(data)

    def read(self, max_bytes: int) -> bytes:
        try:
            data = self._sock.recv(max_bytes)
        except (socket.timeout, BlockingIOError):
            return b""
        if not data:
            self.close()
            raise ConnectionError("connection closed by remote end")
        return data

    def flush_input(self) -> None:
        while self.read(4096):
            pass

    def describe(self) -> str:
        return f"{self.host}:{self.port}"


def list_serial_ports() -> list[tuple[str, str]]:
    try:
        from serial.tools import list_ports
    except ImportError:
        return []
    return [(p.device, p.description or "") for p in sorted(list_ports.comports(), key=lambda p: p.device)]
