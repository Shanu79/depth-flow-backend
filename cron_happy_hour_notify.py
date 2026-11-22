"""
One-shot notification sweep: "ending soon" / "ended" notices, yearly month-unlock notices.

The API process already runs this every 60 s (NOTIFY_SWEEP_ENABLED, default on). Schedule this script too
(every minute, e.g. cron `* * * * *`) if your host puts idle web processes to sleep. It is idempotent:
dedupe keys make overlapping runs harmless.
"""
import logging

import notifications as nt

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(f"Notification sweep: {nt.run_sweep_once()}")
