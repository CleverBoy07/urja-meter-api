# Reflection

### What assumptions did you make?

The ones that matter most are about meaning, not mechanics. I assumed the portal's
timestamps are IST, because they have no zone and the network is in Jaipur. I assumed
`kwh`/`kvah` are cumulative registers, and that a reading's consumption belongs to the
interval ending at its timestamp. Both fit the data: values only increase, and the
differences are realistic. I treated the transformer master list as more authoritative
than names copied onto meter records, and the DT as the true attachment point when a
meter's copy of the hierarchy disagrees. I also concluded that hierarchy codes are
unique only within their parent, because `D-01` appears under three circles while every
DT has one consistent chain. That shaped the whole tree design. Finally, I assumed the
labelled meter status could be wrong, but I didn't overrule it: decommissioned meters
that keep consuming are flagged, not reclassified. That call belongs to the business,
not to an API. All of these are listed with their reasoning in the README.

### Which part was the most difficult, and how did you get unstuck?

The hardest part was deciding what the hierarchy *is*. The data looked broken in a way
that could be read several ways: blank codes, blank names, a renamed transformer, and
codes with several parents. My first instinct was "codes are global IDs and the data is
corrupt", which would have meant picking a winner for every conflict. What got me
unstuck was changing the question from "which parent is right?" to "what stays
consistent?". I grouped meters by DT and compared their full chains, and every DT had
exactly one chain once the blanks were accounted for. That made the answer clear:
identify nodes by path, repair only what can be repaired unambiguously, and report
everything else. The second hard part was reverse-engineering the export signature. It
turned out to be the easiest win once I read the 3 KB page chunk instead of trying to
guess from network traffic.

### If you had another day, what would you improve?

First, a nightly contract test against the real portal, plus schema-drift alerts. The
service depends on undocumented behaviour (the signing scheme, two record formats,
silent parameter handling), and today the only way to notice a change is a failure in
production. Second, observability: Prometheus metrics for portal latency, 429s and
cache hit rate, and request IDs in the logs. Third, endpoints for bulk and aggregated
readings (per feeder, per DT, many meters at once) with CSV output, because that is
what data teams will actually ask for next. And I'd move the tree building and insights
computation to sync time instead of request time.

### What mistake did you make while solving this?

There were a few, and all of them were caught by actually running things. The most
instructive: I exposed the SQLite connection to FastAPI's worker threads without
locking reads. The tests passed, because a single-threaded test client never overlaps
requests. But the browser fires three requests in parallel, and one of them
intermittently got "inventory not loaded" because a cursor had been clobbered. I only
found it because I opened the console in a real browser and saw `/v1/transformers`
return 503 while `curl` returned 200. I fixed it by routing all reads through the
store's lock, and added a regression test that hammers the store from 16 threads.
Earlier, while exploring, I fanned out 8 parallel requests to fetch all meters' readings
and got 321 × 429. That's how I discovered the rate limit, and it's why the client paces
itself. I also wrote a careless regex refactor that turned the new lock helpers into
infinite recursion, and the test suite caught it immediately.

### If you were reviewing your own submission, what would you criticise?

The insights are built on synthetic data that has almost no variance. Power factor is
constant, the load profile is flat, and voltage is always in band. So the anomaly rules
are mostly untested against realistic signals, and some (zero consumption, stale data,
register rollback) have never fired on real portal data, only in tests. The
transformer-loading figure is an estimate from average energy, not peak demand, and
says so, but it could still be misread. The service also shares one portal session
across all API callers, which is fine for an internal tool but blurs the audit trail on
the portal side. Finally, `service.py` does a lot: sync, caching, insights and the
warm-up job are all in one class. I'd split insights into its own module before this grew further.
