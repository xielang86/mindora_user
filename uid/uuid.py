import uuid
import os
import typing, logging
from cryptography.fernet import Fernet, InvalidToken
import hashlib

# -------------------------- 配置项（可自定义） --------------------------
# 加密文件路径（存储uuid1的加密内容）
UUID_ENCRYPT_FILE = "uuid_mac_time.enc"
# Fernet加密密钥（可自行生成替换，生成方法见下方说明）
ENCRYPT_KEY = b'scHtsUOJBon6_RHMOIbkDDx_EDObXiPh8AUMV1c7ch4='  # 需是32位base64编码字符串

# ------------------- 工具函数：邮箱标准化+UID生成 -------------------
def normalize_email(email: str) -> str:
  """邮箱标准化：转小写+去首尾空格"""
  if not email:
    raise ValueError("邮箱不能为空")
  return email.strip().lower()


def generate_uid_and_salt(email: str) -> tuple[str, str]:
  """生成UID（SHA256+加盐）和盐值"""
  normalized_email = normalize_email(email)
  salt = os.urandom(16).hex()  # 生成32位随机盐
  # 加盐Hash生成UID
  hash_obj = hashlib.sha256()
  hash_obj.update((salt + normalized_email).encode("utf-8"))
  uid = hash_obj.hexdigest()
  return uid, salt

def generate_user_uuid(device_id: str, email: str) -> typing.Optional[str]:
  """
  基于设备ID（did）和用户邮箱生成唯一、可复现的UUID
  Args:
    device_id: 系统硬件唯一标识（did），如MAC地址、SN号、设备序列号等
    email: 用户输入的邮箱地址（仅作为字符串使用，不验证有效性）
  
  Returns:
    生成的UUID字符串（小写）；若输入无效返回None
  """
  # 1. 输入合法性校验（硬件设备场景需严格校验，避免无效输入）
  if not isinstance(device_id, str) or len(device_id.strip()) == 0:
    logging.info("错误：设备ID（did）不能为空且必须为字符串")
    return None
  if not isinstance(email, str) or len(email.strip()) == 0:
    logging.info("错误：邮箱地址不能为空且必须为字符串")
    return None
  
  # 2. 标准化输入（去除首尾空格，统一小写，避免因格式差异生成不同UUID）
  normalized_did = device_id.strip()
  normalized_email = email.strip().lower()  # 邮箱大小写不敏感，统一小写
  
  # 3. 拼接种子字符串（确保did和email的组合唯一性）
  seed = f"{normalized_did}_{normalized_email}"
  
  try:
    # 4. 生成UUID5（固定命名空间+种子哈希，可复现）
    # 选择NAMESPACE_OID作为固定命名空间（也可自定义私有命名空间）
    user_uuid = uuid.uuid5(uuid.NAMESPACE_OID, seed)
    # 转为字符串（标准UUID格式，如：1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed）
    return str(user_uuid)
  except Exception as e:
    logging.error(f"生成UUID失败：{str(e)}")
    return None

# use it as default uuid
def get_or_create_uuid():
  """
  首次运行：生成uuid1并加密写入文件
  后续运行：从加密文件读取并解密uuid1
  返回：合法的uuid1字符串
  """
  # 初始化Fernet加密器
  try:
    fernet = Fernet(ENCRYPT_KEY)
  except ValueError as e:
    raise Exception(f"加密密钥无效：{e}，请使用合法的32位base64编码密钥")

  # 第一步：判断加密文件是否存在
  if not os.path.exists(UUID_ENCRYPT_FILE):
    print("首次运行：未检测到uuid加密文件，开始生成并存储...")
    # 生成uuid1（基于MAC地址+时间戳）
    uuid_mac_time = uuid.uuid1()
    uuid_str = str(uuid_mac_time)
    print(f"生成的uuid1：{uuid_str}")

    # 加密uuid字符串（Fernet仅加密字节流）
    encrypted_uuid = fernet.encrypt(uuid_str.encode("utf-8"))

    # 写入加密文件
    try:
      with open(UUID_ENCRYPT_FILE, "wb") as f:
        f.write(encrypted_uuid)
      print(f"uuid1已加密写入文件：{UUID_ENCRYPT_FILE}")
    except PermissionError:
      raise Exception(f"权限不足：无法写入文件 {UUID_ENCRYPT_FILE}")
    except Exception as e:
      raise Exception(f"写入文件失败：{e}")

    return uuid_str
  else:
    print("检测到uuid加密文件，开始读取并解密...")
    # 读取加密文件内容
    try:
      with open(UUID_ENCRYPT_FILE, "rb") as f:
        encrypted_uuid = f.read()
    except PermissionError:
      raise Exception(f"权限不足：无法读取文件 {UUID_ENCRYPT_FILE}")
    except Exception as e:
      raise Exception(f"读取文件失败：{e}")

    # 解密uuid内容
    try:
      decrypted_uuid = fernet.decrypt(encrypted_uuid).decode("utf-8")
      # 验证解密结果是否为合法uuid格式
      uuid.UUID(decrypted_uuid)  # 若格式非法会抛出ValueError
      print(f"解密成功，获取到uuid1：{decrypted_uuid}")
      return decrypted_uuid
    except InvalidToken:
      raise Exception("解密失败：文件已被篡改或密钥错误")
    except ValueError:
      raise Exception("解密结果无效：不是合法的UUID格式")

# -------------------------- 调用示例 --------------------------
if __name__ == "__main__":
  # key = Fernet.generate_key()
  # print("你的专属Fernet密钥：", key)  # 复制该输出替换代码中的ENCRYPT_KEY
  try:
    # 获取uuid1（首次生成，后续读取）
    final_uuid = get_or_create_uuid()
    print(f"最终使用的uuid1：{final_uuid}")
    
    # 模拟硬件设备读取的did（实际场景从硬件寄存器/配置文件读取）
    DEVICE_ID = final_uuid  # 示例：设备序列号、MAC地址等
  
    # 场景1：正常输入（生成固定UUID）
    email1 = "User@Example.Com"  # 带大小写和空格的邮箱
    uuid1 = generate_user_uuid(DEVICE_ID, email1)
    print(f"场景1 - 输入邮箱：{email1}")
    print(f"生成UUID：{uuid1}")  # 输出：固定值（如d3b7c8f0-xxxx-xxxx-xxxx-xxxxxxxxxxxx）
  
    # 场景2：相同did+相同邮箱（大小写/空格不同）→ 生成相同UUID（验证可复现）
    email2 = "  user@example.com  "
    uuid2 = generate_user_uuid(DEVICE_ID, email2)
    print(f"\n场景2 - 输入邮箱：{email2}")
    print(f"生成UUID：{uuid2}")
    print(f"与场景1 UUID是否相同：{uuid1 == uuid2}")  # 输出：True
  
    # 场景3：输入为空（校验失败）
    email3 = ""
    uuid3 = generate_user_uuid(DEVICE_ID, email3)
    print(f"\n场景3 - 输入邮箱：{email3}")
    print(f"生成UUID：{uuid3}")  # 输出：None  
      # ...
  except Exception as e:
    print(f"程序异常：{e}")
