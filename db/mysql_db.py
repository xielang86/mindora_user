import logging
import pymysql
from pymysql.err import OperationalError, ProgrammingError
from dbutils.pooled_db import PooledDB  # 核心：引入DBUtils连接池
from dotenv import load_dotenv
import os

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
def get_user_by_email_or_uid(email: str, uid: str = None) -> dict | None:
  """根据邮箱查询用户"""
  if email is not None or len(email) > 3:
    sql = "SELECT uid, email, salt FROM user_auth WHERE email = %s"
    return mysql_db.query_one(sql, (email,))
  elif uid is not None and len(uid) > 3:
    sql = "SELECT uid, email, salt FROM user_auth WHERE uid = %s"
    return mysql_db.query_one(sql, (uid,))
  else:
    logging.error(f"email {email} or uid {uid} must be none empty")
    return -1


def insert_user(email: str, uid: str, salt: str, device_list: str) -> int:
  """插入新用户"""
  sql = """
  INSERT INTO user_auth (uid, email, salt, device_list) 
  VALUES (%s, %s, %s, %s)
  """
  return mysql_db.execute(sql, (uid, email, salt,device_list))

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

  new_user = get_user_by_email_or_uid(new_email)
  logging.info(f"find new user : {new_user}")

  d_r = del_user_by_email_or_uid(email=new_email)
  logging.info(f"dle r = {d_r}")
  new_user = get_user_by_email_or_uid(new_email)
  logging.info(f"after delete, find new user : {new_user}")