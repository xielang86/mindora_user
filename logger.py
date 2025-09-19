import logging
import os
from datetime import datetime

log_dir = "logs"
os.makedirs(log_dir, exist_ok=True)

log_filename = datetime.now().strftime(f"{log_dir}/log_%Y-%m-%d_%H-%M-%S.log")

# 清理已有的 handler，防止重复配置
for handler in logging.root.handlers[:]:
    logging.root.removeHandler(handler)

logging.basicConfig(
    level=logging.INFO,  # 设置日志最低级别
    format="%(asctime)s - %(threadName)s - %(module)s"
        " - %(name)s - %(levelname)s - [%(pathname)s:%(lineno)d] - %(message)s",
	datefmt="%m/%d/%Y %I:%M:%S %p",
    handlers=[
        logging.FileHandler(log_filename, encoding='utf-8'),
        logging.StreamHandler()
    ]
)

logging.info("日志初始化完成")
