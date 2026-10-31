# Label changes

Labels were corrected after seeing the first runs, so scores against the corrected labels are not blind.

| Alert | Old | New | Reason (from rubric) |
|---|---|---|---|
| 001 | high | high | restarts, degraded impact, no confirmed outage (no change) |
| 007 | medium | high | p99 2.4s for 20m is degraded user impact |
| 009 | high | medium | cert expires in 6 days, not imminent |
| 012 | critical | high | one log line, no confirmed outage now |
| 013 | low | low | ambiguous: not stated whether isolated (no change) |
| 018 | medium | medium | at max, not noise, but no baseline for latency (no change) |
