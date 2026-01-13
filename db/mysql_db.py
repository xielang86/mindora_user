import logging
import pymysql
from pymysql.err import OperationalError, ProgrammingError
from dbutils.pooled_db import PooledDB  # 核心：引入DBUtils连接池
from dotenv import load_dotenv
import os
from auth import UserData

# 加载配置文件
load_dotenv()

class MySQLDB:
  """MySQL数据库操作封装（DBUtils连接池+通用CRUD）"""
  # 连接池实例（全局单例）
  _pool = None

  def __init__(self):
    self.host = os.getenv("MYSQL_HOST")
    self.port = int(os.getenv("MYSQL_PORT"))
    self.user = os.getenv("MYSQL_USER")
    self.password = os.getenv("MYSQL_PASSWORD")
    self.db = os.getenv("MYSQL_DB")
    self.charset = os.getenv("MYSQL_CHARSET")
    self._init_pool()

  def _init_pool(self):
    """初始化DBUtils连接池（替代pymysql不存在的ConnectionPool）"""
    if not MySQLDB._pool:
      MySQLDB._pool = PooledDB(
        creator=pymysql,  # 指定使用的数据库驱动
        host=self.host,
        port=self.port,
        user=self.user,
        password=self.password,
        database=self.db,
        charset=self.charset,
        maxconnections=10,  # 连接池最大连接数
        mincached=2,  # 初始化时，连接池至少创建的空闲连接数
        maxcached=5,  # 连接池最大空闲连接数
        maxshared=3,  # 最大共享连接数（0表示所有连接都是独占的）
        blocking=True,  # 连接池无空闲连接时，是否阻塞等待（True=等待）
        maxusage=None,  # 单个连接最大复用次数（None=无限）
        setsession=[],  # 会话初始化语句（如["SET AUTOCOMMIT=1"]）
        autocommit=True  # 自动提交事务
      )

  def get_connection(self):
    """从连接池获取连接（兼容原有接口）"""
    try:
      return MySQLDB._pool.connection()  # DBUtils的连接获取方式
    except OperationalError as e:
      raise Exception(f"MySQL连接池获取连接失败：{str(e)}")

  def query_one(self, sql: str, params: tuple = ()) -> dict | None:
    """查询单条数据（返回字典格式）"""
    conn = None
    try:
      conn = self.get_connection()
      with conn.cursor(pymysql.cursors.DictCursor) as cursor:
        cursor.execute(sql, params)
        return cursor.fetchone()
    except ProgrammingError as e:
      raise Exception(f"MySQL查询语法错误：{str(e)}")
    finally:
      if conn:
        conn.close()  # DBUtils的close不是真关闭，而是归还到连接池

  def execute(self, sql: str, params: tuple = ()) -> int:
    """执行增/删/改操作"""
    conn = None
    try:
      conn = self.get_connection()
      with conn.cursor() as cursor:
        row_count = cursor.execute(sql, params)
        return row_count
    except ProgrammingError as e:
      raise Exception(f"MySQL执行语法错误：{str(e)}")
    finally:
      if conn:
        conn.close()  # 归还连接到池


mysql_db = MySQLDB()



# ------------------- 业务封装：用户表操作 -------------------

def get_user_by_email_or_uid(email: str = None, uid: str = None) -> UserData| None:
  """根据邮箱查询用户"""
  if email is not None and len(email) > 3:
    sql = "SELECT uid, email, salt , status, device_list, register_time, update_time FROM user_auth WHERE email = %s"
    mysql_dict = mysql_db.query_one(sql, (email,))
  elif uid is not None and len(uid) > 3:
    sql = "SELECT uid, email, salt , status, device_list , register_time, update_time FROM user_auth WHERE uid = %s"
    mysql_dict = mysql_db.query_one(sql, (uid,))
  else:
    logging.error(f"email {email} or uid {uid} must be none empty")
    return None

  if mysql_dict is not None:
    print(mysql_dict)
    return UserData.model_validate(mysql_dict)
  else:
    return None


def get_active_user_by_email_or_uid(email: str, uid: str = None) -> UserData| None:
  user = get_user_by_email_or_uid(email, uid)
  if user == None or user.status != 1:
    return None
  return user
  

def insert_user(email: str, uid: str, salt: str, device_list: str) -> int:
  """插入新用户"""
  sql = """
  INSERT INTO user_auth (uid, email, salt, device_list) 
  VALUES (%s, %s, %s, %s)
  """
  return mysql_db.execute(sql, (uid, email, salt, device_list))

def insert_or_restore_user(email: str, uid: str, salt: str, device_list: str) -> dict:
  """
  插入/恢复用户：
  - UID不存在 → 插入新用户（status=1）
  - UID存在且status=0 → 恢复账号（status=1）+ 更新device_list/update_time
  - UID存在且status=1 → 返回“账号已存在”
  :param user_data: 包含uid的用户字典，如 {"uid": "...", "email": "...", "salt": "...", "device_list": [...]}
  :return: 业务状态结果
  """
  
  placeholders = ", ".join(["%s"] * 5)
  # 核心：仅恢复软删除用户，正常用户不更新
  update_clause = """
      status = CASE WHEN user_auth.status = 0 THEN 1 ELSE user_auth.status END,
      device_list = CASE WHEN user_auth.status = 0 THEN VALUES(device_list) ELSE user_auth.device_list END,
      update_time = CASE WHEN user_auth.status = 0 THEN NOW() ELSE user_auth.update_time END
  """
  sql = f"""
  INSERT INTO user_auth (email, uid, salt, status, device_list)
  VALUES ({placeholders})
  ON DUPLICATE KEY UPDATE {update_clause}
  """
  params = (email, uid, salt, 1, device_list)
  
  try:
    # 4. 执行SQL并判断结果
    row_count = mysql_db.execute(sql, params)
    # row_count=1 → 新插入；row_count=2 → 冲突后更新（仅软删除用户会真更新）
    if row_count == 1:
      return {"code": 0, "msg": "新用户创建成功", "data": {"uid": uid}}
    elif row_count == 2:
      # 检查用户当前状态，判断是“恢复账号”还是“账号已存在”
      user = get_user_by_email_or_uid(uid)
      if user["status"] == 1:
        # 说明是正常用户冲突，未执行任何更新
        return {"code": 401, "msg": "账号已存在（正常状态），未执行更新", "data": {"uid": uid}}
      else:
        return {"code": 0, "msg": "软删除账号已恢复", "data": {"uid": uid}}
    else:
      return {"code": 500, "msg": "操作失败，无数据变更", "data": None}
  except Exception as e:
    return {"code": 500, "msg": f"数据库操作失败：{str(e)}", "data": None}


def del_user_by_email_or_uid(email: str = None, uid: str = None) -> int:
  """插入新用户"""
  if email is not None or len(email) > 3:
    sql = "DELETE from user_auth where email=%s"
    return mysql_db.execute(sql, (email,))
  elif uid is not None and len(uid) > 3:
    sql = "DELETE from user_auth where uid=%s"
    return mysql_db.execute(sql, (uid,))
  else:
    logging.error(f"email {email} or uid {uid} must be none empty")
    return -1

  
def soft_delete_user(uid: str) -> dict:
  """
  软删除用户：将status改为0，更新update_time为当前时间
  :param uid: 用户唯一标识
  :return: 业务结果字典（code/msg/data）
  """
  try:
    # 1. 参数校验：uid不能为空
    if not uid or not isinstance(uid, str):
      return {"code": 400, "msg": "用户UID不能为空且必须为字符串", "data": None}
    
    # 2. 检查用户是否存在
    user = get_user_by_email_or_uid(uid=uid)
    if not user:
      return {"code": 404, "msg": "用户不存在", "data": None}
    
    # 3. 检查用户是否已被软删除
    if user.status == 0:
      return {"code": 200, "msg": "用户已处于软删除状态，无需重复操作", "data": {"uid": uid}}
    
    # 4. 执行软删除：修改status=0 + 更新update_time
    sql = """
    UPDATE user_auth 
    SET status = 0, update_time = NOW() 
    WHERE uid = %s
    """
    row_count = mysql_db.execute(sql, (uid,))
    
    # 5. 结果判断
    if row_count == 1:
      return {
        "code": 0,
        "msg": "用户软删除成功（status=0）",
        "data": {"uid": uid, "update_time": user.update_time}
      }
    else:
      return {"code": 500, "msg": "用户软删除失败，无数据变更", "data": None}
  
  except Exception as e:
    return {"code": 500, "msg": f"软删除用户异常：{str(e)}", "data": None}
  
def update_user_device_list(uid: str, new_device_list: str) -> int:
  """
  修改用户设备列表，数据库自动更新 update_time 为当前时间
  :param uid: 用户唯一标识（更新条件）
  :param new_device_list: 新的设备列表（如 ["device1", "device2"]）
  :return: 影响的行数（1=成功，0=用户不存在）
  """
  # MySQL 的 JSON_SET 函数可直接更新JSON字段，也可直接赋值
  sql = """
  UPDATE user_auth 
  SET device_list = %s 
  WHERE uid = %s
  """
  # 将Python列表转为JSON字符串（pymysql会自动适配MySQL的JSON类型）
  return mysql_db.execute(sql, (new_device_list, uid))
    

if __name__ == "__main__":
  import logger
  none_user = get_user_by_email_or_uid("fucking_user@fucking.com")
  logging.info(f"none_user: {none_user}")
  test_user = get_user_by_email_or_uid("test@example.com")
  logging.info(f"test_user: {test_user}")

  new_email = "zhouzhao@mindora.com"
  new_user = get_user_by_email_or_uid(new_email)
  if new_user != None:
    logging.info(f"user {new_user} has exist, we del it first")
    d_r = del_user_by_email_or_uid(email=new_email)

  r = insert_user(new_email, "xielang", "laoguan", "dddd")
  logging.info(f"insert result: {r}")

  new_user = get_user_by_email_or_uid(email=new_email)
  logging.info(f"find new user : {new_user}")

  ur = update_user_device_list(new_user.uid, "iphone")
  logging.info(f"update device list for {new_user}, device_list=iphone")

  result = soft_delete_user(new_user.uid)
  logging.info(f"soft delete user {new_user} result={result}")

  new_user = get_user_by_email_or_uid(email=new_email)
  logging.info(f"after soft delete find new user : {new_user}")
    
  active_user = get_active_user_by_email_or_uid(email=new_user.email)
  logging.info(f"after soft delete find active user : {new_user}, result={active_user}")

  d_r = del_user_by_email_or_uid(email=new_email)
  logging.info(f"dle r = {d_r}")
  new_user = get_user_by_email_or_uid(new_email)
  logging.info(f"after delete, find new user : {new_user}")