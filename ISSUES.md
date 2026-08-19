# Local Development Setup Issues

The following issues were encountered when setting up the PrimeMover stack locally on macOS (Apple Silicon). These should be addressed in the onboarding documentation or automated via the setup scripts.

---

## 1. Chrome dependency assumes Linux

The orchestrator setup warns that Chrome is not found and suggests `sudo apt install google-chrome-stable`, which is Linux-specific. There is currently no macOS-compatible install path provided.

**Status:** Unresolved

---

## 2. MySQL Docker image incompatible with Apple Silicon

The `docker-compose.yml` specifies `mysql:8.0.25`, which has no ARM image. The `docker-compose.override.yml` forced `platform: linux/amd64`, causing an `exec format error` at runtime. The fix is to use `mysql:8.0.36` (or newer) with `platform: linux/arm64` in the override.

**Recommendation:** Update the base `docker-compose.yml` to use an ARM-compatible MySQL image, or document the override requirement for Apple Silicon users.

---

## 3. Laravel dependencies not installed after clone

After cloning `primemover_api` and starting the containers, the application fails with:

```
Failed opening required '/var/www/html/public/../vendor/autoload.php'
```

The setup does not automatically run `composer install` inside the PHP container. This needs to be executed manually:

```bash
docker compose exec php composer install
```

**Recommendation:** Add this step to the setup script or Makefile.

---

## 4. Missing application secrets and OAuth configuration

Several secrets and keys are required but not generated during setup:

1. **`APP_KEY`** — empty by default, needs `php artisan key:generate`
2. **OAuth encryption keys** — missing, needs `php artisan passport:keys`
3. **Passport personal access client** — not created, needs `php artisan passport:client --personal`
4. **Passport password grant client** — not created, needs `php artisan passport:client --password`
5. **`PASSPORT_CLIENT_ID` and `PASSPORT_CLIENT_SECRET`** — not set in `.env`, required by the `OauthRequestModifier` middleware to inject OAuth credentials into token requests. Without these, the GUI login returns a 400 Bad Request.

**Recommendation:** Automate key generation and client creation in the setup process, and document the required `.env` variables.

---

## 5. Port configuration inconsistencies across components

The API is exposed on port **8090** (configured via `HOST_MACHINE_UNSECURE_HOST_PORT` in the API `.env`), but several other components default to port 8080:

- The **runner** `application.properties` had `backend.endpoint.api=http://localhost:8080/api/v1/`
- The runner communicates with the API via the host network, so it must use the mapped port (8090), not the container-internal port (80).

The relationship between components is not immediately clear. The runner is a native Java process (not dockerized), so it relies on the host-mapped port. The 404 on `localhost:8080` is expected — there is nothing listening there in the default setup.

**Recommendation:** Align the default ports across all component configs, or document the expected port mapping clearly.

---

## 6. GUI `apiBaseUrl` mismatch

The GUI (`primemover_gui`) environment configuration had `apiBaseUrl` set to `http://localhost/api/v1` instead of `http://localhost:8090/api/v1`.

**Recommendation:** Update the default development environment config to use the correct port.

---

## 7. Runner backend configuration points to production

In `application.properties`, the backend endpoint and credentials were set to the production mirror (`primemover.example`) rather than the local development environment. For local development and offline testing, these need to point to `http://localhost:8090/api/v1` with credentials `admin@primemover.example` / `password`.

**Recommendation:** Provide a separate `application-local.properties` profile with correct local defaults, or document the required changes.

---

## 8. GUI redesign — Library not yet functional

In the new `primemover_gui` workspace (Experiment → Bot → Run → Job → Behaviour), the **Library**
section (managing agents / proxies / configurations as reusable building blocks) is **not working
yet**. Adding a bot currently relies on ids that exist from elsewhere. The MCP `list_library` tool
exposes those ids, but the GUI's own Library management UI is incomplete.

**Status:** Unresolved — Library CRUD in the GUI still to be built/wired.

---

## 9. GUI redesign — not yet validated against real running crawlers

The full authoring flow (compose a run with jobs + behaviours) has been verified end-to-end against
the API and with unit/integration tests, but **not yet with a real crawler actually executing the
composed run** in the runner. Scheduling/activation and the runner picking up a UI-composed run needs
a live test (ideally via the Chrome-E2E path, no Multilogin required).

**Status:** Unresolved — needs an end-to-end run through `primemover_runner`.
