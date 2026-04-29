import logging
import hashlib
import json
import secrets
import pymysql
from pymysql.err import OperationalError, ProgrammingError
from dbutils.pooled_db import PooledDB  # 核心：引入DBUtils连接池
from dotenv import load_dotenv
import os
from datetime import datetime
from auth import UserData
from common.user_rights import (
  DEFAULT_USER_LEVEL,
  build_user_rights_payload,
  normalize_user_level,
  resolve_level_upgrade,
)

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
    logging.info(f"insert row count = {row_count}")
    # row_count=1 → 新插入；row_count=2 → 冲突后更新（仅软删除用户会真更新）
    if row_count == 1:
      return {"code": 0, "msg": "新用户创建成功", "data": {"uid": uid}}
    elif row_count == 2:
      # 检查用户当前状态，判断是“恢复账号”还是“账号已存在”
      user = get_user_by_email_or_uid(uid=uid)
      if user.status == 1:
        # 说明是正常用户冲突，未执行任何更新
        return {"code": 200, "msg": "账号已存在（正常状态），未执行更新", "data": {"uid": uid}}
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
  
# ── Web registration: email+password, phone, WeChat ──────────────────────────

def init_web_columns():
  """
  Add extra columns to user_auth if they don't already exist.
  Called once at startup. Safe to run multiple times.
  """
  columns = {
    "password_hash": "VARCHAR(128) DEFAULT NULL COMMENT '密码哈希(PBKDF2-SHA256)'",
    "phone":         "VARCHAR(20)  DEFAULT NULL COMMENT '手机号'",
    "wechat_openid": "VARCHAR(64)  DEFAULT NULL COMMENT '微信openid'",
    "wechat_unionid":"VARCHAR(64)  DEFAULT NULL COMMENT '微信unionid'",
    "nickname":      "VARCHAR(64)  DEFAULT NULL COMMENT '昵称'",
    "avatar_url":    "VARCHAR(512) DEFAULT NULL COMMENT '头像URL'",
  }
  db_name = os.getenv("MYSQL_DB", "")
  for col, definition in columns.items():
    check_sql = (
      "SELECT COUNT(*) as cnt FROM information_schema.COLUMNS "
      "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='user_auth' AND COLUMN_NAME=%s"
    )
    row = mysql_db.query_one(check_sql, (db_name, col))
    if row and row["cnt"] == 0:
      try:
        mysql_db.execute(f"ALTER TABLE user_auth ADD COLUMN {col} {definition}", ())
        logging.info("Added column %s to user_auth", col)
      except Exception as e:
        logging.warning("Could not add column %s: %s", col, e)
  # Unique index on phone (ignore error if already exists)
  for col in ("phone", "wechat_openid"):
    try:
      mysql_db.execute(
        f"CREATE UNIQUE INDEX idx_user_auth_{col} ON user_auth ({col})", ()
      )
    except Exception:
      pass


def init_user_rights_columns():
  """Add membership columns to user_auth if they do not exist."""
  columns = {
    "user_level": "VARCHAR(32) NOT NULL DEFAULT 'free' COMMENT '用户等级：free/pro/premium'",
    "level_end_at": "DATETIME DEFAULT NULL COMMENT '会员等级结束时间'",
  }
  db_name = os.getenv("MYSQL_DB", "")
  for col, definition in columns.items():
    check_sql = (
      "SELECT COUNT(*) as cnt FROM information_schema.COLUMNS "
      "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='user_auth' AND COLUMN_NAME=%s"
    )
    row = mysql_db.query_one(check_sql, (db_name, col))
    if row and row["cnt"] == 0:
      try:
        mysql_db.execute(f"ALTER TABLE user_auth ADD COLUMN {col} {definition}", ())
        logging.info("Added membership column %s to user_auth", col)
      except Exception as e:
        logging.warning("Could not add membership column %s: %s", col, e)


def init_redemption_tables():
  """Create redemption_codes table if the DB user has DDL permission."""
  sql = """
  CREATE TABLE IF NOT EXISTS redemption_codes (
    code_hash VARCHAR(64) NOT NULL COMMENT '兑换码SHA256哈希',
    code_prefix VARCHAR(24) NOT NULL COMMENT '兑换码前缀，便于排查',
    batch_id VARCHAR(64) NOT NULL COMMENT '批次号',
    target_level VARCHAR(32) NOT NULL DEFAULT 'pro' COMMENT '目标等级',
    duration_days INT NOT NULL COMMENT '兑换后有效天数',
    status TINYINT(1) NOT NULL DEFAULT 0 COMMENT '0-未使用，1-已兑换，2-已过期，3-已禁用',
    expire_at DATETIME DEFAULT NULL COMMENT '兑换码本身的过期时间',
    activated_uid VARCHAR(64) DEFAULT NULL COMMENT '兑换用户UID',
    activated_at DATETIME DEFAULT NULL COMMENT '兑换时间',
    rights_json JSON DEFAULT NULL COMMENT '预留扩展权益配置',
    created_by VARCHAR(64) DEFAULT NULL COMMENT '创建人',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (code_hash),
    KEY idx_redemption_batch (batch_id),
    KEY idx_redemption_uid (activated_uid),
    KEY idx_redemption_status (status)
  ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='兑换码表';
  """
  try:
    mysql_db.execute(sql, ())
  except Exception as e:
    logging.warning("Could not create redemption_codes table: %s", e)


def init_membership_schema():
  """Best-effort runtime initialization for rights-related schema."""
  init_web_columns()
  init_user_rights_columns()
  init_redemption_tables()


def _hash_redemption_code(code: str) -> str:
  return hashlib.sha256(code.strip().upper().encode("utf-8")).hexdigest()


def _generate_redemption_code_text() -> str:
  alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
  groups = ["".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(4)]
  return f"MDR-{groups[0]}-{groups[1]}-{groups[2]}-{groups[3]}"


def get_user_rights_info(uid: str) -> dict:
  sql = "SELECT user_level, level_end_at, status FROM user_auth WHERE uid=%s"
  try:
    row = mysql_db.query_one(sql, (uid,))
  except Exception as e:
    logging.warning("membership columns unavailable, fallback to free rights: %s", e)
    return build_user_rights_payload(DEFAULT_USER_LEVEL, None)

  if not row or row.get("status") != 1:
    return build_user_rights_payload(DEFAULT_USER_LEVEL, None)
  return build_user_rights_payload(row.get("user_level"), row.get("level_end_at"))


def create_redemption_codes(
  batch_id: str,
  target_level: str,
  duration_days: int,
  quantity: int,
  expire_at: datetime | None = None,
  created_by: str | None = None,
) -> list[dict]:
  normalized_level = normalize_user_level(target_level)
  if quantity <= 0:
    raise ValueError("quantity must be greater than 0")
  if duration_days <= 0:
    raise ValueError("duration_days must be greater than 0")

  created_codes: list[dict] = []
  conn = None
  try:
    conn = mysql_db.get_connection()
    conn.autocommit(False)
    with conn.cursor() as cursor:
      for _ in range(quantity):
        plain_code = _generate_redemption_code_text()
        cursor.execute(
          """
          INSERT INTO redemption_codes
            (code_hash, code_prefix, batch_id, target_level, duration_days, status, expire_at, created_by)
          VALUES (%s, %s, %s, %s, %s, 0, %s, %s)
          """,
          (
            _hash_redemption_code(plain_code),
            plain_code[:8],
            batch_id,
            normalized_level,
            duration_days,
            expire_at,
            created_by,
          ),
        )
        created_codes.append(
          {
            "code": plain_code,
            "batch_id": batch_id,
            "target_level": normalized_level,
            "duration_days": duration_days,
            "expire_at": expire_at.isoformat() if expire_at else None,
          }
        )
    conn.commit()
    return created_codes
  except Exception:
    if conn:
      conn.rollback()
    raise
  finally:
    if conn:
      try:
        conn.autocommit(True)
      except Exception:
        pass
      conn.close()


def redeem_redemption_code(uid: str, redemption_code: str) -> dict:
  if not uid or not redemption_code:
    return {"code": 400, "msg": "uid and redemption_code are required", "data": None}

  conn = None
  try:
    conn = mysql_db.get_connection()
    conn.autocommit(False)
    now = datetime.now()
    code_hash = _hash_redemption_code(redemption_code)

    with conn.cursor(pymysql.cursors.DictCursor) as cursor:
      cursor.execute(
        """
        SELECT uid, status, user_level, level_end_at
        FROM user_auth
        WHERE uid=%s
        FOR UPDATE
        """,
        (uid,),
      )
      user_row = cursor.fetchone()
      if not user_row:
        conn.rollback()
        return {"code": 404, "msg": "user not found", "data": None}
      if user_row["status"] != 1:
        conn.rollback()
        return {"code": 403, "msg": "user is not active", "data": None}

      cursor.execute(
        """
        SELECT code_hash, batch_id, target_level, duration_days, status, expire_at, activated_uid, activated_at, rights_json
        FROM redemption_codes
        WHERE code_hash=%s
        FOR UPDATE
        """,
        (code_hash,),
      )
      code_row = cursor.fetchone()
      if not code_row:
        conn.rollback()
        return {"code": 404, "msg": "redemption code not found", "data": None}

      if code_row["status"] == 1:
        conn.rollback()
        return {"code": 409, "msg": "redemption code already used", "data": None}
      if code_row["status"] == 3:
        conn.rollback()
        return {"code": 403, "msg": "redemption code disabled", "data": None}
      if code_row["expire_at"] and code_row["expire_at"] <= now:
        cursor.execute(
          "UPDATE redemption_codes SET status=2 WHERE code_hash=%s AND status=0",
          (code_hash,),
        )
        conn.commit()
        return {"code": 410, "msg": "redemption code expired", "data": None}

      try:
        upgrade = resolve_level_upgrade(
          user_row.get("user_level"),
          user_row.get("level_end_at"),
          code_row.get("target_level"),
          int(code_row.get("duration_days") or 0),
          now=now,
        )
      except ValueError as e:
        conn.rollback()
        return {"code": 400, "msg": str(e), "data": None}

      cursor.execute(
        """
        UPDATE redemption_codes
        SET status=1, activated_uid=%s, activated_at=%s
        WHERE code_hash=%s
        """,
        (uid, now, code_hash),
      )
      cursor.execute(
        """
        UPDATE user_auth
        SET user_level=%s, level_end_at=%s, update_time=NOW()
        WHERE uid=%s
        """,
        (upgrade["new_user_level"], upgrade["new_level_end_at"], uid),
      )

    conn.commit()
    rights_payload = build_user_rights_payload(upgrade["new_user_level"], upgrade["new_level_end_at"], now=now)
    rights_payload["redeemed_code"] = {
      "batch_id": code_row["batch_id"],
      "target_level": normalize_user_level(code_row["target_level"]),
      "duration_days": int(code_row["duration_days"]),
      "activated_at": now.isoformat(),
      "action": upgrade["action"],
    }
    if code_row.get("rights_json"):
      rights_payload["code_rights"] = code_row["rights_json"] if isinstance(code_row["rights_json"], dict) else json.loads(code_row["rights_json"])

    return {"code": 0, "msg": "redemption success", "data": rights_payload}
  except Exception as e:
    if conn:
      conn.rollback()
    return {"code": 500, "msg": f"redeem redemption code failed: {e}", "data": None}
  finally:
    if conn:
      try:
        conn.autocommit(True)
      except Exception:
        pass
      conn.close()


def register_user_with_password(email: str, uid: str, salt: str,
                                 password_hash: str, device_list: str) -> int:
  """Insert a new user who registered with email+password (web flow)."""
  sql = """
  INSERT INTO user_auth (uid, email, salt, password_hash, device_list)
  VALUES (%s, %s, %s, %s, %s)
  """
  return mysql_db.execute(sql, (uid, email, salt, password_hash, device_list))


def get_user_password_hash(email: str) -> str | None:
  """Return stored password_hash for the given email, or None."""
  sql = "SELECT password_hash FROM user_auth WHERE email=%s AND status=1"
  row = mysql_db.query_one(sql, (email,))
  return row["password_hash"] if row else None


def get_user_by_phone(phone: str) -> "UserData | None":
  """Query user by phone number."""
  sql = ("SELECT uid, email, salt, status, device_list, register_time, update_time "
         "FROM user_auth WHERE phone=%s")
  row = mysql_db.query_one(sql, (phone,))
  return UserData.model_validate(row) if row else None


def register_phone_user(phone: str, uid: str, salt: str, device_list: str) -> int:
  """Insert a new user who registered via phone+SMS code."""
  sql = """
  INSERT INTO user_auth (uid, phone, salt, device_list)
  VALUES (%s, %s, %s, %s)
  """
  return mysql_db.execute(sql, (uid, phone, salt, device_list))


def get_or_create_wechat_user(openid: str, unionid: str | None,
                               nickname: str, avatar_url: str) -> "UserData":
  """
  Find user by wechat_openid (or wechat_unionid).
  If not found, create a new record.
  Returns the UserData of the (possibly new) user.
  """
  # Try openid first, then unionid
  for col, val in [("wechat_openid", openid), ("wechat_unionid", unionid)]:
    if not val:
      continue
    sql = ("SELECT uid, email, salt, status, device_list, register_time, update_time "
           f"FROM user_auth WHERE {col}=%s AND status=1")
    row = mysql_db.query_one(sql, (val,))
    if row:
      return UserData.model_validate(row)

  # New WeChat user — generate uid + salt
  import os as _os, hashlib as _hl
  salt = _os.urandom(16).hex()
  raw_uid = _hl.sha256((salt + openid).encode()).hexdigest()
  sql = """
  INSERT INTO user_auth (uid, salt, wechat_openid, wechat_unionid, nickname, avatar_url, device_list)
  VALUES (%s, %s, %s, %s, %s, %s, %s)
  """
  mysql_db.execute(sql, (raw_uid, salt, openid, unionid or None, nickname, avatar_url, "wechat"))
  row = mysql_db.query_one(
    "SELECT uid, email, salt, status, device_list, register_time, update_time "
    "FROM user_auth WHERE uid=%s", (raw_uid,)
  )
  return UserData.model_validate(row)


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
