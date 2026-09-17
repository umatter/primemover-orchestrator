# Multilogin X profile flags: what the API accepts (spike, 2026-09-17)

**Question.** `websearch_polarization` experiment 3 wants four privacy arms — Do-Not-Track,
local-storage clearing, canvas blocking, geolocation blocking. Can an MLX profile express
them, and what exact `parameters.flags` values does the API accept?

**Date.** 2026-09-17. **Probe account.** `<multilogin-account-email>` (the runner's own MLX
credentials, read on ex63; the password and the token never left the box and are not
reproduced here). **Endpoint under test.** `POST https://api.multilogin.com/profile/create`,
`X-Strict-Mode: false` — the same endpoint, headers and body shape the runner uses in
production (`primemover/multilogin/mlx/MlxConfiguration.java` sets `API_BASEPATH_SERVER =
"https://api.multilogin.com"` for `ProfileManagementApi`; `ProfileService.createOne()` calls
`profileManagementApi.profileCreate("application/json", "false", profileJson)`).

Status: **feasibility spike only. Nothing in any repo was edited.** This file is the whole output.

---

## 1. Answer per arm

| arm | expressible in an MLX profile? | how |
|---|---|---|
| **Do-Not-Track** | **No** | No flag exists; unknown keys are accepted and silently dropped. Needs a runner-side mechanism (extension or header injection). |
| **Local-storage clearing** | **No** (not as a profile setting) | `parameters.storage.is_local` is *where the profile is stored* (cloud vs. disk), not browser storage. Needs a per-session runner action, or quick profiles. |
| **Canvas blocking** | **Yes** | `parameters.flags.canvas_noise: "disabled"` (also `"mask"` = noise, `"natural"` = real). |
| **Geolocation blocking** | **Yes** | `parameters.flags.geolocation_popup: "block"` (also `"prompt"`, `"allow"`); coordinates separately via `geolocation_masking: "mask" \| "custom"`. |

Two of the four arms are one `maps_to:` line in a pmspec design; two are runner work.

### 1.1 Do-Not-Track — not expressible

*Doc evidence.* The Multilogin X profile-settings reference
(<https://multilogin.com/help/en_US/profile-settings-fingerprint-section>) enumerates the
fingerprint controls — WebRTC, Timezone, Geolocation Access, Geolocation Data, Browser
Languages, Screen Resolution, Font Data, Media Devices, Navigator, WebGL + WebGPU Metadata,
WebGL Graphics, Canvas Graphics, AudioContext, Port Scan Protection — and **no Do Not Track
control appears anywhere**, in that page or in the `/profile/create` body documented in the
runner's own copy of the MLX OpenAPI spec
(`primemover_runner/primemover-generated/src/main/resources/MultiloginX.yaml`,
path `/profile/create`, which lists exactly 16 flag keys plus `quic_mode`).

*Probe evidence — worse than "undocumented": the API takes the key and throws it away.*
Profile `pmspec-spike-20260917-082713-3` was created with

```json
"parameters": {
  "flags": { "…17 known keys…": "…", "do_not_track": "enabled" },
  "do_not_track": true,
  "storage": { "is_local": false, "save_service_worker": false, "clear_on_exit": true },
  "fingerprint": { "screen": {},
     "cmd_params": { "params": [ {"flag": "enable-do-not-track", "value": ""},
                                 {"flag": "load-extension", "value": "/tmp/does-not-exist"} ] } }
}
```

plus a top-level `"do_not_track": true`. Response: **HTTP 201 "Profile successfully created"**.
Reading it back with `POST /profile/metas` returned the flags block with **exactly the 17 known
keys and no `do_not_track`**, no `parameters.do_not_track`, no top-level `do_not_track`, and
`storage: {is_local:false, save_service_worker:false}` with `clear_on_exit` **gone**. The
`cmd_params` array, by contrast, came back **verbatim** — including the invented Chrome flag.

Two consequences:

1. **The API validates flag *values* but not flag *keys*.** A wrong value is a hard 400
   (`{"error_code":"BAD_REQUEST_BODY","message":"wrong canvas noise flag"}`); a wrong *key* is
   accepted and discarded with no warning anywhere. A `do_not_track` arm written as a profile
   flag would look like it worked and would quietly produce a second control arm — the same
   failure shape as the `pi=1` cwd bug. pmspec already refuses unknown flag keys under
   `maps_to: agent.multilogin_profile.parameters.flags.<flag>`; keep that, because MLX will not
   catch it.
2. **`cmd_params` is a pass-through, so it is the only lever** — but there is no Chrome switch to
   pull. The current Chromium switch list
   (<https://peter.sh/experiments/chromium-command-line-switches/>, fetched 2026-09-17) contains
   **zero** matches for `do-not-track`; the only `*track*` switch is `--trace-process-track-uuid`.
   Chrome's DNT is a profile *preference* (`enable_do_not_track`), and there is no enterprise
   policy for it either — which is why the `dnt-enabler` extension project exists at all.

What a DNT arm would take is in §3.

### 1.2 Local-storage clearing — not expressible as a profile setting

*The near-miss to rule out.* `parameters.storage.is_local` reads as though it were about site
storage. It is not: Multilogin's own page
(<https://multilogin.com/help/en_US/profile-management/cloud-and-local-storage>) is about where
**the profile** lives — "Cloud storage: data kept online, synced across devices / Local storage:
data stored on your device at `C:\Users\%username%\mlx\profiles`". It says nothing about browser
localStorage or site data. Probe: `is_local: true` was accepted and stored on
`pmspec-spike-20260917-082713-2`; it changes nothing a page can observe.
`save_service_worker: false` is likewise only about what is *saved*, and only for service workers.

*Nothing in the flag vocabulary clears anything either.* `startup_behavior` was the plausible
candidate and is not one: it is Multilogin's "Startup behavior / Restore" setting (start URLs and
restored tabs), and — see the table — **the API does not validate it at all**. Every one of
`recover, new, clear, fresh, clean, reset, restore, none, default, custom, natural` passed
validation, so the value is not a checked enum and "clear" does not mean what it looks like.

*The one profile-level thing that does clear data* is a **quick profile**
(`POST /v3/profile/quick`): the vendored spec's own words — "Browsing activity and its related
data is not saved after the profile has been stopped" — and the help page, "every time you close
a quick profile, it's deleted along with all its data". The runner does not use quick profiles; it
creates one regular profile per bot and starts it through the local launcher, and one stable
profile per bot is the point of the design. Moving the privacy arm onto quick profiles would throw
away the identity continuity the experiment depends on.

So local-storage clearing is a **per-session action**, not a profile field. See §3.

### 1.3 Canvas blocking — expressible

*Doc evidence.* Profile settings list **Canvas Graphics: Noise / Real (default) / Disabled**, and
Multilogin's knowledge base describes the third as "disabling the Canvas API at the browser level
prevents websites from reading your canvas fingerprint… this is considered a legacy mode and is
not recommended for regular use."

*Probe evidence.* Values accepted by `/profile/create`: `mask`, `natural`, `disabled`; rejected
with `"wrong canvas noise flag"`: `real`, `noise`, `off`, `block`, `custom`. Profile
`pmspec-spike-20260917-082713-2` was created with `canvas_noise: "disabled"` (HTTP 201) and read
back holding `"canvas_noise": "disabled"`.

Mapping to the UI vocabulary: **Noise = `mask`, Real = `natural`, Disabled = `disabled`**. pmspec's
default is `mask` (noise) — neither the "real" control nor the "blocked" arm — so a canvas factor
has three levels, not two, and the control level has to be chosen deliberately.

### 1.4 Geolocation blocking — expressible

*Doc evidence.* Two separate controls: **Geolocation Access** — "Prompt (default) / Allow /
Blocked", where Blocked means "all websites will be automatically denied from receiving your
geolocation coordinates… from the website's perspective, it will seem like you set the setting to
block the location readout in your browser settings" — and **Geolocation Data** — "Masked
(default) / Custom".

*Probe evidence.* `geolocation_popup` accepts exactly `prompt`, `allow`, `block` (everything else
→ `"wrong geolocation masking flag"`); `geolocation_masking` accepts exactly `mask`, `custom`.
Profile `pmspec-spike-20260917-082713-1` was created with `geolocation_popup: "block"`,
`geolocation_masking: "custom"` and `parameters.fingerprint.geolocation = {latitude: 47.3769,
longitude: 8.5417, accuracy: 100, altitude: 0}` (HTTP 201), and read back with all four stored
unchanged.

The arm is `geolocation_popup`, **not** `geolocation_masking`: the runner's default
`geolocation_masking: "mask"` derives a plausible location from the proxy exit IP, which is a
consistency setting, not a privacy one. Note too that `geolocation_masking: "custom"` is accepted
**without** a `fingerprint.geolocation` block — unlike `fonts`/`localization`/`navigator`/
`media_devices`/`timezone` custom, which 400 with `"no <X> data"` — so a half-configured custom
geolocation fails open rather than erroring.

---

## 2. The accepted vocabulary, per flag

Method — this is why the table can be trusted and why it cost no profiles: MLX validates
`parameters.flags` **before** it validates `core_version`. Every probe carried
`core_version: 1`, so a body whose flags were all acceptable came back
`"Can't set core older than 147. Please set higher core version"` and **created nothing**, while a
bad value came back `"wrong <x> flag"`. 111 value probes plus 8 shape probes ran this way; none of
them created a profile.

Legend: **ok** = accepted; **ok\*** = accepted, but then requires the matching
`parameters.fingerprint.<block>` (the API answers `"no <X> data"` if it is missing); **—** = 400.

| flag | `mask` | `natural` | `custom` | `disabled` | `real` | `block` | other accepted | rejection message |
|---|---|---|---|---|---|---|---|---|
| `audio_masking` | ok | ok | — | — | — | — | | `wrong audio masking flag` |
| `canvas_noise` | ok | ok | — | **ok** | — | — | | `wrong canvas noise flag` |
| `fonts_masking` | ok | ok | ok* | — | — | — | | `wrong fonts masking flag` |
| `geolocation_masking` | ok | — | ok | — | — | — | | `wrong geolocation masking flag` |
| `geolocation_popup` | — | — | — | — | — | **ok** | `prompt`, `allow` | `wrong geolocation masking flag` |
| `graphics_masking` | ok | ok | ok | — | — | — | | `wrong graphics masking flag` |
| `graphics_noise` | ok | ok | — | — | — | — | | `wrong graphic noise masking flag` |
| `localization_masking` | ok | ok | ok* | — | — | — | | `wrong localization masking flag` |
| `media_devices_masking` | ok | ok | ok* | — | — | — | | `wrong media devices masking flag` |
| `navigator_masking` | ok | ok | ok* | — | — | — | | `wrong navigator masking flag` |
| `ports_masking` | ok | ok | — | — | — | — | | `wrong ports masking flag` |
| `proxy_masking` | — | — | ok | ok | — | — | | `wrong proxy masking flag` |
| `quic_mode` | — | ok | — | ok | — | — | | `quic_mode allowed values: natural, disabled` |
| `screen_masking` | ok | ok | ok | — | — | — | | `wrong screen masking flag` |
| `startup_behavior` | ok | ok | ok | ok | ok | ok | **anything** — not validated | (none) |
| `timezone_masking` | ok | ok | ok* | — | — | — | | `wrong timezone masking flag` |
| `webrtc_masking` | ok | ok | ok* | ok | — | — | | `wrong webrtc masking flag` |

Reading notes for whoever wires validation into pmspec:

- **`natural` is the API's word for the UI's "Real"**. `real` is rejected everywhere. The one
  explicit message the API volunteers — `quic_mode allowed values: natural, disabled` — confirms
  the house vocabulary is `{mask, natural, custom, disabled}` plus the two special cases
  (`geolocation_popup` in {prompt, allow, block}, `proxy_masking` in {custom, disabled}).
- **`startup_behavior` accepts any string** and stores it verbatim (the spike profiles came back
  with `"recover"`), so a typo there is silent. pmspec should pin it to the documented UI values
  rather than pass anything through.
- **`ports_masking: custom` is rejected** even though the help page lists Port Scan Protection as
  "Masked / Custom / Real". The API is the authority; the help page is not.
- **A bad value is a 400 at profile-create time — i.e. at bot setup on the VM, not at compile
  time.** That is exactly the `core_version: 142` failure mode this repo already hit (see
  `converter.py`'s comment on "Can't set core older than 147"): every spec-uploaded bot fails
  setup. Validating values in pmspec against this table turns a run-killer into a compile error.
- `quic_mode` and `startup_behavior` are accepted and stored even though the documented
  `/profile/create` body does not list them; pmspec's default profile already sends both.

Shape findings from the 8 non-value probes (all `core_version: 1`, none created anything):

| probe | result |
|---|---|
| `parameters.flags.do_not_track` | accepted (then dropped — see §1.1) |
| `parameters.do_not_track` | accepted (dropped) |
| top-level `do_not_track` | accepted (dropped) |
| `parameters.storage.is_local: true` | accepted, stored |
| `parameters.storage.clear_on_exit` | accepted (dropped) |
| `cmd_params` `enable-do-not-track` | accepted, stored verbatim — **cmd_params flags are not validated** |
| `cmd_params` `zzz-not-a-chrome-flag` | accepted, stored verbatim |
| `geolocation_masking: custom` with no `fingerprint.geolocation` | accepted (unlike the other `custom` values) |

Operational aside: Cloudflare, not the documented 50-ops/min workspace limit, is what bites. A
plain `python-urllib` User-Agent gets **403 Cloudflare error 1010** on `/user/signin`, and roughly
40 requests/min against `/profile/create` started returning Cloudflare HTML challenge pages
instead of JSON. Both are silent-corruption traps for future tooling: the body stops being JSON
while the endpoint still looks alive. Probes were re-run at one request per 4 s, which was clean.

---

## 3. What the runner would need

**Canvas and geolocation: nothing.** pmspec's `DEFAULT_MULTILOGIN_PROFILE`
(`pmspec/spec/converter.py`) is stored as the agent's `multiLoginCreationJson` and used verbatim by
`TemplateMapper.getReplacedTemplate()`, which only substitutes the `%%…%%` tokens. Both arms are
pure `maps_to:` lines:

```yaml
maps_to:
  agent.multilogin_profile.parameters.flags.canvas_noise:      {control: mask,   blocked: disabled}
  agent.multilogin_profile.parameters.flags.geolocation_popup: {control: prompt, blocked: block}
```

**One risk to verify before trusting the geolocation arm.** `ProfileService.updateProxy()` fires a
`POST /profile/partial_update` whose body is
`{"profile_id":…, "proxy":{…}, "parameters":{"fingerprint":{"cmd_params":{"params":[js-flags, load-extension]}}}}`.
Whether MLX merges that into the existing `fingerprint` or **replaces** it is not documented — the
spec's `/profile/partial_update` description says only "Update a profile partially according to
your requirements" — and this spike did not test it, because testing it needs a fourth profile. If
it replaces, a `geolocation_masking: "custom"` arm loses its `fingerprint.geolocation` (and the
`screen` block) the moment the proxy is attached. Worth one more probe before that arm is built.
`flags` sit outside `fingerprint`, so `canvas_noise` is not at risk either way.

**Do-Not-Track: a runner change, and the least bad option is an extension.**

- *Extension (recommended).* A ~10-line unpacked extension calling
  `chrome.privacy.websites.doNotTrackEnabled.set({value: true})` — the documented route, since
  there is neither a switch nor a policy. It rides `cmd_params` `load-extension`, which the probe
  shows is stored verbatim. **But** `ProfileService.updateProxy()` already writes `load-extension`
  with the single H.264 path and runs on every proxy attach, so the DNT path must be appended
  there (Chrome takes a comma-separated list); putting it only in the pmspec profile template
  would be overwritten. That makes DNT a per-arm value inside a field the runner rewrites per bot:
  it needs a real runner-side hook (e.g. a per-agent extension list), not a spec-only change.
- *MITM header injection.* The recording proxy could add `DNT: 1`. Rejected as the primary route:
  `recorder.enabled` is **off** by default (`NoRecordingProxy`), and a header alone leaves
  `navigator.doNotTrack` reading `null`, so any site checking the JS API sees an untreated bot.
- *Chrome preference file.* Not reachable — MLX owns the profile directory.

**Local-storage clearing: a runner change, and a small one.** The runner already has
`primemover.util.LocalStorage` (and `SessionStorage`), but only with a **read** helper
(`getItems`, used by the job-data recorder); the `clear()` in that file is commented-out Python.
The arm is one `JavascriptExecutor` call — `window.localStorage.clear()` (plus
`sessionStorage.clear()`, and CDP `Storage.clearDataForOrigin` if cookies/IndexedDB are in scope)
— exposed as a new job type. pmspec's registry has 17 job types (`visit, wait, click, try_click,
type, try_type, scroll, scroll_length, try_scroll, try_scroll_length, lambda_select,
single_select, single_select_url, switch, try_switch, handle_alert, try_handle_alert`) and **none
executes script or touches storage**, so this is a new `clear_storage` job type on both sides: a
runner task operator plus a `JobTypeSchema` entry. Being a job is also the right shape for the
experiment — "clear between sessions" is a workflow step with a defined position, not an ambient
profile property.

---

## 4. Cleanup

**Cleanup succeeded.** Exactly three profiles were created, all in the runner's folder
`2e8c5d68-bade-44dc-a45b-211c5435ebfd`:

| name | id |
|---|---|
| `pmspec-spike-20260917-082713-1` | `cca4cb99-cafb-4ef5-96cc-2323915bc118` |
| `pmspec-spike-20260917-082713-2` | `90b79ee9-f11d-4a3d-bb02-1a15508f4483` |
| `pmspec-spike-20260917-082713-3` | `d11c73b3-aff6-410f-9907-33b5cfc2ab5a` |

`POST /profile/remove` with `{"ids":[all three], "permanently": true}` returned **HTTP 200
"Profile successfully deleted"**. Confirmed three ways:

- `POST /profile/metas` on the three ids → **400 "Wrong data in request"** (ids no longer resolve);
- `POST /profile/search` `{is_removed: false, search_text: "pmspec-spike", folder_id: …}` →
  `total_count: 0`;
- `POST /profile/search` `{is_removed: true, …}` → `total_count: 0` (nothing in the recoverable
  bin either).

No browser was ever started, no pre-existing profile was read or written, the folder was not
modified, and the probe helper scripts were deleted from ex63's `/tmp` afterwards. The 119
validation probes created nothing by construction (`core_version: 1`).

## 5. Sources

- Multilogin, profile settings — Fingerprint section: <https://multilogin.com/help/en_US/profile-settings-fingerprint-section>
- Multilogin, cloud vs. local profile storage: <https://multilogin.com/help/en_US/profile-management/cloud-and-local-storage>
- Multilogin, quick browser profiles: <https://multilogin.com/help/en_US/profile-management/quick-browser-profiles>
- Multilogin knowledge base, masking browser parameters (geolocation, canvas): <https://docs.multilogin.com/l/en/category/o40iDKTlyQ-masking-browser-parameters>
- Multilogin API, Postman documenter (source of the runner's vendored spec): <https://documenter.getpostman.com/view/28533318/2s946h9Cv9>
- Vendored MLX OpenAPI spec: `primemover_runner/primemover-generated/src/main/resources/MultiloginX.yaml`
- Chromium command-line switch list: <https://peter.sh/experiments/chromium-command-line-switches/>
- Chrome DNT has no policy or switch, hence the extension route: <https://github.com/a8763506128977812212307169331690/dnt-enabler>
