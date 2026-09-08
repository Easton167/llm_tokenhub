from sqlalchemy import BigInteger, CheckConstraint, UniqueConstraint

from app.db.models import Base

EXPECTED_TABLES = {
    "users",
    "roles",
    "user_roles",
    "role_model_grants",
    "api_keys",
    "providers",
    "channels",
    "channel_keys",
    "models",
    "model_channel_map",
    "billing_rules",
    "wallets",
    "wallet_transactions",
    "request_logs",
    "usage_logs",
    "audit_logs",
}

PHASE2_ORG_TABLES = {
    "orgs",
    "org_members",
    "member_quotas",
}

PHASE2_REPORT_TABLES = {
    "channel_costs",
    "usage_daily_summary",
}

GIFT_TABLES = {
    "gift_grants",
}

MONEY_COLUMNS = {
    ("wallets", "balance"),
    ("wallets", "held"),
    ("wallets", "gift_balance"),
    ("wallets", "gift_held"),
    ("wallets", "credit_limit"),
    ("wallet_transactions", "amount"),
    ("wallet_transactions", "gift_amount"),
    ("wallet_transactions", "balance_after"),
    ("wallet_transactions", "held_after"),
    ("wallet_transactions", "gift_balance_after"),
    ("wallet_transactions", "gift_held_after"),
    ("billing_rules", "price_input"),
    ("billing_rules", "price_output"),
    ("billing_rules", "price_cached_input"),
    ("billing_rules", "price_per_call"),
    ("billing_rules", "cost_input"),
    ("billing_rules", "cost_output"),
    ("billing_rules", "min_charge_unit"),
    ("usage_logs", "cost_amount"),
    ("usage_logs", "charge_amount"),
    ("usage_logs", "gift_charge_amount"),
    ("usage_logs", "cash_charge_amount"),
    ("member_quotas", "quota_limit"),
    ("member_quotas", "quota_used"),
    ("channel_costs", "cost_input"),
    ("channel_costs", "cost_output"),
    ("usage_daily_summary", "revenue_amount"),
    ("usage_daily_summary", "gift_revenue_amount"),
    ("usage_daily_summary", "cash_revenue_amount"),
    ("usage_daily_summary", "cost_amount"),
}


def _table(name: str):
    return Base.metadata.tables[name]


def test_all_mvp_tables_registered() -> None:
    assert EXPECTED_TABLES.issubset(set(Base.metadata.tables.keys()))


def test_phase2_org_tables_registered() -> None:
    assert PHASE2_ORG_TABLES.issubset(set(Base.metadata.tables.keys()))


def test_phase2_report_tables_registered() -> None:
    assert PHASE2_REPORT_TABLES.issubset(set(Base.metadata.tables.keys()))


def test_gift_tables_registered() -> None:
    assert GIFT_TABLES.issubset(set(Base.metadata.tables.keys()))


def test_channel_costs_version_unique() -> None:
    assert "uq_channel_costs_channel_upstream_version" in {
        c.name for c in _table("channel_costs").constraints
    }


def test_usage_daily_summary_dims_unique_index() -> None:
    assert "uq_usage_daily_summary_dims" in {
        i.name for i in _table("usage_daily_summary").indexes
    }


def test_users_email_unique() -> None:
    constraints = {c.name for c in _table("users").constraints if isinstance(c, UniqueConstraint)}
    assert "users_email_key" in constraints or any(
        "email" in {col.name for col in c.columns}
        for c in _table("users").constraints
        if isinstance(c, UniqueConstraint)
    )


def test_api_keys_key_hash_unique() -> None:
    assert any(
        "key_hash" in {col.name for col in c.columns}
        for c in _table("api_keys").constraints
        if isinstance(c, UniqueConstraint)
    )


def test_models_alias_unique() -> None:
    assert any(
        "alias" in {col.name for col in c.columns}
        for c in _table("models").constraints
        if isinstance(c, UniqueConstraint)
    )


def test_wallets_owner_unique() -> None:
    assert "uq_wallets_owner" in {c.name for c in _table("wallets").constraints}


def test_wallets_owner_type_check() -> None:
    assert "ck_wallets_owner_type" in {
        c.name for c in _table("wallets").constraints if isinstance(c, CheckConstraint)
    }


def test_wallets_owner_id_has_no_users_fk() -> None:
    """组织钱包 owner_id 指向 orgs，不能再强制 FK 到 users。"""
    fks = _table("wallets").foreign_key_constraints
    assert not any("owner_id" in {col.name for col in fk.columns} for fk in fks)


def test_billing_rules_model_version_unique() -> None:
    assert "uq_billing_rules_model_version" in {
        c.name for c in _table("billing_rules").constraints
    }


def test_model_channel_map_unique() -> None:
    assert "uq_model_channel_map" in {c.name for c in _table("model_channel_map").constraints}


def test_request_logs_request_id_unique() -> None:
    request_id_col = _table("request_logs").c.request_id
    assert request_id_col.unique is True


def test_usage_logs_request_id_unique() -> None:
    request_id_col = _table("usage_logs").c.request_id
    assert request_id_col.unique is True


def test_wallet_transactions_idempotency_index() -> None:
    indexes = {idx.name: idx for idx in _table("wallet_transactions").indexes}
    assert "uq_wallet_transactions_request_type" in indexes
    idx = indexes["uq_wallet_transactions_request_type"]
    assert idx.unique is True
    assert {col.name for col in idx.columns} == {"wallet_id", "ref_request_id", "type"}


def test_money_fields_use_bigint() -> None:
    for table_name, column_name in MONEY_COLUMNS:
        column = _table(table_name).c[column_name]
        assert isinstance(column.type, BigInteger)


def test_wallet_non_negative_checks() -> None:
    check_names = {c.name for c in _table("wallets").constraints}
    assert "ck_wallets_balance_non_negative" in check_names
    assert "ck_wallets_held_non_negative" in check_names
    assert "ck_wallets_gift_balance_non_negative" in check_names
    assert "ck_wallets_gift_held_non_negative" in check_names


def test_gift_grants_constraints_and_index() -> None:
    table = _table("gift_grants")
    check_names = {c.name for c in table.constraints if isinstance(c, CheckConstraint)}
    assert "ck_gift_grants_remaining_range" in check_names
    assert "ck_gift_grants_reason" in check_names
    assert "ck_gift_grants_status" in check_names
    assert "uq_gift_grants_wallet_idempotency" in {c.name for c in table.constraints}
    assert "ix_gift_grants_wallet_status_expires" in {i.name for i in table.indexes}


def test_org_members_org_user_unique() -> None:
    assert "uq_org_members_org_user" in {
        c.name for c in _table("org_members").constraints if isinstance(c, UniqueConstraint)
    }


def test_member_quotas_org_member_unique() -> None:
    assert "uq_member_quotas_org_member" in {
        c.name for c in _table("member_quotas").constraints if isinstance(c, UniqueConstraint)
    }


def test_org_attribution_columns_nullable() -> None:
    """一期兼容：组织归属与发起成员字段可空。"""
    assert _table("request_logs").c.org_id.nullable is True
    assert _table("request_logs").c.actor_user_id.nullable is True
    assert _table("usage_logs").c.org_id.nullable is True
    assert _table("usage_logs").c.actor_user_id.nullable is True
    assert _table("wallet_transactions").c.org_id.nullable is True


def test_org_attribution_indexes() -> None:
    assert "ix_request_logs_org_created" in {i.name for i in _table("request_logs").indexes}
    assert "ix_usage_logs_org_created" in {i.name for i in _table("usage_logs").indexes}
    assert "ix_wallet_transactions_org_created" in {
        i.name for i in _table("wallet_transactions").indexes
    }
