from datetime import datetime

import pytest
import xlwt
from openpyxl import Workbook

from runbow007.excel import WorkbookValidationError, read_orders
from runbow007.pipeline import Pipeline

HEADERS = [
    "所属组织",
    "承运商名称",
    "离厂时间(承运商提货时间)",
    "WMS过账时间",
    "预计到达时间",
    "相关单号",
    "订单号",
    "状态",
    "合同状态",
    "总箱数",
    "实际到达时间",
    "签收时间",
    "是否延迟",
    "延迟原因",
    "承运商时效",
    "电子签签署时间",
    "明细单总数",
]


def _write_sample(
    path,
    *,
    duplicate=False,
    conflicting_duplicate=False,
    blank_departure=False,
    blank_expected_arrival=False,
    omit_expected_arrival_header=False,
    omit_actual_arrival_header=False,
    omit_signed_header=False,
    blank_related_order_no=False,
    omit_related_order_no_header=False,
    duplicate_related_order_no=False,
    box_count=10,
):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "maintainCompanyOrderPage"
    headers = list(HEADERS)
    row = [
        "华东中心仓-上海嘉定",
        "华东虹迪",
        None if blank_departure else datetime(2026, 8, 5, 10, 0),
        datetime(2026, 8, 5, 9, 15),
        None if blank_expected_arrival else datetime(2026, 8, 6, 18, 0),
        None if blank_related_order_no else "REL-C001",
        "C001",
        "运输在途（已离厂）",
        "签署中",
        box_count,
        None,
        None,
        "否",
        None,
        24,
        None,
        1,
    ]
    omitted_headers = []
    if omit_expected_arrival_header:
        omitted_headers.append("预计到达时间")
    if omit_actual_arrival_header:
        omitted_headers.append("实际到达时间")
    if omit_signed_header:
        omitted_headers.append("签收时间")
    for omitted_header in omitted_headers:
        position = headers.index(omitted_header)
        del headers[position]
        del row[position]
    if omit_related_order_no_header:
        related_position = headers.index("相关单号")
        del headers[related_position]
        del row[related_position]
    sheet.append(headers)
    sheet.append(row)
    if duplicate or conflicting_duplicate:
        second_row = list(row)
        if conflicting_duplicate:
            second_row[headers.index("总箱数")] = 11
        sheet.append(second_row)
    if duplicate_related_order_no:
        second_row = list(row)
        second_row[headers.index("订单号")] = "C002"
        sheet.append(second_row)
    workbook.save(path)


def test_reads_xlsx_and_maps_actual_headers(tmp_path):
    path = tmp_path / "orders.xlsx"
    _write_sample(path)
    parsed = read_orders(path, expected_ui_total=1)
    assert parsed.sheet_name == "maintainCompanyOrderPage"
    assert parsed.unique_order_count == 1
    assert parsed.orders[0].related_order_no == "REL-C001"
    assert parsed.orders[0].carrier_sla_hours == 24
    assert parsed.orders[0].is_delayed is False
    assert parsed.orders[0].expected_arrival_at == datetime(2026, 8, 6, 18, 0)


def test_preserves_fractional_box_count(tmp_path):
    path = tmp_path / "orders.xlsx"
    _write_sample(path, box_count=18.9999)

    parsed = read_orders(path)

    assert parsed.orders[0].box_count == 18.9999


def test_reads_real_biff8_xls_export_shape(tmp_path):
    path = tmp_path / "orders.xls"
    workbook = xlwt.Workbook()
    sheet = workbook.add_sheet("maintainCompanyOrderPage")
    for column, header in enumerate(HEADERS):
        sheet.write(0, column, header)
    date_style = xlwt.easyxf(num_format_str="YYYY-MM-DD HH:MM:SS")
    row = [
        "华东中心仓-上海嘉定",
        "华东虹迪",
        datetime(2026, 8, 5, 10, 0),
        datetime(2026, 8, 5, 9, 15),
        None,
        "REL-XLS001",
        "XLS001",
        "运输在途（已离厂）",
        "签署中",
        10,
        None,
        None,
        "否",
        None,
        24,
        None,
        1,
    ]
    for column, value in enumerate(row):
        if isinstance(value, datetime):
            sheet.write(1, column, value, date_style)
        elif value is not None:
            sheet.write(1, column, value)
    workbook.save(str(path))

    parsed = read_orders(path, expected_ui_total=1)

    assert parsed.sheet_name == "maintainCompanyOrderPage"
    assert parsed.orders[0].order_no == "XLS001"
    assert parsed.orders[0].related_order_no == "REL-XLS001"
    assert parsed.orders[0].wms_posted_at == datetime(2026, 8, 5, 9, 15)
    assert parsed.orders[0].expected_arrival_at is None


def test_allows_blank_departure_time_for_r1(tmp_path):
    path = tmp_path / "orders.xlsx"
    _write_sample(path, blank_departure=True)

    parsed = read_orders(path, expected_ui_total=1)

    assert parsed.orders[0].departed_at is None


@pytest.mark.parametrize(
    "options",
    [
        {"blank_expected_arrival": True},
        {"omit_expected_arrival_header": True},
    ],
)
def test_expected_arrival_can_be_absent_when_not_required(tmp_path, options):
    path = tmp_path / "orders.xlsx"
    _write_sample(path, **options)

    parsed = read_orders(path, expected_ui_total=1)

    assert parsed.orders[0].expected_arrival_at is None


@pytest.mark.parametrize(
    ("rule_code", "options", "missing_header"),
    [
        ("R2", {"omit_expected_arrival_header": True}, "预计到达时间"),
        ("R3", {"omit_actual_arrival_header": True}, "实际到达时间"),
        ("R3", {"omit_signed_header": True}, "签收时间"),
    ],
)
def test_pipeline_rejects_missing_selected_rule_time_header(
    tmp_path, app_config, rule_code, options, missing_header
):
    path = tmp_path / "orders.xlsx"
    _write_sample(path, **options)

    with pytest.raises(WorkbookValidationError, match=missing_header):
        Pipeline(app_config).process_file(path, rule_codes=[rule_code])


def test_rejects_ui_total_mismatch(tmp_path):
    path = tmp_path / "orders.xlsx"
    _write_sample(path)
    with pytest.raises(WorkbookValidationError, match="页面显示 2 条"):
        read_orders(path, expected_ui_total=2)


def test_accepts_ui_total_drift_within_tolerance(tmp_path):
    """读页面总数到 TMS 生成导出之间订单还在增减，几条的漂移不该让整轮失败。"""
    path = tmp_path / "orders.xlsx"
    _write_sample(path)

    parsed = read_orders(path, expected_ui_total=2, total_tolerance=1)

    assert len(parsed.orders) == 1


def test_rejects_ui_total_drift_beyond_tolerance(tmp_path):
    path = tmp_path / "orders.xlsx"
    _write_sample(path)
    with pytest.raises(WorkbookValidationError, match="页面显示 20 条"):
        read_orders(path, expected_ui_total=20, total_tolerance=1)


def test_accepts_identical_duplicate_order_rows(tmp_path):
    path = tmp_path / "orders.xlsx"
    _write_sample(path, duplicate=True)

    parsed = read_orders(path, expected_ui_total=1)

    assert [order.order_no for order in parsed.orders] == ["C001"]
    assert parsed.row_count == parsed.unique_order_count == 1
    assert parsed.raw_row_count == 2


def test_conflicting_duplicate_order_numbers_use_the_last_row(tmp_path):
    path = tmp_path / "orders.xlsx"
    _write_sample(path, conflicting_duplicate=True)

    parsed = read_orders(path, expected_ui_total=1)

    assert parsed.row_count == parsed.unique_order_count == 1
    assert parsed.raw_row_count == 2
    assert parsed.orders[0].box_count == 11
    assert parsed.orders[0].source_row == 3


@pytest.mark.parametrize(
    "options",
    [
        {"blank_related_order_no": True},
        {"omit_related_order_no_header": True},
    ],
)
def test_related_order_number_is_required(tmp_path, options):
    path = tmp_path / "orders.xlsx"
    _write_sample(path, **options)

    with pytest.raises(WorkbookValidationError, match="相关单号"):
        read_orders(path)


def test_allows_duplicate_related_order_numbers(tmp_path):
    path = tmp_path / "orders.xlsx"
    _write_sample(path, duplicate_related_order_no=True)

    parsed = read_orders(path, expected_ui_total=2)

    assert [order.order_no for order in parsed.orders] == ["C001", "C002"]
    assert [order.related_order_no for order in parsed.orders] == [
        "REL-C001",
        "REL-C001",
    ]
