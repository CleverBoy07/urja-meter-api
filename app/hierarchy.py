"""Reconstruct the distribution network hierarchy from per-meter records.

The portal stores the full chain (Zone > Circle > Division > Subdivision >
Substation > Feeder > DT) *on every meter*, so the tree has to be rebuilt bottom-up
and the copies disagree in places. What we observed and how we resolve it:

1. Blank codes or blank names on some nodes (e.g. ``{"code": "", "name": "Circle 6"}``).
   -> Fill from the unambiguous name<->code pairing seen on other meters at that level.
2. A DT whose name differs from the transformer master list
   (DT-007 is "Sanganer DT 7" in the DT list, "Old Malviya Nagar Xfmr" on 3 meters).
   -> The DT master list (``/portal/dts``) is authoritative; the old name is kept as an alias.
3. The same code under different parents (e.g. Division D-01 appears under C-01, C-03
   and C-05) while every individual DT has exactly one consistent upstream chain.
   -> Codes are only unique *within their parent*. Nodes are identified by their full
      path (``Z-01/C-01/D-01``), and code reuse is reported, never "fixed".
4. A meter whose chain differs from its DT's majority chain.
   -> The meter is attached via its DT (the physical connection), and the deviation is
      recorded as a data-quality issue.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

LEVELS = ["zone", "circle", "division", "subdivision", "substation", "feeder", "transformer"]
RAW_LEVEL_KEY = {"transformer": "dt"}
LEVEL_LABELS = {
    "zone": "Zone",
    "circle": "Circle",
    "division": "Division",
    "subdivision": "Subdivision",
    "substation": "Substation",
    "feeder": "Feeder",
    "transformer": "Distribution transformer",
}


def _raw_node(raw_meter: dict[str, Any], level: str) -> dict[str, str]:
    node = (raw_meter.get("hierarchy") or {}).get(RAW_LEVEL_KEY.get(level, level)) or {}
    return {"code": (node.get("code") or "").strip(), "name": (node.get("name") or "").strip()}


def _unique_pairings(raw_meters: list[dict[str, Any]], level: str) -> tuple[dict[str, str], dict[str, str]]:
    """name->code and code->name maps, keeping only pairings that are unambiguous."""
    pairs: dict[str, Counter] = defaultdict(Counter)
    rev: dict[str, Counter] = defaultdict(Counter)
    for m in raw_meters:
        n = _raw_node(m, level)
        if n["code"] and n["name"]:
            pairs[n["name"]][n["code"]] += 1
            rev[n["code"]][n["name"]] += 1
    name_to_code = {name: c.most_common(1)[0][0] for name, c in pairs.items() if len(c) == 1}
    code_to_name = {code: c.most_common(1)[0][0] for code, c in rev.items() if len(c) == 1}
    return name_to_code, code_to_name


def build_network(
    raw_meters: list[dict[str, Any]], transformers: list[dict[str, Any]]
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Return ({meter_id: network info}, quality report)."""
    issues: list[dict[str, Any]] = []
    dt_master = {t["code"]: t for t in transformers}
    maps = {level: _unique_pairings(raw_meters, level) for level in LEVELS}

    # Step 1: repair blanks per meter, and apply the DT master list.
    repaired: dict[str, list[dict[str, str]]] = {}
    for m in raw_meters:
        meter_id = str(m.get("meterId", "")).upper()
        chain = []
        for level in LEVELS:
            node = _raw_node(m, level)
            name_to_code, code_to_name = maps[level]
            if not node["code"] and node["name"] in name_to_code:
                node["code"] = name_to_code[node["name"]]
                issues.append(_issue(meter_id, "blank_code_filled", level, f"{LEVEL_LABELS[level]} code was blank; inferred {node['code']} from name '{node['name']}'"))
            elif not node["name"] and node["code"] in code_to_name:
                node["name"] = code_to_name[node["code"]]
                issues.append(_issue(meter_id, "blank_name_filled", level, f"{LEVEL_LABELS[level]} name was blank; inferred '{node['name']}' from code {node['code']}"))
            elif not node["code"]:
                issues.append(_issue(meter_id, "unresolved_blank_code", level, f"{LEVEL_LABELS[level]} code is blank and could not be inferred", severity="warning"))
            chain.append(node)

        dt = chain[-1]
        dt_code = (m.get("dtCode") or dt["code"]).strip()
        if dt["code"] and dt_code and dt["code"] != dt_code:
            issues.append(_issue(meter_id, "dt_code_mismatch", "transformer", f"dtCode {dt_code} differs from hierarchy DT {dt['code']}; using dtCode", severity="warning"))
            dt["code"] = dt_code
        master = dt_master.get(dt["code"])
        if master:
            if dt["name"] and dt["name"] != master["name"]:
                issues.append(_issue(meter_id, "dt_name_alias", "transformer", f"Meter record calls {dt['code']} '{dt['name']}'; master list says '{master['name']}'"))
                dt["alias"] = dt["name"]
            dt["name"] = master["name"]
        else:
            issues.append(_issue(meter_id, "dt_not_in_master", "transformer", f"{dt['code']} not present in the transformer list", severity="warning"))
        repaired[meter_id] = chain

    # Step 2: one canonical upstream chain per DT (majority vote across its meters).
    by_dt: dict[str, list[str]] = defaultdict(list)
    for meter_id, chain in repaired.items():
        by_dt[chain[-1]["code"]].append(meter_id)
    canonical: dict[str, tuple[tuple[str, str], ...]] = {}
    for dt_code, meter_ids in by_dt.items():
        votes = Counter(tuple((n["code"], n["name"]) for n in repaired[mid][:-1]) for mid in meter_ids)
        canonical[dt_code] = votes.most_common(1)[0][0]
        master = dt_master.get(dt_code)
        feeder_code = canonical[dt_code][-1][0]
        if master and master.get("feederCode") and master["feederCode"] != feeder_code:
            issues.append(_issue(None, "dt_feeder_mismatch", "feeder", f"{dt_code}: master list feeder {master['feederCode']} vs meters' feeder {feeder_code}", severity="warning"))

    networks: dict[str, dict[str, Any]] = {}
    for meter_id, chain in repaired.items():
        dt = chain[-1]
        upstream = canonical[dt["code"]]
        if tuple((n["code"], n["name"]) for n in chain[:-1]) != upstream:
            issues.append(_issue(meter_id, "chain_differs_from_dt", None, f"Meter's upstream chain differs from the majority chain for {dt['code']}; attached via its DT", severity="warning"))
        nodes = [{"code": c, "name": n} for c, n in upstream] + [{k: v for k, v in dt.items()}]
        path = ""
        out = {}
        for level, node in zip(LEVELS, nodes):
            path = f"{path}/{node['code']}" if path else node["code"]
            out[level] = {**node, "id": path}
        networks[meter_id] = {"nodes": out, "path": path}

    # Step 3: report codes that recur under different parents (identity is path-scoped).
    reuse = []
    for i, level in enumerate(LEVELS[1:], start=1):
        parents: dict[str, set[str]] = defaultdict(set)
        for net in networks.values():
            parents[net["nodes"][level]["code"]].add(net["nodes"][LEVELS[i - 1]]["id"])
        for code, ps in sorted(parents.items()):
            if len(ps) > 1:
                reuse.append({"level": level, "code": code, "parents": sorted(ps)})

    counts = Counter(i["type"] for i in issues)
    report = {
        "issue_counts": dict(sorted(counts.items())),
        "issues": issues,
        "code_reuse": reuse,
        "transformers_without_meters": sorted(set(dt_master) - set(by_dt)),
    }
    return networks, report


def _issue(meter_id: str | None, type_: str, level: str | None, message: str, severity: str = "info") -> dict[str, Any]:
    return {"meter_id": meter_id, "type": type_, "level": level, "severity": severity, "message": message}


def build_tree(meters: list[dict[str, Any]], transformers: dict[str, dict[str, Any]], max_depth: int = len(LEVELS)) -> list[dict[str, Any]]:
    """Nested tree with per-node meter counts by status, built from cleaned meters."""
    root: dict[str, Any] = {"children": {}}
    for m in meters:
        cursor = root
        for depth, level in enumerate(LEVELS[:max_depth]):
            node = m["network"][level]
            child = cursor["children"].get(node["id"])
            if child is None:
                child = {
                    "id": node["id"],
                    "level": level,
                    "code": node["code"],
                    "name": node["name"],
                    "meter_count": 0,
                    "status_counts": Counter(),
                    "children": {},
                }
                if level == "transformer" and node["code"] in transformers:
                    child["capacity_kva"] = transformers[node["code"]].get("capacity_kva")
                cursor["children"][node["id"]] = child
            child["meter_count"] += 1
            child["status_counts"][m["status"] or "unknown"] += 1
            cursor = child

    def finish(node: dict[str, Any]) -> dict[str, Any]:
        children = sorted(node["children"].values(), key=lambda n: n["id"])
        out = {k: v for k, v in node.items() if k != "children"}
        out["status_counts"] = dict(out["status_counts"])
        out["children"] = [finish(c) for c in children]
        return out

    return [finish(c) for c in sorted(root["children"].values(), key=lambda n: n["id"])]
