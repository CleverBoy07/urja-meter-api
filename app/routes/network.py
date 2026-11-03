from __future__ import annotations

from collections import Counter

from fastapi import APIRouter, Depends, HTTPException, Query

from app.hierarchy import LEVELS
from app.models import DataQualityResponse, InsightsResponse, NodeResponse, TransformerDetail, TransformerList, TreeResponse
from app.routes.deps import get_service
from app.service import Service

router = APIRouter(prefix="/v1", tags=["Network"])


@router.get("/transformers", response_model=TransformerList, summary="List distribution transformers (DTs)")
def list_transformers(svc: Service = Depends(get_service), feeder: str | None = Query(None, description="Filter by feeder code")):
    svc.require_inventory()
    items = svc.store.transformers()
    if feeder:
        items = [t for t in items if (t.get("feeder_code") or "").upper() == feeder.upper()]
    return {"data": items, "meta": svc.inventory_meta()}


@router.get("/transformers/{code}", response_model=TransformerDetail, summary="One transformer with its upstream chain and meters")
def get_transformer(code: str, svc: Service = Depends(get_service)):
    svc.require_inventory()
    t = svc.store.get_transformer(code)
    if t is None:
        raise HTTPException(404, f"Transformer {code} not found")
    meters = svc.store.meters_on_transformer(code)
    network = {lvl: meters[0]["network"][lvl] for lvl in LEVELS[:-1]} if meters else None
    return {
        "data": t,
        "network": network,
        "meters": meters,
        "status_counts": dict(Counter(m["status"] for m in meters)),
        "meta": svc.inventory_meta(),
    }


@router.get("/network/tree", response_model=TreeResponse, summary="Reconstructed network hierarchy")
def tree(
    svc: Service = Depends(get_service),
    depth: int = Query(len(LEVELS), ge=1, le=len(LEVELS), description="1 = zones only … 7 = down to transformers"),
):
    svc.require_inventory()
    return {"data": svc.tree(depth), "meta": svc.inventory_meta()}


@router.get("/network/nodes/{node_id:path}", response_model=NodeResponse, summary="One hierarchy node, its children and meters")
def node(
    node_id: str,
    svc: Service = Depends(get_service),
    limit: int = Query(100, ge=0, le=1000),
):
    svc.require_inventory()
    node_id = node_id.strip("/").upper()
    found = svc.node(node_id)
    if not found:
        raise HTTPException(404, f"Network node {node_id} not found")
    meters, total = svc.store.query_meters(network_path_prefix=node_id, limit=limit)
    return {"data": found, "meters": meters, "meter_total": total, "meta": svc.inventory_meta()}


@router.get("/data-quality", response_model=DataQualityResponse, tags=["Insights"], summary="What was wrong with the portal's data and how it was repaired")
def data_quality(svc: Service = Depends(get_service)):
    svc.require_inventory()
    return svc.quality_report


@router.get("/insights", response_model=InsightsResponse, tags=["Insights"], summary="Fleet health: anomalies, top consumers, transformer loading")
def insights(svc: Service = Depends(get_service), window_days: int = Query(7, ge=1, le=31)):
    svc.require_inventory()
    return svc.insights(window_days)
