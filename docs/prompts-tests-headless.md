# Prompts de génération des suites de tests headless

> Ces prompts sont exécutés **après** les prompts A/B/C/D d'implémentation.
> Ils génèrent les outils de test automatisés pour valider le comportement
> avant tout déploiement en campagne réelle.

---

## Prompt E — Tests headless Device Management (pytest)

> Repository : `device-management/`
> Créer : `tests/test_enriched_config.py`, `tests/conftest_campaigns.py`,
>          `docker-compose.test.yml` (si absent)

```text
Context
-------
You are working in the device-management FastAPI project (Python 3.12).
The enriched config endpoint (GET /config/{device}/config.json) has been
implemented per the spec in docs/plugin-dm-protocol-update-features.md.

The existing test infrastructure uses pytest + FastAPI TestClient.
The DB is PostgreSQL. Tests must use a REAL PostgreSQL instance (not mocks)
with transactions rolled back after each test (use pytest fixtures with
BEGIN/ROLLBACK). Use the existing docker-compose.yml as reference; create
docker-compose.test.yml with a dedicated test DB on port 5433.

Goal
----
Create a complete headless test suite covering all enriched config scenarios.

1. Create tests/conftest_campaigns.py with fixtures:

   @pytest.fixture(scope="session")
   def db_conn():
       # Connect to test DB (env: TEST_DATABASE_URL)
       # Run db/migrations/002_campaigns.sql
       # Yield connection
       # Drop test tables after session

   @pytest.fixture(autouse=True)
   def db_transaction(db_conn):
       # BEGIN savepoint before each test
       # ROLLBACK to savepoint after each test
       # Ensures test isolation without full DB reset

   @pytest.fixture
   def client(db_conn):
       # FastAPI TestClient with DB dependency overridden to use db_conn
       return TestClient(app)

   @pytest.fixture
   def seed_artifact(db_conn):
       # Insert one artifact: device_type="libreoffice", version="2.0.0",
       #   s3_path="libreoffice/2.0.0/mirai.oxt", checksum="sha256:abc123"
       # Return artifact id

   @pytest.fixture
   def seed_campaign(db_conn, seed_artifact):
       # Insert active campaign pointing to seed_artifact
       # Return campaign id

   @pytest.fixture
   def seed_cohort_percentage(db_conn):
       # Insert cohort type="percentage", config={"percentage": 100}
       # Return cohort id

   @pytest.fixture
   def seed_feature_flag(db_conn):
       # Insert feature_flag name="test_feature", default_value=True
       # Return flag id

2. Create tests/test_enriched_config.py with these test cases:

   TC-DM-01 test_no_plugin_version_returns_null_update:
     GET /config/libreoffice/config.json (no X-Plugin-Version)
     assert response.status_code == 200
     assert response.json()["update"] is None
     assert response.json()["features"] == {}
     assert response.json()["meta"]["schema_version"] == 2

   TC-DM-02 test_schema_version_2_in_meta:
     GET with X-Plugin-Version: "1.0.0"
     assert meta.schema_version == 2
     assert "generated_at" in meta
     assert "client_uuid" in meta

   TC-DM-03 test_feature_flag_default_value:
     seed_feature_flag (default_value=True)
     GET with X-Plugin-Version: "1.0.0"
     assert features["test_feature"] == True

   TC-DM-04 test_feature_flag_cohort_override_false:
     seed_feature_flag (default_value=True)
     seed cohort with manual member = test client_uuid
     insert feature_flag_override: value=False for that cohort
     GET with X-Client-UUID matching member
     assert features["test_feature"] == False

   TC-DM-05 test_feature_flag_min_version_gates:
     seed_feature_flag (default_value=False)
     insert override value=True, min_plugin_version="2.0.0"
     GET with X-Plugin-Version: "1.5.0"
     assert features["test_feature"] == False  (version too old)
     GET with X-Plugin-Version: "2.0.0"
     assert features["test_feature"] == True   (version meets threshold)

   TC-DM-06 test_update_action_when_plugin_behind:
     seed_campaign (artifact.version="2.0.0"), active
     GET with X-Plugin-Version: "1.0.0", X-Platform-Type: "libreoffice"
     assert update["action"] == "update"
     assert update["target_version"] == "2.0.0"
     assert update["artifact_url"].startswith("/binaries/")
     assert update["checksum"].startswith("sha256:")

   TC-DM-07 test_update_null_when_current:
     seed_campaign (artifact.version="2.0.0"), active
     GET with X-Plugin-Version: "2.0.0"
     assert update is None

   TC-DM-08 test_rollback_action:
     seed rollback_artifact (version="1.9.0")
     seed_campaign with rollback_artifact_id set
     GET with X-Plugin-Version: "2.1.0"  (newer than target)
     assert update["action"] == "rollback"
     assert update["target_version"] == "1.9.0"

   TC-DM-09 test_campaign_device_status_created:
     seed_campaign
     GET with X-Plugin-Version: "1.0.0", X-Client-UUID: "test-uuid"
     row = SELECT * FROM campaign_device_status WHERE client_uuid="test-uuid"
     assert row["status"] == "notified"
     assert row["version_before"] == "1.0.0"

   TC-DM-10 test_percentage_cohort_deterministic:
     seed cohort type="percentage", config={"percentage": 50}
     seed_campaign targeting that cohort
     uuid_in = deterministic uuid that falls in 50% (precompute sha256 % 100 < 50)
     uuid_out = deterministic uuid that falls out
     GET with X-Client-UUID: uuid_in, X-Plugin-Version: "1.0.0" → update not null
     GET with X-Client-UUID: uuid_out, X-Plugin-Version: "1.0.0" → update null

   TC-DM-11 test_platform_variant_thunderbird:
     GET with X-Platform-Type: "thunderbird", X-Platform-Version: "60.9.1"
     assert meta["platform_variant"] == "tb60"
     GET with X-Platform-Version: "128.3.1"
     assert meta["platform_variant"] == "tb128"

   TC-DM-12 test_platform_variant_chrome_mv3:
     GET with X-Platform-Type: "chrome", X-Manifest-Version: "3"
     assert meta["platform_variant"] == "mv3"

   TC-DM-13 test_no_active_campaign_returns_null_update:
     seed_campaign with status="draft" (not active)
     GET with X-Plugin-Version: "1.0.0"
     assert update is None

   TC-DM-14 test_config_dict_unchanged:
     GET with X-Plugin-Version: "1.0.0"
     existing_key = one of the keys from the base config.json file
     assert existing_key in response.json()["config"]

   TC-DM-15 test_no_new_tables_graceful_degradation:
     # If migration not run: endpoint must still return 200
     # Simulate by dropping the campaigns table temporarily
     assert response.status_code == 200
     assert update is None

3. Add to Makefile or scripts/test.sh:
   docker-compose -f docker-compose.test.yml up -d postgres-test
   TEST_DATABASE_URL=postgresql://... pytest tests/test_enriched_config.py -v --tb=short
   docker-compose -f docker-compose.test.yml down

All tests must pass before any production campaign is activated.
```

---

## Prompt F — Tests headless AssistantMiraiLibreOffice (pytest + mocks UNO)

> Repository : `AssistantMiraiLibreOffice/`
> Créer : `tests/test_update_features.py`, `tests/mocks/uno_mock.py`

```text
Context
-------
You are working in AssistantMiraiLibreOffice.
The plugin code is in src/mirai/entrypoint.py (Python, LibreOffice UNO).
LibreOffice UNO is NOT available in CI — all UNO interfaces must be mocked.
The existing test infrastructure uses pytest. Check tests/ for existing patterns.

Goal
----
Create a complete headless test suite for _fetch_config(), _is_feature_enabled(),
_perform_update() and related methods WITHOUT requiring a running LibreOffice.

1. Create tests/mocks/uno_mock.py:
   - MagicMock subclass for self.sm (ServiceManager)
   - MagicMock subclass for self.ctx (ComponentContext)
   - Mock for PackageInformationProvider: getExtensionVersion returns "1.2.0"
   - Mock for ExtensionManager: addExtension records calls
   - Mock for uno module: systemPathToFileUrl returns "file:///tmp/test.oxt"
   - Patch sys.modules["uno"] = mock_uno before importing entrypoint

2. Create tests/test_update_features.py:

   Use a helper build_plugin() that instantiates the relevant class from
   entrypoint.py with mocked sm/ctx, bypassing __init__ UNO calls if needed
   (use object.__new__ + manual attribute assignment).

   TC-LO-01 test_is_feature_enabled_no_cache:
     plugin._features_cache not set
     assert plugin._is_feature_enabled("writer_assistant") == True  (default)

   TC-LO-02 test_is_feature_enabled_cache_false:
     plugin._features_cache = {"writer_assistant": False}
     assert plugin._is_feature_enabled("writer_assistant") == False

   TC-LO-03 test_is_feature_enabled_missing_key:
     plugin._features_cache = {"other": True}
     assert plugin._is_feature_enabled("writer_assistant", default=True) == True

   TC-LO-04 test_fetch_config_v2_populates_features:
     mock _urlopen to return EnrichedConfigResponse (schema_version=2)
       with features={"calc_assistant": False}
     call plugin._fetch_config(force=True)
     assert plugin._features_cache == {"calc_assistant": False}

   TC-LO-05 test_fetch_config_v2_schedules_update:
     mock response: update={"action":"update","target_version":"2.0.0",...}
     with patch.object(plugin, "_schedule_update") as mock_schedule:
       plugin._fetch_config(force=True)
     mock_schedule.assert_called_once()
     directive = mock_schedule.call_args[0][0]
     assert directive["action"] == "update"

   TC-LO-06 test_fetch_config_v2_no_update_when_null:
     mock response: update=null
     with patch.object(plugin, "_schedule_update") as mock_schedule:
       plugin._fetch_config(force=True)
     mock_schedule.assert_not_called()

   TC-LO-07 test_fetch_config_legacy_no_features:
     mock response: flat dict without "meta" key (schema_version=1)
     plugin._features_cache = {"old": True}  # pre-existing
     plugin._fetch_config(force=True)
     assert plugin._features_cache == {"old": True}  # unchanged

   TC-LO-08 test_fetch_config_update_not_retriggered:
     plugin._update_in_progress = True
     mock response with update.action="update"
     with patch.object(plugin, "_schedule_update") as mock_schedule:
       plugin._fetch_config(force=True)
     mock_schedule.assert_not_called()

   TC-LO-09 test_perform_update_checksum_ok:
     known_bytes = b"fake oxt content"
     import hashlib
     checksum = "sha256:" + hashlib.sha256(known_bytes).hexdigest()
     directive = {"action":"update","target_version":"2.0.0",
                  "artifact_url":"/binaries/lo/2.0.0/mirai.oxt",
                  "checksum": checksum, "urgency":"normal",
                  "campaign_id":1, "current_version":"1.0.0"}
     mock _urlopen to return known_bytes
     mock addExtension
     plugin._perform_update(directive)
     assert mock_add_extension.called
     assert plugin._update_in_progress == False

   TC-LO-10 test_perform_update_checksum_mismatch:
     directive with checksum="sha256:wronghex"
     mock _urlopen returns b"real content" (hash won't match)
     mock addExtension
     plugin._perform_update(directive)
     assert not mock_add_extension.called
     # telemetry ExtensionUpdateFailed should be sent
     assert plugin._update_in_progress == False

   TC-LO-11 test_perform_update_clears_flag_on_exception:
     mock addExtension raises RuntimeError
     plugin._update_in_progress = True
     plugin._perform_update(directive_with_valid_checksum)
     assert plugin._update_in_progress == False

   TC-LO-12 test_get_extension_version_ok:
     mock PackageInformationProvider.getExtensionVersion returns "1.2.0"
     assert plugin._get_extension_version() == "1.2.0"

   TC-LO-13 test_get_extension_version_fallback:
     mock PackageInformationProvider.getExtensionVersion raises Exception
     assert plugin._get_extension_version() == ""

3. Run with:
   pytest tests/test_update_features.py -v --tb=short
   (no LibreOffice required — pure Python mock environment)
```

---

## Prompt G — Tests headless Chrome Extension MV3 (Jest)

> Repository : `mirai-assistant/`
> Dossier : `chrome-extension/`
> Créer : `tests/dm.test.js`, `tests/__mocks__/chrome.js`, `package.json` (si absent)

```text
Context
-------
You are working in mirai-assistant/chrome-extension/.
background.js has been modified to add fetchDMConfig(), handleUpdateDirective(),
isFeatureEnabled() per Prompt C.

The Chrome extension APIs (chrome.storage, chrome.notifications, chrome.runtime,
chrome.alarms) are not available in Node.js — they must be mocked.

Goal
----
Create a Jest test suite that runs in Node.js without a browser.

1. Create package.json (or update existing) with:
   {
     "scripts": { "test": "jest --coverage" },
     "devDependencies": {
       "jest": "^29",
       "jest-environment-node": "^29"
     },
     "jest": {
       "testEnvironment": "node",
       "setupFiles": ["./tests/__mocks__/chrome.js"]
     }
   }

2. Create tests/__mocks__/chrome.js:
   global.chrome = {
     storage: {
       local: {
         get: jest.fn(),
         set: jest.fn().mockResolvedValue(undefined)
       }
     },
     runtime: {
       getManifest: jest.fn().mockReturnValue({ version: "1.2.1" })
     },
     notifications: {
       create: jest.fn()
     },
     alarms: {
       create: jest.fn(),
       onAlarm: { addListener: jest.fn() }
     }
   };
   global.fetch = jest.fn();

3. Create tests/dm.test.js with these test cases:

   TC-CR-01 test_fetchDMConfig_noop_when_no_base_url:
     chrome.storage.local.get resolves with { dm_base_url: "" }
     await fetchDMConfig()
     expect(fetch).not.toHaveBeenCalled()

   TC-CR-02 test_fetchDMConfig_sends_correct_headers:
     chrome.storage.local.get resolves with { dm_base_url: "http://dm", dm_client_uuid: "u1" }
     fetch resolves with { ok:true, json: () => ({meta:{schema_version:2}, features:{}, update:null}) }
     await fetchDMConfig()
     const [url, opts] = fetch.mock.calls[0]
     expect(url).toBe("http://dm/config/chrome/config.json")
     expect(opts.headers["X-Plugin-Version"]).toBe("1.2.1")
     expect(opts.headers["X-Platform-Type"]).toBe("chrome")
     expect(opts.headers["X-Manifest-Version"]).toBe("3")

   TC-CR-03 test_fetchDMConfig_stores_features:
     mock response: features={"f":true}
     await fetchDMConfig()
     expect(chrome.storage.local.set).toHaveBeenCalledWith(
       expect.objectContaining({ dm_features: { f: true } })
     )

   TC-CR-04 test_fetchDMConfig_calls_handleUpdate:
     mock response: update={action:"update",target_version:"2.0.0", urgency:"normal"}
     const spy = jest.spyOn(module, "handleUpdateDirective")
     await fetchDMConfig()
     expect(spy).toHaveBeenCalledWith(expect.objectContaining({action:"update"}))

   TC-CR-05 test_handleUpdateDirective_creates_notification:
     await handleUpdateDirective({target_version:"2.0.0",urgency:"normal",artifact_url:"/b/x"})
     expect(chrome.notifications.create).toHaveBeenCalled()
     const [, opts] = chrome.notifications.create.mock.calls[0]
     expect(opts.message).toContain("2.0.0")

   TC-CR-06 test_handleUpdateDirective_critical_urgency_message:
     await handleUpdateDirective({target_version:"2.0.0",urgency:"critical",artifact_url:"/b/x"})
     const [, opts] = chrome.notifications.create.mock.calls[0]
     expect(opts.message).toContain("Critique")

   TC-CR-07 test_isFeatureEnabled_returns_default_absent:
     chrome.storage.local.get resolves with { dm_features: {} }
     const result = await isFeatureEnabled("f")
     expect(result).toBe(true)

   TC-CR-08 test_isFeatureEnabled_returns_stored_false:
     chrome.storage.local.get resolves with { dm_features: { f: false } }
     const result = await isFeatureEnabled("f")
     expect(result).toBe(false)

   TC-CR-09 test_fetchDMConfig_silent_on_fetch_error:
     fetch.mockRejectedValue(new Error("network"))
     await expect(fetchDMConfig()).resolves.not.toThrow()

   TC-CR-10 test_relay_headers_added_when_enrolled:
     storage: { dm_relay_client_id: "rc1", dm_relay_client_key: "key1", dm_base_url: "http://dm" }
     await fetchDMConfig()
     const [, opts] = fetch.mock.calls[0]
     expect(opts.headers["X-Relay-Client-Id"]).toBe("rc1")

4. Run with: npm test
   Coverage threshold: 80% branches minimum.
```

---

## Prompt H — Tests headless Thunderbird 60.9.1 (Jest + XPCOM mocks)

> Repository : `mirai-assistant/`
> Dossier : `matisse/thunderbird/60.9.1/`
> Créer : `tests/test_plugin_state.js`, `tests/test_auto_updater.js`,
>          `tests/__mocks__/xpcom.js`, `package.json`

```text
Context
-------
You are working in mirai-assistant/matisse/thunderbird/60.9.1/.
The modules use XPCOM globals: ChromeUtils, Services, Cu, Components, AddonManager.
These are NOT available in Node.js — they must be fully mocked.

Goal
----
Create Jest tests for plugin-state.js and auto-updater.js logic,
exercising the schema detection and update flow without Thunderbird.

1. Create tests/__mocks__/xpcom.js — set up globals before tests:
   global.ChromeUtils = {
     import: jest.fn().mockReturnValue({}),
   };
   global.Cu = {
     importGlobalProperties: jest.fn(),
     import: jest.fn().mockReturnValue({}),
   };
   global.Components = { utils: global.Cu };
   global.Services = {
     scriptloader: { loadSubScript: jest.fn() },
     console: { logStringMessage: jest.fn() },
   };
   global.AddonManager = {
     getAddonByID: jest.fn(),
     getInstallForURL: jest.fn(),
   };
   global.XMLHttpRequest = jest.fn().mockImplementation(() => ({
     open: jest.fn(), send: jest.fn(),
     setRequestHeader: jest.fn(),
     onload: null, onerror: null, ontimeout: null,
     status: 200, responseText: "",
   }));
   global.crypto = {
     subtle: { digest: jest.fn().mockResolvedValue(new ArrayBuffer(32)) }
   };

2. Create a helper extractPluginStateLogic(responseJson):
   Since plugin-state.js uses EXPORTED_SYMBOLS JSM pattern, extract only the
   parsing logic into a testable pure function, OR load the module with
   require() after setting up global mocks and stub _fetchWithTimeout to
   return responseJson directly.

3. Create tests/test_plugin_state.js:

   TC-TB-01 test_v2_response_extracts_update_fields:
     response = { meta:{schema_version:2},
                  update:{action:"update",artifact_url:"/b/x.xpi",
                          target_version:"0.8.0",urgency:"normal",
                          checksum:"sha256:abc",campaign_id:5},
                  features:{f:true}, config:{} }
     result = parseResponse(response)  // testable extracted function
     expect(result._updateUrl).toBe("/b/x.xpi")
     expect(result._lastVersion).toBe("0.8.0")
     expect(result._updateUrgency).toBe("normal")
     expect(result._updateChecksum).toBe("sha256:abc")
     expect(result._campaignId).toBe(5)

   TC-TB-02 test_v2_response_null_update:
     response = { meta:{schema_version:2}, update:null, features:{}, config:{} }
     result = parseResponse(response)
     expect(result._updateUrl).toBeNull()
     expect(result._lastVersion).toBeNull()

   TC-TB-03 test_v1_legacy_flat:
     response = { updateUrl:"http://x/a.xpi", lastVersion:"0.7.1", owuiEndpoint:"http://llm" }
     result = parseResponse(response)  // legacy branch
     expect(result._updateUrl).toBe("http://x/a.xpi")
     expect(result._lastVersion).toBe("0.7.1")
     expect(result._features).toEqual({})

   TC-TB-04 test_features_extracted_v2:
     response = { meta:{schema_version:2}, features:{writer:false, calc:true}, update:null, config:{} }
     result = parseResponse(response)
     expect(result._features).toEqual({writer:false, calc:true})

   TC-TB-05 test_isFeatureEnabled_present_false:
     state._features = { writer: false }
     expect(isFeatureEnabled("writer", true)).toBe(false)

   TC-TB-06 test_isFeatureEnabled_absent_returns_default:
     state._features = {}
     expect(isFeatureEnabled("unknown", true)).toBe(true)

   TC-TB-07 test_xhr_headers_set:
     // trigger _checkRemoteConfig() via stub
     // verify xhr.setRequestHeader called with correct headers
     expect(xhrMock.setRequestHeader).toHaveBeenCalledWith("X-Plugin-Version", expect.any(String))
     expect(xhrMock.setRequestHeader).toHaveBeenCalledWith("X-Platform-Type", "thunderbird")
     expect(xhrMock.setRequestHeader).toHaveBeenCalledWith("X-Platform-Version", "60.9.1")

4. Create tests/test_auto_updater.js:

   TC-TB-08 test_checksum_verified_before_install:
     getUpdateChecksum returns "sha256:deadbeef"
     mock fetch/XHR to return bytes whose sha256 != "deadbeef"
     await AutoUpdater.checkForUpdates()
     expect(AddonManager.getInstallForURL).not.toHaveBeenCalled()

   TC-TB-09 test_install_proceeds_when_checksum_absent:
     getUpdateChecksum returns ""
     await AutoUpdater.checkForUpdates()
     expect(AddonManager.getInstallForURL).toHaveBeenCalled()

   TC-TB-10 test_critical_urgency_modal:
     getUpdateUrgency returns "critical"
     const notifSpy = jest.fn()
     // stub notification display function
     await AutoUpdater._installUpdate(url, version)
     expect(notifSpy).toHaveBeenCalled()

5. Run with: npm test --testPathPattern=tests/test_plugin_state
   Coverage threshold: 75% branches minimum.
```

---

## Prompt I — Suite de tests E2E (intégration complète)

> Repository : `device-management/`
> Créer : `tests/e2e/`, `tests/e2e/docker-compose.e2e.yml`,
>          `tests/e2e/test_e2e_campaign.py`, `tests/e2e/fixtures/`

```text
Context
-------
You are creating an end-to-end integration test suite for the complete
update & feature toggling pipeline. The E2E stack runs:
- Device Management (FastAPI) on port 3001
- PostgreSQL on port 5433
- A mock binary server (simple HTTP serving fake .oxt files) on port 9999
- A mock Keycloak (or real Keycloak in dev mode) on port 8080 (optional)

Goal
----
Create tests that simulate real plugin-DM interactions: enrollment,
config fetch, campaign activation, update directive delivery, and
campaign status tracking.

1. Create tests/e2e/docker-compose.e2e.yml:
   services:
     dm:
       build: .
       ports: ["3001:3001"]
       environment:
         - DATABASE_URL=postgresql://test:test@postgres-e2e:5432/dm_e2e
         - DM_BINARIES_MODE=local
         - DM_LOCAL_BINARIES_DIR=/tmp/binaries
         - AUTH_VERIFY_ACCESS_TOKEN=false  # disable JWT for E2E
     postgres-e2e:
       image: postgres:15
       environment: [POSTGRES_DB=dm_e2e, POSTGRES_USER=test, POSTGRES_PASSWORD=test]
     mock-binaries:
       image: python:3.12-slim
       command: python -m http.server 9999
       volumes: ["./fixtures/binaries:/srv:ro"]
       working_dir: /srv

2. Create tests/e2e/fixtures/:
   - binaries/libreoffice/2.0.0/mirai.oxt  (fake file, 100 bytes)
   - binaries/libreoffice/1.9.0/mirai.oxt  (fake rollback file)
   - Precompute SHA256 of each and store in fixtures/checksums.json

3. Create tests/e2e/test_e2e_campaign.py:

   Use httpx (async) to call the real DM API.
   BASE_URL = os.environ.get("DM_URL", "http://localhost:3001")

   @pytest.fixture(scope="module", autouse=True)
   def dm_stack():
       # docker-compose -f docker-compose.e2e.yml up -d --wait
       # run migration: db/migrations/002_campaigns.sql
       # yield
       # docker-compose down

   TC-E2E-01 test_healthz:
     GET /healthz → 200

   TC-E2E-02 test_config_no_campaign_returns_null_update:
     GET /config/libreoffice/config.json
       headers: X-Plugin-Version=1.0.0, X-Client-UUID=e2e-uuid-1
     assert update is None
     assert meta.schema_version == 2

   TC-E2E-03 test_full_campaign_lifecycle:
     # Step 1: Create artifact via DB insert
     artifact_id = db.insert(artifacts, device_type="libreoffice", version="2.0.0",
                             s3_path="libreoffice/2.0.0/mirai.oxt",
                             checksum=fixtures/checksums["lo-2.0.0"])
     # Step 2: Create active campaign
     campaign_id = db.insert(campaigns, type="plugin_update", status="active",
                             artifact_id=artifact_id, urgency="normal")
     # Step 3: Plugin fetches config
     r = GET /config/libreoffice/config.json
           X-Plugin-Version=1.0.0, X-Client-UUID=e2e-uuid-2
     assert r["update"]["action"] == "update"
     assert r["update"]["target_version"] == "2.0.0"
     # Step 4: Verify campaign_device_status created
     row = db.select(campaign_device_status, client_uuid="e2e-uuid-2")
     assert row["status"] == "notified"

   TC-E2E-04 test_rollback_campaign:
     # Active campaign targets 2.0.0, rollback to 1.9.0
     # Plugin reports version 2.1.0 (higher than target)
     r = GET /config/libreoffice/config.json X-Plugin-Version=2.1.0
     assert r["update"]["action"] == "rollback"
     assert r["update"]["target_version"] == "1.9.0"

   TC-E2E-05 test_feature_flag_per_cohort:
     # Create percentage cohort 100%, create feature flag with override=False
     r = GET /config/libreoffice/config.json X-Plugin-Version=1.0.0 X-Client-UUID=any
     assert r["features"]["test_feature"] == False

   TC-E2E-06 test_binary_download_from_update_url:
     # After receiving update directive, download the artifact_url
     artifact_url = r["update"]["artifact_url"]
     binary_r = GET {BASE_URL}{artifact_url}
     assert binary_r.status_code == 200
     content = binary_r.content
     assert hashlib.sha256(content).hexdigest() == r["update"]["checksum"].split(":")[1]

   TC-E2E-07 test_paused_campaign_returns_null:
     # Set campaign status="paused"
     db.update(campaigns, id=campaign_id, status="paused")
     r = GET /config/libreoffice/config.json X-Plugin-Version=1.0.0
     assert r["update"] is None

4. Create scripts/run-e2e.sh:
   #!/bin/bash
   set -e
   cd tests/e2e
   docker-compose -f docker-compose.e2e.yml up -d --wait
   sleep 3  # wait for DM startup
   DM_URL=http://localhost:3001 pytest test_e2e_campaign.py -v --tb=short
   EXIT_CODE=$?
   docker-compose -f docker-compose.e2e.yml down
   exit $EXIT_CODE

5. Add to CI (GitHub Actions or equivalent):
   - job: e2e-tests
     runs-on: ubuntu-latest
     steps:
       - uses: actions/checkout@v4
       - run: bash scripts/run-e2e.sh
```
