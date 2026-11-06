# Incident drill report: dependency failures under load

- Date: 2026-11-05, 09:01 to 09:06 IST
- Environment: local Docker Compose stack (five FastAPI services, Prometheus, Grafana)
- Traffic: synthetic, about 5 checkouts per second from a single-threaded generator; 10% of requests deliberately ask for out-of-stock quantities
- Raw data: `docs/drill-results.json`. Reproduce with `python scripts/incident_drill.py`.

## Summary

Two failures were injected by stopping containers under load: promise-service (an optional dependency) and then inventory-service (a critical one).

Detection worked. Both outages raised alerts within 22 to 32 seconds and the checkout rollback logic was exact. The optional-dependency outage did not block purchases, but it made checkout slower than the critical-dependency outage did: peak p95 latency was 9.25 s, against 0.08 s at baseline.

The drill also exposed a data-integrity defect that no alert caught. inventory-service keeps stock and reservations in memory. After it restarted it came back empty: 585 of the 800 live orders (73%) no longer had a stock reservation, and for about 16 seconds every checkout was rejected as out of stock while the service reported healthy.

## Impact

| | Incident 1: promise-service down | Incident 2: inventory-service down |
|---|---|---|
| Outage length | 44.7 s | 70.8 s |
| First alert | ServiceDown at 28.2 s | ServiceDown at 22.2 s |
| Other alerts | CheckoutDegraded at 32.3 s | CheckoutUnavailable at 28.2 s, GatewayHighErrorRate at 58.4 s |
| Customer symptom | Checkouts succeeded without a delivery date (13 counted as degraded over the drill) but slowly: peak p95 9.25 s | 35 checkouts failed with HTTP 503 (33 during the outage, 2 while the container was starting) |
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

### 2. A failing dependency makes checkout slow, and the optional dependency hurt most (severity: high)

Evidence from Prometheus (30 s rate windows, see the latency evidence section below):

- Baseline: mean 0.03 s, p95 0.08 s.
- promise-service down (optional dependency): peak mean 4.52 s, peak p95 9.25 s.
- inventory-service down (critical dependency): peak mean 2.31 s, peak p95 3.62 s.

Checkouts kept succeeding when promise-service was down, but a customer waiting 4 to 9 seconds for a response would not call that graceful. The outage of the optional dependency was slower than the outage of the critical one, which is the opposite of what the design intends.

The CheckoutLatencyP95High alert (p95 above 0.5 s for 1 minute) did fire, so the slowdown was detected. The drill did not record how long it took to fire.

Latencies above the gateway's 2 s timeout mean that timeout does not bound the whole call. Likely cause, not yet verified: httpx applies its timeout to the connect, read and write phases, but not to name resolution, and Docker removes a stopped container's name, so the lookup may stall for several seconds before failing. The same stall would explain the one generator request that exceeded its own 5 s timeout. A check is to time a request to the stopped service's hostname from inside the gateway container.

The generator-side figures (about 2.9 s and 2.0 s per request, from throughput) were averages over each whole phase and understate these peaks. Because the generator is closed-loop, real open-loop traffic would see requests queue up and the impact would be larger.

### 3. Alert timing artifacts (severity: low)

- CheckoutDegraded stayed firing for 67 s after promise-service recovered because it uses a 1-minute rate window.
- GatewayHighErrorRate fired 58 s into incident 2, 30 s after CheckoutUnavailable. The ratio alert is a useful backstop but should not be the page.

## Action items

| Priority | Action | How we will know it is fixed |
|---|---|---|
| P1 | Persist inventory and order state in a database instead of process memory (PostgreSQL locally, RDS in Terraform) | Re-run the drill: `orders_without_reservation` is 0 after the inventory restart |
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
