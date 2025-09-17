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