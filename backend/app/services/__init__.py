"""业务服务层：无 HTTP、不自行 commit，由路由 / 任务传入 AsyncSession。

网关主链路会碰到的单例（均为模块末尾 ``xxx_service = XxxService()``）：

- ``auth_service``：校验平台 API Key，返回 AuthContext
- ``permission_service``：角色授权 ∩ Key.allowed_models
- ``rate_limit_service``：Key RPM/TPM；Redis 滑动窗口，失败降级内存
- ``billing_subject_service``：决定扣哪只钱包（个人 / 组织 / 回退）
- ``billing_service``：按 BillingRule 算 hold / capture 金额（整数 + Decimal）
- ``wallet_service``：行锁 + 赠送优先拆分 + request_id 幂等
- ``routing_service``：模型 → 渠道 → Key，加权随机 + 熔断/限额过滤
- ``quota_service``：组织成员额度；惰性周期重置
- ``usage_service``：只追加 request_log / usage_log
"""
