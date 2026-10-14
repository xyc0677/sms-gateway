# Implementation notes

[简体中文](../internals.md) · **English**

For changing code or digging into problems. To just run the gateway, the README is enough.

## Layout

```
app/            FastAPI app (main.py), single-page web UI (index.html), number lookup (numinfo.py + numdb/)
asterisk/       Asterisk image, config templates (conf/), chan_quectel patch, modem status poller
caddy/          Caddyfile
flexisip/       Flexisip image (built from source + patch), config templates, start script
dns/            optional LAN DNS (dnsmasq)
host/           udev rules and NetworkManager config to install on the host
install.sh      installer: asks questions, writes .env, prepares the modem, builds and starts (--dry-run only writes the config)
```

`${VAR}` placeholders in the configs are filled from `.env` when the containers start:
- Asterisk: `entrypoint.sh` runs envsubst with an explicit list of variables, so Asterisk's own dialplan variables such as `${EXTEN}` are left alone.
- Flexisip: rendered with sed by `start.sh`.
- dnsmasq: envsubst.
- Caddy: reads `{$VAR}` in the Caddyfile itself.

## Texts

- Receiving: chan_quectel emits the AMI event `QuectelNewSMSBase64`. The app decodes it, then:
  1. decides whether to block it (`classify_sms`);
  2. stores it in SQLite;
  3. pushes it: normal texts through Bark, with `timeSensitive` level and a copy action when there is a verification code; blocked texts only as a silent `passive` notification.
- Sending: AMI `QuectelSendSMS`; the delivery event `QuectelReport` carries the message id as Payload.
- +CMT patch (`asterisk/chan_quectel-cmt.patch`): for some texts (IMS, direct delivery) the EC20 doesn't store them and announce them with `+CMTI`, but pushes a two-line `+CMT` header + PDU instead. Upstream chan_quectel drops both lines. The patch makes the reader collect both lines, parse them like `+CMGR`, and emit the usual event.
- A text counts as marketing only when all three hold:
  - it has an opt-out phrase (`MARKETING_RE`; mandatory for marketing texts in mainland China);
  - it has sales wording or a link (`PROMO_RE`);
  - it has no account, delivery or billing wording (`SERVICE_RE`).

  Texts with a verification code are never blocked by the marketing rule, but the user's own keyword rules take precedence.

## Calls

- Incoming: `[from-quectel]` first runs `Gosub(callcheck)`, which asks `127.0.0.1:18300/internal/call-check` through CURL (with `INTERNAL_KEY`; requests from the internet always get 403).
  - `block`: `Hangup(21)` rejects the call. Rejecting costs nothing while roaming; the caller hears busy.
  - allowed: `Dial(PJSIP/iphone/sip:iphone@SIP_DOMAIN)` goes through `outbound_proxy` to Flexisip, which looks up the registration and wakes the phone with a push.
  - On a timeout or error the call is allowed.
- Smart blocking: the first call from a number triggers a live Baidu lookup, waiting at most 2.2 s (Asterisk's CURL timeout is 3 s in total). If it doesn't finish in time the call rings; the lookup continues in the background and is cached for next time.
- Why not dial the registered contact directly: the contact as rewritten by Flexisip carries `transport=tls`, Asterisk has no TLS transport, and going back through Flexisip loops (482). So every call goes to Flexisip with the From domain `pbx-<PBX_TOKEN>.SIP_DOMAIN`:
  - Flexisip's Router filter only matches that From domain; only then does it treat the request as a call to the phone, look up the registration and push;
  - every other request is sent to Asterisk by `routes.conf`, regardless of its request URI, so Asterisk authenticates it.

  Early versions got both of these wrong:
  - With a fixed `pbx.SIP_DOMAIN`, or matching URI parameters such as `doroute`/`CtRt`, anyone could make Flexisip push a call straight to the phone, bypassing call screening and with any caller ID. Hence the secret `PBX_TOKEN` in the domain.
  - If `routes.conf` only forwards requests for our own domain, Flexisip relays everything else onwards, i.e. it becomes an open SIP relay into the internet and your LAN.
- Media goes directly between phone and Asterisk, not through Flexisip.
  - ICE: the LAN address at home (`media_address = LAN_IP`), the STUN-learned public address outside.
  - SDES-SRTP is mandatory.
  - The web phone uses DTLS-SRTP (`webrtc=yes`); signalling goes through Caddy's `/ws` to Asterisk's port 8088. Caddy first asks the app (`forward_auth`) whether the session is logged in and the Origin is right.
- Outgoing: `[outbound]` only allows `+86` and `+81`. Dialling rules:
  - a leading `00` becomes `+`;
  - a leading `0` (Chinese landline) becomes `+86` without the 0;
  - anything else gets `+86`.
- Echo test: `*43`. Dry run of the screening decision: `channel originate Local/+86xxx@callcheck-test application Wait 1`.

## Linphone push chain

```
Flexisip (external-push-flexiapi)
  → https://SMS_DOMAIN:CADDY_HTTPS_PORT/flexiapi-router   (local Caddy; extra_hosts points it at 127.0.0.1)
  → app (checks FLEXIAPI_ROUTER_KEY)
  → subscribe.linphone.org/api/push_notification          (with the linphone.org account's API key)
```

- The app creates and renews the API key with Digest authentication. The key is tied to the outgoing IP it was created from, so IPv4 is forced.
- The Flexisip image includes `lenient-pn-prid.patch`: when Linphone iOS 6.2.x is cold-started by a VoIP push and re-registers, the second pn-prid token lacks its label. Stock Flexisip silently rejects that and never pushes again. The patch is by sparkcyf and is verified during the build. Re-check it on every Flexisip upgrade.
- Flexisip keeps registrations in memory; after it restarts, open Linphone once on the phone.

## Modem status and watchdog

- Every 30 seconds `asterisk/modemstat.sh` runs read-only queries on the spare AT port (USB interface 03, `/dev/quectel-status`) and writes the answers to `data/asterisk/modemstat/status.txt`:
  - `AT+QTEMP`
  - `AT+QENG="servingcell"`
  - `AT+QSPN`
  - `AT+QNWINFO`
  - `AT+QCFG="ims"`

  chan_quectel uses interface 02, which also receives all unsolicited messages (`+CMTI`, `RING`...), so the poller never steals events. A separate port is used because chan_quectel has no AMI action for arbitrary AT commands.
- The app's `watchdog()` checks every 60 seconds:
  - whether Asterisk is reachable;
  - whether the modem is online and registered;
  - whether VoLTE is available;
  - the modem temperature;
  - whether the status data is stale.

  Once a problem lasts longer than its grace period, a `timeSensitive` alert is pushed; another push follows when it recovers.
- Scheduled jobs:
  - a backup after 04:00 every day (`sqlite3` online backup, 30 days kept);
  - a summary between 09:00 and 12:00 every day.

  The date of the last run is stored in the settings table (`last_backup`, `last_summary`), so restarts don't repeat them.

## Web page

- A single-page app: Ionic 8 in iOS style, with custom glass-style navigation bar, tab bar and composer.
- Page transitions use the View Transitions API; going back restores the cached list and scroll position.
- Login by password (rate limited) or passkey (py_webauthn). The RP ID is the host of `public_base_url`, so passkeys only work on the public domain.
- Number lookup is offline: mobile ranges from `phone.dat` plus a landline area-code table; rules for Japanese numbers and common service numbers live in `numinfo.py`. Number reports are parsed from the number card in Baidu's mobile search:
  - at least 3 seconds between lookups;
  - results cached for 30 days;
  - lookups pause for 6 hours when Baidu shows a verification page.
