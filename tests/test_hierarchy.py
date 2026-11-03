import json
from pathlib import Path

from app.hierarchy import build_network, build_tree

FIX = Path(__file__).parent / "fixtures"


def load():
    return json.loads((FIX / "export.json").read_text())["data"], json.loads((FIX / "dts.json").read_text())


def test_blank_codes_are_inferred_from_names():
    meters, dts = load()
    nets, report = build_network(meters, dts)
    # J100011 has {"code": "", "name": "Circle 6"} in the portal
    assert nets["J100011"]["nodes"]["circle"]["code"] == "C-06"
    assert report["issue_counts"].get("blank_code_filled", 0) >= 1


def test_dt_master_name_wins_and_old_name_is_alias():
    meters, dts = load()
    nets, report = build_network(meters, dts)
    dt = nets["J100400"]["nodes"]["transformer"]
    assert dt["name"] == "Sanganer DT 7" and dt["alias"] == "Old Malviya Nagar Xfmr"


def test_node_ids_are_path_scoped():
    meters, dts = load()
    nets, _ = build_network(meters, dts)
    node = nets["J100000"]["nodes"]["division"]
    assert node["id"] == "Z-01/C-01/D-01"
    assert nets["J100000"]["path"].endswith("/DT-001")


def test_chain_majority_vote_overrides_outlier():
    meters, dts = load()
    a = json.loads(json.dumps(meters[0]))
    b = json.loads(json.dumps(meters[0]))
    c = json.loads(json.dumps(meters[0]))
    a["meterId"], b["meterId"], c["meterId"] = "X1", "X2", "X3"
    c["hierarchy"]["substation"] = {"name": "Substation 99", "code": "SS-99"}
    nets, report = build_network([a, b, c], dts)
    assert nets["X3"]["nodes"]["substation"]["code"] == nets["X1"]["nodes"]["substation"]["code"]
    assert any(i["type"] == "chain_differs_from_dt" and i["meter_id"] == "X3" for i in report["issues"])


def test_tree_counts():
    meters, dts = load()
    nets, _ = build_network(meters, dts)
    cleaned = [{"meter_id": m["meterId"], "status": m["installStatus"].lower(), "network": nets[m["meterId"]]["nodes"]} for m in meters]
    tree = build_tree(cleaned, {}, max_depth=2)
    assert sum(z["meter_count"] for z in tree) == len(meters)
    assert all(c["level"] == "circle" and c["children"] == [] for z in tree for c in z["children"])
