import { useEffect, useMemo, useState } from 'react'
import { Alert, Card, Col, Row, Statistic, Table, Tag, Typography, message } from 'antd'
import {
  fetchGifts,
  fetchTransactions,
  fetchWallet,
  formatTxType,
  walletTotalAvailable,
  type GiftGrantPublic,
  type Wallet,
  type WalletTransaction,
} from '../api/wallet'
import { ApiError } from '../api/client'
import { useAuth } from '../auth/AuthContext'
import { formatDateTime } from '../utils/datetime'
import {
  GIFT_EXPIRY_WARN_DAYS,
  giftExpiresWithinDays,
  pickNearestExpiringGrant,
} from '../utils/giftExpiry'
import { formatMoney } from '../utils/money'

export function WalletPage() {
  const { token } = useAuth()
  const [wallet, setWallet] = useState<Wallet | null>(null)
  const [txs, setTxs] = useState<WalletTransaction[]>([])
  const [grants, setGrants] = useState<GiftGrantPublic[]>([])
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    if (!token) return
    setLoading(true)
    Promise.all([fetchWallet(token), fetchTransactions(token), fetchGifts(token)])
      .then(([w, transactions, gifts]) => {
        setWallet(w)
        setTxs(transactions)
        setGrants(gifts)
      })
      .catch((err) => {
        message.error(err instanceof ApiError ? err.message : '加载失败')
      })
      .finally(() => setLoading(false))
  }, [token])

  const currency = wallet?.currency || 'CNY'
  const cashBalance = wallet?.balance ?? 0
  const giftBalance = wallet?.gift_balance ?? 0
  const totalAvailable = walletTotalAvailable(wallet)
  const nearest = useMemo(() => pickNearestExpiringGrant(grants), [grants])
  const expiringSoon = useMemo(
    () => grants.filter((g) => giftExpiresWithinDays(g.expires_at)),
    [grants],
  )

  return (
    <div>
      <Typography.Title level={3} style={{ marginBottom: 4 }}>
        个人钱包
      </Typography.Title>
      <Typography.Paragraph type="secondary">
        查看充值余额、赠送余额与流水。在线充值请前往「在线充值」页（入账到充值余额）。
        赠送余额由平台发放，不可退款、不可提现。
      </Typography.Paragraph>
      <Row gutter={16} style={{ marginBottom: 24 }}>
        <Col xs={24} md={6}>
          <Card loading={loading}>
            <Statistic title="充值余额" value={formatMoney(cashBalance)} suffix={currency} />
          </Card>
        </Col>
        <Col xs={24} md={6}>
          <Card loading={loading}>
            <Statistic title="赠送余额" value={formatMoney(giftBalance)} suffix={currency} />
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              不可退款、不可提现
            </Typography.Text>
          </Card>
        </Col>
        <Col xs={24} md={6}>
          <Card loading={loading}>
            <Statistic title="总可用额" value={formatMoney(totalAvailable)} suffix={currency} />
          </Card>
        </Col>
        <Col xs={24} md={6}>
          <Card loading={loading}>
            <Statistic
              title="冻结中"
              value={formatMoney((wallet?.held ?? 0) + (wallet?.gift_held ?? 0))}
              suffix={currency}
            />
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              现金 {formatMoney(wallet?.held ?? 0)} / 赠送 {formatMoney(wallet?.gift_held ?? 0)}
            </Typography.Text>
          </Card>
        </Col>
      </Row>
      {expiringSoon.length > 0 && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 16 }}
          message={`有 ${expiringSoon.length} 笔赠送批次将于 ${GIFT_EXPIRY_WARN_DAYS} 天内到期，请尽快使用`}
          description="赠送余额到期将作废，且不可退款、不可提现。"
        />
      )}
      <Card title="赠送批次" style={{ marginBottom: 16 }}>
        <Table
          rowKey="id"
          loading={loading}
          dataSource={grants}
          pagination={false}
          onRow={(row) => ({
            style: nearest && row.id === nearest.id ? { background: '#fff7e6' } : undefined,
          })}
          locale={{ emptyText: '暂无有效赠送批次' }}
          columns={[
            {
              title: '金额',
              dataIndex: 'amount',
              width: 120,
              render: (v: number) => formatMoney(v),
            },
            {
              title: '剩余',
              dataIndex: 'remaining',
              width: 120,
              render: (v: number) => formatMoney(v),
            },
            {
              title: '到期时间',
              dataIndex: 'expires_at',
              render: (v: string | null, row: GiftGrantPublic) => {
                const isNearest = nearest?.id === row.id
                const soon = giftExpiresWithinDays(v)
                return (
                  <span>
                    {v ? formatDateTime(v) : '永久有效'}
                    {isNearest && v ? (
                      <Tag color="gold" style={{ marginLeft: 8 }}>
                        最近到期
                      </Tag>
                    ) : null}
                    {soon ? (
                      <Tag color="orange" style={{ marginLeft: 8 }}>
                        {GIFT_EXPIRY_WARN_DAYS} 天内到期
                      </Tag>
                    ) : null}
                  </span>
                )
              },
            },
          ]}
        />
      </Card>
      <Card title="流水">
        <Table
          rowKey="id"
          loading={loading}
          dataSource={txs}
          pagination={{ pageSize: 20 }}
          columns={[
            {
              title: '类型',
              dataIndex: 'type',
              width: 110,
              render: (v: string) => {
                const m = formatTxType(v)
                return <Tag color={m.color}>{m.text}</Tag>
              },
            },
            {
              title: '金额',
              dataIndex: 'amount',
              render: (v: number, row: WalletTransaction) => (
                <span>
                  {formatMoney(v)}
                  {row.gift_amount ? (
                    <Typography.Text type="secondary" style={{ display: 'block', fontSize: 12 }}>
                      含赠金 {formatMoney(Math.abs(row.gift_amount))}
                    </Typography.Text>
                  ) : null}
                </span>
              ),
            },
            {
              title: '充值余额后',
              dataIndex: 'balance_after',
              render: (v: number) => formatMoney(v),
            },
            { title: '请求 ID', dataIndex: 'ref_request_id', ellipsis: true },
            { title: '备注', dataIndex: 'remark', ellipsis: true },
            {
              title: '时间',
              dataIndex: 'created_at',
              render: (v: string) => formatDateTime(v),
            },
          ]}
        />
      </Card>
    </div>
  )
}
