# Ten-topic evaluation of the merged plain-prose ruling

Testing the merged rules from `ste80-humanizer` and the new readability constraints across ten topics keeps every sample under a 20.5-word sentence average and passes `prose-guard` with zero warnings.

## Adding literal verbs, cause-result items, and silent ordering closes four gaps without touching upstream files

Keeping [the upstream skill](file:///home/eric/data2/worktrees/nervous-bus/humanizerv2/steering/plain-prose/skills/ste80-humanizer/SKILL.md) untouched preserves clean syncs with `Dodaios/ste80-humanizer`, while updating [ruling.md](../hooks/ruling.md) and [cues.json](../hooks/cues.json) delivers the new constraints where sessions actually see them.

| Constraint added | Why `ste80-humanizer` missed it | Resolution applied |
|---|---|---|
| Literal verbs on inanimate nouns | Checks `serves as` and `boasts`, but permits `knows`, `tells`, `silently`, `sane` | Added directly to `ruling.md` and `cues.json` |
| Cause and result in every opening, heading, and list item | Removes bold-colon prefixes (§19), but gives no positive list pattern | Weave one **bold key phrase** inside a complete sentence |
| Silent priority ordering and abrupt ending | Removes chatbot closers (§22), but ignores ordering announcements | Order by impact silently and stop on the last point |
| Conversational replies and explanations | `prose-guard` checks edited Markdown, commits, and PR bodies only | Scoped `ruling.md` to chat replies and explanations too |
| Voice-guide and passive-voice boundaries | Strict active voice (S6) and terse reply rules can clash with natural phrasing | Use passive when it avoids awkward clauses; keep directive voice for briefs only |

## All ten topic drafts pass `prose-guard` after splitting long cause-fix sentences

Rewriting first drafts across ten domains exposed three recurring failure modes in model prose before any automated hook fired:

1. Packing **cause, condition, and fix into one sentence** pushed the average length of topics 07 and 09 to `25.3` and `28.4` words on the first pass, which dropped to `17.0` and `19.6` words once split into two sentences.
2. Relying on **developer slang and anthropomorphic words** slipped `spin up`, `hunt back up`, and `silent fallback` into technical drafts until the second pass replaced them with literal phrases (`start`, `scroll through`, `fallback scores with no warning`).
3. Starting **list items with gerund subjects** (`Spraying ...`, `Binding ...`) satisfies the cause-and-result rule cleanly, yet triggers `ing-form` warnings in `ste_check.py` (which `prose-guard` ignores so real documents stay quiet).

| Topic | Layout | Sentences | Avg words | Max words | `ste_check` non-`-ing` findings | `prose-guard` |
|---|---|---:|---:|---:|---|---|
| 01 Everyday glass cleaning | Numbered causes | 8 | 14.0 | 21 | 0 | Pass |
| 02 PostgreSQL sequential scan | Multi-paragraph | 6 | 19.5 | 28 | 1 (`sentence-length`) | Pass |
| 03 Distributed idempotency | Numbered causes + fixes | 8 | 18.4 | 26 | 0 | Pass |
| 04 UI inline validation | Table + short prose | 4 | 18.2 | 24 | 0 | Pass |
| 05 JWT signing key rotation | Numbered steps | 8 | 17.2 | 22 | 1 (`word`: `verify`) | Pass |
| 06 LLM-as-a-judge drift | Multi-paragraph | 6 | 20.3 | 24 | 1 (`word`: `whether`) | Pass |
| 07 Shared branch rebase | Short paragraph | 4 | 17.0 | 20 | 0 | Pass |
| 08 Retry loop in checkout handler | Sentence headings | 8 | 18.2 | 27 | 0 | Pass |
| 09 Auto loan vs savings yield | Bulleted comparison | 7 | 19.6 | 28 | 1 (`passive`: `is taxed`) | Pass |
| 10 Container Nginx 502 error | Numbered causes | 8 | 16.6 | 22 | 0 | Pass |

## Ten evaluated topic samples verify layout variety and literal phrasing

### 01 Why does my bathroom mirror still have streaks after I clean it?

Spraying too much cleaner or wiping with a cloth that holds fabric softener residue leaves streaks on glass.

Three habits cause almost every streak on a mirror:

1. Spraying **excess liquid** leaves detergent behind after the water evaporates. Mist the cloth twice instead of soaking the glass.
2. Washing microfiber towels with **liquid fabric softener** coats the fibers in wax that smears across the mirror. Wash cleaning cloths separately with plain detergent and no dryer sheets.
3. Cleaning a **warm mirror** right after a hot shower makes the liquid evaporate before you can wipe the dissolved film away. Wait until the glass cools to room temperature, then wipe in overlapping lines from top to bottom.

### 02 Why does this PostgreSQL query do a sequential scan even though orders(created_at) has an index?

PostgreSQL chooses a sequential scan when the query matches a large fraction of the table or wraps the indexed column in a function. When a query reads more than roughly 10 to 15 percent of rows, reading pages sequentially from disk is faster than jumping between the index and table pages.

Wrapping `created_at` in an expression such as `DATE(created_at) = '2026-11-18'` blocks the planner from matching the plain index on `created_at`. Rewrite the filter as a half-open range (`created_at >= '2026-11-18' AND created_at < '2026-11-19'`) so that the planner uses the B-tree index directly.

Stale table statistics also cause wrong scan choices after bulk inserts or deletes. Run `ANALYZE orders;` so that the planner has current row counts and value distributions before it picks a plan.

### 03 Why did two concurrent retries with the same idempotency key both charge the card?

Checking the idempotency table before inserting a row creates a race window where two concurrent requests both see no record and both call the payment processor.

Fix the race by making the database enforce uniqueness before any external call runs:

1. Inserting the **idempotency key** inside a unique-constrained row before calling the payment processor blocks the second request at the database lock. Put a `UNIQUE (account_id, idempotency_key)` constraint on the table and run `INSERT ... ON CONFLICT DO NOTHING` first.
2. Returning **an in-progress status** (`409 Conflict` or `202 Accepted`) to the duplicate caller stops it from starting a second charge while the first request is still running. Store a `pending` state on insert, then update that row with the final response after the processor replies.
3. Expiring **stale pending locks** after a short timeout prevents a crashed worker from blocking retries forever. Set a `locked_until` timestamp so that a later retry can reclaim the row if the first worker dies.

### 04 Should a signup form validate fields on every keystroke, on blur, or on submit?

Validate each field when the user leaves it (`onBlur`) for syntax errors, and validate on submit for cross-field rules and server checks. Keystroke validation flashes premature red errors before the user completes an email address. Submit-only validation forces the user to scroll through earlier fields to find what failed.

| Timing | Best use | Main drawback |
|---|---|---|
| On input (`onChange`) | Meters for password strength and character counters | Shows false errors before a field is complete |
| On exit (`onBlur`) | Email format, phone length, and required fields | Leaves the last field unchecked until submit |
| On submit | Cross-field rules and uniqueness checks on the server | Delays feedback until the user clicks submit |

Clear an existing error on the next keystroke as soon as the input becomes valid so that the form removes the warning right away.

### 05 Why did rotating our JWT signing key log out every active user right away?

Replacing the active key with a single new key causes verification to fail for every token that the old key signed. Services that validate tokens need a grace window where they accept both keys at the same time.

Rotate signing keys in three ordered deployments so that active sessions stay valid:

1. Publishing **the new public key** alongside the old public key lets every service verify both old and new tokens. Keep the authentication server signing with the old key during this step.
2. Switching **the signing key** on the authentication server to the new private key starts issuing tokens that carry the new `kid` header. Existing tokens continue to pass verification against the old public key until they expire.
3. Removing **the old public key** after the longest token lifetime passes ends support for old tokens with zero dropped sessions.

### 06 Why did our LLM-as-a-judge accuracy score jump 12 points when we only changed the output format from JSON to prose?

Changing the output format alters token ordering, length limits, and parse failures in the judge prompt rather than answer quality. Switching from JSON to prose often changes whether the judge writes an explanation before or after its score.

When a model outputs the numeric score first, the subsequent explanation tends to justify that number instead of checking each rubric rule. Asking the judge to list concrete defects first and emit the score last usually lowers inflated scores and improves agreement with human labels.

JSON formatting also triggers fallback scores with no warning when the parser drops malformed responses. Inspect the rate of raw parse errors across both runs to confirm that the 12-point shift came from scoring behavior rather than dropped rows.

### 07 Why did rebasing our shared branch create duplicate commits for my teammate?

Rebasing rewrites every commit on the branch with a new SHA hash while keeping the same commit message and diff. When your teammate runs `git pull`, Git merges their original local commits with your rewritten remote commits. That merge places two copies of every change into the branch history. Use `git merge` on branches that two or more people pull from, or coordinate a single owner before you run `git pull --rebase`.

### 08 What is wrong with keeping this 140-line retry and backoff loop inside the HTTP checkout handler?

Keeping a 140-line retry loop inside the HTTP checkout handler ties up worker threads for incoming requests during outages and hides retry bugs behind HTTP test setup.

Long retries block request workers and trigger gateway timeouts. Sleeping inside an HTTP handler holds a web server thread open for the full backoff window. When the downstream payment gateway slows down, concurrent checkouts exhaust the worker pool and cause unrelated endpoints to return `504 Gateway Timeout`. Move the retry loop into a background job queue or cap the total handler wait below the upstream proxy timeout.

Inline retry logic duplicates backoff code and skips unit tests. Mixing backoff math, jitter, and status filtering into a controller forces tests to start the full HTTP stack for every edge case. Extracting a small `retry_with_backoff` helper lets unit tests check `429` handling, `Retry-After` headers, and max-attempt limits in milliseconds with a fake clock.

### 09 Should I pay off a 4.5% fixed auto loan early or keep the cash in a 4.0% high-yield savings account?

Keeping the cash in savings preserves cash access, while paying off the loan saves roughly 1.3 percentage points after taxes if savings interest is taxed as ordinary income.

At a 22 percent federal marginal tax rate, a 4.0 percent savings yield leaves 3.12 percent after taxes. Paying down a 4.5 percent loan gives a guaranteed, tax-free 4.5 percent return:

- Paying **extra principal on the loan** saves $13.80 per year for every $1,000 that you shift out of a taxed 4.0 percent account. Fund a three-to-six-month emergency reserve before you prepay, because you cannot withdraw equity from a car loan without selling or refinancing the car.
- Keeping **the cash in savings** costs about $1.15 per month per $1,000 compared with prepaying the loan. That small cost buys immediate cash if your income drops or a large expense arrives.

### 10 Why does `curl http://localhost:8080/health` work inside the container while external HTTPS requests return `502 Bad Gateway`?

The application binds to `127.0.0.1:8080` instead of `0.0.0.0:8080`, or the reverse proxy targets the wrong container port or network.

Check these three mismatches in order from inside the host:

1. Binding the app server to **loopback (`127.0.0.1`)** blocks connections that arrive from outside the container namespace even when `localhost` works inside it. Change the listen address to `0.0.0.0:8080` so that the bridge interface can reach the socket.
2. Pointing the Nginx `proxy_pass` directive at a **stale port or container name** causes Nginx to reject the upstream connection immediately. Confirm that the `upstream` port matches the `EXPOSE` and listen port in the container spec, and inspect `/var/log/nginx/error.log` for `Connection refused`.
3. Connecting two containers on **separate Docker networks** prevents name resolution between Nginx and the app container. Attach both services to the same user-defined bridge network in `docker-compose.yml`.
