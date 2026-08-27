from __future__ import annotations

import sqlite3

import pytest

from runbow007.pipeline import Pipeline


def _rows(database_path, query):
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute(query).fetchall()


def test_pipeline_dry_run_archives_and_persists_real_workbook(
    tmp_path, app_config, make_order, write_orders_xlsx
):
    source = write_orders_xlsx(
        tmp_path / "orders.xlsx",
        [make_order(order_no="D001", is_delayed=True, delay_reason=None)],
    )

    result = Pipeline(app_config).process_file(source, rule_codes=["R4"])

    assert result.row_count == 1
    assert result.candidate_count == 1
    assert result.sent_count == 0
    assert result.dry_run is True
    assert result.source_file.parent.parent == app_config.runtime.downloads_dir
    assert result.source_file.read_bytes() == source.read_bytes()
    run = _rows(app_config.runtime.database_path, "SELECT * FROM runs")[0]
    assert (run["status"], run["row_count"], run["candidate_count"]) == ("success", 1, 1)
    assert _rows(app_config.runtime.database_path, "SELECT state FROM reminder_events")[0][
        "state"
    ] == "open"
    assert not _rows(app_config.runtime.database_path, "SELECT * FROM deliveries")


def test_pipeline_sends_single_message_and_deduplicates_same_day(
    tmp_path, app_config, make_order, write_orders_xlsx, monkeypatch
):
    source = write_orders_xlsx(
        tmp_path / "orders.xlsx",
        [
            make_order(order_no=f"D00{index}", is_delayed=True, delay_reason=None)
            for index in range(1, 4)
        ],
    )
    sent_messages = []

    class FakeClient:
        def __init__(self, config, *, app_secret):
            assert config.chat_id == "test-chat"
            assert app_secret == "secret"

        def send(self, message):
            sent_messages.append(message)
            return f"om_{len(sent_messages)}"

    monkeypatch.setattr("runbow007.pipeline.get_feishu_secret", lambda app_id: "secret")
    monkeypatch.setattr("runbow007.pipeline.FeishuClient", FakeClient)
    pipeline = Pipeline(app_config)

    first = pipeline.process_file(source, rule_codes=["R4"], send=True)
    second = pipeline.process_file(source, rule_codes=["R4"], send=True)

    assert first.sent_count == 3
    assert second.sent_count == 0
    assert len(sent_messages) == 1
    assert sent_messages[0].title == "R4订单提醒汇总"
    assert len(sent_messages[0].content) == 8
    assert sent_messages[0].content[1][0]["text"] == "【R4｜延迟无原因提醒】"
    assert sent_messages[0].content[2][0]["text"] == "综合统计：共 3 个订单。"
    deliveries = _rows(
        app_config.runtime.database_path,
        "SELECT status, message_id FROM deliveries ORDER BY id",
    )
    assert len(deliveries) == 3
    assert {row["status"] for row in deliveries} == {"sent"}
    assert [row["message_id"] for row in deliveries] == ["om_1", "om_1", "om_1"]


def test_pipeline_reports_current_and_new_candidates_separately(
    tmp_path, app_config, make_order, write_orders_xlsx, monkeypatch
):
    first_source = write_orders_xlsx(
        tmp_path / "first.xlsx",
        [make_order(order_no="D001", is_delayed=True, delay_reason=None)],
    )
    second_source = write_orders_xlsx(
        tmp_path / "second.xlsx",
        [
            make_order(order_no="D001", is_delayed=True, delay_reason=None),
            make_order(order_no="D002", is_delayed=True, delay_reason=None),
        ],
    )
    sent_messages = []

    class FakeClient:
        def __init__(self, config, *, app_secret):
            pass

        def send(self, message):
            sent_messages.append(message)
            return f"om_{len(sent_messages)}"

    monkeypatch.setattr("runbow007.pipeline.get_feishu_secret", lambda app_id: "secret")
    monkeypatch.setattr("runbow007.pipeline.FeishuClient", FakeClient)
    pipeline = Pipeline(app_config)

    pipeline.process_file(first_source, rule_codes=["R4"], send=True)
    result = pipeline.process_file(second_source, rule_codes=["R4"], send=True)

    lines = [line[0]["text"] for line in sent_messages[-1].content]
    assert result.candidate_count == 2
    assert result.sent_count == 1
    assert (
        "当前符合条件共 2 个订单；以下为本轮新增或到期重提醒的 1 个订单，"
        "另有 1 个此前已提醒。"
    ) in lines
    assert "- 相关单号 D002" in lines
    assert "- 相关单号 D001" not in lines


def test_pipeline_send_all_current_repeats_the_full_current_result(
    tmp_path, app_config, make_order, write_orders_xlsx, monkeypatch
):
    source = write_orders_xlsx(
        tmp_path / "orders.xlsx",
        [
            make_order(
                order_no=f"INTERNAL-{index}",
                related_order_no=f"RELATED-{index}",
                is_delayed=True,
                delay_reason=None,
            )
            for index in range(1, 4)
        ],
    )
    sent_messages = []

    class FakeClient:
        def __init__(self, config, *, app_secret):
            pass

        def send(self, message):
            sent_messages.append(message)
            return f"om_{len(sent_messages)}"

    monkeypatch.setattr("runbow007.pipeline.get_feishu_secret", lambda app_id: "secret")
    monkeypatch.setattr("runbow007.pipeline.FeishuClient", FakeClient)
    pipeline = Pipeline(app_config)

    first = pipeline.process_file(
        source, rule_codes=["R4"], send=True, send_all_current=True
    )
    second = pipeline.process_file(
        source, rule_codes=["R4"], send=True, send_all_current=True
    )

    assert first.sent_count == second.sent_count == 3
    assert len(sent_messages) == 2
    for message in sent_messages:
        lines = [line[0]["text"] for line in message.content]
        assert "此前已提醒" not in "\n".join(lines)
        for index in range(1, 4):
            assert f"- 相关单号 RELATED-{index}" in lines


def test_pipeline_send_all_current_does_not_send_an_empty_message(
    tmp_path, app_config, make_order, write_orders_xlsx, monkeypatch
):
    source = write_orders_xlsx(
        tmp_path / "no-match.xlsx",
        [make_order(order_no="NO-R4", related_order_no="RELATED-NO-R4")],
    )

    class UnexpectedClient:
        def __init__(self, config, *, app_secret):
            raise AssertionError("没有规则命中时不应创建飞书客户端")

    monkeypatch.setattr("runbow007.pipeline.FeishuClient", UnexpectedClient)

    result = Pipeline(app_config).process_file(
        source, rule_codes=["R4"], send=True, send_all_current=True
    )

    assert result.candidate_count == 0
    assert result.sent_count == 0


def test_pipeline_force_send_repeats_current_candidates_for_acceptance(
    tmp_path, app_config, make_order, write_orders_xlsx, monkeypatch
):
    source = write_orders_xlsx(
        tmp_path / "orders.xlsx",
        [make_order(order_no="FORCE001", is_delayed=True, delay_reason=None)],
    )
    sent_messages = []

    class FakeClient:
        def __init__(self, config, *, app_secret):
            pass

        def send(self, message):
            sent_messages.append(message)
            return f"om_{len(sent_messages)}"

    monkeypatch.setattr("runbow007.pipeline.get_feishu_secret", lambda app_id: "secret")
    monkeypatch.setattr("runbow007.pipeline.FeishuClient", FakeClient)
    pipeline = Pipeline(app_config)

    first = pipeline.process_file(source, rule_codes=["R4"], send=True)
    second = pipeline.process_file(
        source, rule_codes=["R4"], send=True, force_send=True
    )

    assert first.sent_count == 1
    assert second.sent_count == 1
    assert len(sent_messages) == 2


def test_pipeline_force_send_delivers_empty_acceptance_summary(
    tmp_path, app_config, make_order, write_orders_xlsx, monkeypatch
):
    source = write_orders_xlsx(
        tmp_path / "empty-summary.xlsx",
        [make_order(order_no="NO-R4", is_delayed=False, delay_reason=None)],
    )
    sent_messages = []

    class FakeClient:
        def __init__(self, config, *, app_secret):
            pass

        def send(self, message):
            sent_messages.append(message)
            return "om_empty"

    monkeypatch.setattr("runbow007.pipeline.get_feishu_secret", lambda app_id: "secret")
    monkeypatch.setattr("runbow007.pipeline.FeishuClient", FakeClient)

    result = Pipeline(app_config).process_file(
        source,
        rule_codes=["R4"],
        send=True,
        force_send=True,
    )

    assert result.candidate_count == 0
    assert result.sent_count == 0
    assert len(sent_messages) == 1
    assert sent_messages[0].content[-1][0]["text"] == "无符合条件订单。"
    assert not _rows(app_config.runtime.database_path, "SELECT * FROM deliveries")


def test_pipeline_rejects_force_send_in_dry_run(app_config):
    with pytest.raises(ValueError, match="强制发送只能与真实发送同时启用"):
        Pipeline(app_config).process_file(
            "missing.xlsx", rule_codes=["R4"], send=False, force_send=True
        )


def test_pipeline_rejects_send_all_current_in_dry_run(app_config):
    with pytest.raises(ValueError, match="全量发送只能与真实发送同时启用"):
        Pipeline(app_config).process_file(
            "missing.xlsx", rule_codes=["R4"], send_all_current=True
        )


def test_pipeline_limited_send_is_stable_and_never_moves_to_later_orders(
    tmp_path, app_config, make_order, write_orders_xlsx, monkeypatch
):
    source = write_orders_xlsx(
        tmp_path / "orders.xlsx",
        [
            make_order(order_no=f"L00{index}", is_delayed=True, delay_reason=None)
            for index in range(1, 5)
        ],
    )
    sent_messages = []

    class FakeClient:
        def __init__(self, config, *, app_secret):
            pass

        def send(self, message):
            sent_messages.append(message)
            return "om_limited"

    monkeypatch.setattr("runbow007.pipeline.get_feishu_secret", lambda app_id: "secret")
    monkeypatch.setattr("runbow007.pipeline.FeishuClient", FakeClient)
    pipeline = Pipeline(app_config)

    first = pipeline.process_file(
        source, rule_codes=["R4"], send=True, max_send_orders=3
    )
    second = pipeline.process_file(
        source, rule_codes=["R4"], send=True, max_send_orders=3
    )

    assert first.sent_count == 3
    assert second.sent_count == 0
    assert "此前已提醒" not in "\n".join(
        line[0]["text"] for line in sent_messages[0].content
    )
    deliveries = _rows(
        app_config.runtime.database_path,
        """
        SELECT DISTINCT reminder_events.order_no
        FROM deliveries
        JOIN reminder_events USING(event_key)
        WHERE deliveries.status = 'sent'
        ORDER BY reminder_events.order_no
        """,
    )
    assert [row["order_no"] for row in deliveries] == ["L001", "L002", "L003"]


@pytest.mark.parametrize("limit", [0, 6])
def test_pipeline_rejects_unsafe_send_limits(app_config, limit):
    with pytest.raises(ValueError, match="1–5"):
        Pipeline(app_config).process_file(
            "missing.xlsx", rule_codes=["R4"], send=True, max_send_orders=limit
        )


def test_pipeline_rejects_send_limit_in_dry_run(app_config):
    with pytest.raises(ValueError, match="真实发送"):
        Pipeline(app_config).process_file(
            "missing.xlsx", rule_codes=["R4"], send=False, max_send_orders=3
        )


def test_pipeline_records_failed_delivery_and_failed_run(
    tmp_path, app_config, make_order, write_orders_xlsx, monkeypatch
):
    source = write_orders_xlsx(
        tmp_path / "orders.xlsx",
        [
            make_order(order_no=f"F00{index}", is_delayed=True, delay_reason=None)
            for index in range(1, 4)
        ],
    )

    class FailingClient:
        def __init__(self, config, *, app_secret):
            pass

        def send(self, message):
            raise RuntimeError("simulated Feishu outage")

    monkeypatch.setattr("runbow007.pipeline.get_feishu_secret", lambda app_id: "secret")
    monkeypatch.setattr("runbow007.pipeline.FeishuClient", FailingClient)

    with pytest.raises(RuntimeError, match="simulated Feishu outage"):
        Pipeline(app_config).process_file(source, rule_codes=["R4"], send=True)

    failed_run = _rows(app_config.runtime.database_path, "SELECT * FROM runs")[0]
    assert failed_run["status"] == "failed"
    assert "simulated Feishu outage" in failed_run["error"]
    deliveries = _rows(app_config.runtime.database_path, "SELECT * FROM deliveries")
    assert len(deliveries) == 3
    assert {row["status"] for row in deliveries} == {"failed"}


def test_pipeline_allows_a_large_drop_in_row_count(
    tmp_path, app_config, make_order, write_orders_xlsx
):
    pipeline = Pipeline(app_config)
    full = write_orders_xlsx(
        tmp_path / "full.xlsx",
        [make_order(order_no=f"F{index:04d}") for index in range(200)],
    )
    pipeline.process_file(full, rule_codes=["R4"])

    tiny = write_orders_xlsx(tmp_path / "tiny.xlsx", [make_order(order_no="T001")])
    result = pipeline.process_file(tiny, rule_codes=["R4"])

    assert result.row_count == 1
    runs = _rows(
        app_config.runtime.database_path,
        "SELECT status, row_count FROM runs ORDER BY started_at",
    )
    assert [(row["status"], row["row_count"]) for row in runs] == [
        ("success", 200),
        ("success", 1),
    ]


def test_pipeline_max_row_count_guard_can_be_disabled(
    tmp_path, app_config, make_order, write_orders_xlsx
):
    app_config.rules.max_row_count = 0
    pipeline = Pipeline(app_config)
    source = write_orders_xlsx(
        tmp_path / "large.xlsx",
        [make_order(order_no=f"F{index:04d}") for index in range(600)],
    )

    assert pipeline.process_file(source, rule_codes=["R4"]).row_count == 600


def test_pipeline_rejects_a_suspiciously_large_export(
    tmp_path, app_config, make_order, write_orders_xlsx
):
    """超过固定上限的附件会在写库前被拒绝。"""
    app_config.rules.max_row_count = 500
    pipeline = Pipeline(app_config)
    normal = write_orders_xlsx(
        tmp_path / "normal.xlsx",
        [make_order(order_no=f"N{index:04d}") for index in range(200)],
    )
    pipeline.process_file(normal, rule_codes=["R4"])

    huge = write_orders_xlsx(
        tmp_path / "huge.xlsx",
        [make_order(order_no=f"H{index:04d}") for index in range(600)],
    )
    with pytest.raises(ValueError, match="超过单次处理上限 500 行"):
        pipeline.process_file(huge, rule_codes=["R4"])


def test_pipeline_row_limit_counts_duplicate_source_rows(
    tmp_path, app_config, make_order, write_orders_xlsx
):
    app_config.rules.max_row_count = 1
    order = make_order(order_no="DUP001")
    source = write_orders_xlsx(tmp_path / "duplicate.xlsx", [order, order])

    with pytest.raises(
        ValueError,
        match=r"2 个非空数据行.*超过单次处理上限 1 行",
    ):
        Pipeline(app_config).process_file(source, rule_codes=["R4"])


def test_pipeline_uses_the_last_duplicate_order_without_duplicate_candidates(
    tmp_path, app_config, make_order, write_orders_xlsx
):
    first = make_order(order_no="DUP001", is_delayed=False)
    last = make_order(order_no="DUP001", is_delayed=True, delay_reason=None)
    source = write_orders_xlsx(tmp_path / "duplicate.xlsx", [first, last])

    result = Pipeline(app_config).process_file(source, rule_codes=["R4"])

    assert result.row_count == 1
    assert result.candidate_count == 1
    assert result.rule_counts == (("R4", 1),)


def test_pipeline_allows_growth_below_the_absolute_row_limit(
    tmp_path, app_config, make_order, write_orders_xlsx
):
    """上限不再按历史行数放大，200 行历史不能把 600 行附件误拦截。"""
    app_config.rules.max_row_count = 1_000
    pipeline = Pipeline(app_config)
    pipeline.process_file(
        write_orders_xlsx(
            tmp_path / "normal.xlsx",
            [make_order(order_no=f"N{i:04d}") for i in range(200)],
        ),
        rule_codes=["R4"],
    )
    larger = write_orders_xlsx(
        tmp_path / "larger.xlsx",
        [make_order(order_no=f"L{i:04d}") for i in range(600)],
    )

    assert pipeline.process_file(larger, rule_codes=["R4"]).row_count == 600


def test_pipeline_accepts_20_000_rows_and_rejects_20_001(app_config):
    pipeline = Pipeline(app_config)

    pipeline._guard_max_row_count(20_000)
    with pytest.raises(ValueError, match="超过单次处理上限 20000 行"):
        pipeline._guard_max_row_count(20_001)


def test_pipeline_rejects_unknown_or_disabled_rules(app_config):
    pipeline = Pipeline(app_config)
    with pytest.raises(ValueError, match="未知规则: RX"):
        pipeline.process_file("missing.xlsx", rule_codes=["RX"])

    app_config.rules.enabled = ("R1",)
    with pytest.raises(ValueError, match="请求的规则均未启用"):
        pipeline.process_file("missing.xlsx", rule_codes=["R4"])
