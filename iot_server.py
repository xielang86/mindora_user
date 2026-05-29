import json
import asyncio
import os
import socket
import threading
from time import sleep
try:
    from websockets import serve as websocket_serve
except ModuleNotFoundError:
    websocket_serve = None
try:
    from zeroconf import Zeroconf, ServiceInfo  # 使用同步版本
except ModuleNotFoundError:
    Zeroconf = None
    ServiceInfo = None

# 配置信息
USE_BLE = os.getenv("IOT_USE_BLE", "0").strip().lower() in {"1", "true", "yes", "on", "bluepy"}
BLE_DEVICE_NAME = "MyDevice"
BLE_SERVICE_UUID = "0000ffe0-0000-1000-8000-00805f9b34fb"
BLE_CHARACTERISTIC_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"
WEBSOCKET_PORT = 8765
USER_SERVER_PORT = 9001
MDNS_SERVICE_TYPE = "_mydevice._tcp.local."
MDNS_SERVICE_NAME = "MyDevice._mydevice._tcp.local."
MDNS_LOCAL_NAME = f"{BLE_DEVICE_NAME}.local"
MDNS_HOST_NAME = f"{MDNS_LOCAL_NAME}."

# 存储连接状态和zeroconf实例
connected_clients = {
    'ble': None,
    'websocket': None
}
zeroconf_instance = None

if USE_BLE:
    from bluepy.btle import (
        Peripheral,
        Characteristic,
        UUID,
        DefaultDelegate,
        BTLEException,
        ADDR_TYPE_RANDOM,
    )


def get_lan_ip():
    """Return the LAN-facing IP used by phones/apps on the same network."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return socket.gethostbyname(socket.gethostname())


def build_user_server_info():
    ip_address = get_lan_ip()
    base_url = f"http://{ip_address}:{USER_SERVER_PORT}"
    local_base_url = f"http://{MDNS_LOCAL_NAME}:{USER_SERVER_PORT}"
    return {
        "type": "api_info",
        "device": BLE_DEVICE_NAME,
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

if USE_BLE:
    # BLE特征值和服务定义
    class DataCharacteristic(Characteristic):
        def __init__(self, uuid, service):
            super().__init__(uuid, ["read", "write", "notify"], service)
            self.value = b""
            self.peripheral = None

        def set_peripheral(self, peripheral):
            self.peripheral = peripheral

        def set_value(self, value):
            self.value = value
            if self.peripheral:
                self.peripheral.notify(self.handle, value)

        def onRead(self):
            return self.value

        def onWrite(self, data):
            try:
                message = json.loads(data.decode('utf-8'))
                print(f"BLE收到数据: {message}")
                
                # 处理消息并生成响应
                response = process_message(message)
                response_data = json.dumps(response).encode('utf-8')
                
                self.set_value(response_data)
            except Exception as e:
                print(f"BLE数据处理错误: {e}")

    class BLEServerDelegate(DefaultDelegate):
        def __init__(self, characteristic):
            super().__init__()
            self.characteristic = characteristic

        def onConnect(self, connHandle, addr):
            print(f"BLE客户端已连接: {addr}")
            connected_clients['ble'] = connHandle

        def onDisconnect(self, connHandle, addr):
            print(f"BLE客户端已断开: {addr}")
            connected_clients['ble'] = None

# 处理消息的函数
def process_message(message):
    """处理接收到的消息并返回响应"""
    print(f"处理消息: {message}")
    
    # 根据消息类型进行处理
    if message.get('type') == 'ping':
        return {
            'type': 'pong',
            'message': '收到Ping请求',
            'timestamp': message.get('timestamp'),
            'api_info': build_user_server_info().get("user_server"),
        }
    elif message.get('type') == 'api_info':
        return build_user_server_info()
    elif message.get('type') == 'data_request':
        # 模拟返回一些设备数据
        return {
            'type': 'data_response',
            'data': {
                'temperature': 25.5,
                'humidity': 45.2,
                'status': 'normal'
            }
        }
    else:
        return {'type': 'error', 'message': '未知消息类型'}

# BLE服务器处理
def run_ble_server():
    """启动BLE服务器（在单独线程中运行）"""
    if not USE_BLE:
        print("BLE disabled by config. Set IOT_USE_BLE=1 to enable bluepy BLE mode.")
        return

    print("启动BLE服务器...")
    
    try:
        # 创建外设和服务
        peripheral = Peripheral()
        service_uuid = UUID(BLE_SERVICE_UUID)
        characteristic_uuid = UUID(BLE_CHARACTERISTIC_UUID)
        service = peripheral.addService(service_uuid)
        
        # 获取特征并设置外设引用
        characteristic = DataCharacteristic(characteristic_uuid, service)
        characteristic.set_peripheral(peripheral)
        
        # 设置委托
        peripheral.setDelegate(BLEServerDelegate(characteristic))
        
        # 配置广播信息
        peripheral.addrType = ADDR_TYPE_RANDOM
        
        # 添加广播数据：设备名称和服务UUID
        name_bytes = BLE_DEVICE_NAME.encode('utf-8')
        adv_data = bytes([len(name_bytes) + 1, 0x09]) + name_bytes
        
        # 添加服务UUID到广播
        service_uuid_16 = service_uuid.getBaseUUID16()
        uuid_adv_data = bytes([3, 0x03, (service_uuid_16 >> 8) & 0xFF, service_uuid_16 & 0xFF])
        peripheral.setAdvertisingData(adv_data + uuid_adv_data)
        
        # 开始广播
        peripheral.startAdvertising()
        print(f"BLE服务器已启动，设备名: {BLE_DEVICE_NAME}, UUID: {BLE_CHARACTERISTIC_UUID}")
        
        # 保持服务器运行
        while True:
            peripheral.waitForNotifications(1.0)
            sleep(0.1)
            
    except BTLEException as e:
        print(f"BLE服务器错误: {e}")
    except KeyboardInterrupt:
        print("BLE服务器正在关闭...")
    finally:
        try:
            peripheral.stopAdvertising()
            peripheral.disconnect()
        except:
            pass

# WebSocket服务器处理
async def websocket_handler(websocket):
    """处理WebSocket连接"""
    print("WebSocket客户端已连接")
    connected_clients['websocket'] = websocket
    
    try:
        async for message in websocket:
            try:
                data = json.loads(message)
                print(f"WebSocket收到数据: {data}")
                
                response = process_message(data)
                await websocket.send(json.dumps(response))
            except json.JSONDecodeError:
                await websocket.send(json.dumps({
                    'type': 'error', 
                    'message': '无效的JSON格式'
                }))
            except Exception as e:
                await websocket.send(json.dumps({
                    'type': 'error', 
                    'message': f'处理消息时出错: {str(e)}'
                }))
    except Exception as e:
        print(f"WebSocket错误: {e}")
    finally:
        print("WebSocket客户端已断开")
        connected_clients['websocket'] = None

async def run_websocket_server():
    """启动WebSocket服务器"""
    if websocket_serve is None:
        raise RuntimeError("websockets is required to start the WebSocket server")

    print("启动WebSocket服务器...")
    async with websocket_serve(websocket_handler, "0.0.0.0", WEBSOCKET_PORT):
        print(f"WebSocket服务器已启动，端口: {WEBSOCKET_PORT}")
        await asyncio.Future()  # 保持服务器运行

# 同步mDNS服务注册（在单独线程中执行）
def register_mdns_sync():
    """同步注册mDNS服务"""
    global zeroconf_instance
    if Zeroconf is None or ServiceInfo is None:
        raise RuntimeError("zeroconf is required to register the mDNS service")

    print("注册mDNS服务...")
    # 获取本地IP地址
    ip_address = get_lan_ip()
    
    # 创建服务信息
    service_info = ServiceInfo(
        MDNS_SERVICE_TYPE,
        MDNS_SERVICE_NAME,
        addresses=[socket.inet_aton(ip_address)],
        port=WEBSOCKET_PORT,
        server=MDNS_HOST_NAME,
        properties={
            b"name": BLE_DEVICE_NAME.encode("utf-8"),
            b"type": b"controller",
            b"ws_port": str(WEBSOCKET_PORT).encode("utf-8"),
            b"user_server_host": MDNS_LOCAL_NAME.encode("utf-8"),
            b"user_server_base_url": f"http://{MDNS_LOCAL_NAME}:{USER_SERVER_PORT}".encode("utf-8"),
            b"user_server_port": str(USER_SERVER_PORT).encode("utf-8"),
            b"user_profile_path": b"/user_profile",
            b"analysis_path": b"/analysis",
            b"sleep_advice_path": b"/sleep_advice",
        },
    )
    
    # 注册服务
    zeroconf_instance = Zeroconf()
    zeroconf_instance.register_service(service_info)
    print(f"mDNS服务已注册，IP: {ip_address}, 端口: {WEBSOCKET_PORT}")
    return service_info

# 同步mDNS服务注销
def unregister_mdns_sync(service_info):
    """同步注销mDNS服务"""
    global zeroconf_instance
    if zeroconf_instance:
        zeroconf_instance.unregister_service(service_info)
        zeroconf_instance.close()
        print("mDNS服务已注销")

# 主函数
async def main():
    """主函数，启动所有服务"""
    # 在单独线程中注册mDNS服务，避免阻塞事件循环
    loop = asyncio.get_event_loop()
    service_info = await loop.run_in_executor(None, register_mdns_sync)
    
    try:
        if USE_BLE:
            # 在单独线程中启动BLE服务器
            ble_thread = threading.Thread(target=run_ble_server, daemon=True)
            ble_thread.start()
        else:
            print("WiFi-only mode enabled. BLE/bluepy server will not start.")
        
        # 启动WebSocket服务器
        await run_websocket_server()
        
    except KeyboardInterrupt:
        print("服务器正在关闭...")
    finally:
        # 注销mDNS服务
        await loop.run_in_executor(None, unregister_mdns_sync, service_info)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("程序已退出")
    
