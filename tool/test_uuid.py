import uuid

# 1. 生成随机UUID（最常用）
uuid_random = uuid.uuid4()
print("随机UUID（uuid4）：", uuid_random)
print("UUID字符串格式：", str(uuid_random))  # 转换为字符串（常用存储/传输格式）
print("UUID十六进制格式：", uuid_random.hex)  # 无分隔符的十六进制字符串

# 2. 基于MAC地址+时间戳（uuid1）
uuid_mac_time = uuid.uuid1()
print("\n基于MAC+时间戳的UUID（uuid1）：", uuid_mac_time)

# 3. 基于命名空间+字符串（uuid3/utf-5，可重复生成）
namespace = uuid.NAMESPACE_DNS  # 预定义命名空间（还有NAMESPACE_URL/NAMESPACE_OID等）
name = "user_12345"  # 自定义字符串
uuid_hash3 = uuid.uuid3(namespace, name)
uuid_hash5 = uuid.uuid5(namespace, name)
print("\n基于MD5哈希的UUID（uuid3）：", uuid_hash3)
print("基于SHA-1哈希的UUID（uuid5）：", uuid_hash5)

# 4. 解析现有UUID字符串
# uuid_str = "12345678-ABCD-EFGH-IJKL-1234567890AB"
# parsed_uuid = uuid.UUID(uuid_str)
# print("\n解析后的UUID：", parsed_uuid)
# print("UUID版本：", parsed_uuid.version)  # 输出UUID类型（1/3/4/5）
