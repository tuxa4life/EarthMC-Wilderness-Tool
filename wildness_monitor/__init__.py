"""Wildness Monitor — EarthMC wilderness alert system for the Georgia nation.

Package layout:
    config        all tunable constants + secrets (env-overridable)
    http          shared HTTP retry helper
    logkit        logging, stats counters, error/duration formatting
    earthmc_api   raw EarthMC API endpoints (online players, location, players)
    geometry      pure math: compass headings
    tracking      position/wilderness/movement caches + wilderness detection
    towns         open-towns cache (markers parse, verify, rank)
    residents     Georgia resident list (fetch/save/load/refresh)
    alerts        outbound Discord webhook alerts
    monitor       orchestration: main loop + API-down state machine
    cli           developer CLI (player lookup, closest-town)
"""
