"""
Mindora 设备端 IoT 服务（mDNS + WebSocket + BLE）

跟 iOS App 的发现/绑定逻辑对齐：
  - mDNS 服务类型 _mindora._tcp.，TXT 暴露 device_id 字段（每台唯一）
  - BLE GATT 暴露 read characteristic，value = device_id UTF-8 字节（同 TXT 一致，用于 iOS 端 Bonjour↔BLE 合并）
  - 设备型号 DEVICE_MODEL = "Mindora 2026"（用户可见名，所有同款设备共用，mDNS service name + BLE 广播 local name）
  - DNS host slug 用 model + device_id 末尾，保证 LAN 内唯一

详细规格 / 使用 / 联调步骤见 doc/iot_server_README.md。

平台支持（一份代码三平台跑，无需任何环境变量）：
  - macOS：bless 走 CoreBluetooth via PyObjC
  - Linux：bless 走 BlueZ via DBus（需系统装 bluez，pip 自动拉 dbus-next）
  - Windows：bless 走 WinRT

跑这个脚本前请：
  pip install -r requirements.txt
  （含 bless / zeroconf / websockets）
"""
import asyncio
import json
import os
import socket
import sys

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
      3. mnd-dev-0000                兜底硬编码，仅警示用，不应进生产
    """
    for path in ["/etc/mindora/device_id", os.path.expanduser("~/.mindora/device_id")]:
        try:
            with open(path) as f:
                value = f.read().strip()
                if value:
                    return value
        except FileNotFoundError:
            continue
    return "mnd-dev-0000"


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
BLE_SERVICE_UUID = "12345678-1234-5678-1234-56789abcdef0"
BLE_CHARACTERISTIC_UUID = "12345678-1234-5678-1234-56789abcdef1"

WEBSOCKET_PORT = 8765
USER_SERVER_PORT = 9001

# mDNS：iOS BonjourDiscovery 监听 _mindora._tcp.
# service name 用带空格的 DEVICE_MODEL = "Mindora 2026"——这就是用户看到的型号；
# 同款多台在同一 LAN 时 Bonjour 自动加 (2) (3) 后缀避免重名。
# DNS hostname 必须用 dash 形式 DEVICE_HOST_SLUG（DNS 协议不允许空格），且需在 LAN 唯一。
MDNS_SERVICE_TYPE = "_mindora._tcp.local."
MDNS_SERVICE_NAME = f"{DEVICE_MODEL}._mindora._tcp.local."
MDNS_LOCAL_NAME = f"{DEVICE_HOST_SLUG}.local"
MDNS_HOST_NAME = f"{MDNS_LOCAL_NAME}."

zeroconf_instance = None


def get_lan_ip():
    """返回手机在同一局域网能访问的本机 IP。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return socket.gethostbyname(socket.gethostname())


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
    print(f"处理消息: {message}")
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
# BLE peripheral —— 用 bless 做跨平台
# ==========================================
async def run_ble_server():
    """跨平台 BLE peripheral，bless 自动按 sys.platform 选后端：
       macOS → CoreBluetooth；Linux → BlueZ via DBus；Windows → WinRT。
    """
    if BlessServer is None:
        print("[BLE] 缺依赖 bless，跳过 BLE 服务。解决: pip install bless")
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
        BLE_CHARACTERISTIC_UUID,
        GATTCharacteristicProperties.read,
        device_id_bytes,
        GATTAttributePermissions.readable,
    )

    print("[BLE] 启动 bless peripheral...")
    await server.start()
    print("[BLE] 广播已启动:")
    print(f"  device model : {DEVICE_MODEL}")
    print(f"  service UUID : {BLE_SERVICE_UUID}")
    print(f"  char UUID    : {BLE_CHARACTERISTIC_UUID}")
    print(f"  char value   : {DEVICE_ID}")

    try:
        # 阻塞直到外层 cancel
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        pass
    finally:
        try:
            await server.stop()
            print("[BLE] 已停止")
        except Exception as e:
            print(f"[BLE] 停止时报错（忽略）: {e}")


# ==========================================
# WebSocket
# ==========================================
async def websocket_handler(websocket):
    """处理 WebSocket 连接。iOS 不连这里，给其他客户端 / 未来扩展用。"""
    print("WebSocket客户端已连接")
    try:
        async for message in websocket:
            try:
                data = json.loads(message)
                print(f"WebSocket收到数据: {data}")
                response = process_message(data)
                await websocket.send(json.dumps(response))
            except json.JSONDecodeError:
                await websocket.send(json.dumps({'type': 'error', 'message': '无效的JSON格式'}))
            except Exception as e:
                await websocket.send(json.dumps({'type': 'error', 'message': f'处理消息时出错: {str(e)}'}))
    except Exception as e:
        print(f"WebSocket错误: {e}")
    finally:
        print("WebSocket客户端已断开")


async def run_websocket_server():
    if websocket_serve is None:
        print("[WS] 缺依赖 websockets，跳过 WebSocket 服务。解决: pip install websockets")
        # 不能 return，否则 main() 会立即退出。挂起让 BLE 继续跑。
        await asyncio.Event().wait()
        return
    print("启动WebSocket服务器...")
    async with websocket_serve(websocket_handler, "0.0.0.0", WEBSOCKET_PORT):
        print(f"WebSocket服务器已启动，端口: {WEBSOCKET_PORT}")
        await asyncio.Event().wait()


# ==========================================
# mDNS
# ==========================================
def register_mdns_sync():
    """同步注册 mDNS 服务。"""
    global zeroconf_instance
    if Zeroconf is None or ServiceInfo is None:
        print("[mDNS] 缺依赖 zeroconf，跳过 mDNS 注册。解决: pip install zeroconf")
        return None

    print("注册mDNS服务...")
    ip_address = get_lan_ip()

    service_info = ServiceInfo(
        MDNS_SERVICE_TYPE,
        MDNS_SERVICE_NAME,
        addresses=[socket.inet_aton(ip_address)],
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
    zeroconf_instance.register_service(service_info)
    print("mDNS服务已注册:")
    print(f"  device_id     : {DEVICE_ID}")
    print(f"  service type  : {MDNS_SERVICE_TYPE}")
    print(f"  service name  : {MDNS_SERVICE_NAME}")
    print(f"  host          : {MDNS_HOST_NAME}")
    print(f"  IP            : {ip_address}")
    print(f"  port          : {WEBSOCKET_PORT}")
    return service_info


def unregister_mdns_sync(service_info):
    global zeroconf_instance
    if zeroconf_instance and service_info is not None:
        zeroconf_instance.unregister_service(service_info)
        zeroconf_instance.close()
        print("mDNS服务已注销")


# ==========================================
# Main
# ==========================================
async def main():
    print("=" * 60)
    print(f"Mindora IoT Server  device_id={DEVICE_ID}  device_model={DEVICE_MODEL}")
    print(f"  platform: {sys.platform}")
    print("=" * 60)

    loop = asyncio.get_event_loop()
    service_info = await loop.run_in_executor(None, register_mdns_sync)

    ble_task = asyncio.create_task(run_ble_server())
    try:
        await run_websocket_server()
    except KeyboardInterrupt:
        print("服务器正在关闭...")
    finally:
        ble_task.cancel()
        try:
            await ble_task
        except (asyncio.CancelledError, Exception):
            pass
        await loop.run_in_executor(None, unregister_mdns_sync, service_info)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("程序已退出")
