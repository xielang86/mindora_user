-- init_auth_db.sql 示例：创建用户认证表+插入测试数据
create database mindora_db;
USE mindora_db;

CREATE TABLE `user_auth` (
  `uid` VARCHAR(64) NOT NULL COMMENT '用户唯一标识（SHA256生成的64位字符串）',
  `email` VARCHAR(128) NOT NULL COMMENT '用户邮箱（标准化后存储）',
  `salt` VARCHAR(32) NOT NULL COMMENT '生成UID的加盐值（32位十六进制字符串）',
  `status` TINYINT(1) NOT NULL DEFAULT 1 COMMENT '用户状态：1-正常，0-禁用',
  `user_level` VARCHAR(32) NOT NULL DEFAULT 'free' COMMENT '用户等级：free/pro/premium',
  `level_end_at` DATETIME NULL DEFAULT NULL COMMENT '会员等级截止时间',
  `device_list` VARCHAR(256) NOT NULL,
  `register_time` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '注册时间（自动填充当前时间）',
  `update_time` DATETIME NULL DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间（自动更新）',
  -- 主键+唯一索引（防止邮箱重复注册）
  PRIMARY KEY (`uid`),
  UNIQUE KEY `idx_unique_email` (`email`) USING BTREE,
  -- 可选：添加时间索引（优化按注册时间查询）
  KEY `idx_register_time` (`register_time`) USING BTREE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='用户认证核心表';
INSERT INTO user_auth (uid, email, salt, status, device_list) 
VALUES ('f47ac10b58cc4372a5670e02b2c3d479', 'test@example.com', '8a9d7c6b5e4f3a2b1c0d9e8f7g6h5j4k', 1, "xxx-xxx-xxx-xxx");

CREATE TABLE `redemption_codes` (
  `code_hash` VARCHAR(64) NOT NULL COMMENT '兑换码SHA256哈希，数据库不直接存明文码',
  `code_prefix` VARCHAR(24) NOT NULL COMMENT '明文码前缀，便于人工排查',
  `batch_id` VARCHAR(64) NOT NULL COMMENT '兑换码批次号',
  `target_level` VARCHAR(32) NOT NULL DEFAULT 'pro' COMMENT '兑换后目标等级',
  `duration_days` INT NOT NULL COMMENT '兑换成功后增加的有效天数',
  `status` TINYINT(1) NOT NULL DEFAULT 0 COMMENT '0-未使用，1-已兑换，2-已过期，3-已禁用',
  `expire_at` DATETIME NULL DEFAULT NULL COMMENT '兑换码自身过期时间',
  `activated_uid` VARCHAR(64) NULL DEFAULT NULL COMMENT '实际兑换的用户UID',
  `activated_at` DATETIME NULL DEFAULT NULL COMMENT '兑换时间',
  `rights_json` JSON NULL COMMENT '预留扩展权益',
  `created_by` VARCHAR(64) NULL DEFAULT NULL COMMENT '创建人',
  `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  PRIMARY KEY (`code_hash`),
  KEY `idx_redemption_batch` (`batch_id`),
  KEY `idx_redemption_uid` (`activated_uid`),
  KEY `idx_redemption_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='兑换码表';

-- If user_auth already exists in production, run this migration instead:
-- ALTER TABLE `user_auth`
--   ADD COLUMN `user_level` VARCHAR(32) NOT NULL DEFAULT 'free' COMMENT '用户等级：free/pro/premium',
--   ADD COLUMN `level_end_at` DATETIME NULL DEFAULT NULL COMMENT '会员等级截止时间';
