"""Loopback test for BleDataChannel: chunking -> proxy -> response framing, no radio."""
import asyncio
import json
import sys
import time

sys.path.insert(0, "/home/xielang/work/code/mindora/mindora_user")

import aiohttp
import iot_server


class MockCharacteristic:
    def __init__(self):
        self.value = b""


class MockServer:
    """Stand-in for BlessServer: captures every notified frame."""

    def __init__(self):
        self.char = MockCharacteristic()
        self.frames = []

    def get_characteristic(self, uuid):
        return self.char

    def update_value(self, service_uuid, char_uuid):
        assert service_uuid == iot_server.BLE_SERVICE_UUID
        assert char_uuid == iot_server.BLE_RESPONSE_CHAR_UUID
        self.frames.append(bytes(self.char.value))
        return True


def make_request_frames(payload: bytes, chunk_payload: int = 100):
    total = len(payload)
    chunks = [payload[i:i + chunk_payload] for i in range(0, total, chunk_payload)]
    return [
        total.to_bytes(2, "little") + bytes([idx, 1 if idx == len(chunks) - 1 else 0]) + c
        for idx, c in enumerate(chunks)
    ]


async def main():
    server = MockServer()
    async with aiohttp.ClientSession() as session:
        channel = iot_server.BleDataChannel(server, session)

        body = {
            "request_type": "query_profile",
            "timestamp": int(time.time()),
            "version": "1.0",
            "data": {"uid": "mindora_test_uid1", "device_type": "pc"},
        }
        envelope = json.dumps({"path": "/user_profile", "body": body}).encode()
        frames = make_request_frames(envelope)
        print(f"request: {len(envelope)}B in {len(frames)} chunks")

        class FakeChar:
            service = None

        for f in frames:
            await channel.on_write(FakeChar(), bytearray(f))

        assert server.frames, "no response frames captured — _send_response never fired!"
        # reassemble
        total = int.from_bytes(server.frames[0][0:2], "little")
        payload = b"".join(bytes(f[4:]) for f in sorted(server.frames, key=lambda f: f[2]))[:total]
        resp = json.loads(payload.decode())
        print(f"response: {total}B in {len(server.frames)} frames, code={resp.get('code')}")
        print(json.dumps(resp, ensure_ascii=False, indent=2)[:800])
        assert resp.get("code") == 0, f"unexpected code: {resp}"
        print("\n✅ loopback OK: BLE 分片 -> 代理 user_server -> 响应分片 全链路通过")

        # error path: invalid JSON
        server.frames.clear()
        await channel.on_write(FakeChar(), bytearray(make_request_frames(b"{oops")[0]))
        total = int.from_bytes(server.frames[0][0:2], "little")
        err = json.loads(b"".join(bytes(f[4:]) for f in server.frames)[:total].decode())
        assert err["code"] == 400, err
        print("✅ error path OK: invalid JSON -> code=400")


asyncio.run(main())
