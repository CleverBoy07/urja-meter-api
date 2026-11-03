from __future__ import annotations

from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from app.models import Facets, MeterList, MeterResponse, ReadingsResponse
from app.routes.deps import get_service, split_multi
from app.service import Service

router = APIRouter(prefix="/v1/meters", tags=["Meters"])



def _parse_pair(value: str, name: str, count: int) -> tuple[float, ...]:
    try:
        parts = tuple(float(p) for p in value.split(","))
    except ValueError:
        parts = ()
    if len(parts) != count:
        raise HTTPException(422, f"'{name}' must be {count} comma-separated numbers")
    return parts


@router.get("", response_model=MeterList, summary="List and filter meters")
def list_meters(
    svc: Service = Depends(get_service),
    q: str | None = Query(None, description="Substring match on meter id or serial number (case-insensitive)."),
    status: list[str] | None = Query(None, description="installed, faulty, decommissioned. Repeat or comma-separate for OR."),
    make: list[str] | None = Query(None),
    phase: list[str] | None = Query(None, description="single, three"),
    install_type: list[str] | None = Query(None, description="whole_current, ct_operated"),
    record_format: list[str] | None = Query(None, description="legacy, v2"),
    transformer: list[str] | None = Query(None, description="Transformer (DT) code, e.g. DT-007"),
    feeder: list[str] | None = Query(None, description="Feeder code, e.g. F-001 (note: codes can repeat under different substations)"),
    substation: list[str] | None = Query(None),
    subdivision: list[str] | None = Query(None),
    division: list[str] | None = Query(None),
    circle: list[str] | None = Query(None),
    zone: list[str] | None = Query(None),
    network_node: str | None = Query(None, description="Path-scoped node id from /v1/network, e.g. Z-01/C-01; returns everything beneath it."),
    near: str | None = Query(None, description="'lat,lng' — return meters within radius_km, nearest first.", examples=["26.91,75.79"]),
    radius_km: float = Query(1.0, gt=0, le=100),
    bbox: str | None = Query(None, description="'south,west,north,east' bounding box."),
    sort: str = Query("meter_id", description="meter_id, serial_no, make, status, transformer_code, phase; prefix '-' for descending. Proximity queries always sort by distance."),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    svc.require_inventory()
    filters = {
        "status": [s.lower() for s in split_multi(status)],
        "make": split_multi(make),
        "phase": [s.lower() for s in split_multi(phase)],
        "install_type": [s.lower() for s in split_multi(install_type)],
        "record_format": split_multi(record_format),
        "transformer": [s.upper() for s in split_multi(transformer)],
        "feeder": [s.upper() for s in split_multi(feeder)],
        "substation": [s.upper() for s in split_multi(substation)],
        "subdivision": [s.upper() for s in split_multi(subdivision)],
        "division": [s.upper() for s in split_multi(division)],
        "circle": [s.upper() for s in split_multi(circle)],
        "zone": [s.upper() for s in split_multi(zone)],
    }
    items, total = svc.store.query_meters(
        q=q,
        filters=filters,
        network_path_prefix=network_node.strip("/").upper() if network_node else None,
        near=_parse_pair(near, "near", 2) if near else None,
        radius_km=radius_km if near else None,
        bbox=_parse_pair(bbox, "bbox", 4) if bbox else None,
        sort=sort,
        limit=limit,
        offset=offset,
    )
    next_offset = offset + limit if offset + limit < total else None
    return {"data": items, "page": {"total": total, "limit": limit, "offset": offset, "next_offset": next_offset}, "meta": svc.inventory_meta()}


@router.get("/facets", response_model=Facets, summary="Counts per attribute value (for building filters)")
def facets(svc: Service = Depends(get_service)):
    svc.require_inventory()
    return {"data": svc.store.facets(), "meta": svc.inventory_meta()}


@router.get("/{meter_id}", response_model=MeterResponse, summary="Get one meter", responses={404: {"description": "Unknown meter"}})
def get_meter(meter_id: str, svc: Service = Depends(get_service)):
    svc.require_inventory()
    meter = svc.store.get_meter(meter_id)
    if meter is None:
        raise HTTPException(404, f"Meter {meter_id} not found")
    return {"data": meter, "meta": svc.inventory_meta()}


@router.get(
    "/{meter_id}/readings",
    response_model=ReadingsResponse,
    response_model_by_alias=True,
    summary="Energy readings and consumption for a meter",
    description=(
        "Returns cleaned interval readings (`granularity=raw`) or per-day consumption (`granularity=daily`) "
        "for an inclusive date range in IST. The portal stores cumulative registers; this endpoint also "
        "derives per-interval consumption. With no dates, returns the 7 days ending at the meter's latest "
        "reading (matching the portal's default). Results are cached; see `meta.cache`."
    ),
    responses={404: {"description": "Unknown meter"}, 422: {"description": "Invalid date range"}},
)
async def get_readings(
    meter_id: str,
    svc: Service = Depends(get_service),
    from_: date | None = Query(None, alias="from", description="Start date (inclusive), YYYY-MM-DD, IST."),
    to: date | None = Query(None, description="End date (inclusive), YYYY-MM-DD, IST."),
    granularity: Literal["raw", "daily"] = "raw",
):
    svc.require_inventory()
    try:
        return await svc.readings(meter_id, from_, to, granularity)
    except KeyError:
        raise HTTPException(404, f"Meter {meter_id} not found") from None
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
