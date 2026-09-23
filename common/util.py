def merge_two_sorted(a, b):
  merged = []
  i = j = 0
  while i < len(a) and j < len(b):
    # 比较元组的第一个元素
    if a[i][0] < b[j][0]:
      merged.append(a[i])
      i += 1
    elif a[i][0] > b[j][0]:
      merged.append(b[j])
      j += 1
    else:
      # 第一个元素相同，保留来自第一个列表(a)的元素
      merged.append(a[i])
      i += 1
      j += 1  # 跳过第二个列表中对应元素
  
  # 添加剩余元素
  merged.extend(a[i:])
  merged.extend(b[j:]) 
  return merged

def merge_two_sorted_dedup(a, b):
  """按元素第 1 位（timestamp）归并去重；同 timestamp 时 b（新数据）覆盖 a（旧数据）。

  健康数据同步接口_0814.md §4.2：同一晚内客户端会重发同时间戳的修正值，
  后发的是更完整的值，必须覆盖先存的。
  """
  merged = []
  i = j = 0
  while i < len(a) and j < len(b):
    if len(merged) > 0 and merged[-1][0] == a[i][0]:
      i += 1
      continue

    if len(merged) > 0 and merged[-1][0] == b[j][0]:
      j += 1
      continue

    # 比较元组的第一个元素
    if a[i][0] < b[j][0]:
      merged.append(a[i])
      i += 1
    elif a[i][0] > b[j][0]:
      merged.append(b[j])
      j += 1
    else:
      # 第一个元素相同，取来自第二个列表(b)的元素（后发修正值覆盖）
      merged.append(b[j])
      i += 1
      j += 1  # 跳过第一个列表中对应元素

  # 添加剩余元素
  merged.extend(a[i:])
  merged.extend(b[j:])
  return merged


def normalize_email(email: str) -> str:
  """邮箱标准化：转小写+去首尾空格"""
  if not email:
    raise ValueError("邮箱不能为空")
  return email.strip().lower()

if __name__ == "__main__":
  a = [(100000001, 80), (100000001, 80), (100000001, 80), (100000001, 80), (100000001, 80), (100000001, 80), (100000001, 80), (100000001, 80), (100000001, 80), (100000001, 80), (100000001, 80), (100000001, 80), (100000001, 80), (100000001, 80), (1000000000, 78)]
  b = [(100000001, 80), (100000001, 80), (100000001, 80), (100000001, 80), (100000001, 80), (100000001, 80), (100000001, 80), (100000001, 80), (100000001, 80), (100000001, 80), (100000001, 80), (100000001, 80), (100000001, 80), (100000001, 80), (1000000000, 78)]
  c = merge_two_sorted(a, b)
  print(len(c))
  print(len(a))

  d = merge_two_sorted_dedup(a, b)
  print(d)
