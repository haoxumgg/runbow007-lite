from datetime import datetime
from zoneinfo import ZoneInfo

from runbow007.config import RulesConfig
from runbow007.rules import RuleEngine


def test_r1_detects_missing_departure_after_wms_timeout(make_order):
    engine = RuleEngine(RulesConfig())
    order = make_order(
        departed_at=None,
        wms_posted_at=datetime(2026, 8, 5, 9, 0),
    )
    candidates = engine.evaluate(
        [order], now=datetime(2026, 8, 5, 10, 31), rule_codes=["R1"]
    )
    assert len(candidates) == 1
    assert candidates[0].scenario == "departure_missing_overdue"
    assert candidates[0].reason == "WMS过账已 91.0 分钟，仍无离厂时间"
    assert candidates[0].event_key == "R1|C001|2026-08-05T09:00:00"


def test_all_rule_event_keys_keep_using_internal_order_number(make_order):
    engine = RuleEngine(RulesConfig())
    now = datetime(2026, 8, 6, 13, 30)
    timestamp = datetime(2026, 8, 6, 10, 0)
    orders = [
        make_order(
            order_no="INTERNAL-R1",
            related_order_no="RELATED-R1",
            departed_at=None,
            wms_posted_at=datetime(2026, 8, 6, 10, 0),
        ),
        make_order(
            order_no="INTERNAL-R2",
            related_order_no="RELATED-R2",
            expected_arrival_at=timestamp,
            actual_arrival_at=datetime(2026, 8, 7, 10, 0),
        ),
        make_order(
            order_no="INTERNAL-R3",
            related_order_no="RELATED-R3",
            transport_status="已签收",
            contract_status="签署中",
            actual_arrival_at=timestamp,
            signed_at=None,
        ),
        make_order(
            order_no="INTERNAL-R4",
            related_order_no="RELATED-R4",
            is_delayed=True,
            delay_reason=None,
        ),
    ]

    candidates = engine.evaluate(
        orders,
        now=now,
        rule_codes=["R1", "R2", "R3", "R4"],
    )
    keys_by_rule = {
        candidate.rule_code: candidate.event_key
        for candidate in candidates
        if candidate.order.order_no.endswith(candidate.rule_code)
    }

    assert keys_by_rule == {
        "R1": "R1|INTERNAL-R1|2026-08-06T10:00:00",
        "R2": "R2|INTERNAL-R2|2026-08-06",
        "R3": "R3|unsigned|INTERNAL-R3",
        "R4": "R4|INTERNAL-R4",
    }
    assert all("RELATED" not in event_key for event_key in keys_by_rule.values())


def test_r1_uses_strict_timeout_and_requires_missing_departure(make_order):
    engine = RuleEngine(RulesConfig(wms_lead_minutes=90))
    boundary = make_order(
        order_no="C001", departed_at=None, wms_posted_at=datetime(2026, 8, 5, 9, 0)
    )
    departed = make_order(
        order_no="C002", departed_at=datetime(2026, 8, 5, 10, 0)
    )
    missing_wms = make_order(order_no="C003", departed_at=None, wms_posted_at=None)
    future_wms = make_order(
        order_no="C004", departed_at=None, wms_posted_at=datetime(2026, 8, 5, 11, 0)
    )
    assert not engine.evaluate(
        [boundary, departed, missing_wms, future_wms],
        now=datetime(2026, 8, 5, 10, 30),
        rule_codes=["R1"],
    )


def test_r1_compares_aware_local_now_with_naive_tms_timestamp(make_order):
    engine = RuleEngine(RulesConfig())
    order = make_order(
        departed_at=None,
        wms_posted_at=datetime(2026, 8, 5, 9, 0),
    )
    candidates = engine.evaluate(
        [order],
        now=datetime(2026, 8, 5, 10, 31, tzinfo=ZoneInfo("Asia/Shanghai")),
        rule_codes=["R1"],
    )
    assert len(candidates) == 1


def test_r2_uses_expected_arrival_today_and_requires_in_transit_status(make_order):
    engine = RuleEngine(RulesConfig())
    order = make_order(
        expected_arrival_at=datetime(2026, 8, 6, 22, 0),
        actual_arrival_at=datetime(2026, 8, 10, 18, 0),
        transport_status="运输在途（已离厂）",
    )
    candidates = engine.evaluate(
        [order], now=datetime(2026, 8, 6, 13, 30), rule_codes=["R2"]
    )
    assert len(candidates) == 1
    assert candidates[0].reason == "预计到达日期为今天但运输状态仍为在途"


def test_r2_rejects_missing_other_day_or_non_transit_expected_arrival(make_order):
    engine = RuleEngine(RulesConfig())
    actual_today = datetime(2026, 8, 6, 12, 0)
    missing = make_order(
        order_no="C001",
        expected_arrival_at=None,
        actual_arrival_at=actual_today,
    )
    other_day = make_order(
        order_no="C002",
        expected_arrival_at=datetime(2026, 8, 5, 23, 59),
        actual_arrival_at=actual_today,
    )
    signed = make_order(
        order_no="C003",
        expected_arrival_at=datetime(2026, 8, 6, 0, 1),
        transport_status="已签收",
    )

    assert not engine.evaluate(
        [missing, other_day, signed],
        now=datetime(2026, 8, 6, 13, 30),
        rule_codes=["R2"],
    )


def test_r3_detects_both_scenarios(make_order):
    engine = RuleEngine(RulesConfig())
    timestamp = datetime(2026, 8, 5, 18, 30)
    unsigned = make_order(
        transport_status="已签收",
        contract_status="签署中",
        actual_arrival_at=timestamp,
        signed_at=None,
    )
    pending = make_order(
        order_no="C002",
        transport_status="运输在途（已离厂）",
        contract_status="已完成",
        actual_arrival_at=None,
        signed_at=timestamp,
    )
    candidates = engine.evaluate(
        [unsigned, pending], now=datetime(2026, 8, 6), rule_codes=["R3"]
    )
    assert {item.scenario for item in candidates} == {"customer_unsigned", "operation_pending"}
    assert {item.reason for item in candidates} == {
        "实际到达时间不为空，订单已签收但合同仍在签署中",
        "签收时间不为空，合同已完成但运输状态仍为运输在途（已离厂）",
    }


def test_r3_requires_each_scenarios_own_time_and_exact_field_values(make_order):
    engine = RuleEngine(RulesConfig())
    missing_actual_arrival = make_order(
        order_no="C001",
        transport_status="已签收",
        contract_status="签署中",
        actual_arrival_at=None,
        signed_at=datetime(2026, 8, 5, 18, 30),
    )
    missing_signed = make_order(
        order_no="C002",
        transport_status="运输在途（已离厂）",
        contract_status="已完成",
        actual_arrival_at=datetime(2026, 8, 5, 18, 30),
        signed_at=None,
    )
    legacy_transit_status = make_order(
        order_no="C003",
        transport_status="运输在途",
        contract_status="已完成",
        signed_at=datetime(2026, 8, 5, 18, 30),
    )
    wrong_unsigned_status = make_order(
        order_no="C004",
        transport_status="运输在途（已离厂）",
        contract_status="签署中",
        actual_arrival_at=datetime(2026, 8, 5, 18, 30),
        signed_at=None,
    )
    wrong_unsigned_contract = make_order(
        order_no="C005",
        transport_status="已签收",
        contract_status="已完成",
        actual_arrival_at=datetime(2026, 8, 5, 18, 30),
        signed_at=None,
    )
    wrong_pending_contract = make_order(
        order_no="C006",
        transport_status="运输在途（已离厂）",
        contract_status="签署中",
        actual_arrival_at=None,
        signed_at=datetime(2026, 8, 5, 18, 30),
    )

    assert not engine.evaluate(
        [
            missing_actual_arrival,
            missing_signed,
            legacy_transit_status,
            wrong_unsigned_status,
            wrong_unsigned_contract,
            wrong_pending_contract,
        ],
        now=datetime(2026, 8, 6),
        rule_codes=["R3"],
    )


def test_r2_accepts_legacy_in_transit_alias_but_r3_requires_departed_status(make_order):
    engine = RuleEngine(RulesConfig())
    timestamp = datetime(2026, 8, 6, 18, 30)
    r2_order = make_order(
        order_no="R2-LEGACY",
        transport_status="运输在途",
        expected_arrival_at=timestamp,
    )
    r3_order = make_order(
        order_no="R3-LEGACY",
        transport_status="运输在途",
        contract_status="已完成",
        expected_arrival_at=timestamp,
        signed_at=timestamp,
    )

    candidates = engine.evaluate(
        [r2_order, r3_order],
        now=datetime(2026, 8, 6, 20, 0),
        rule_codes=["R2", "R3"],
    )

    assert {(item.rule_code, item.order.order_no) for item in candidates} == {
        ("R2", "R2-LEGACY"),
        ("R2", "R3-LEGACY"),
    }


def test_r4_requires_delayed_yes_and_empty_reason(make_order):
    engine = RuleEngine(RulesConfig())
    matching = make_order(order_no="C001", is_delayed=True, delay_reason="   ")
    not_delayed = make_order(order_no="C002", is_delayed=False, delay_reason=None)
    has_reason = make_order(order_no="C003", is_delayed=True, delay_reason="天气原因")
    candidates = engine.evaluate(
        [matching, not_delayed, has_reason],
        now=datetime(2026, 8, 6),
        rule_codes=["R4"],
    )
    assert len(candidates) == 1
    assert candidates[0].order.order_no == "C001"
    assert candidates[0].reason == "是否延迟为是且延迟原因为空"
