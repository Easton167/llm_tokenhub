"""报表 API 请求/响应模型。

时区口径：所有 from_date / to_date / summary_date 均为 UTC 日历日，
与 usage_daily_summary.summary_date、capture 流水按 UTC 日归属一致。

收入口径：
- `revenue_amount`：计费总额（含赠金抵扣），**不是**平台收入。
- `cash_revenue_amount` / `cash_charge_amount`：平台真实收入（现金支付）。
- `gross_profit` = cash_revenue_amount - cost_amount（纯赠金消费时为负，属预期）。
"""

from datetime import date
from uuid import UUID

from pydantic import BaseModel, Field


class ReportMeta(BaseModel):
    """报表公共元信息。"""

    timezone: str = Field(default="UTC", description="日界与日期过滤时区口径")
    from_date: date
    to_date: date


class DailyMetricOut(BaseModel):
    summary_date: date
    request_count: int
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    revenue_amount: int = Field(description="计费总额（含赠金抵扣，不是平台收入）")
    cash_revenue_amount: int = Field(description="平台收入（现金口径）")
    gift_consumed: int = Field(description="赠金消耗")
    gift_charge_amount: int = Field(description="赠金消耗（用量接口命名，同 gift_consumed）")
    cash_charge_amount: int = Field(description="现金支付（用量接口命名，同 cash_revenue_amount）")
    gift_cost: int = Field(description="赠金消费对应的上游成本")
    cost_amount: int
    gross_profit: int = Field(
        description="毛利 = cash_revenue_amount - cost_amount（纯赠金时为负，属预期）"
    )


class TrendReportOut(ReportMeta):
    items: list[DailyMetricOut]


class TopItemOut(BaseModel):
    dim_id: UUID | None = Field(description="维度 ID；org 维度下个人计费行为 null")
    request_count: int
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    revenue_amount: int = Field(description="计费总额（含赠金抵扣，不是平台收入）")
    cash_revenue_amount: int
    gift_consumed: int
    gift_charge_amount: int
    cash_charge_amount: int
    gift_cost: int
    cost_amount: int
    gross_profit: int = Field(
        description="毛利 = cash_revenue_amount - cost_amount（纯赠金时为负，属预期）"
    )


class TopReportOut(ReportMeta):
    dimension: str
    items: list[TopItemOut]


class ChannelShareItemOut(BaseModel):
    channel_id: UUID | None
    request_count: int
    revenue_amount: int = Field(description="计费总额（含赠金抵扣，不是平台收入）")
    cash_revenue_amount: int
    gift_consumed: int
    gift_charge_amount: int
    cash_charge_amount: int
    gift_cost: int
    cost_amount: int
    gross_profit: int = Field(
        description="毛利 = cash_revenue_amount - cost_amount（纯赠金时为负，属预期）"
    )
    cost_share_bps: int = Field(
        description="成本占比（基点，万分之一；total_cost=0 时为 0）"
    )


class ChannelShareReportOut(ReportMeta):
    total_cost: int
    items: list[ChannelShareItemOut]


class ReconcileReportOut(ReportMeta):
    """报表计费总额与钱包 capture 流水对账；含赠金拆分校验。"""

    summary_revenue: int = Field(description="usage_daily_summary.revenue_amount 合计（计费总额）")
    capture_revenue: int = Field(description="同期 capture 流水 -sum(amount)")
    matched: bool
    usage_gift_total: int = Field(description="Σ usage_logs.gift_charge_amount")
    capture_gift_total: int = Field(description="同期赠送池 capture 流水 -sum(gift_amount)")
    gift_matched: bool = Field(description="赠金拆分是否与 capture 流水一致")
    alerts: list[str] = Field(
        default_factory=list,
        description="不一致项：revenue_mismatch / gift_split_mismatch，供前端标红",
    )


class GiftDashboardOut(ReportMeta):
    """管理端赠金看板：敞口为当前快照，发放/撤回/过期/消耗按日期范围。"""

    gift_balance_total: int = Field(description="全平台未消耗赠金敞口 gift_balance + gift_held")
    granted_amount: int = Field(description="区间内发放金额")
    revoked_amount: int = Field(description="区间内撤回金额（未消耗部分）")
    expired_amount: int = Field(description="区间内过期回收金额")
    gift_consumed: int = Field(description="区间内赠金消耗")
    gift_cost: int = Field(description="区间内赠金对应上游成本")
    granted_all_time: int = Field(description="累计发放金额")
    consumed_all_time: int = Field(description="累计赠金消耗")
    redemption_rate_bps: int = Field(
        description="核销率（基点）：累计消耗 / 累计发放，发放为 0 时为 0"
    )


class UserDailyReportOut(ReportMeta):
    items: list[DailyMetricOut]


class OrgDailyReportOut(ReportMeta):
    org_id: UUID
    items: list[DailyMetricOut]


class OrgMemberShareItemOut(BaseModel):
    user_id: UUID
    request_count: int
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    revenue_amount: int = Field(description="计费总额（含赠金抵扣，不是平台收入）")
    cash_revenue_amount: int
    gift_consumed: int
    gift_charge_amount: int
    cash_charge_amount: int
    gift_cost: int
    cost_amount: int
    gross_profit: int = Field(
        description="毛利 = cash_revenue_amount - cost_amount（纯赠金时为负，属预期）"
    )


class OrgMemberShareReportOut(ReportMeta):
    org_id: UUID
    items: list[OrgMemberShareItemOut]
