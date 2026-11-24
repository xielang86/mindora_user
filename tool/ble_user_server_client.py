"""
BLE 客户端：通过 iot_server 的 BLE 数据通道访问 user_server。

链路：bleak(本程序) --BLE--> iot_server(BleDataChannel) --HTTP--> user_server:9001

协议（跟 iot_server.BleRequestBuffer / BleDataChannel._send_response 对齐）：
  请求：往 BLE_REQUEST_CHAR_UUID 分片写入
        [2B total_length LE][1B chunk_index][1B final_flag][payload]
        payload 拼起来是 JSON: {"path": "/user_profile", "body": {...}}
  响应：订阅 BLE_RESPONSE_CHAR_UUID 的 notify，同样的分片格式，拼起来是 user_server 的 JSON 响应

⚠️ 单适配器机器不能连自己：本程序要跟 iot_server 跑在两台设备上
  （比如 iot_server 跑在设备/树莓派上，本程序跑在开发机或另一块 BLE 网卡上）。

用法：
  python tool/ble_user_server_client.py                      # 扫描 "Mindora 2026"，query_profile
  python tool/ble_user_server_client.py --address AA:BB:..   # 直连已知 MAC，跳过扫描
  python tool/ble_user_server_client.py --path /analysis --body '{...}'
  python tool/ble_user_server_client.py --path /analysis --body '{"request_type": ...}'
"""
import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

try:
    from bleak import BleakClient, BleakScanner
except ModuleNotFoundError:
    print("缺少依赖 bleak，请先 pip install bleak")
    sys.exit(1)

import iot_server  # 复用 UUID / 设备名常量，保证两端永远对齐

CHUNK_PAYLOAD = 180          # 每片载荷，保守值，MTU 185+ 都安全
RESPONSE_TIMEOUT = 30.0      # 等响应分片拼齐的超时
WRITE_DELAY = 0.02           # 分片写入间隔，避免对端缓冲溢出


def build_envelope(path: str, body: dict) -> bytes:
    return json.dumps({"path": path, "body": body}, ensure_ascii=False).encode("utf-8")


def chunk_payload(payload: bytes) -> list[bytes]:
    """按 iot_server.BleRequestBuffer 的格式分片。"""
    total = len(payload)
    chunks = [payload[i:i + CHUNK_PAYLOAD] for i in range(0, total, CHUNK_PAYLOAD)] or [b""]
    frames = []
    for idx, chunk in enumerate(chunks):
        header = total.to_bytes(2, "little") + bytes([idx, 1 if idx == len(chunks) - 1 else 0])
        frames.append(header + chunk)
    return frames


class ResponseReassembler:
    """拼响应分片，拼齐后塞进 Future。"""

    def __init__(self):
        self._chunks: dict[int, bytes] = {}
        self._total = None
        self._received = 0
        self.done: asyncio.Future = asyncio.get_running_loop().create_future()

    def feed(self, data: bytearray):
        if len(data) < 4:
            print(f"[warn] 响应分片头过短: {len(data)} bytes")
            return
        total = int.from_bytes(data[0:2], "little")
        idx, final = data[2], data[3]
        if self._total != total:
            self._chunks, self._received, self._total = {}, 0, total
        if idx not in self._chunks:
            self._chunks[idx] = bytes(data[4:])
            self._received += len(data) - 4
        if final and self._received >= total and not self.done.done():
            payload = b"".join(self._chunks[i] for i in sorted(self._chunks))[:total]
            self.done.set_result(payload)


async def find_device(address: str | None, name: str, timeout: float):
    if address:
        print(f"[1/4] 直连 {address} ...")
        return address
    print(f"[1/4] 扫描 BLE 设备（name={name!r} 或 service={iot_server.BLE_SERVICE_UUID}）...")
    device = await BleakScanner.find_device_by_filter(
        lambda d, ad: (d.name and name in d.name)
        or iot_server.BLE_SERVICE_UUID.lower() in [u.lower() for u in ad.service_uuids],
        timeout=timeout,
    )
    if device is None:
        print("未找到设备。确认 iot_server 已启动且 --no-ble 没开，且本机与它不是一个适配器。")
        sys.exit(2)
    print(f"      找到: {device.name}  {device.address}")
    return device.address


async def run(args):
    target = await find_device(args.address, args.name, args.scan_timeout)

    async with BleakClient(target, timeout=args.connect_timeout) as client:
        print(f"[2/4] 已连接，MTU={client.mtu_size}")

        # 读 device_id，验证和 mDNS TXT 一致
        device_id = await client.read_gatt_char(iot_server.BLE_DEVICE_ID_CHAR_UUID)
        print(f"      device_id = {device_id.decode('utf-8', 'replace')}")

        reasm = ResponseReassembler()
        await client.start_notify(
            iot_server.BLE_RESPONSE_CHAR_UUID,
            lambda _char, data: reasm.feed(data),
        )

        if args.body:
            body = json.loads(args.body)
        else:
            body = {
                "request_type": "query_profile",
                "timestamp": int(time.time()),
                "version": "1.0",
                "data": {"uid": args.uid, "device_type": "pc"},
            }
        envelope = build_envelope(args.path, body)
        frames = chunk_payload(envelope)
        print(f"[3/4] 发送 {args.path}，{len(envelope)}B 拆成 {len(frames)} 片...")
        for frame in frames:
            await client.write_gatt_char(iot_server.BLE_REQUEST_CHAR_UUID, frame, response=True)
            await asyncio.sleep(WRITE_DELAY)

        print(f"[4/4] 等待响应（超时 {RESPONSE_TIMEOUT}s）...")
        payload = await asyncio.wait_for(reasm.done, timeout=RESPONSE_TIMEOUT)
        await client.stop_notify(iot_server.BLE_RESPONSE_CHAR_UUID)

    resp = json.loads(payload.decode("utf-8"))
    print(f"\n{'=' * 20} BLE 响应 ({len(payload)}B) {'=' * 20}")
    print(json.dumps(resp, ensure_ascii=False, indent=2))
    code = resp.get("code", -1)
    if code == 0:
        print("\n结果: ✅ 成功")
    else:
        print(f"\n结果: ❌ code={code} msg={resp.get('msg')}")
    return code


def main():
    p = argparse.ArgumentParser(description="Test BLE data channel to user_server via iot_server")
    p.add_argument("--address", default=None, help="设备 MAC/地址，给了就跳过扫描")
    p.add_argument("--name", default=iot_server.DEVICE_MODEL, help="扫描匹配的设备名")
    p.add_argument("--path", default="/user_profile", help="代理到 user_server 的路径")
    p.add_argument("--body", default=None, help="请求体 JSON 字符串；缺省用 query_profile")
    p.add_argument("--uid", default="test_user", help="缺省 query_profile 用的调试 uid")
    p.add_argument("--scan-timeout", type=float, default=10.0)
    p.add_argument("--connect-timeout", type=float, default=20.0)
    args = p.parse_args()
    code = asyncio.run(run(args))
    sys.exit(0 if code == 0 else 1)


if __name__ == "__main__":
    main()
