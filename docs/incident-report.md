# Incident drill report: dependency failures under load

- Date: 2026-11-05, 09:01 to 09:06 IST
- Environment: local Docker Compose stack (five FastAPI services, Prometheus, Grafana)
- Traffic: synthetic, about 5 checkouts per second from a single-threaded generator; 10% of requests deliberately ask for out-of-stock quantities
- Raw data: `docs/drill-results.json`. Reproduce with `python scripts/incident_drill.py`.

## Summary

Two failures were injected by stopping containers under load: promise-service (an optional dependency) and then inventory-service (a critical one).

Detection worked. Both outages raised alerts within 22 to 32 seconds and the checkout rollback logic was exact. The optional-dependency outage did not block purchases, but it made checkout very slow: peak p95 latency was 9.25 s in run 1 and 4.88 s in run 2, against 0.08 s and 0.05 s at baseline.

The drill also exposed a data-integrity defect that no alert caught. inventory-service keeps stock and reservations in memory. After it restarted it came back empty: 585 of the 800 live orders (73%) no longer had a stock reservation, and for about 16 seconds every checkout was rejected as out of stock while the service reported healthy.

## Impact

| | Incident 1: promise-service down | Incident 2: inventory-service down |
|---|---|---|
| Outage length | 44.7 s | 70.8 s |
| First alert | ServiceDown at 28.2 s | ServiceDown at 22.2 s |
| Other alerts | CheckoutDegraded at 32.3 s | CheckoutUnavailable at 28.2 s, GatewayHighErrorRate at 58.4 s |
| Customer symptom | Checkouts succeeded without a delivery date (13 counted as degraded over the drill) but slowly: peak p95 9.25 s in run 1, 4.88 s in run 2 | 35 checkouts failed with HTTP 503 (33 during the outage, 2 while the container was starting) |
| Healthy after restart | 1.1 s | 2.1 s |
| Alerts cleared after restart | ServiceDown 13.2 s, CheckoutDegraded 67.4 s | ServiceDown 32.7 s, GatewayHighErrorRate 46.8 s, CheckoutUnavailable 66.9 s |
| Data integrity | No loss | 585 reservations lost (see root cause 1) |

## Timeline (IST, 2026-11-05)

| Time | Event |
|---|---|
| 09:01:00 | Baseline traffic starts |
| 09:01:30 | promise-service stopped |
| 09:01:59 | Alert firing: ServiceDown (promise-service), T+28 s |
| 09:02:03 | Alert firing: CheckoutDegraded, T+32 s |
| 09:02:15 | promise-service restarted, healthy within 1.1 s |
| 09:02:28 | ServiceDown resolved |
| 09:03:23 | CheckoutDegraded resolved (67 s after recovery) |
| 09:03:40 | inventory-service stopped |
| 09:04:02 | Alert firing: ServiceDown (inventory-service), T+22 s |
| 09:04:08 | Alert firing: CheckoutUnavailable, T+28 s |
| 09:04:39 | Alert firing: GatewayHighErrorRate, T+58 s |
| 09:04:51 | inventory-service restarted with empty state, healthy within 2.1 s |
| 09:04:53 to 09:05:09 | Checkouts rejected as out of stock (78 load-generator requests) |
| 09:05:08 | Probe checkout returns HTTP 409 |
| 09:05:09 | Stock reseeded by the drill script |
| 09:05:24 to 09:05:58 | ServiceDown, GatewayHighErrorRate and CheckoutUnavailable resolve |

## What went well

- Fast detection: both outages were caught by a paging alert in under 30 seconds.
- Partial graceful degradation: checkouts kept succeeding without promise-service, but slowly (see finding 2).
- Exact rollbacks: 207 orders cancelled = 172 out-of-stock + 35 unavailable, and the compensation-failure counter stayed at 0.
- Fast recovery: both services were healthy 1 to 2 seconds after restart, with no manual steps apart from the reseed.

## Root causes and findings

### 1. Restarting inventory-service silently lost reservations (severity: high)

Cause: inventory-service stores stock and reservations in process memory, so a restart wipes them.

Evidence from the drill:

- Live orders: 1007 created - 207 cancelled = 800 (matches 787 successful + 13 degraded checkouts).
- Reservations present after restart: 215.
- Orders without a reservation: 800 - 215 = 585.
- After the reseed, inventory reports 5000 units available per region while 585 units are already promised to customers, so the next orders can oversell.

Why it was not detected:

- The restart looked healthy: ServiceDown resolved and `/health/deps` returned ok.
- The wrong answers were ordinary HTTP 409 responses. GatewayHighErrorRate only counts 5xx.
- There is no alert on restarts, on the out-of-stock ratio, or on order/reservation mismatch.
- The false out-of-stock window lasted about 16 seconds, shorter than the 1-minute rate windows the alerts use. Detection alone cannot reliably catch this, so the fix has to include prevention.

### 2. A failing dependency makes checkout slow (severity: high)

Evidence from Prometheus, run 1 (30 s rate windows; run 2 is in the latency evidence section at the end of this report):

- Baseline: mean 0.03 s, p95 0.08 s.
- promise-service down (optional dependency): peak mean 4.52 s, peak p95 9.25 s.
- inventory-service down (critical dependency): peak mean 2.31 s, peak p95 3.62 s.

Checkouts kept succeeding when promise-service was down, but a customer waiting 4 to 9 seconds for a response would not call that graceful. With one run per incident and lookup stalls that vary from call to call (4.0 s and 7.86 s in the probe below), the ordering between the two incidents is indicative only. The robust finding is that both failures are slow, including the one on a dependency the design treats as optional.

The CheckoutLatencyP95High alert (p95 above 0.5 s for 1 minute) did fire, so the slowdown was detected. The drill did not record how long it took to fire.

Latencies above the gateway's 2 s timeout mean that timeout does not bound the whole call. Cause (verified): httpx applies its timeout to the connect, read and write phases, not to name resolution, and Docker removes a stopped container's hostname, so the lookup stalls before failing. Timing a request to the stopped promise-service from inside the gateway container gave a raw DNS lookup failure after 7.86 s and an httpx call failure after 4.0 s, against a configured timeout of 2.0 s. The stall length varies per lookup. This is consistent with the drill (peak mean 4.52 s, peak p95 9.25 s) and with the one generator request that exceeded its own 5 s timeout.

The generator-side figures (about 2.9 s and 2.0 s per request, from throughput) were averages over each whole phase and understate these peaks. Because the generator is closed-loop, real open-loop traffic would see requests queue up and the impact would be larger.

### 3. Alert timing artifacts (severity: low)

- CheckoutDegraded stayed firing for 67 s after promise-service recovered because it uses a 1-minute rate window.
- GatewayHighErrorRate fired 58 s into incident 2, 30 s after CheckoutUnavailable. The ratio alert is a useful backstop but should not be the page.

## Action items

| Priority | Action | How we will know it is fixed |
|---|---|---|
| P1 | Persist state in a database instead of process memory. DONE for inventory-service and order-service (PostgreSQL; see the follow-ups). OPEN: RDS in Terraform and separate instances per service | Run 2 and run 3 drills; scripts/orphan_probe.py |
| P1 | Add a reconciliation check comparing live orders with reservations; alert when they differ | The drill's derived metric becomes a standing alert |
| P1 | Alert on service restarts (`changes(process_start_time_seconds[10m]) > 0`) | Alert fires within one scrape of a restart |
| P1 | Alert on out-of-stock ratio spikes (for example above 50% of checkouts over 1 minute; tune to the real baseline) | Fires during a repeat of the empty-inventory scenario |
| P1 | Bound total checkout time: add an overall deadline per request, make the optional promise-service call fail fast (short total timeout, skipped while it keeps failing), and add a circuit breaker for all downstream calls | Re-run the drill: incident 1 peak p95 stays under 0.5 s and incident 2 fails fast |
| P2 | Shorten the CheckoutDegraded rate window so it clears sooner after recovery | Clears within about 30 s of recovery |
| P3 | Repeat the drill with open-loop load (k6) to measure real queueing impact and variance across runs | Results reproduced across several runs |

## Caveats

This was a drill on a local stack with synthetic traffic and in-memory services, run once, so timings have no variance estimate. Alert delays include the 5 s scrape and evaluation intervals and each rule's `for:` duration. The clock times come from the drill script.

## Latency evidence (queried from Prometheus)

| Window | Peak mean checkout latency | Peak p95 checkout latency |
|---|---|---|
| Baseline (before incident 1) | 0.03s | 0.08s |
| Incident 1 (promise-service down) | 4.52s | 9.25s |
| Incident 2 (inventory-service down) | 2.31s | 3.62s |

CheckoutLatencyP95High (threshold 0.5 s for 1 minute) during the drill: firing, pending.
process_start_time_seconds is exported by 6 scrape targets, so a restart alert based on it is possible.

## Follow-up: PostgreSQL persistence (run 2, 2026-11-07)

inventory-service now stores stock and reservations in PostgreSQL. Reservations are claimed with an insert that ignores duplicates, stock rows are locked in sorted order, and database constraints back up the application checks. The drill was re-run on a fresh stack with the same scenarios; the first run's results are kept in `docs/drill-results-before.json`.

| Metric | Run 1 (in-memory) | Run 2 (PostgreSQL) |
|---|---|---|
| Live orders | 800 | 853 |
| Active reservations in inventory | 215 | 853 |
| Orders without a reservation | 585 | 0 |
| Probe checkout right after the inventory restart | HTTP 409 | HTTP 201 |
| Load requests rejected as out of stock after the restart | 78 of 80 (98%) | 8 of 80 (10%) |
| Checkout rollbacks exact | yes | yes (139 cancelled = 100 out-of-stock + 39 unavailable) |

Reading the table:

- The 8 rejections in run 2 are the deliberate out-of-stock requests, which are about 10% of the generated traffic, as in the baseline phases. Run 1's 78 were false rejections caused by the empty inventory.
- Run 2 made 318 reservations after the restart (the counter resets with the process); 853 - 318 = 535 reservations made before the restart were still present afterwards. This is derived, not measured directly.

Alert timing in the two runs, seconds after the failure was injected:

| Alert | Run 1 | Run 2 |
|---|---|---|
| ServiceDown (promise-service) | 28.2 | 28.2 |
| CheckoutDegraded | 32.3 | 28.2 |
| ServiceDown (inventory-service) | 22.2 | 26.1 |
| CheckoutUnavailable | 28.2 | 32.1 |
| GatewayHighErrorRate | 58.4 | 62.3 |

The two runs agree to within about 4 seconds, which gives a rough sense of run-to-run variance.

Not changed by this fix, and visible in run 2: checkout is still slow while a dependency is down. The generator completed 15 requests during incident 1 and 38 during incident 2, against about 5 per second normally. The gateway deadline and circuit breaker are still open.

### Remaining gaps at the time of run 2 (both addressed in the follow-up below)

- order-service is still in-memory. If it restarts, orders disappear but their reservations remain in PostgreSQL, so stock is held for orders that no longer exist.
- The drill restarts services but never takes PostgreSQL itself down, so a database outage is untested.

## Latency evidence, run 2 (PostgreSQL)

| Window | Peak mean checkout latency | Peak p95 checkout latency |
|---|---|---|
| Baseline (before incident 1) | 0.03s | 0.05s |
| Incident 1 (promise-service down) | 3.90s | 4.88s |
| Incident 2 (inventory-service down) | 2.31s | 3.62s |

CheckoutLatencyP95High (threshold 0.5 s for 1 minute) during the drill: firing, pending.
process_start_time_seconds is exported by 6 scrape targets, so a restart alert based on it is possible.

Reading run 2: incident 1 peaked at a p95 of 4.88 s against 9.25 s in run 1, so single-run peaks differ by about 2x and the stall length clearly varies from run to run. Incident 2's figures (mean 2.31 s, p95 3.62 s) are identical to run 1's to two decimals. The baseline and incident 1 rows differ between the runs, and the monitoring volume was recreated between them, so the query is not reading the same data. The match is most likely coincidence from the closed-loop generator and histogram bucket interpolation, which is an unverified explanation.

## Follow-up: order persistence and database drill (run 3, 2026-11-08)

order-service now stores orders in PostgreSQL as well (a separate `orders` database on the same instance). A third drill then restarted order-service and took PostgreSQL itself down under load. Raw data: `docs/drill-results-db.json`.

### Mirror-image probe: orders against reservations across an order-service restart

| | order-service in memory | order-service on PostgreSQL |
|---|---|---|
| Order lookup after the restart | HTTP 404 | HTTP 200 for 5 of 5 orders |
| Live orders vs active reservations | order gone, 854 reservations unchanged | 5 live orders, 5 active reservations |

The probe is `scripts/orphan_probe.py`. It exits non-zero if orders are lost or the two counts disagree.

### Drill results

| | Incident 3: order-service stopped | Incident 4: PostgreSQL stopped |
|---|---|---|
| Outage length | 45.1 s | 81.0 s |
| Alerts and time to fire | ServiceDown (order-service) 22.1 s, CheckoutUnavailable 32.2 s | CheckoutUnavailable 28.2 s, GatewayHighErrorRate 62.3 s |
| ServiceDown fired for any service | yes | no |
| Customer symptom | 14 load-generator checkouts failed with HTTP 503 | 41 failed with 503 during the outage and 7 more before recovery; 3 probe checkouts each failed after 2.0 s |
| Recovery | healthy 2.1 s after restart | first successful checkout 15.7 s after PostgreSQL restarted |
| Alerts cleared after restart | ServiceDown 12.1 s, CheckoutUnavailable 68.4 s | GatewayHighErrorRate 66.0 s, CheckoutUnavailable 82.1 s |

### Reconciliation of orders against reservations

| Snapshot | Live orders | Active reservations |
|---|---|---|
| Baseline, under load | 134 | 140 |
| After the order-service restart, under load | 447 | 451 |
| Before the PostgreSQL outage, under load | 545 | 550 |
| After the PostgreSQL outage, under load | 865 | 869 |
| Final, load stopped | 869 | 869 |

### Findings

- Persistence held through both incidents. Live orders kept growing across the order-service restart (134 to 447) and nothing was lost. The final snapshot, taken with the load stopped, shows exact equality and the gateway's compensation-failure counter stayed at 0.
- The mid-run snapshots show 4 to 6 more reservations than orders. This is most likely a sampling artifact: the two counts are read one after the other, about a second apart, while about 4 checkouts per second complete, so inventory is read slightly later than orders. It is supported by the final snapshot and by the orphan probe (no concurrent load) but was not proven separately. Pausing the load before reconciling would measure this cleanly.
- During the PostgreSQL outage checkouts failed fast: three probe checkouts returned 503 after 2.00 to 2.01 s. That matches both the connection pool timeout and the gateway's own 2 s timeout, so it is not known which fires first.
- The services stayed up during the database outage, so ServiceDown never fired. The first alert was CheckoutUnavailable at 28.2 s. No alert says the database is down.
- Recovery took 15.7 s from PostgreSQL restarting to the first successful checkout, with 7 more 503s seen by the load generator in that interval. How much of that is database startup and how much is the pool reconnecting was not measured. The `healthy_after_s` value of 0.0 for incident 4 in `drill-results-db.json` is a measurement artifact (sampled after the recovery loop); the script now measures it from the restart.
- Alerts clear 66 to 82 s after recovery because of the 1-minute rate windows.
- No orphan order appeared when PostgreSQL went down. Both services share one PostgreSQL instance, so during an outage the gateway cannot cancel an order it has just created (the cancel call needs the same database). A single run says little about a narrow window, so the structural risk remains.

### Action items from run 3

| Priority | Action | How we will know it is fixed |
|---|---|---|
| P1 | DONE: DatabaseUnreachable alert on a `db_up` gauge in both database-backed services | Run 4: fired 18.1 s (order-service) and 28.2 s (inventory-service) after PostgreSQL stopped |
| P1 | DONE: ServiceRestarted alert on process start time | Run 4: fired 12.1 s after a 2 s quick restart of inventory-service; ServiceDown stayed silent |
| P2 | Separate PostgreSQL instances per service (or a managed instance with a replica) and add a reconciliation job that finds orders without reservations | An outage drill that checks reconciliation directly |
| P2 | Pause the load before taking the reconciliation snapshots | Mid-run snapshots agree exactly |

## Follow-up: detection alerts (run 4, 2026-11-08)

Two alerts were added after run 3. `DatabaseUnreachable` uses a `db_up` gauge evaluated at scrape time in order-service and inventory-service and fires after 15 s at 0. `ServiceRestarted` fires when `process_start_time_seconds` changes within 10 minutes. `scripts/alert_check.py` took PostgreSQL down under load, then did a quick restart of inventory-service. Raw data: `docs/alert-check.json`.

| | Run 3 (before the new alerts) | Run 4 (with the new alerts) |
|---|---|---|
| First alert after PostgreSQL stopped | CheckoutUnavailable at 28.2 s | DatabaseUnreachable (order-service) at 18.1 s |
| Other alerts | GatewayHighErrorRate at 62.3 s | DatabaseUnreachable (inventory-service) 28.2 s, CheckoutUnavailable 38.2 s (GatewayHighErrorRate was not watched in this run) |
| Outage length | 81.0 s | 50.6 s |
| First alert names the failing dependency | no | yes |
| Alerts cleared after PostgreSQL restarted | GatewayHighErrorRate 66.0 s, CheckoutUnavailable 82.1 s | DatabaseUnreachable 8.5 s and 12.5 s, CheckoutUnavailable 62.8 s |
| First successful checkout after PostgreSQL restarted | 15.7 s | 0.4 s |

### Findings

- The first alert arrived 10 s earlier than run 3's first alert (18.1 s against 28.2 s) and names the database. The script prints that DatabaseUnreachable fired 20.1 s before CheckoutUnavailable, but that compares against a CheckoutUnavailable that was itself 10 s slower than in run 3 (38.2 s against 28.2 s), so a fairer figure is about 10 s. With one run each this is indicative only.
- The two services fired 10 s apart (18.1 s and 28.2 s) with identical configuration. The reason is not known, so this alert's timing should be read as about plus or minus 10 s.
- Not tested: whether DatabaseUnreachable fires with no customer traffic at all, which is its purpose. The load generator ran throughout this check.
- A quick restart is now visible: ServiceRestarted fired 12.1 s after restarting inventory-service, and ServiceDown did not fire.
- ServiceRestarted stays active for 10 minutes after each restart by design, so drills raise it. It suits a notification or ticket, not a page.
- Recovery after PostgreSQL restarted varied from 15.7 s (run 3, after an 81 s outage) to 0.4 s (run 4, after a 51 s outage). The runs differ in outage length and nothing here separates database startup from the connection pool reconnecting, so recovery time should be treated as unstable. Both figures are measured from the moment `docker compose start postgres` returned.
