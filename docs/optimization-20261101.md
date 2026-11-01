# Navigation and performance changes — 2026-11-01

The Mini App has a Telegram back button, with an on-page fallback for browsers and older Telegram clients. Back closes the agreement first, returns from checkout to tariffs while keeping the payment request, and closes the Mini App from the tariff or approved-contact screen. An in-flight checkout cannot redirect to Finik after Back. A separate return-to-payment button restores the receipt flow.

Payment polling now runs only when a checkout may exist. It pauses while the page is hidden and resumes when visible. Generation checks prevent an earlier request from creating a second polling loop. Telegram ready/expand and back-handler registration run once per SDK instance. Bank requests, payment approval rules, tariffs and access expiry remain unchanged.

The queue's daily status uses one aggregate query and one result row instead of two sorted event-history queries. Ordinary publications and external repeats keep their existing accounting, including legacy receipt markers and snapshot-only records. The totals now come from one database statement.

Historical deduplication selects 8 apartment columns instead of all 30, avoiding descriptions and unrelated payment/publication fields. District normalization and photo identity parsing have bounded caches (1,024 districts and 16,384 photo URLs). The matching rules are unchanged.

Validation:

- Full local suite: 923 passed, 5 skipped. PostgreSQL checks run separately in CI.
- UI regression cases cover Back, agreement dismissal, payment restoration, Back during an in-flight request, native navigation, older clients and paused/background polling.
- Mobile browser preview and a mocked lifetime checkout verify navigation without sending a real payment.
- Aggregate-query tests compare the old event lists with the new counters and assert a single database query.
- Synthetic local benchmark: building indexes for 5,000 historical plus 600 recent listings, median of 7 runs, 94.90 ms before / 53.73 ms after (43.4% lower). This measures index construction, not overall production latency.

No schema migration, publication-history reset, advertising purchase, account/session change, collector cadence change or tariff change is needed for this deployment.
