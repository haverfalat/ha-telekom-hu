# Magyar Telekom for Home Assistant / HACS

An unofficial custom integration for Hungarian Magyar Telekom mobile
subscriptions. Requires **Home Assistant 2026.10 or newer**.

## Features

Each configured mobile number has its own device and these sensors:

| Sensor | Domestic | EU roaming |
| --- | --- | --- |
| Data remaining | GB | GB |
| Data used | GB | GB |
| Total allowance | GB | GB |
| Remaining percentage | % | % |
| Allowance expiry | Timestamp | Timestamp |

An additional diagnostic sensor shows the current session-renewal deadline.
Domestic and EU allowances can represent the same shared quota. They are never
added together. Unlimited amounts and missing values are unknown, not zero.
Data-size conversions follow the Telekom web client (1024 MB = 1 GB).

Default polling is every **15 minutes**, configurable between 5 minutes and
24 hours through the integration’s options. Use Home Assistant’s normal
`homeassistant.update_entity` action when you need an immediate update.

## Login lifetime

The integration renews the web session automatically, saves rotated cookies
immediately, and reloads them after Home Assistant restarts. The live renewal
test removed the access cookie, observed HTTP 401, renewed the session through
`POST /belepes/api/oidc/refresh-session?client_id=onecare`, and retrieved usage
again. The new refresh cookie received a later expiry.

This supports ongoing operation after one interactive login **while Telekom
continues accepting refreshes**. It cannot guarantee a permanent session.
Telekom can revoke credentials; a password change or a long offline period may
require signing in again. In the live test, access lasted about 29 minutes and
the refreshed renewal window was about 29 days. These are observed values,
not a promise that Telekom will keep the same policy. Months of continuous
operation have not been tested.

Expired/revoked credentials trigger Home Assistant’s reauthentication flow.
Temporary network errors or server outages make sensors unavailable and retry
later, without discarding the session or asking you to sign in unnecessarily.

## Install

The component is ready in `custom_components/telekom_hu`. It does not depend on
the standalone source package under `src/` or on a browser running in Home
Assistant. Its only external requirement is `httpx==0.28.1`.

### Manual installation / local archive

1. Extract `custom_components/telekom_hu` from the release archive into your
   Home Assistant configuration directory, producing
   `<config>/custom_components/telekom_hu/manifest.json`.
2. Restart Home Assistant.
3. Follow the setup steps below.

### HACS

Repository: **https://github.com/haverfalat/ha-telekom-hu**. Add it as a custom HACS integration:

1. In HACS, open Custom repositories.
2. Add the published repository URL with category **Integration**.
3. Download Magyar Telekom, restart Home Assistant, and configure it below.

## Configure once

On a computer with Python and Chrome, use the supplied repository’s helper:

```sh
uv sync --extra browser
uv run --extra browser python tools/browser_login.py
```

Complete Telekom’s official login yourself, including account password,
CAPTCHA, and SMS when requested. Select the correct household/account if there
is more than one. The helper confirms dashboard access and writes
`.telekom/web-session.json`, then closes its browser.

In Home Assistant:

1. Go to **Settings → Devices & services → Add integration → Magyar Telekom**.
2. Enter your mobile number, such as `+36301234567`.
3. Paste the complete contents of `.telekom/web-session.json` into the Session
   export field. The integration tests account access before finishing setup.

The initial login needs a browser because Telekom may require CAPTCHA and
SMS. The integration polls and renews with pure HTTP requests afterward.
It does not store your account password or SMS code.

For another mobile number, add another integration entry using a separately
created browser session from an account containing that number. Do not copy
the same rotating refresh credentials between independent entries. The helper
accepts `--profile` and `--output` for separate logins. Duplicate entries for the
same number are prevented. A failed import leaves existing credentials unchanged.

## Reauthentication

If Home Assistant says Telekom needs attention, rerun the browser helper and
paste the new export into the reauthentication dialog. The phone number and
sensor identities stay the same, so existing dashboards and automations keep
their entities. You do not need to remove and recreate the integration.

## Data and removal

Rotating cookies are stored using Home Assistant’s private storage under
`.storage/telekom_hu.<account-key>`. The config entry contains the phone number,
not the cookie export. Treat Home Assistant configuration backups as sensitive;
private storage is not a separate encrypted vault. Removing the integration
removes its local session store. Diagnostics exclude tokens, phone numbers,
account identifiers, and actual usage values. No changes are made to your
Telekom plan, add-ons, or billing settings.

The local browser helper uses a dedicated `.telekom/browser-profile`; do not
share it or the exported session. Source distributions and the HACS archive
exclude them.

## Validation

Tested against Home Assistant Core **2026.10.4**, including config flow,
sensor creation, polling options, unloading, reauthentication, cookie rotation,
expiry, temporary failures, and session reuse after a simulated restart.
Live tests separately verified refresh and data reads against the authorized
Telekom account. This is not yet tested on the user’s running Home Assistant
instance or over months of operation.

To run the dedicated Home Assistant tests:

```sh
uv venv --python 3.14 analysis/ha-venv
uv pip install --python analysis/ha-venv/bin/python \
  homeassistant==2026.10.4 pytest-homeassistant-custom-component==0.13.375
analysis/ha-venv/bin/python -m pytest -c tests_hass/pytest.ini tests_hass -q
```

Implementation follows the official
[config-flow documentation](https://developers.home-assistant.io/docs/config_entries_config_flow_handler/),
[coordinator documentation](https://developers.home-assistant.io/docs/integration_fetching_data/),
and [HACS repository structure](https://hacs.xyz/docs/publish/integration/).
