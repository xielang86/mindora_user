"""生成 RS256 JWT 密钥对（一次性运维脚本）。

用法：
  python tool/gen_jwt_keys.py                # 生成到 ./jwt_private.pem + ./jwt_public.pem
  python tool/gen_jwt_keys.py --out-dir /etc/mindora/keys

私钥只部署到云端 auth_server（JWT_PRIVATE_KEY_PATH 指向它）；
公钥部署到云端 user_server 和嵌入式设备（JWT_PUBLIC_KEY_PATH，公钥不是秘密）。
"""
import argparse
import os
import stat
import sys


def main():
  p = argparse.ArgumentParser(description="Generate RS256 keypair for Mindora JWT")
  p.add_argument("--out-dir", default=".", help="输出目录，默认当前目录")
  p.add_argument("--bits", type=int, default=2048, help="RSA 位数，默认 2048")
  p.add_argument("--force", action="store_true", help="覆盖已存在的 key 文件")
  args = p.parse_args()

  try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
  except ImportError:
    print("缺少依赖：pip install cryptography")
    sys.exit(1)

  priv_path = os.path.join(args.out_dir, "jwt_private.pem")
  pub_path = os.path.join(args.out_dir, "jwt_public.pem")
  for path in (priv_path, pub_path):
    if os.path.exists(path) and not args.force:
      print(f"已存在，跳过（--force 可覆盖）: {path}")
      sys.exit(2)

  key = rsa.generate_private_key(public_exponent=65537, key_size=args.bits)

  os.makedirs(args.out_dir, exist_ok=True)
  with open(priv_path, "wb") as f:
    f.write(key.private_bytes(
      serialization.Encoding.PEM,
      serialization.PrivateFormat.TraditionalOpenSSL,
      serialization.NoEncryption(),
    ))
  os.chmod(priv_path, stat.S_IRUSR | stat.S_IWUSR)  # 0600

  with open(pub_path, "wb") as f:
    f.write(key.public_key().public_bytes(
      serialization.Encoding.PEM,
      serialization.PublicFormat.SubjectPublicKeyInfo,
    ))

  print(f"私钥（仅云端，0600）: {priv_path}")
  print(f"公钥（云端+设备）  : {pub_path}")
  print("\n部署：")
  print(f"  auth_server : JWT_PRIVATE_KEY_PATH={priv_path}")
  print(f"  user_server : JWT_PUBLIC_KEY_PATH={pub_path}")
  print(f"  设备端      : 把 {pub_path} 打进镜像，JWT_PUBLIC_KEY_PATH 指向它")


if __name__ == "__main__":
  main()
