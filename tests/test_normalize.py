from datetime import date

from app.normalize import (
    clean_meter,
    clean_readings,
    daily_rollup,
    parse_detail_fields,
    parse_labelled_node,
    parse_portal_timestamp,
    to_float,
    with_deltas,
)
from app.portal.devalue import page_data


def test_to_float_handles_portal_strings():
    assert to_float("48438.74") == 48438.74
    assert to_float("") is None
    assert to_float(None) is None
    assert to_float("n/a") is None
    assert to_float(100) == 100.0


def test_timestamp_is_day_first_and_ist():
    ts = parse_portal_timestamp("03/06/2026 23:30")
    assert (ts.year, ts.month, ts.day, ts.hour, ts.minute) == (2026, 6, 3, 23, 30)
    assert ts.isoformat().endswith("+05:30")
    assert parse_portal_timestamp("2026-06-03") is None


def test_duplicate_timestamps_are_merged_preferring_complete_values():
    rows, quality = clean_readings([
        {"timestamp": "30/06/2026 00:00", "kwh": "", "kvah": "13750.93", "voltR": ""},
        {"timestamp": "30/06/2026 00:00", "kwh": "12732.34", "kvah": "13750.93", "voltR": "229"},
        {"timestamp": "29/06/2026 00:00", "kwh": "12724.70", "kvah": "13742.67", "voltR": "232"},
    ])
    assert [r["ts"].day for r in rows] == [29, 30]  # sorted
    assert rows[1]["kwh"] == 12732.34 and rows[1]["voltage_r"] == 229
    assert quality["duplicates_merged"] == 1


def test_deltas_and_daily_rollup_for_half_hourly_meter():
    raw = [{"timestamp": f"01/06/2026 {h:02d}:{m:02d}", "kwh": str(100 + i * 0.5), "kvah": str(110 + i * 0.54), "voltR": "230"}
           for i, (h, m) in enumerate((h, m) for h in range(24) for m in (0, 30))]
    raw.append({"timestamp": "02/06/2026 00:00", "kwh": "124", "kvah": "135.92", "voltR": "230"})
    rows = with_deltas(clean_readings(raw)[0])
    assert rows[0]["kwh_delta"] is None and rows[1]["kwh_delta"] == 0.5
    daily = daily_rollup(rows, 30)
    day = daily[date(2026, 6, 1)]
    assert day["kwh"] == 24.0 and day["intervals"] == 48 and day["completeness"] == 1.0


def test_register_going_backwards_is_flagged_not_counted():
    rows, _ = clean_readings([
        {"timestamp": "01/06/2026 00:00", "kwh": "100", "kvah": "110", "voltR": "230"},
        {"timestamp": "02/06/2026 00:00", "kwh": "90", "kvah": "100", "voltR": "300"},
    ])
    out = with_deltas(rows)
    assert out[1]["kwh_delta"] is None
    assert "register_decreased" in out[1]["flags"] and "voltage_out_of_band" in out[1]["flags"]


def test_both_detail_shapes_parse_identically():
    legacy = {"data": [{"parameterName": "Meter ID", "parameterValue": "J1"}, {"parameterName": "Installation Type", "parameterValue": "CT Operated"}]}
    v2 = {"classData": '{"installed_meter":{"MeterId":"J1","InstallationType":"CT Operated"}}'}
    f1, b1 = parse_detail_fields(legacy)
    f2, b2 = parse_detail_fields(v2)
    assert f1 == f2 == {"meterId": "J1", "installType": "CT Operated"}
    assert (b1, b2) == ("legacy", "v2")


def test_labelled_node():
    assert parse_labelled_node("Malviya Nagar DT 1 (DT-001)") == {"name": "Malviya Nagar DT 1", "code": "DT-001"}
    assert parse_labelled_node(" (C-05)") == {"name": "", "code": "C-05"}
    assert parse_labelled_node(None) == {"name": "", "code": ""}


def test_clean_meter_enums():
    m = clean_meter({"meterId": "j1", "serialNo": "L&84997", "make": "Genus", "phaseType": "three",
                     "installStatus": "Decommissioned", "installType": "Whole Current", "build": "v2",
                     "dtCode": "DT-001", "geo": {"lat": "26.9", "lng": "75.8"}})
    assert m["meter_id"] == "J1" and m["status"] == "decommissioned" and m["install_type"] == "whole_current"
    assert m["location"] == {"lat": 26.9, "lng": 75.8}
    assert m["serial_no"] == "L&84997"  # kept verbatim; '&' is real portal data


def test_devalue_page_data():
    payload = {"type": "data", "nodes": [{"type": "skip"}, {"type": "data", "data": [{"a": 1, "b": 2}, "x", [3, 1], 7]}]}
    data, err = page_data(payload)
    assert err is None and data == {"a": "x", "b": [7, "x"]}
    data, err = page_data({"nodes": [None, {"type": "error", "error": {"message": "Meter not found"}, "status": 404}]})
    assert data is None and err["status"] == 404
