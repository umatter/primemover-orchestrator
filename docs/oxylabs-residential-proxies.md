# Oxylabs Residential Proxies — integration notes

Research notes for moving the PrimeMover setup from GeoSurf to Oxylabs residential
proxies. Every factual claim below is sourced to a page under
`https://developers.oxylabs.io`; anything not sourced is marked as an assumption
or an open question.

Docs tip: append `.md` to any docs URL for clean markdown
(`.../session-control.md`). The site also has an index at `/llms.txt`.

---

## 1. What already exists on our side

Oxylabs is **not** a greenfield integration — a partial one is already in the
tree and has been for some time. Inventory:

| Layer | What exists | File |
|---|---|---|
| API schema | `oxylabs` is a declared proxy type | `primemover_api` `Proxy.php` — `TYPES = ['http','geosurf','brightdata','oxylabs']` |
| API storage | `hostname`, `port`, `username`, `password`, `type` columns; one proxy per crawler | `proxies` table (`2020_04_30_112454_create_proxies_table.php`) |
| Runner model | `OxylabsProxy extends HttpProxy implements ISessionProxy` | `primemover/model/proxy/OxylabsProxy.java` |
| Runner dispatch | `type.contains("oxylabs")` → `OxylabsProxy`, location from **the crawler's agent** | `primemover/api/converter/PrimemoverApiConverter.java:49` |
| Session injection | `%%SESSIONID%%` placeholder substituted per session | `primemover/capture/SessionDecorator.java:71` |
| Credential override | Spring props `oxylabs.overwrite.user` / `oxylabs.overwrite.password` | `SessionDecorator.java:27-30` |
| Location source | `crawler.getAgent().getLocation()` — the Agent's `location` column | `primemover_api` `Agent.php` (fillable) |
| Spec default | `location: US-IL-CHICAGO` | `experiment_designer/spec/parser.py:44`, `converter.py:297,366` |

The username the runner currently builds:

```
customer-{USER}-sessid-{SESSION}-{LOCATION}-sesstime-30
```

…where `{LOCATION}` is the agent's `location` column spliced in **raw**.

---

## 2. Why the existing integration will not work as-is

Three real defects (2.1–2.3), one thing that is fine but for a non-obvious reason
(2.4), and two gotchas that will cost an afternoon each if unknown (2.5–2.6).
Most of it is in or adjacent to `OxylabsProxy.getUsername()`.

### 2.1 Token order is wrong — location must precede `sessid`

Every documented example puts location **before** the session token. The
canonical order is `customer → cc → city → st → sessid → sesstime`:

> `customer-USERNAME-cc-US-city-CITY-sessid-abcde12345-sesstime-5:PASSWORD`
> — [making-requests.md]

The docs contain **zero** examples with a location token after `sessid`. Our
builder emits `-sessid-{SESSION}-{LOCATION}-`, i.e. exactly the unsupported
order. The location segment needs to move to sit between `customer-{USER}` and
`-sessid-`.

### 2.2 Location values are injected raw, but Oxylabs requires typed tokens

Our code does `.append("-").append(this.getLocation())`. Oxylabs location values
are **never bare** — they are keyed, typed tokens (`cc-DE`, `cc-GB-city-london`,
`st-us_california`). Our values are **GeoSurf-shaped**. The canonical example in
the API's own OpenAPI annotation (`AgentController.php:24`) is:

```
location = "US-CO-COLORADO_SPRINGS"
```

(The legacy GeoSurf seeder confirms the lineage: `us-30m.geosurf.io:8000` with
username `167462+US-MA-BOSTON+167462-982060`.)

So the string the runner emits today is:

```
customer-{USER}-sessid-{32 lowercase hex}-US-CO-COLORADO_SPRINGS-sesstime-30
```

`US`, `CO` and `COLORADO_SPRINGS` are three **unkeyed** segments. A sweep of all
52 distinct `customer-…` forms across the docs found no unkeyed location example
anywhere. Documented consequence: HTTP **400 `Bad Request` / `Unsupported
parameters were used.`**

⚠️ **The worse failure mode is that it does *not* 400.** If Oxylabs silently
ignores the unrecognised segments, we get a perfectly working proxy with a random
worldwide exit IP while the experiment record claims a specific geography. That
is a silently corrupted dataset, not an outage. Which of the two happens is
undocumented — see §11.

This is the single largest piece of work: a **location translation layer**. See §5.

### 2.3 Session IDs are lowercase hex, 32 chars

`SessionDecorator.java:39`:

```java
sessionId = prefix + UUID.randomUUID().toString().replace("-", "");
```

That yields a 32-character **lowercase** hex string. The docs specify:

> `sessid` … Random string, 0-9, and A-Z characters are supported.
> — [making-requests.md]

Lowercase is not documented as supported (the help-center loosely says
"alphanumeric string of your choice", so it may well work). No maximum length is
documented anywhere; published examples are 5–10 characters. Stripping the
hyphens is correct and important — a hyphen would corrupt the hyphen-delimited
token stream. **Cheap fix:** `.toUpperCase()` and consider truncating, which
makes it unambiguously conformant at no cost.

### 2.4 `sesstime-30` is fine — but for a subtle reason

Good news: `30` is the **best-attested value in the entire documentation set**.
It appears verbatim in the session-control credential list
(`customer-USERNAME-cc-GB-sessid-zxcvb-sesstime-30:PASSWORD`) and is the exact
value Oxylabs publishes in its own **Multilogin** integration guide.

The docs contradict themselves on the ceiling, but the contradiction is
resolvable — **the real maximum is 1440 minutes and the "30" is stale**:

| Source | Stated maximum |
|---|---|
| `making-requests.md` parameter table | "The maximum session time is **30 minutes**." |
| `making-requests.md` grammar figure, dated **upd 2025-06** | "To control the session time (max — **1440 min**)" |
| `session-control.md` | "maintain the same IP for up to **1440 minutes** (24 hours)" |

Two sources say 1440, including the newest-dated artifact **on the same page** as
the "30" claim. So the range is 1–1440 (no minimum stated; smallest example is 5).

Keep the hardcoded `30` anyway — it is safe under every reading and is the value
Oxylabs itself publishes for Multilogin. But note two things if it is ever
revisited: `oxylabsSessionMinutes` is `private static`, i.e. **one value shared by
every crawler**, and raising it does not buy IP stability on its own (§6).

### 2.5 `oxylabs.overwrite.user` replaces the *whole* username, not the account name

Easy to get backwards. `SessionDecorator.java:76-88` assigns
`tmp = innerProxy.getUsername()` — the **fully assembled**
`customer-…-sesstime-30` string — and then, if the override property is set, does
`tmp = oxylabsUserOverwrite`, discarding the assembled string entirely.
Placeholder substitution runs *afterwards*.

So `oxylabs.overwrite.user` must be a **complete username template**, including the
`customer-` prefix, the location tokens, and a literal `%%SESSIONID%%` if you want
a session. Setting it to a bare account name silently disables all geo targeting
and session control.

Useful for a quick manual override; a footgun if mistaken for a credential field.

### 2.6 No Oxylabs credentials exist in this deployment yet

`primemover-runner/config/application.properties` sets only the **geosurf**
overwrite pair; the Oxylabs pair is absent, and no Oxylabs secret appears in
either `.env`. Nothing can be tested end-to-end until an account and a Proxy User
exist. Note that Oxylabs' "Proxy User" is a **separate entity from the dashboard
login** — the Proxy User credentials are what belong in `proxies.username` /
`proxies.password`.

---

## 3. Connection facts

| Setting | Value | Note |
|---|---|---|
| Host | `pr.oxylabs.io` | single backconnect entry node |
| Port | `7777` | rotating by default; stickiness comes from `sessid`, **not** a different port |
| Username prefix | `customer-` | mandatory |
| Protocols | HTTP, HTTPS, HTTP/3, SOCKS5 | |
| HTTPS to the proxy | `https://pr.oxylabs.io:7777` | same host/port, just the scheme |
| Geo self-check | `https://ip.oxylabs.io/location` | the GeoSurf `geo.geosurf.io` replacement |

### How the proxy chain actually works here (matters for everything below)

Chrome/MLX does **not** talk to Oxylabs. The real chain is:

```
Chrome (MLX profile)  →  BrowserUp recorder proxy (local, LittleProxy)  →  Oxylabs
```

`BrowserUpRecorderProxy.java:169-188` configures Oxylabs as BrowserUp's *chained
upstream*:

```java
proxy.chainedProxyAuthorization(username, password, AuthType.BASIC);
proxy.setChainedProxy(new InetSocketAddress(decoratedProxy.getUrl(), decoratedProxy.getPort()));
proxy.setChainedProxyHTTPS(typedTargetProxy.useHttps());   // OxylabsProxy sets this true
```

Three consequences:

- **The Oxylabs credential never reaches Chrome.** It is a Java-side HTTP Basic
  credential on the chained proxy. So the documented warning that "`SOCKS5` proxy
  authentication is not supported by Google Chrome" does not apply to us
  directly — but SOCKS5 is still out, because LittleProxy's `setChainedProxy`
  chains over HTTP(S) only. **HTTP or HTTPS to Oxylabs.**
- `setUseHttps(true)` means BrowserUp connects to `https://pr.oxylabs.io:7777`,
  which the docs do support on the same host/port. But Oxylabs warns "some
  libraries (or their older versions) … may not support `HTTPS` protocol", and
  LittleProxy's HTTPS-upstream support is exactly the kind of thing that warning
  is about. **Smoke-test this specifically**; falling back to `useHttps=false` is
  a one-line change if it misbehaves.
- **There is no Oxylabs-specific header block** in `BrowserUpRecorderProxy` —
  Oxylabs targeting rides entirely on the username string. Note the contrast with
  GeoSurf, which is *not* username-based at all: it injects `loc` and `X-session`
  as CONNECT headers via an `HttpFiltersSourceAdapter` (line ~195). So the two
  providers use fundamentally different mechanisms, and the Oxylabs path being
  username-only is correct.

That same GeoSurf filter is, incidentally, the proof that **coordinate targeting
is feasible for us**: `X-Oxylabs-Geolocation` is a proxy/CONNECT header, and we
already have a working pattern for injecting exactly that.

### Ignore Oxylabs' Multilogin guide

Oxylabs publishes `integrations/proxy-integrations/multilogin.md`, and it is
tempting because it names our browser. **It does not apply to our architecture.**
It documents typing host/port/username into Multilogin's *GUI proxy dialog* —
there is no coverage of the MLX profile API payload, and it does not distinguish
classic Multilogin from Multilogin X. Since we chain through BrowserUp, MLX
should keep pointing at the local recorder proxy and know nothing about Oxylabs.

It is still useful as corroboration — it independently confirms
`pr.oxylabs.io` / `7777` / the `customer-` prefix, and it is where
`customer-username-sessid-qwerty123-sesstime-30` appears verbatim, which is the
strongest single endorsement of our hardcoded `sesstime-30`.

### IP whitelisting (alternative to credentials)

Already accommodated: `BrowserUpRecorderProxy` only calls
`chainedProxyAuthorization(...)` when username *and* password are both non-empty
(the code comments this as the "Whitelisting check"). So leaving them blank in
the `proxies` row is a supported path today.

With whitelisting, parameters move from the username into the **subdomain**,
against a different host label: `<params>.bc.pr.oxylabs.io:7777`. That would push
per-crawler geo targeting into DNS names, which fits our schema worse than the
username approach. Not recommended, but it is the documented escape hatch.

---

## 4. Username grammar reference

All targeting and session control is carried in the HTTP Basic **username**.
Mandatory tokens are `customer` and `password`.

| Token | Value format | Notes |
|---|---|---|
| `customer` | our proxy-user name | mandatory prefix |
| `cc` | ISO 3166-1 alpha-2, case-insensitive | `cc-US`, `cc-DE`, `cc-GB` |
| `city` | English name, lowercase, `_` for spaces | **must be accompanied by `cc`**; `cc-DE-city-berlin`, `city-rio_de_janeiro` |
| `st` | US state, `us_` prefix | `st-us_california`; US-only. Valid **without** `cc`: `st-us_illinois-city-springfield` |
| `cn` | continent code | exactly seven: `cn-AF cn-AN cn-AS cn-EU cn-NA cn-OC cn-SA` |
| `postalcode` | 5-digit US ZIP | **requires `cc`**: `cc-US-postalcode-90210`. US-only, matched via MaxMind |
| `sessid` | `0-9`, `A-Z` | sticky session; rotates on drop |
| `sessid_oneip` | same | pins to one exit node; **502 instead of rotating** |
| `sesstime` | integer minutes | used together with `sessid`; see §2.4 |
| `platform` | `windows` `macos` `linux` `android` `ios` | **gated** — needs Account Manager + possible KYC |
| `ipversion` | `4` or `6` (bare digit) | **gated**; not `ipv4` |

**Coordinates are not a username token.** They use a proxy header:

```
--proxy-header "X-Oxylabs-Geolocation: {lat}:{lon};{radius_miles}"
```

Radius has a hard floor of 10 miles. Sent on CONNECT, not as a normal request
header — awkward through BrowserUp; probably not worth it for us.

---

## 5. The location mapping problem (main work item)

Our agents carry `US-CO-COLORADO_SPRINGS`. Oxylabs wants keyed tokens. Something
must translate.

### The mapping, using documented forms only

⚠️ **Do not invent `cc-US-st-us_colorado-city-colorado_springs`.** Combining `cc`
**and** `st` in one username is **not documented anywhere**. The two documented
shapes are mutually exclusive in the published examples:

| Form | Documented as |
|---|---|
| `cc-US-city-colorado_springs` | `city` "must be accompanied by `cc`" — `cc-GB-city-london` |
| `st-us_colorado-city-colorado_springs` | state page's valid combos: `st-us_illinois-city-springfield` — **no `cc`** |

Pick one and stick to it. `cc-US-city-…` is the safer default: it works for every
country, whereas `st-` is US-only, so a `cc`-based mapper is the only one that
generalises when a study adds a non-US condition.

| Ours | Oxylabs (`cc` form) | Oxylabs (`st` form) |
|---|---|---|
| `US-CO-COLORADO_SPRINGS` | `cc-US-city-colorado_springs` | `st-us_colorado-city-colorado_springs` |
| `US-MA-BOSTON` | `cc-US-city-boston` | `st-us_massachusetts-city-boston` |
| `US-IL-CHICAGO` | `cc-US-city-chicago` | `st-us_illinois-city-chicago` |
| country only | `cc-US` | n/a |

Note the middle segment (`CO`, `MA`, `IL`) is a **two-letter state code, which has
no Oxylabs equivalent** — `st` wants the full name as `us_colorado`. So the mapper
must either expand the abbreviation or drop it. Dropping it is fine in the `cc`
form.

### Validate against Oxylabs' own lists — they are plain fetchable files

Both canonical lists are downloadable, not opaque assets:

- **US states** — `us_states.txt`, 49 lines
  (`content.gitbook.com/content/BQ7Zf9paoN3FTeGcyfY1/blobs/cVjpiu1GKicluiVIT9og/us_states.txt`)
- **Cities** — `City_list.csv`, 10,437 rows, `Country;City` format, 2,120 of them US
  (`content.gitbook.com/content/BQ7Zf9paoN3FTeGcyfY1/blobs/sWaiK5UGhLNaDBE9NDTt/City_list.csv`)

Vendor these into `experiment_designer` as validation tables. `US;colorado_springs`
and `US;boston` are both present, so our existing geographies do map.

### Where the translation belongs

**In the `experiment_designer` / Laravel layer, not in Java.** Reasons:

- Validation belongs where specs are authored — `pmspec validate` can reject an
  unmappable location before an experiment is ever uploaded, which is far better
  than discovering it as a 400 mid-run.
- The rules are fiddly and worth unit-testing in Python: city requires `cc`;
  multi-word cities need `_`; `st` is US-only; ZIP is US-only.
- It keeps `OxylabsProxy.java` dumb — it should splice in an
  already-valid token string and nothing more.

Under that split, `OxylabsProxy.getUsername()` reduces to fixing the ordering
(§2.1) and splicing in an already-valid token string.

### ⚠️ Decision needed: `agents.location` cannot serve both grammars

This is the one architectural choice that has to be made before any code is
written. GeoSurf reads `agents.location` and puts it in the `loc` CONNECT header
in **its own format**; Oxylabs needs the keyed-token format. The same column
cannot hold both.

Two options:

1. **Repurpose `agents.location`** to the Oxylabs grammar — simplest, but it
   breaks the GeoSurf path, so it is only safe as part of a clean cutover with no
   intent to run GeoSurf again.
2. **Add a provider-agnostic geo field** (e.g. structured `country` / `state` /
   `city`) and derive each provider's string from it. More work, but it keeps both
   providers runnable and makes the experiment record store the *intent* rather
   than one vendor's encoding — which is the better answer for reproducible
   research.

Recommend option 2. Note `AgentStoreRequest.php` currently validates `location`
as only `['nullable']` — there is nothing to migrate around, and nothing catching
a bad value today.

⚠️ Availability is never guaranteed at city/ZIP level: the residential pool is
dynamic, and Oxylabs repeats that narrowing location has no availability
guarantee. An over-narrow filter is a documented cause of
`502 / Exit node not found`. For experimental designs this matters — a condition
that silently cannot be filled is worse than one that fails loudly.

---

## 6. Session strategy — the important design decision

Our requirement: a **stable exit IP for the duration of a job**, so the target
site sees one coherent session.

Plain `sessid` does **not** give us that:

> By default, a session lasts 10 minutes, or ends after 60 seconds without any
> requests – whichever comes first. Once that happens, a new IP address is
> assigned automatically. — [session-control.md]

> If you pause for 60 seconds or the IP goes offline, the system assigns a new
> one, so your next request with `sessid-abcd1234` is routed through a
> **different IP**. — [session-control.md]

Two consequences that bite us specifically:

1. **The 60-second inactivity rule is dangerous for browser automation.** Our
   crawlers have human-like think time and dwell pauses. Any pause over 60s can
   silently rotate the exit IP mid-job even though `sesstime-30` has not
   elapsed. Reusing the same `sessid` does **not** resurrect the old IP — it just
   gets rebound to a new one, and the crawler never notices the geography
   changed underneath it.
2. **`sesstime` is a hard cutoff**, not a graceful drain: "The session expires
   once the time limit is reached, even if some requests are still in progress."
   Jobs must be scoped to finish inside the window.

### Use `sessid_oneip`

> With `sessid_oneip`, the session is bound to a single exit node: once that IP
> is no longer available, instead of rotating to a new one, the request fails
> with an HTTP `502` response. — [session-control.md]

For data validity, a loud 502 we can detect, log against the correlation ID, and
retry with a fresh session is **strictly better** than an undetected mid-job IP
swap. It combines with `sesstime` identically, so it is a one-token change.

**Recommendation:** make `sessid` vs `sessid_oneip` a flag on the proxy record,
defaulting to `sessid_oneip` for experiment runs.

### Alternative: country-specific sticky entry nodes

Geography can instead be encoded in **host + port**, needing no username
parameters at all:

```
us-pr.oxylabs.io:10001-19999      gb-pr.oxylabs.io:20001-29999
de-pr.oxylabs.io:30001-39999      fr-pr.oxylabs.io:40001-49999
ch-pr.oxylabs.io:39001-39999      ca-pr.oxylabs.io:30001-39999
pr.oxylabs.io:10000-49999  (random pool)
```

This is a *very* natural fit for our schema — one `hostname`/`port` pair per
crawler row, which is exactly what the `proxies` table already models, and no
username munging at all. But two disqualifiers for most of our designs:

- stickiness is only **10 minutes** (vs 30 via `sesstime`)
- "Country-specific ports do not support **city-level** results"

So: viable for country-level studies, ruled out the moment a design needs a city
condition. Keep it in mind as the simpler path for country-only experiments.

---

## 7. Replacing `AssertProxyByGeosurfJobHandler`

The current handler browses to `https://geo.geosurf.io/` and parses IP info.
The Oxylabs equivalents are mandated — third-party IP checkers are a
**restricted target category** (`ipinfo.info`, `2ip.io` are named), and Oxylabs
requires its own:

- `https://ip.oxylabs.io`
- `https://ip.oxylabs.io/json`
- `https://ip.oxylabs.io/location`

Note this makes `https://geo.geosurf.io/` itself a **restricted target** on
Oxylabs — the current handler's URL cannot simply be left in place.

### ⚠️ The response is not flat, and the providers disagree

`/location` aggregates **four** databases and nests them. A live fetch returns:

```json
{"ip":"…","providers":{
  "dbip":        {"country":"CH","asn":"AS559","org_name":"SWITCH","city":"Zurich","zip_code":"", …},
  "ip2location":{"country":"CH","asn":"","org_name":"","city":"Zurich","zip_code":"8099", …},
  "ipinfo":     {"country":"CH","asn":"AS559","org_name":"SWITCH","city":"", …},
  "maxmind":    {"country":"CH","asn":"AS559","org_name":"SWITCH","city":"","time_zone_identifier":"Europe/Zurich", …}}}
```

There is **no top-level `country` or `city`** — you must choose a provider. And in
that live sample the providers disagreed: `dbip` and `ip2location` said
`"Zurich"`, while `ipinfo` and `maxmind` returned an empty string.

This breaks a naive port. `IpInfo.java` is flat (`ip, country, state, city, loc,
postal, isp, asn, organization`) and `AssertProxyByGeosurfJobHandler` does a
straight `assertEquals(typedJob.getCity(), ipInfo.getCity())`.

The trap: Oxylabs assigns its own `cc`/`st`/`city` values **from MaxMind**, yet
MaxMind is one of the providers that returned an empty city. So asserting city
against MaxMind can fail *even when the targeting worked correctly*.

**Assert country strictly; treat city as best-effort across providers** (e.g. pass
if any provider matches, and log the disagreement). An over-strict city assertion
would fail valid runs and quietly bias which conditions survive.

The good news: the mechanism ports cleanly. `/location` returns
`application/json`, so Chrome renders it in a `<pre>` tag — exactly what the
GeoSurf handler already assumes.

New work: an `AssertProxyByOxylabsJobHandler` plus a job type in the spec DSL.
`IpInfo` currently lives in package `primemover.model.geosurf` and is reused by
`AssertProxyByPrimemoverApiJobHandler` — move it somewhere provider-neutral and
give it a nested-providers shape.

---

## 8. Error handling

Proxy-layer errors carry an **`X-Error-Description`** response header with detail
text. If the error came from the target site rather than Oxylabs, that header is
**absent** — so its presence/absence is the documented way to distinguish "our
proxy failed" from "Google/YouTube served a bot challenge". That distinction is
worth surfacing in the trace builder.

| Code | Meaning for us | Retry? |
|---|---|---|
| 400 `Unsupported parameters were used.` | malformed username — the error our current location bug produces | no, fix the string |
| 400 | SOCKS4 attempted | no |
| 403 `Restricted target.` | the target is blocked for our account | no, contact support |
| 407 `Access Denied` | **either** bad credentials **or** traffic quota exhausted — indistinguishable by status alone, must read `X-Error-Description` | no |
| 502 `Session failed. Please start a new session.` | dead sticky session | mint a **fresh** sessid, don't retry the same one |
| 502 `Exit node not found (session ended)` | session expiry, or an over-narrow location filter | new session / widen filter |
| 500, 504, 522 | transient | "Please try again." |

No numeric retry count, backoff interval, or retry budget is documented anywhere.
Note the table has **no 429 and no 525** row, although a help-center page names
`525 No Exit Found` — treat 525 as possible-but-undocumented.

---

## 9. Restricted targets — YouTube and Google

**This needs resolving before committing an experiment wave.**

Neither `youtube.com` nor Google Search appears on the Residential Proxies
restricted-targets page. The 8 published categories include
"Entertainment & streaming" — which names only Netflix, Spotify, Twitch and
Disney — and "Ads", which names only Google Tag Manager and Doubleclick (ad-tech
beacons, not the SERP).

But the list is explicitly **non-exhaustive** ("includes but is not limited to"),
and the help-center publishes a *materially different* version of the same table
(no "Ads" row, different IP-checker examples). So "not listed" is **not** a
guarantee.

Practical stance — both of these, not one:

1. Email `support@oxylabs.io` naming `youtube.com` and `google.com` explicitly,
   get written confirmation, and complete any KYC. Some restricted targets can be
   unlocked this way, and Oxylabs asks you to confirm access **before
   purchasing**.
2. Run a smoke crawler and watch for HTTP **403 `Access Denied` /
   `Restricted target.`** — the exact documented block signal.

The 8 published categories, verbatim, prefaced by "**❗️The list includes but is not
limited to targets such as:**":

| Category | Examples given |
|---|---|
| Entertainment & streaming | Netflix, Spotify, Twitch, Disney |
| Banking / financial | PayPal, Bank of America, Binance |
| Government websites | usa.gov, canada.ca |
| Gaming | Playstation, Steam |
| Ticketing | Ticketmaster, Eventbrite |
| Mailing | Outlook, Yahoo mail |
| Ads | **Google Tag Manager, Doubleclick** |
| IP checking websites | ipinfo.info, 2ip.io |

### ⚠️ A second-order confound nobody has checked

A Google SERP page pulls **Google Tag Manager and Doubleclick subresources**, and
those *are* in the restricted "Ads" category. So even if `google.com` itself is
permitted, those subresources may be blocked, and the page could render
atypically. For a study measuring what users see, that is a confound in the data,
not merely a technical wrinkle — and it would not show up as a failed job.

Worth checking explicitly in a smoke run: capture a SERP HAR through Oxylabs and
diff the request list against one captured without a proxy.

---

## 10. Public API — sub-users and cost attribution

Base: `https://residential-api.oxylabs.io/v2` · Swagger:
`/v2/docs` · OpenAPI spec: `https://residential-api.oxylabs.io/swagger-ui/v2/spec.yaml`

Auth is two-step: HTTP Basic against `POST /v2/login` returns `{user_id, token}`;
every other call uses `Authorization: Bearer {token}`. **The JWT expires after 1
hour** and must be regenerated — the same token-refresh pattern
`trace_builder.py` already implements for the PrimeMover API.

Eight paths:

```
POST   /login
GET    /users/{userId}/sub-users
POST   /users/{userId}/sub-users
GET    /users/{userId}/sub-users/{subUserId}
PATCH  /users/{userId}/sub-users/{subUserId}
DELETE /users/{userId}/sub-users/{subUserId}
GET    /users/{userId}/sub-users/{subUserId}/target-stats
GET    /users/{userId}/client-stats
```

`createSubUser` accepts `username` (letters, digits, underscores **only**),
`password` (12–64 chars, needs lower + upper + digit + one of `#_~+=`),
`traffic_limit` in **GB** (`null` = unlimited, `0` = no more traffic),
`lifetime`, `auto_disable`.

**This is how we get per-experiment cost attribution:** one sub-user per
experiment with a `traffic_limit`, then `target-stats` / `client-stats` for
usage. `auto_disable` plus a limit is a hard spend cap — genuinely useful given
residential is billed by traffic. Worth an `orchestrator` command and an MCP tool.

Gotcha: the "main user" whose credentials authenticate `/login` is defined as the
**first sub-user in the dashboard users list**, not a separate account identity.
And if that password was changed recently, `/login` fails and you must use the
*original* password or contact support.

---

## 11. Open questions

### Blocking — resolve before writing the migration

1. **Is username token ORDER enforced?** Undocumented; the word "order" appears on
   no page, and no example supports our current order. **The failure mode fails
   open** — a working proxy with a wrong-country exit IP. Reordering to the
   documented form costs nothing, so do it regardless rather than betting on
   order-insensitivity. Once credentials exist, one probe settles it:
   ```bash
   curl -x pr.oxylabs.io:7777 -U "customer-USER-sessid-ABC123-cc-DE-sesstime-30:PASS" https://ip.oxylabs.io/location
   curl -x pr.oxylabs.io:7777 -U "customer-USER-cc-DE-sessid-ABC123-sesstime-30:PASS" https://ip.oxylabs.io/location
   ```
2. **Is an unkeyed location segment rejected (400) or silently ignored?** Decides
   whether today's code fails loudly or quietly corrupts geography (§2.2).
3. **What is the concurrency limit for Residential?** There is **no residential
   fair-usage page** in `llms.txt` (only ISP, Dedicated ISP, Datacenter, Dedicated
   Datacenter) and **no 429 row** in the response-code table. Do **not** assume the
   ISP 100-session / 50 GB rule applies. Unbounded risk for a many-parallel-crawler
   design — ask the account manager.
4. **Written clearance for `youtube.com` and `google.com`** (§9).
5. **Do our target geographies actually have pool depth?** `City_list.csv` lists the
   cities, but coverage is explicitly not guaranteed: "due to the dynamic nature of
   Residential Proxies we do not guarantee we will have proxies there at specific
   moment in time." A condition that silently cannot be filled is worse than one
   that fails.

### Decisions for us, not questions for Oxylabs

6. `cc-US-city-…` **or** `st-us_…-city-…`? Pick a documented form; do not invent
   the combination (§5).
7. Repurpose `agents.location` or add a provider-agnostic geo field (§5).
8. `sessid` or `sessid_oneip` — silent IP swap vs loud 502 (§6). And whether
   `oxylabsSessionMinutes` stops being `private static`.

### Smaller open items

9. Does `sessid` accept **lowercase**? Our UUID hex is lowercase (§2.3). No max
   length documented either.
10. Are `sessid` values namespaced per sub-user? If not, colliding session strings
    across parallel crawlers could share an exit IP — though 32-char UUIDs make
    that vanishingly unlikely *if* accepted as-is.
11. Does the 60-second inactivity timeout still apply when `sesstime` is set? The
    wording is ambiguous and it decides whether think-time pauses are safe.
12. When a `sessid` session rotates mid-flight, does the replacement IP honour the
    original `cc`/`city`? Not stated — matters for geographic validity.
13. Is `X-Error-Description` returned on the **CONNECT** response (our HTTPS path)
    or only on plain-HTTP responses? Decides whether HAR capture can surface it.
14. No minimum `sesstime`; behaviour when exceeding the max (clamp / reject /
    ignore) undocumented.
15. `522` is "Bad Response" in the residential table but "Timeout" in the
    help-center list; `525 No Exit Found` appears in the help-center list with no
    table row.
16. No way to explicitly **release** a session before its timer expires.
17. **Max sub-user count is undocumented** — a 400 says "user has reached the
    maximum number of sub users" with no figure. Matters if we plan one sub-user
    per experiment.
18. **Does deleting a sub-user destroy its historical traffic stats?** `GET
    /sub-users` returns only **ACTIVE** sub-users. If deletion loses the record,
    snapshot usage before cleanup or lose the cost data.
19. Residential billing ($/GB) is nowhere in the developer docs, and the Public API
    reports **GB only, no currency** — cost attribution needs our own contracted
    rate.

---

## 12. Change list

Ordered by dependency.

**Prerequisites (nothing below can be tested without these)**
1. Oxylabs account + a **Proxy User** (separate from the dashboard login).
2. Written clearance for `youtube.com` / `google.com`; complete KYC if required.
3. Ask the account manager for the Residential concurrency limit and the
   contracted $/GB rate.

**Decide first**
4. `cc-` vs `st-` mapping form (§5).
5. Repurpose `agents.location` vs add a provider-agnostic geo field (§5).

**`experiment_designer`**
6. Location token mapper + validator: `US-CO-COLORADO_SPRINGS` →
   `cc-US-city-colorado_springs`. Enforce: city requires `cc`; `_` for multi-word;
   `st`/`postalcode` US-only; never emit `cc`+`st` together.
7. Vendor `us_states.txt` and `City_list.csv` as validation tables (§5).
8. Reject unmappable locations in `pmspec validate` before upload.
9. Add an assert-proxy job type pointing at `ip.oxylabs.io/location`.

**`primemover_api`**
10. Validate the geo value on the **Agent** — `AgentStoreRequest.php` currently has
    only `'location' => ['nullable']`. (Not `ProxyStoreRequest`; the proxies table
    has no location column.)
11. Decide where the `sessid` vs `sessid_oneip` flag lives.

**`primemover_runner`**
12. `OxylabsProxy.getUsername()`: move the location segment **before** `-sessid-`.
13. `SessionDecorator`: uppercase the session id (§2.3).
14. Support `sessid_oneip`, defaulting to it for experiment runs.
15. `AssertProxyByOxylabsJobHandler` with a **nested-providers** `IpInfo`; assert
    country strictly, city best-effort; move `IpInfo` out of `model.geosurf` (§7).
16. Handle 502 `Session failed` by minting a fresh session rather than retrying.
17. Surface `X-Error-Description` into logs/traces and the HAR.

**`orchestrator`**
18. Public API client for sub-users + traffic stats; CLI command and MCP tool.
    Snapshot usage before deleting a sub-user (§11.18).
19. Smoke-test command: one request per configured location, assert
    `ip.oxylabs.io/location` matches the intended geography.
20. Probe token order with the two curl commands in §11.1 as soon as credentials
    land.

**Verify before relying on it**
21. HTTPS-to-proxy through BrowserUp/LittleProxy (§3).
22. SERP subresource diff for the GTM/Doubleclick confound (§9).

---

## Sources

- `get-started/quick-start-proxies.md`
- `products/proxies/residential-proxies.md`
- `products/proxies/residential-proxies/making-requests.md`
- `products/proxies/residential-proxies/whitelisting-ips.md`
- `products/proxies/residential-proxies/session-control.md`
- `products/proxies/residential-proxies/protocols.md`
- `products/proxies/residential-proxies/location-settings.md` (+ `select-country`, `select-city`, `select-state`, `continent`, `zip-postal-code`, `coordinates`, `asn-targeting`)
- `products/proxies/residential-proxies/advanced-filters.md`
- `products/proxies/residential-proxies/endpoint-generator.md`
- `products/proxies/residential-proxies/public-api.md`
- `products/proxies/residential-proxies/additional-information.md`
- `products/proxies/residential-proxies/restricted-targets.md`
- `integrations/proxy-integrations/multilogin.md`
- `help-center/products-and-features/supported-protocols.md`
