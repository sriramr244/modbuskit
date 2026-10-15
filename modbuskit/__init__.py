"""ModbusKit - modular Modbus master: RTU, RTU over TCP, Modbus TCP."""
from .client import ModbusClient, Response, Status
from .tags import Tag, plan_blocks, tags_from_range
from .transport import SerialTransport, TcpTransport, list_serial_ports

__all__ = ["ModbusClient", "Response", "Status", "Tag", "plan_blocks", "tags_from_range",
           "SerialTransport", "TcpTransport", "list_serial_ports", "make_client"]


def make_client(mode: str, *, port=None, baudrate=9600, parity="N", bytesize=8, stopbits=1,
                strip_echo=False, host=None, tcp_port=502, timeout=1.0, retries=0) -> ModbusClient:
    """mode: 'rtu' (serial), 'rtu_tcp' (RTU over TCP), 'tcp' (Modbus TCP/IP)."""
    if mode == "rtu":
        return ModbusClient(SerialTransport(port, baudrate, parity, bytesize, stopbits, strip_echo),
                            "rtu", timeout, retries)
    if mode == "rtu_tcp":
        return ModbusClient(TcpTransport(host, tcp_port), "rtu", timeout, retries)
    if mode == "tcp":
        return ModbusClient(TcpTransport(host, tcp_port), "tcp", timeout, retries)
    raise ValueError(f"unknown mode {mode}")
