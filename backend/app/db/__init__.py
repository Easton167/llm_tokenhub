"""异步 SQLAlchemy：引擎/会话在 ``session.py``，声明式基类在 ``base.py``，表在 ``models/``。

金额列一律 BigInteger（最小货币单位）。时间列一律 timestamptz。
主键应用层 UUID，不依赖数据库序列（方便请求内先生成再关联日志）。
"""
