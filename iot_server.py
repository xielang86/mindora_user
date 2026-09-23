"""
Mindora 设备端 IoT 服务（mDNS + WebSocket + BLE）

跟 iOS App 的发现/绑定逻辑对齐：
  - mDNS 服务类型 _mindora._tcp.，TXT 暴露 device_id 字段（每台唯一）
  - BLE GATT 暴露 read characteristic，value = device_id UTF-8 字节（同 TXT 一致，用于 iOS 端 Bonjour↔BLE 合并）
  - 设备型号 DEVICE_MODEL = "Mindora 2026"（用户可见名，所有同款设备共用，mDNS service name + BLE 广播 local name）
  - DNS host slug 用 model + device_id 末尾，保证 LAN 内唯一

详细规格 / 使用 / 联调步骤见 Tools/doc/设备端接口契约.md。

平台支持（一份代码三平台跑，无需任何环境变量）：
  - macOS：bless 走 CoreBluetooth via PyObjC
  - Linux：bless 走 BlueZ via DBus（需系统装 bluez，pip 自动拉 dbus-next）
  - Windows：bless 走 WinRT

跑这个脚本前请：
  pip install -r requirements.txt
  （含 bless / zeroconf / websockets）
"""
# 注解延迟求值：可选依赖缺失时会把 ClientSession / BlessServer 置为 None，
# 若注解在定义时求值，`ClientSession | None` 会变成 `None | None` 直接 TypeError 崩掉，
# 让「缺依赖也能跑其余子系统」的降级设计失效。
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import socket
import sys
import time
import uuid

from dotenv import load_dotenv

import logger

load_dotenv()
run_dir = os.getenv("RUN_DIR")
if run_dir is None or len(run_dir) == 0:
    run_dir = os.path.dirname(os.path.abspath(__file__))

logger.init_log(f"{run_dir}/iot_server_logs")

try:
    from aiohttp import ClientSession
except ModuleNotFoundError:
    ClientSession = None
try:
    from websockets import serve as websocket_serve
except ModuleNotFoundError:
    websocket_serve = None
try:
    from zeroconf import Zeroconf, ServiceInfo
except ModuleNotFoundError:
    Zeroconf = None
    ServiceInfo = None
try:
    from bless import (
        BlessServer,
        GATTCharacteristicProperties,
        GATTAttributePermissions,
    )
except ModuleNotFoundError:
    BlessServer = None


# ==========================================
# Device Identity
# ==========================================
def read_device_id():
    """启动时读取唯一 device_id，按优先级查多个位置：
      1. /etc/mindora/device_id      生产环境（产线烧录，需 root）
      2. ~/.mindora/device_id        开发期 fallback（用户态文件，免 sudo）
      3. mnd-dev-<mac>               兜底，仅警示用，不应进生产

    兜底必须带本机 MAC（uuid.getnode）——绝不能用固定常量。否则同一 LAN 上多台都没
    烧 device_id 文件的开发机会拿到同一个 id，mDNS service name / host slug 全撞，
    照样抛 NonUniqueNameException。MAC 稳定（重启不变）且 LAN 内唯一，正好做兜底。
    """
    for path in ["/etc/mindora/device_id", os.path.expanduser("~/.mindora/device_id")]:
        try:
            with open(path) as f:
                value = f.read().strip()
                if value:
                    return value
        except FileNotFoundError:
            continue
    return f"mnd-dev-{uuid.getnode():012x}"


DEVICE_ID = read_device_id()


# ⚠️ 名称 / id 概念分层（不要再混）：
#   DEVICE_MODEL  = "Mindora 2026"  → 设备"型号"，硬编码常量，所有同款机器共用此名（类比 "iPhone 15 Pro"）。
#                                     用户在 App 里看到的就是这个；切换 SKU（Pro / Air）改这一行即可。
#   DEVICE_ID     = read_device_id() → 每台机器唯一的底层 id（产线烧 /etc/mindora/device_id）。
#                                     iOS 端用它合并 Bonjour + BLE 同一台设备，正常用户场景下不直接看见。
#
# 历史教训：之前一版把 "Mindora 2026" 当作 per-device hash 派生名（用 device_id MD5 算 4 位后缀），
# 把"型号"和"实例标识"混在一根字符串里。语义错乱——明明是型号，看起来又像每台不同。
# 现在彻底分开。
DEVICE_MODEL = "Mindora 2026"

# DNS / mDNS hostname 协议不允许空格，需要在 LAN 内唯一（同型号多台不能撞 host）。
# 用 model slug + device_id 末尾 8 位拼出唯一 host。
DEVICE_HOST_SLUG = "Mindora-2026-" + DEVICE_ID.split("-")[-1][-8:]


# ==========================================
# 配置常量
# ==========================================
# BLE：跟 iOS 端常量对齐的占位 UUID。
# ⚠️ 上线前必须固件 + iOS 同步换成 `uuidgen` 真随机 UUID，避免跟全网用同样示例的 BLE 配件撞车。
BLE_SERVICE_UUID = "9fcc2fbe-a190-4ee9-96db-68e82f5f15cc"
BLE_DEVICE_ID_CHAR_UUID = "6a50d363-d2de-4bd9-b66f-7b02947e7a9c"
BLE_REQUEST_CHAR_UUID = "a733eb73-3985-4e22-a536-d5de7342e5b4"
BLE_RESPONSE_CHAR_UUID = "65a042cb-42b5-4a58-8bff-68c614336f14"

WEBSOCKET_PORT = 8765
USER_SERVER_PORT = 9001
USER_SERVER_BASE_URL = os.getenv("USER_SERVER_BASE_URL", f"http://127.0.0.1:{USER_SERVER_PORT}")

# mDNS：iOS BonjourDiscovery 监听 _mindora._tcp.
# ⚠️ service 实例名必须保持干净的 DEVICE_MODEL（"Mindora 2026"）——iOS 端 UnifiedDeviceDiscovery
#    直接拿 service.name 当列表显示名（不读 TXT name），任何塞进实例名的后缀都会原样显示成
#    "Mindora 2026 (xxx)" 那种长串。所以唯一性绝不靠实例名扛：
#      - LAN 内唯一靠 hostname（MDNS_LOCAL_NAME，带 DEVICE_HOST_SLUG，iOS 不显示）+ TXT device_id；
#      - 同款多台真撞实例名时，register_service(allow_name_change=True) 让 zeroconf 自动加 " (2)"，
#        这正是 iOS 侧注释里预期的行为（设备端处理，App 不用管）。
# DNS hostname 必须用 dash 形式 DEVICE_HOST_SLUG（DNS 协议不允许空格），且需在 LAN 唯一。
MDNS_SERVICE_TYPE = "_mindora._tcp.local."
MDNS_SERVICE_NAME = f"{DEVICE_MODEL}._mindora._tcp.local."
MDNS_LOCAL_NAME = f"{DEVICE_HOST_SLUG}.local"
MDNS_HOST_NAME = f"{MDNS_LOCAL_NAME}."

zeroconf_instance = None


def _usable(ip: str) -> bool:
    """回环与 link-local 一律不可用 —— 它们在语义上就不可能被另一台设备访问到。
    **不做任何网段黑名单**：100.64/10（CGNAT）、198.18/15 等只要真的挂在网卡上就照用，
    否则会误伤把它们当内网用的真实环境。"""
    return bool(ip) and not ip.startswith("127.") and not ip.startswith("169.254.")


def get_lan_ips() -> list[str]:
    """枚举本机所有可用的 IPv4（去重、保持顺序）。

    历史教训：老实现用 `socket.connect(("8.8.8.8", 80))` 探本机地址，失败时退回
    `gethostbyname(gethostname())`。这有三个坑，实测全都踩到了：
      1. 开机竞态 —— Wi-Fi 还没拿到地址时 connect 抛 OSError，兜底返回 /etc/hosts 里的
         127.0.0.2，回环地址被写进 mDNS 注册，服务在局域网里整个不可见（现象：手机一次
         didFind 都没有），且网络起来后不会重注册；
      2. 无外网环境（工厂内网 / 断网调试）没有默认路由，connect 永远失败；
      3. 多网卡 / VPN 时选出的是"去 8.8.8.8 该走哪张网卡"，未必是手机所在网段
         （实测 macOS 开 VPN 时拿到 198.18.0.1）。

    现在改为枚举所有网卡地址并**全部注册**——mDNS 的 A 记录本就允许多条，客户端会逐个
    尝试，不需要我们猜哪个对，也不需要外网。
    """
    ips: list[str] = []

    # 首选 ifaddr：zeroconf 自己就依赖它，不引入新依赖，跨平台。
    try:
        import ifaddr
        for adapter in ifaddr.get_adapters():
            for ip in adapter.ips:
                if ip.is_IPv4 and _usable(ip.ip) and ip.ip not in ips:
                    ips.append(ip.ip)
        if ips:
            return ips
    except Exception as e:  # noqa: BLE001 - 枚举失败就走下面的降级
        logging.warning(f"[net] ifaddr 枚举失败，降级: {e}")

    # 降级 1：解析系统命令（Linux: ip / macOS: ifconfig）
    try:
        import re
        import subprocess
        out = subprocess.run(["ip", "-4", "-o", "addr", "show"],
                             capture_output=True, text=True, timeout=5).stdout
        if not out:
            out = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=5).stdout
        for m in re.finditer(r"inet\s+(\d+\.\d+\.\d+\.\d+)", out):
            ip = m.group(1)
            if _usable(ip) and ip not in ips:
                ips.append(ip)
        if ips:
            return ips
    except Exception as e:  # noqa: BLE001
        logging.warning(f"[net] 系统命令枚举失败，降级: {e}")

    # 降级 2：出网地址探测。只作兜底 —— 它依赖默认路由，没外网时必然失败。
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            ip = sock.getsockname()[0]
            if _usable(ip):
                ips.append(ip)
    except OSError:
        pass
    return ips


def wait_for_lan_ips(timeout: float = 600.0, interval: float = 2.0) -> list[str]:
    """等到至少有一个可用地址再返回；超时返回空列表。

    开机时 iot_server 往往比 Wi-Fi 先就绪（systemd 用户级 unit 只 After=default.target，
    跟网络无关），必须在这里等，而不是拿一个错地址去注册。等 10 分钟是宽松取值：
    路由器重启、Wi-Fi 漫游、用户在配网页面磨蹭都可能让网络迟迟不就绪，等着不花任何成本；
    真的等超了也不是绝路 —— 调用方会转入后台重试（见 keep_trying_mdns_registration）。
    """
    deadline = time.time() + timeout
    attempts = 0
    while True:
        ips = get_lan_ips()
        if ips:
            if attempts:
                logging.info(f"[net] 等待 {attempts * interval:.0f}s 后拿到地址: {ips}")
            return ips
        if time.time() >= deadline:
            return []
        attempts += 1
        if attempts <= 3 or attempts % 15 == 0:   # 前几次照打，之后每 30s 一条，避免刷屏
            logging.info(f"[net] 暂无可用局域网地址，已等待 {attempts * interval:.0f}s，继续重试…")
        time.sleep(interval)


def get_lan_ip():
    """兼容旧调用点：返回首个可用地址，取不到时返回 127.0.0.1（仅用于拼展示用的 URL）。"""
    ips = get_lan_ips()
    return ips[0] if ips else "127.0.0.1"


def build_user_server_info():
    """user_server 路由元信息（WebSocket / BLE write JSON 时通过 type:api_info 取）。
    iOS 当前不发任何消息，这里给其他客户端用。"""
    ip_address = get_lan_ip()
    base_url = f"http://{ip_address}:{USER_SERVER_PORT}"
    local_base_url = f"http://{MDNS_LOCAL_NAME}:{USER_SERVER_PORT}"
    return {
        "type": "api_info",
        "device": DEVICE_MODEL,
        "device_id": DEVICE_ID,
        "user_server": {
            "base_url": base_url,
            "local_name": MDNS_LOCAL_NAME,
            "local_base_url": local_base_url,
            "protocol": "http",
            "port": USER_SERVER_PORT,
            "routes": {
                "login": f"{base_url}/login",
                "user_profile": f"{base_url}/user_profile",
                "analysis": f"{base_url}/analysis",
                "sleep_advice": f"{base_url}/sleep_advice",
            },
            "local_routes": {
                "login": f"{local_base_url}/login",
                "user_profile": f"{local_base_url}/user_profile",
                "analysis": f"{local_base_url}/analysis",
                "sleep_advice": f"{local_base_url}/sleep_advice",
            },
        },
    }


def process_message(message):
    """处理 WebSocket 收到的 JSON 消息。iOS 不发任何消息，这里给其他客户端用。"""
    logging.info(f"处理消息: {message}")
    if message.get('type') == 'ping':
        return {
            'type': 'pong',
            'message': '收到Ping请求',
            'timestamp': message.get('timestamp'),
            'device_id': DEVICE_ID,
            'api_info': build_user_server_info().get("user_server"),
        }
    elif message.get('type') == 'api_info':
        return build_user_server_info()
    elif message.get('type') == 'data_request':
        return {
            'type': 'data_response',
            'data': {'temperature': 25.5, 'humidity': 45.2, 'status': 'normal'}
        }
    else:
        return {'type': 'error', 'message': '未知消息类型'}


# ==========================================
# BLE data channel — proxy user_server API over BLE
# ==========================================

class BleRequestBuffer:
    """Buffer and reassemble chunked BLE writes into a single payload."""

    def __init__(self, timeout: float = 10.0):
        self._buffers: dict[int, dict] = {}
        self._timeout = timeout

    def feed(self, connection_id: int, chunk: bytes) -> bytes | None:
        """Feed a chunk and return the full payload once complete.

        Chunk format (little endian):
          [2 bytes total_length] [1 byte chunk_index] [1 byte final_flag] [payload]
        """
        if len(chunk) < 4:
            logging.warning("[BLE] malformed chunk header")
            return None

        total_length = int.from_bytes(chunk[0:2], "little")
        chunk_index = chunk[2]
        final_flag = chunk[3]
        payload = chunk[4:]

        buf = self._buffers.setdefault(connection_id, {
            "total_length": total_length,
            "chunks": {},
            "received_length": 0,
            "deadline": asyncio.get_running_loop().time() + self._timeout,
        })

        if buf["total_length"] != total_length:
            logging.warning("[BLE] total length mismatch, resetting buffer")
            self._buffers[connection_id] = {
                "total_length": total_length,
                "chunks": {chunk_index: payload},
                "received_length": len(payload),
                "deadline": asyncio.get_running_loop().time() + self._timeout,
            }
            return None

        if chunk_index not in buf["chunks"]:
            buf["chunks"][chunk_index] = payload
            buf["received_length"] += len(payload)

        if final_flag and buf["received_length"] >= total_length:
            sorted_chunks = [buf["chunks"][i] for i in sorted(buf["chunks"].keys())]
            full_payload = b"".join(sorted_chunks)[:total_length]
            self._buffers.pop(connection_id, None)
            return full_payload

        return None

    def cleanup_expired(self):
        now = asyncio.get_running_loop().time()
        expired = [cid for cid, buf in self._buffers.items() if buf["deadline"] < now]
        for cid in expired:
            self._buffers.pop(cid, None)


class BleDataChannel:
    """Proxy BLE requests to the local user_server HTTP API."""

    def __init__(self, server: BlessServer | None, session: ClientSession | None):
        self.server = server
        self.session = session
        self.request_buffer = BleRequestBuffer()
        self.response_seq = 0

    async def on_write(self, characteristic, value: bytearray, **_kwargs):
        """Handle a BLE write on the request characteristic."""
        if self.server is None:
            logging.warning("[BLE] data channel not initialized")
            return

        # Use connection handle as buffer key if available, else fallback to 0.
        connection_id = getattr(characteristic, "service", 0) or 0
        payload = self.request_buffer.feed(connection_id, bytes(value))
        if payload is None:
            return

        self.request_buffer.cleanup_expired()

        try:
            envelope = json.loads(payload.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            logging.error(f"[BLE] invalid JSON request: {e}")
            await self._send_response(connection_id, {"code": 400, "msg": "invalid JSON", "data": None})
            return

        path = envelope.get("path", "/user_profile")
        body = envelope.get("body", {})
        if not isinstance(path, str) or not path.startswith("/"):
            await self._send_response(connection_id, {"code": 400, "msg": "invalid path", "data": None})
            return

        logging.info(f"[BLE] proxy {path}")
        try:
            if self.session is None:
                await self._send_response(connection_id, {"code": 503, "msg": "user_server proxy unavailable", "data": None})
                return
            async with self.session.post(f"{USER_SERVER_BASE_URL}{path}", json=body, timeout=30) as resp:
                resp_data = await resp.json()
                await self._send_response(connection_id, resp_data)
        except Exception as e:
            logging.error(f"[BLE] proxy error: {e}")
            await self._send_response(connection_id, {"code": 500, "msg": f"proxy error: {e}", "data": None})

    async def _send_response(self, connection_id: int, data: dict):
        """Fragment and send a JSON response via BLE notify."""
        if self.server is None:
            return

        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        total_length = len(payload)
        if total_length > 0xFFFF:
            # 2 字节长度头最多表达 65535；超出时回错误而不是让 to_bytes 抛 OverflowError
            logging.error(f"[BLE] response too large: {total_length} bytes, sending error instead")
            payload = json.dumps(
                {"code": 413, "msg": f"response too large: {total_length} bytes", "data": None},
                ensure_ascii=False,
            ).encode("utf-8")
            total_length = len(payload)

        chunk_size = 512 - 4  # reserve 4 bytes header
        chunks = [payload[i:i + chunk_size] for i in range(0, len(payload), chunk_size)]

        response_char = self.server.get_characteristic(BLE_RESPONSE_CHAR_UUID)
        if response_char is None:
            logging.error("[BLE] response characteristic not found")
            return

        for idx, chunk in enumerate(chunks):
            header = total_length.to_bytes(2, "little")
            header += bytes([idx])
            header += bytes([1 if idx == len(chunks) - 1 else 0])
            frame = header + chunk
            try:
                # bless 的 update_value 是同步方法且不带 value 参数：
                # 必须先写 characteristic.value，再调 update_value 触发 notify
                response_char.value = frame
                ok = self.server.update_value(BLE_SERVICE_UUID, BLE_RESPONSE_CHAR_UUID)
                if not ok:
                    logging.error("[BLE] notify failed: update_value returned False")
                    break
                # 给对端留点消化时间，避免高速 notify 丢包
                await asyncio.sleep(0.01)
            except Exception as e:
                logging.error(f"[BLE] notify failed: {e}")
                break


async def run_ble_server(enable_data_channel: bool = True):
    """跨平台 BLE peripheral，bless 自动按 sys.platform 选后端：
       macOS → CoreBluetooth；Linux → BlueZ via DBus；Windows → WinRT。
    """
    if BlessServer is None:
        logging.warning("[BLE] 缺依赖 bless，跳过 BLE 服务。解决: pip install bless")
        return

    device_id_bytes = DEVICE_ID.encode("utf-8")

    def read_request(characteristic, **_kwargs):
        # 收到 read 请求时回当前 value（默认就是 device_id 字节流）
        return characteristic.value

    server = BlessServer(name=DEVICE_MODEL)
    server.read_request_func = read_request

    await server.add_new_service(BLE_SERVICE_UUID)
    await server.add_new_characteristic(
        BLE_SERVICE_UUID,
        BLE_DEVICE_ID_CHAR_UUID,
        GATTCharacteristicProperties.read,
        device_id_bytes,
        GATTAttributePermissions.readable,
    )

    http_session = None
    ble_channel = None
    if enable_data_channel:
        await server.add_new_characteristic(
            BLE_SERVICE_UUID,
            BLE_REQUEST_CHAR_UUID,
            GATTCharacteristicProperties.write,
            # value 必须是 None：CoreBluetooth 规定「带缓存值的特征必须只读」，
            # 传 b"" 也算带值，start() 会抛 NSInternalInconsistencyException
            # （在 delegate 线程里抛，表现为 server.start() 永远不返回、日志停在
            # 「启动 bless peripheral...」，极难查）。
            None,
            GATTAttributePermissions.writeable,
        )
        await server.add_new_characteristic(
            BLE_SERVICE_UUID,
            BLE_RESPONSE_CHAR_UUID,
            GATTCharacteristicProperties.notify,
            None,   # 同上：notify 特征也不能带缓存值
            GATTAttributePermissions.readable,
        )
        if ClientSession is not None:
            http_session = ClientSession()
        ble_channel = BleDataChannel(server, http_session)

        # bless 的 write_request_func 是**同步**调用的（backends/server.py:
        # `self.write_request_func(characteristic, value)`），而且在 CoreBluetooth /
        # BlueZ 的回调线程上跑。直接把 async 的 on_write 挂上去，只会生成一个从不被
        # await 的协程 —— BLE 层写入成功、应用层什么都没发生，客户端只能等到超时，
        # 且没有任何报错。必须包一层同步函数投递回事件循环。
        loop = asyncio.get_running_loop()

        def _dispatch_write(characteristic, value, **kwargs):
            asyncio.run_coroutine_threadsafe(
                ble_channel.on_write(characteristic, value, **kwargs), loop
            )

        server.write_request_func = _dispatch_write

    logging.info("[BLE] 启动 bless peripheral...")
    # prioritize_local_name=False 是必须的：bless 的 CoreBluetooth 后端在
    # len(name) > 10 时会把 service UUID 从广播包里整个丢掉（只留 local name），
    # 而 iOS 用 scanForPeripherals(withServices:) 过滤扫描 —— 广播里没有 service UUID
    # 就永远扫不到。DEVICE_MODEL = "Mindora 2026" 正好 12 字符，必然踩中。
    # Linux/BlueZ 与 Windows 后端的 start(**kwargs) 会忽略该参数，跨平台安全。
    await server.start(prioritize_local_name=False)
    logging.info("[BLE] 广播已启动:")
    logging.info(f"  device model : {DEVICE_MODEL}")
    logging.info(f"  service UUID : {BLE_SERVICE_UUID}")
    logging.info(f"  device_id char : {BLE_DEVICE_ID_CHAR_UUID}")
    if enable_data_channel:
        logging.info(f"  request char   : {BLE_REQUEST_CHAR_UUID}")
        logging.info(f"  response char  : {BLE_RESPONSE_CHAR_UUID}")
    logging.info(f"  char value   : {DEVICE_ID}")

    # 广播自检：bless 的 start() 返回不代表控制器真的在广播。实测过一次典型翻车 ——
    # rc.local 里有 `systemctl restart bluetooth`，如果它在 iot_server 之后执行，
    # 已注册的 LE 广播会被 bluetoothd 重启一并抹掉，而 bless 毫不知情，日志照样写着
    # 「广播已启动」，手机却一个广播都扫不到（btmgmt info 里 advertising 位是空的）。
    # bluetoothd 什么时候被谁重启不受本进程控制，所以只能定期回读 + 自动重挂。
    watchdog = asyncio.create_task(watch_ble_advertising(server))

    try:
        # 阻塞直到外层 cancel
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        pass
    finally:
        watchdog.cancel()
        try:
            await server.stop()
            logging.info("[BLE] 已停止")
        except Exception as e:
            logging.error(f"[BLE] 停止时报错（忽略）: {e}")
        if http_session:
            await http_session.close()


async def ble_advertising_active(server) -> bool | None:
    """回读「现在是否真的在广播」。返回 None 表示查不了（此时不要乱重挂）。

    判据是 **adapter.Powered 且 LEAdvertisingManager1.ActiveInstances > 0**，两个都走 D-Bus 异步读。
    踩过的两个坑，别再改回去：
      1. 只看 `bless.is_advertising()`（即 ActiveInstances）不够 —— bluetoothd 被重启后
         广播对象仍注册着，ActiveInstances 依旧 > 0，但控制器实际已经不发了（实测 adapter
         连 Powered 都是 false），自检等于形同虚设；
      2. 别用 `btmgmt info` 之类的子进程 —— 实测在 systemd 服务里 btmgmt 会挂住不返回，
         subprocess 超时后静默降级，同样让自检失效，还会留下一堆僵尸进程。
    """
    adapter = getattr(server, "adapter", None)
    if adapter is not None:
        try:
            props = adapter.get_interface("org.freedesktop.DBus.Properties")
            powered = (await props.call_get("org.bluez.Adapter1", "Powered")).value
            instances = (await props.call_get(
                "org.bluez.LEAdvertisingManager1", "ActiveInstances")).value
            if not powered:
                logging.warning("[BLE] 适配器 Powered=false（蓝牙被关掉或 bluetoothd 刚重启）")
            return bool(powered) and instances > 0
        except Exception as e:  # noqa: BLE001
            logging.warning(f"[BLE] D-Bus 回读失败: {e}")

    # macOS / 其他后端没有 BlueZ 的 adapter 代理，退回 bless 自带查询。
    try:
        return bool(await server.is_advertising())
    except Exception as e:  # noqa: BLE001
        logging.warning(f"[BLE] 广播状态回读失败: {e}")
    return None


async def ble_power_on(server):
    """适配器被关掉时重新打开（bluetoothd 重启后常见）。仅 BlueZ 有效。"""
    adapter = getattr(server, "adapter", None)
    if adapter is None:
        return
    try:
        props = adapter.get_interface("org.freedesktop.DBus.Properties")
        from dbus_next import Variant
        await props.call_set("org.bluez.Adapter1", "Powered", Variant("b", True))
        logging.info("[BLE] 已重新打开适配器电源")
    except Exception as e:  # noqa: BLE001
        logging.warning(f"[BLE] 打开适配器电源失败: {e}")


async def watch_ble_advertising(server, interval: float = 30.0):
    """周期自检：不在广播就重挂一次。

    为什么必须有：广播可能在**任何时刻**被外部因素掐掉，且本进程收不到任何通知 ——
    实测这台设备的 rc.local 里有 `systemctl restart bluetooth`，它一旦晚于本服务执行，
    已注册的广播随 bluetoothd 一起蒸发，而 bless 毫不知情，日志还写着「广播已启动」。
    这类外部依赖的重启时机不受本进程控制，只能靠周期回读 + 自愈，不能指望启动顺序。
    """
    logging.info(f"[BLE] 广播自检已启动，每 {interval:.0f}s 回读一次")
    undetectable_logged = False
    while True:
        await asyncio.sleep(interval)
        try:
            active = await _check_and_heal(server, undetectable_logged)
        except Exception:  # noqa: BLE001
            # 任务里未捕获的异常会让 task 静默死掉（asyncio 不会打印，除非有人 await 它），
            # 自检就此永久停摆且毫无痕迹 —— 这类静默失败必须堵死。
            logging.exception("[BLE] 自检循环异常，本轮跳过")
            continue
        if active is None and not undetectable_logged:
            undetectable_logged = True


async def _check_and_heal(server, undetectable_logged: bool):
    """回读一次广播状态，未在广播则重挂。返回本次读到的状态（None = 查不了）。"""
    active = await ble_advertising_active(server)
    if active is None:
        if not undetectable_logged:
            logging.warning("[BLE] 无法回读广播状态，自检降级为不生效（仅提示一次）")
        return None
    if active:
        return True
    logging.error("[BLE] 自检发现未在广播 → 尝试恢复")
    await ble_power_on(server)
    try:
        await ble_remount(server)
    except Exception as e:  # noqa: BLE001
        # bless 的 BlueZ 后端在进程内**无法二次挂载**：start() 与 start_advertising()
        # 都会 bus.export() 到固定路径，第二次必然抛
        # "An interface with this name is already exported"。
        # 已实测无法绕开，所以只能退出让 systemd（Restart=always）重新拉起 ——
        # 全新进程拿到干净的 D-Bus 连接，广播必然恢复。退出比"卡在不广播"强得多：
        # 不广播 = App 永远发现不了设备，而重启只损失几秒。
        logging.error(f"[BLE] 进程内重挂失败（bless 限制）: {e} → 退出，由 systemd 拉起")
        os._exit(3)
    ok = await ble_advertising_active(server)
    logging.info(f"[BLE] 广播已重新挂载，回读状态={ok}")
    return ok


async def ble_remount(server):
    """重新把 GATT 应用与广播挂回 BlueZ。

    **不能直接 stop() + start()**：bless 的 BlueZ 后端 start() 里有
    `self.bus.export(self.app.path, self.app)`，而 stop() 并不 unexport，
    第二次 start 必然抛 `ValueError: An interface with this name is already exported`。
    所以这里只重做 start() 的后两步（register + start_advertising）——
    bluetoothd 重启后丢的正是这两样（它自己的注册表），我们在总线上的 export 还在。
    """
    app = getattr(server, "app", None)
    adapter = getattr(server, "adapter", None)
    if app is None or adapter is None:
        # 非 BlueZ 后端（如 macOS）：退回整体重启
        await server.stop()
        await server.start(prioritize_local_name=False)
        return
    try:
        await app.stop_advertising(adapter)
    except Exception:  # noqa: BLE001 - 本来就没在广播，忽略
        pass
    try:
        await app.register(adapter)
    except Exception as e:  # noqa: BLE001 - 已注册时会报错，可继续
        logging.info(f"[BLE] 重新注册 GATT 应用: {e}")
    await app.start_advertising(adapter)


# ==========================================
# WebSocket
# ==========================================
async def websocket_handler(websocket):
    """处理 WebSocket 连接。iOS 不连这里，给其他客户端 / 未来扩展用。"""
    logging.info("WebSocket客户端已连接")
    try:
        async for message in websocket:
            try:
                data = json.loads(message)
                logging.info(f"WebSocket收到数据: {data}")
                response = process_message(data)
                await websocket.send(json.dumps(response))
            except json.JSONDecodeError:
                await websocket.send(json.dumps({'type': 'error', 'message': '无效的JSON格式'}))
            except Exception as e:
                await websocket.send(json.dumps({'type': 'error', 'message': f'处理消息时出错: {str(e)}'}))
    except Exception as e:
        logging.error(f"WebSocket错误: {e}")
    finally:
        logging.info("WebSocket客户端已断开")


async def run_websocket_server():
    if websocket_serve is None:
        logging.warning("[WS] 缺依赖 websockets，跳过 WebSocket 服务。解决: pip install websockets")
        # 不能 return，否则 main() 会立即退出。挂起让 BLE 继续跑。
        await asyncio.Event().wait()
        return
    logging.info("启动WebSocket服务器...")
    async with websocket_serve(websocket_handler, "0.0.0.0", WEBSOCKET_PORT):
        logging.info(f"WebSocket服务器已启动，端口: {WEBSOCKET_PORT}")
        await asyncio.Event().wait()


# ==========================================
# mDNS
# ==========================================
def register_mdns_sync():
    """同步注册 mDNS 服务。"""
    global zeroconf_instance
    if Zeroconf is None or ServiceInfo is None:
        logging.warning("[mDNS] 缺依赖 zeroconf，跳过 mDNS 注册。解决: pip install zeroconf")
        return None

    logging.info("注册mDNS服务...")
    ip_addresses = wait_for_lan_ips()
    if not ip_addresses:
        # 宁可不注册，也绝不拿回环地址去注册一个「看得见却连不上」的假服务。
        logging.error("[mDNS] 等不到可用的局域网地址，跳过 mDNS 注册（BLE / WebSocket 不受影响）")
        return None

    service_info = ServiceInfo(
        MDNS_SERVICE_TYPE,
        MDNS_SERVICE_NAME,
        # 全部地址都注册：A 记录本就允许多条，客户端会逐个尝试，不用我们猜哪张网卡对。
        addresses=[socket.inet_aton(ip) for ip in ip_addresses],
        port=WEBSOCKET_PORT,
        server=MDNS_HOST_NAME,
        properties={
            # iOS 端的 Bonjour ↔ BLE 合并 join key，必须有且和 BLE characteristic 值完全相同
            b"device_id": DEVICE_ID.encode("utf-8"),
            b"name": DEVICE_MODEL.encode("utf-8"),
            b"type": b"controller",
            # 以下字段 iOS 不读，留给其他客户端 / 未来扩展
            b"ws_port": str(WEBSOCKET_PORT).encode("utf-8"),
            b"user_server_host": MDNS_LOCAL_NAME.encode("utf-8"),
            b"user_server_base_url": f"http://{MDNS_LOCAL_NAME}:{USER_SERVER_PORT}".encode("utf-8"),
            b"user_server_port": str(USER_SERVER_PORT).encode("utf-8"),
            b"user_profile_path": b"/user_profile",
            b"analysis_path": b"/analysis",
            b"sleep_advice_path": b"/sleep_advice",
        },
    )

    zeroconf_instance = Zeroconf()
    # allow_name_change=True：实例名保持干净的 "Mindora 2026"，同款多台同 LAN 必然撞实例名，
    # 靠 zeroconf 自动加 " (2)" 兜底（iOS 列表显示 "Mindora 2026" / "Mindora 2026 (2)"，正是
    # iOS 侧预期行为）。同时兜住"本机崩溃没 unregister 就快速重启、旧记录未过 TTL"的自撞。
    # 真正的设备唯一性靠 hostname(DEVICE_HOST_SLUG) + TXT device_id，不靠实例名。
    zeroconf_instance.register_service(service_info, allow_name_change=True)
    logging.info("mDNS服务已注册:")
    logging.info(f"  device_id     : {DEVICE_ID}")
    logging.info(f"  service type  : {MDNS_SERVICE_TYPE}")
    logging.info(f"  service name  : {MDNS_SERVICE_NAME}")
    logging.info(f"  host          : {MDNS_HOST_NAME}")
    logging.info(f"  IP            : {', '.join(ip_addresses)}")
    logging.info(f"  port          : {WEBSOCKET_PORT}")
    return service_info


async def keep_trying_mdns_registration(interval: float = 30.0):
    """启动时没等到网络（超过 wait_for_lan_ips 的窗口）时，转入后台无限重试注册。

    宁可晚注册，也不能「本次启动失败就永远没有 mDNS」—— 那等于把设备的 Wi-Fi 通道
    绑死在开机那一刻的网络状态上，跟这次修的开机竞态是同一类错误。
    """
    while True:
        await asyncio.sleep(interval)
        if not get_lan_ips():
            continue
        logging.info("[mDNS] 网络已就绪，补做注册")
        info = await asyncio.get_running_loop().run_in_executor(None, register_mdns_sync)
        if info is not None:
            return info


async def watch_mdns_addresses(service_info, interval: float = 30.0):
    """地址变了就重新通告。覆盖 DHCP 续租换 IP、切 Wi-Fi、插拔网线、开机时网络后到位
    等场景 —— 注册一次就再也不管，是「重启后有时好有时坏」的根因之一。"""
    if service_info is None or zeroconf_instance is None:
        return
    known = {socket.inet_ntoa(a) for a in service_info.addresses}
    while True:
        await asyncio.sleep(interval)
        current = set(get_lan_ips())
        if not current or current == known:
            continue
        logging.info(f"[mDNS] 本机地址变化 {sorted(known)} → {sorted(current)}，重新通告")
        try:
            service_info.addresses = [socket.inet_aton(ip) for ip in sorted(current)]
            await asyncio.get_running_loop().run_in_executor(
                None, zeroconf_instance.update_service, service_info
            )
            known = current
        except Exception as e:  # noqa: BLE001
            logging.error(f"[mDNS] 重新通告失败: {e}")


def unregister_mdns_sync(service_info):
    global zeroconf_instance
    if zeroconf_instance and service_info is not None:
        zeroconf_instance.unregister_service(service_info)
        zeroconf_instance.close()
        logging.info("mDNS服务已注销")


# ==========================================
# Main
# ==========================================
def parse_args():
    """命令行开关：默认全开（mDNS + BLE + WebSocket）。子系统可按需禁用，方便排查
    单一信道的问题，或在没装 zeroconf / websockets / bless 的环境跑一部分。

    常用组合：
      python iot_server.py                 # 默认全开
      python iot_server.py --ble-only      # 只开 BLE（= --no-mdns --no-ws 的快捷）
      python iot_server.py --no-mdns       # 不注册 mDNS（断网 / 排查 Bonjour 时常用）
      python iot_server.py --no-ble        # 不开 BLE（无蓝牙硬件的环境）
      python iot_server.py --no-ws         # 不起 WebSocket（iOS 端不用 ws，省个端口）
    """
    p = argparse.ArgumentParser(description="Mindora IoT Server")
    p.add_argument("--ble-only", action="store_true",
                   help="只开 BLE，跳过 mDNS 注册和 WebSocket 服务")
    p.add_argument("--no-ble", action="store_true", help="跳过 BLE GATT 广播")
    p.add_argument("--no-ble-data", action="store_true", help="BLE 只广播 device_id，不开请求/响应数据通道")
    p.add_argument("--no-mdns", action="store_true", help="跳过 mDNS 注册")
    p.add_argument("--no-ws", action="store_true", help="跳过 WebSocket 服务")
    return p.parse_args()


async def main(args):
    enable_mdns = not (args.no_mdns or args.ble_only)
    enable_ble = not args.no_ble
    enable_ble_data = enable_ble and not args.no_ble_data
    enable_ws = not (args.no_ws or args.ble_only)

    logging.info("=" * 60)
    logging.info(f"Mindora IoT Server  device_id={DEVICE_ID}  device_model={DEVICE_MODEL}")
    logging.info(f"  platform: {sys.platform}")
    logging.info(f"  enabled : mDNS={enable_mdns}  BLE={enable_ble}  BLE_data={enable_ble_data}  WebSocket={enable_ws}")
    logging.info("=" * 60)

    if not (enable_mdns or enable_ble or enable_ws):
        logging.error("[FATAL] 所有子系统都被禁用，无事可做。退出。")
        return

    loop = asyncio.get_event_loop()
    service_info = await loop.run_in_executor(None, register_mdns_sync) if enable_mdns else None
    if enable_mdns and service_info is None:
        # 开机时网络迟迟不就绪：转入后台重试，不放弃 mDNS。
        mdns_watch = asyncio.create_task(keep_trying_mdns_registration())
    else:
        mdns_watch = asyncio.create_task(watch_mdns_addresses(service_info)) if service_info else None
    ble_task = asyncio.create_task(run_ble_server(enable_ble_data)) if enable_ble else None

    # 主阻塞点优先级：WebSocket（如果开）→ 否则等 BLE task → 否则纯 mDNS 模式只睡等 Ctrl+C
    try:
        if enable_ws:
            await run_websocket_server()
        elif ble_task is not None:
            await ble_task
        else:
            await asyncio.Event().wait()
    except KeyboardInterrupt:
        logging.info("服务器正在关闭...")
    finally:
        if mdns_watch is not None:
            mdns_watch.cancel()
        if ble_task is not None:
            ble_task.cancel()
            try:
                await ble_task
            except (asyncio.CancelledError, Exception):
                pass
        if service_info is not None:
            await loop.run_in_executor(None, unregister_mdns_sync, service_info)


if __name__ == "__main__":
    args = parse_args()
    try:
        asyncio.run(main(args))
    except KeyboardInterrupt:
        logging.info("程序已退出")
