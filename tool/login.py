import requests
import time,sys
import uuid, json
from auth import AuthRequest,AuthResponse, AuthRequestType, AuthData

# 配置
# BASE_URL = "http://127.0.0.1:9103/auth"
BASE_URL = "https://api.mindora316.com/auth"
TEST_EMAIL = sys.argv[1]
DEVICE_ID = uuid.uuid4()

def print_step(msg):
  print(f"\n{'='*20} {msg} {'='*20}")

# 自定义JSON编码器：处理UUID类型
class UUIDEncoder(json.JSONEncoder):
  def default(self, obj):
    if isinstance(obj, uuid.UUID):
      # 将UUID对象转为字符串
      return str(obj)
    # 其他类型按默认逻辑处理
    return super().default(obj)

def test_auth_flow():
  session_jwt = None

  # --- 1. 测试发送验证码 ---
  print_step("步骤 1: 发送验证码")
  ts = int(time.time())
  request = AuthRequest(request_type=AuthRequestType.SEND_VERIFY_CODE, timestamp=ts, version="1.0", data=AuthData(email=TEST_EMAIL, device_id=DEVICE_ID))
  request_dict = request.model_dump()
  request_json = json.dumps(request_dict, cls=UUIDEncoder)
  resp_send = requests.post(BASE_URL, data=request_json)
  print(f"状态码: {resp_send.status_code}")
  print(f"响应: {resp_send.json()}")
  
  # 提示：因为是模拟，验证码会在服务端控制台打印
  # 如果你在服务端用的是 secrets 随机生成，请去服务端后台查看打印的值
  # 这里我们假设你从后台拿到了验证码，或者服务端逻辑为了方便测试打印了它

  # --- 2. 测试验证码登录 (及注册) ---
  print_step("步骤 2: 验证码登录")
  verify_code = input("\n请输入服务端控制台显示的 4 位验证码: ")
  request.request_type = AuthRequestType.LOGIN_WITH_EMAIL_VERIFY_CODE
  request.data = AuthData(email=TEST_EMAIL, device_id=DEVICE_ID, verify_code=verify_code)
  request_dict = request.model_dump()
  request_json = json.dumps(request_dict, cls=UUIDEncoder)
  resp_login = requests.post(BASE_URL, data=request_json)
  print(f"响应: {resp_login}")
  resp = AuthResponse.model_validate(resp_login.json())
  print(f"响应body data: {resp}")

  jwt_token = resp.data.token

  # --- 3. 测试 JWT 令牌登录 ---
  print_step("步骤 3: JWT 登录验证")
  request.request_type = AuthRequestType.LOGIN_WITH_JWT
  request.data = AuthData(emai=TEST_EMAIL, device_id=DEVICE_ID, jwt_token = jwt_token)
  request.timestamp = int(time.time())

  request_dict = request.model_dump()
  request_json = json.dumps(request_dict, cls=UUIDEncoder)
  print(request_json)
  resp_jwt = requests.post(BASE_URL, data=request_json)
  print(f"http resp: {resp_jwt}")
  print(f"http resp data: {resp_jwt.json()}")
  # if resp_jwt.json().get("code") == 0:
  #   print("✅ JWT 验证通过")

  # resp = AuthResponse.model_validate_json(resp_jwt.json())

  # --- 4. 测试删除用户 ---
  print_step("步骤 4: 删除用户")
  request.request_type = AuthRequestType.DELETE_USER
  request.data.jwt_token = jwt_token
  request_dict = request.model_dump()
  request_json = json.dumps(request_dict, cls=UUIDEncoder)
  resp_delete = requests.post(BASE_URL, data=request_json)
  print(f"响应: {resp_delete.json()}")

if __name__ == "__main__":
  try:
    test_auth_flow()
  except Exception as e:
    print(f"连接失败: {e}")
