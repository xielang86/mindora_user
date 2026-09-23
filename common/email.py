import smtplib
import random
import string
from email.mime.text import MIMEText
from email.header import Header

def generate_verify_code(length: int = 6) -> str:
  """生成随机6位数字验证码（可自定义长度）"""
  # 仅数字验证码（更符合用户习惯）
  code = ''.join(random.choices(string.digits, k=length))
  return code

def send_verify_code_via_163(
  sender_email: str,
  sender_auth_code: str,
  receiver_email: str,
  verify_code: str
) -> dict:
  """
  用163邮箱发送验证码邮件
  :param sender_email: 发件人163邮箱（如 xxx@163.com）
  :param sender_auth_code: 163邮箱SMTP授权码（不是登录密码）
  :param receiver_email: 收件人邮箱（北美用户也可填Gmail/Outlook）
  :param verify_code: 生成的验证码
  :return: 发送结果（成功/失败）
  """
  # 163邮箱SMTP配置（固定）
  smtp_server = "smtp.163.com"
  smtp_port = 465  # SSL端口（推荐，更安全）

  # 构造邮件内容（简洁清晰，适配北美邮箱显示）
  email_subject = Header("Your Verification Code (Test)", "utf-8").encode()
  email_body = f"""
  <p>Hello,</p>
  <p>Your verification code is: <strong style="color: #ff4500; font-size: 18px;">{verify_code}</strong></p>
  <p>This code is valid for 5 minutes. Do not share it with anyone.</p>
  <p>Test Email from 163 SMTP</p>
  """
  # 构建邮件对象（HTML格式，更美观）
  msg = MIMEText(email_body, "html", "utf-8")
  msg["From"] = f"Verification Service <{sender_email}>"
  msg["To"] = receiver_email
  msg["Subject"] = email_subject

  try:
    # 连接163 SMTP服务器（SSL加密）
    with smtplib.SMTP_SSL(smtp_server, smtp_port) as smtp_obj:
      # 登录SMTP服务器（用授权码，不是邮箱密码）
      smtp_obj.login(sender_email, sender_auth_code)
      # 发送邮件（批量发送可传列表，如 ["a@xx.com", "b@xx.com"]）
      smtp_obj.sendmail(sender_email, receiver_email, msg.as_string())
    return {
      "code": 0,
      "msg": "验证码邮件发送成功",
      "data": {"verify_code": verify_code, "receiver": receiver_email}
    }
  except smtplib.SMTPAuthenticationError:
    return {"code": 400, "msg": "SMTP登录失败：邮箱/授权码错误", "data": None}
  except smtplib.SMTPRecipientsRefused:
    return {"code": 401, "msg": "收件人邮箱格式错误/不存在", "data": None}
  except Exception as e:
    return {"code": 500, "msg": f"发送失败：{str(e)}", "data": None}

# ------------------- 测试示例 -------------------
if __name__ == "__main__":
  # 替换为你的163邮箱和授权码
  MY_163_EMAIL = "mindora2026@163.com"
  MY_163_AUTH_CODE = "RZkiYNHsVxLGvVHG"  # deadline=20260412
  
  # 测试收件人（可填北美邮箱，如 xxx@gmail.com）
  TEST_RECEIVER = "xielangtc@163.com"
  
  # 生成6位验证码
  verify_code = generate_verify_code(6)
  print(f"生成的验证码：{verify_code}")
  
  # 发送邮件
  result = send_verify_code_via_163(
    sender_email=MY_163_EMAIL,
    sender_auth_code=MY_163_AUTH_CODE,
    receiver_email=TEST_RECEIVER,
    verify_code=verify_code
  )
  print("发送结果：", result)