# Stripe 沙箱验证（任务 8）

本文说明如何在 Stripe **Test mode** 下验证：创建支付 → Webhook 回调 → 钱包入账，以及伪造签名拒绝与掉单对账。

## 1. 环境变量

在 `backend/.env` 配置（密钥不落库、不入代码）：

```env
STRIPE_SECRET_KEY=sk_test_xxx
STRIPE_WEBHOOK_SECRET=whsec_xxx
PAYMENT_PUBLIC_BASE_URL=https://你的公网或隧道地址
PAYMENT_ORDER_EXPIRE_MINUTES=30
PAYMENT_RECONCILE_STALE_SECONDS=300
```

| 变量 | 说明 |
|------|------|
| `STRIPE_SECRET_KEY` | Stripe Dashboard → Developers → API keys（Test） |
| `STRIPE_WEBHOOK_SECRET` | Webhook endpoint 的 Signing secret |
| `PAYMENT_PUBLIC_BASE_URL` | 后端对外根 URL，用于拼装 `success_url` / 文档中的回调地址 |
| `PAYMENT_RECONCILE_STALE_SECONDS` | 对账扫描：`created` 且创建超过该秒数的订单 |

金额注意：系统内部单位为 `MONEY_SCALE`（默认 10000 = 1 元）。Stripe CNY 以「分」计价，创建 Stripe 支付单时金额必须能整除换算到分（例如 `10000`、`20000`）。

## 2. 配置 Webhook

1. Stripe Dashboard → Developers → Webhooks → Add endpoint。
2. Endpoint URL：`{PAYMENT_PUBLIC_BASE_URL}/payments/callbacks/stripe`
3. 监听事件至少包含：
   - `checkout.session.completed`
   - `checkout.session.expired`
4. 将 Signing secret 写入 `STRIPE_WEBHOOK_SECRET`。
5. 本地开发可用 [Stripe CLI](https://stripe.com/docs/stripe-cli)：

```bash
stripe listen --forward-to localhost:8000/payments/callbacks/stripe
```

CLI 会打印 `whsec_...`，填入环境变量后重启后端。

## 3. 全链路验证（支付 → 回调 → 入账）

1. 启动后端，确保已迁移到含 `payment_orders` 的版本。
2. 用户登录后创建支付单：

```http
POST /payments
Authorization: Bearer <user_jwt>
Content-Type: application/json

{
  "amount": 10000,
  "target_type": "personal",
  "provider": "stripe",
  "return_url": "http://localhost:8088/wallet"
}
```

3. 响应中的 `pay_url` 为 Stripe Checkout 链接；用测试卡支付，例如：
   - 卡号 `4242 4242 4242 4242`
   - 任意未来有效期、任意 CVC
4. 支付成功后 Stripe 推送 webhook；后端验签通过后将订单标为 `paid` 并写入钱包 `recharge` 流水（幂等键 `payment:{order_no}`）。
5. 查询确认：

```http
GET /payments/{order_no}
GET /wallets/me
```

期望：`status=paid`，余额增加 `amount`。

## 4. 伪造签名（应拒绝且无资金变动）

```bash
curl -i -X POST "$PAYMENT_PUBLIC_BASE_URL/payments/callbacks/stripe" \
  -H "Content-Type: application/json" \
  -H "Stripe-Signature: t=1700000000,v1=forged" \
  -d '{"type":"checkout.session.completed","data":{"object":{}}}'
```

期望：HTTP **4xx**，对应支付单仍为 `created`，钱包余额不变。

自动化覆盖见：`backend/tests/test_payment_stripe.py` 中的 `test_stripe_webhook_forged_signature_rejected_no_credit`。

## 5. 回调重放（幂等）

同一条已验签成功的 `checkout.session.completed` 连续 POST 多次，钱包只入账一次。

自动化覆盖：`test_stripe_webhook_valid_and_replay_credits_once`。

## 6. 掉单对账（回调丢失）

若渠道侧已支付但 webhook 未到达，订单会长时间停留在 `created`。运行对账任务：

```bash
cd backend
python -m app.tasks.reconcile_payments --stale-seconds 60
# 或先预览
python -m app.tasks.reconcile_payments --dry-run
```

任务会向 Stripe `GET /v1/checkout/sessions/{id}` 查单：已 `paid` 则补入账；渠道 `expired` 则本地标 `expired`；仍 `pending` 则保持 `created` 等待下次扫描。

自动化覆盖：`test_payment_reconcile_recovers_lost_callback`（Mock 渠道模拟「渠道已付、回调丢失」）。

建议 cron 示例（每 5 分钟）：

```cron
*/5 * * * * cd /path/to/backend && python -m app.tasks.reconcile_payments
```

## 7. 相关代码

| 模块 | 路径 |
|------|------|
| Stripe Provider | `backend/app/payments/stripe.py` |
| Webhook | `POST /payments/callbacks/stripe` |
| 对账任务 | `backend/app/tasks/reconcile_payments.py` |
| 测试 | `backend/tests/test_payment_stripe.py` |
