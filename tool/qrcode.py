import sys
from pathlib import Path

# 直接执行本脚本时，Python 会优先搜索 tool/，把 qrcode.py 自身当成第三方包。
# 排除该目录的所有路径项（含当前目录），保留原有命令行入口。
if __name__ == "__main__":
    script_dir = Path(__file__).resolve().parent
    sys.path = [entry for entry in sys.path if Path(entry).resolve() != script_dir]

import qrcode
import qrcode.image.svg

app_store_url = sys.argv[1]

# 使用 SvgImage 工厂类
factory = qrcode.image.svg.SvgImage

qr = qrcode.QRCode(
    version=None,  # 自动命中 Version 2
    error_correction=qrcode.constants.ERROR_CORRECT_M,
    box_size=10,   # 控制 SVG 内部基础模块的单位尺寸
    border=4,
    image_factory=factory
)

qr.add_data(app_store_url)
qr.make(fit=True)

# 生成并保存 SVG
img = qr.make_image()
img.save("download.svg")

print("已成功生成 SVG 矢量二维码：download.svg")
