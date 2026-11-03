"""Public response schemas. These drive the generated openapi.json."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Status = Literal["installed", "faulty", "decommissioned"]


class _Model(BaseModel):
    model_config = ConfigDict(populate_by_name=True)


class Problem(_Model):
    """RFC 9457 problem details."""

    type: str = Field("about:blank", examples=["https://urja-meter-api/problems/not-found"])
    title: str
    status: int
    detail: str | None = None


class Location(_Model):
    lat: float = Field(examples=[26.938961])
    lng: float = Field(examples=[75.830957])


class NetworkNode(_Model):
    id: str = Field(description="Path-scoped node id, unique across the network (codes alone are not).", examples=["Z-01/C-01/D-01"])
    code: str = Field(examples=["D-01"])
    name: str = Field(examples=["Division 1"])
    alias: str | None = Field(None, description="Alternative name seen on some meter records (transformers only).")


class Network(_Model):
    zone: NetworkNode
    circle: NetworkNode
    division: NetworkNode
    subdivision: NetworkNode
    substation: NetworkNode
    feeder: NetworkNode
    transformer: NetworkNode


class DataQualityIssue(_Model):
    type: str = Field(examples=["blank_code_filled"])
    level: str | None = Field(None, examples=["circle"])
    severity: Literal["info", "warning"]
    message: str


class Meter(_Model):
    meter_id: str = Field(examples=["J100000"])
    serial_no: str | None = Field(examples=["SE33962"])
    make: str | None = Field(examples=["HPL"])
    phase: Literal["single", "three"] | None
    status: Status | None
    install_type: Literal["whole_current", "ct_operated"] | None
    record_format: str | None = Field(description="Which portal record format the meter uses ('legacy' or 'v2').")
    transformer_code: str | None = Field(examples=["DT-001"])
    network: Network
    network_path: str = Field(examples=["Z-01/C-01/D-01/SD-01/SS-01/F-001/DT-001"])
    location: Location | None
    data_quality: list[DataQualityIssue] = Field(description="Repairs or inconsistencies found in the portal's record for this meter.")
    distance_km: float | None = Field(None, description="Only present on proximity queries.")


class Page(_Model):
    total: int
    limit: int
    offset: int
    next_offset: int | None


class FreshnessMeta(_Model):
    synced_at: str | None = Field(description="When the inventory snapshot was taken from the portal (IST).")
    age_s: float | None
    stale: bool = Field(description="True if the snapshot is older than twice the refresh interval (e.g. portal down).")
    source: str | None = Field(description="'export' (signed bulk endpoint) or 'crawl' (page-by-page fallback).")
    last_sync_error: str | None


class MeterList(_Model):
    data: list[Meter]
    page: Page
    meta: FreshnessMeta


class MeterResponse(_Model):
    data: Meter
    meta: FreshnessMeta


class Facets(_Model):
    data: dict[str, dict[str, int]] = Field(examples=[{"status": {"installed": 238, "faulty": 90, "decommissioned": 75}}])
    meta: FreshnessMeta


class Reading(_Model):
    timestamp: str = Field(description="ISO 8601, IST (+05:30)", examples=["2026-06-24T00:30:00+05:30"])
    kwh_register: float | None = Field(description="Cumulative active-energy register (kWh) as read.")
    kvah_register: float | None = Field(description="Cumulative apparent-energy register (kVAh) as read.")
    kwh: float | None = Field(description="Consumption since the previous reading (kWh). Null for the first reading or if the register went backwards.")
    kvah: float | None
    voltage_r: float | None = Field(description="R-phase voltage (V).")
    interval_start: str | None = Field(description="Start of the interval this row's kWh covers.")
    flags: list[str] = Field(description="e.g. missing_kwh, voltage_out_of_band, register_decreased")


class DailyConsumption(_Model):
    date: str = Field(examples=["2026-06-24"])
    kwh: float
    kvah: float
    power_factor: float | None
    intervals: int = Field(description="Number of intervals that contributed.")
    completeness: float | None = Field(description="intervals / expected intervals for the meter's cadence (1.0 = complete day).")
    voltage_min: float | None
    voltage_max: float | None
    voltage_avg: float | None


class ReadingsSummary(_Model):
    consumption_kwh: float
    apparent_energy_kvah: float
    power_factor: float | None
    avg_daily_kwh: float | None
    peak_day: str | None
    readings: int
    cadence_minutes: int | None = Field(description="30 for AMI half-hourly meters, 1440 for daily reads.")
    first_reading: str | None
    last_reading: str | None
    voltage_min: float | None
    voltage_max: float | None
    voltage_out_of_band: int
    latest_register_kwh: float | None


class ReadingsMeta(_Model):
    timezone: str
    cache: Literal["hit", "miss"]
    portal_calls: int
    cleaning: dict[str, int] = Field(description="What was cleaned on this fetch (duplicates merged, blanks, ...).")


class ReadingsResponse(_Model):
    meter_id: str
    from_: str = Field(alias="from")
    to: str
    granularity: Literal["raw", "daily"]
    summary: ReadingsSummary
    data: list[Reading] | list[DailyConsumption]
    meta: ReadingsMeta


class Transformer(_Model):
    code: str = Field(examples=["DT-001"])
    name: str = Field(examples=["Malviya Nagar DT 1"])
    feeder_code: str | None = Field(examples=["F-001"])
    capacity_kva: float | None = Field(examples=[100])
    meter_count: int
    aliases: list[str]


class TransformerList(_Model):
    data: list[Transformer]
    meta: FreshnessMeta


class TransformerDetail(_Model):
    data: Transformer
    network: dict[str, NetworkNode] | None = Field(description="Upstream chain (zone … feeder) for this transformer.")
    meters: list[Meter]
    status_counts: dict[str, int]
    meta: FreshnessMeta


class TreeNode(_Model):
    id: str
    level: Literal["zone", "circle", "division", "subdivision", "substation", "feeder", "transformer"]
    code: str
    name: str
    meter_count: int
    status_counts: dict[str, int]
    capacity_kva: float | None = None
    children: list[TreeNode]


class TreeResponse(_Model):
    data: list[TreeNode]
    meta: FreshnessMeta


class NodeResponse(_Model):
    data: TreeNode
    meters: list[Meter] = Field(description="Meters under this node (first `limit`).")
    meter_total: int
    meta: FreshnessMeta


class DataQualityResponse(_Model):
    issue_counts: dict[str, int]
    code_reuse: list[dict] = Field(description="Codes that appear under more than one parent (why node ids are path-scoped).")
    transformers_without_meters: list[str]
    issues: list[dict]
    generated_at: str | None = None


class Anomaly(_Model):
    meter_id: str
    transformer_code: str | None
    status: str | None
    type: str = Field(examples=["decommissioned_but_consuming"])
    severity: Literal["high", "medium", "low"]
    message: str


class InsightsResponse(_Model):
    window: dict
    coverage: dict = Field(description="How many meters have cached readings; insights only cover those.")
    warmup: dict
    status_counts: dict[str, int]
    cadence_counts: dict[str, int]
    anomaly_counts: dict[str, int]
    anomalies: list[Anomaly]
    top_consumers: list[dict]
    daily_fleet_kwh: list[dict] = Field(description="Sum over covered meters only; compare with coverage before reading trends.")
    consumption_histogram: list[dict] = Field(description="Meters per 2 kWh/day bin of average daily consumption.")
    avg_daily_kwh_per_meter: float | None
    transformer_loading: list[dict]


class Health(_Model):
    status: Literal["ok", "degraded", "starting"]
    inventory: FreshnessMeta
    meters: int
    portal: dict


class SyncResult(_Model):
    meters: int
    transformers: int
    source: str
    duration_s: float


TreeNode.model_rebuild()
