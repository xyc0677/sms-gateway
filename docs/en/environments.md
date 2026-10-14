# Other environments

[简体中文](../environments.md) · **English**

The README follows my own setup: Japan, a public IPv4 address, iPhone, an EC20 modem, an x86 NAS. Here is how to adapt it to other situations.

Sections marked "untested" are worked out from how things fit together. The general approach is sound, but expect to adjust details yourself.

## Network

### No public IPv4

First check two things:
- the WAN address your router shows;
- the output of `curl -4 ifconfig.me`.

If they differ, or the WAN address looks like `100.64.x.x` or `10.x.x.x`, you're behind carrier-grade NAT and nothing can connect in from outside. Pick by what you need:

#### Texts only (simplest)

Receiving texts and sending pushes are both outgoing connections, so no inbound port is needed:

1. Use Bark's official server `https://api.day.app` instead of the self-hosted one. Copy the test URL of the official server in the Bark app and add it under Settings → Bark devices. Turn on push encryption too, since the content passes through the official server.
2. Start only what's needed: `sudo docker compose up -d asterisk app`.
3. To reach the web page from outside:
   - with Tailscale: connect the phone to Tailscale and open `http://<the machine's 100.x address>:18300`;
   - or with a Cloudflare Tunnel pointing a domain at `http://localhost:18300`. Requests through the tunnel carry a `Cf-Connecting-IP` header, so the gateway treats them as coming from the internet, which is the safe choice.
   - With a tunnel, set "public address of this page" in Settings to the tunnel's domain, otherwise passkeys won't work.
4. Set call blocking to "Block all" so calls are rejected and only logged.

#### Calls too, through Tailscale (untested)

The idea is that the phone reaches home through Tailscale instead of the open internet:

1. Point the A record of `SIP_DOMAIN` at the machine's Tailscale address (100.x.x.x). Public DNS records may point at private addresses.
2. Certificates can no longer be validated over HTTP; switch to DNS validation, see "Ports 80/443 blocked" below.
3. Keep Tailscale on the iPhone connected at all times. Waking the phone for calls is an outgoing connection from Flexisip to linphone.org and isn't affected.

The cost: a bit more battery use, and no incoming calls whenever Tailscale is disconnected.

#### Calls too, through a rented VPS (untested)

1. Connect the home machine to the VPS with WireGuard.
2. On the VPS, forward TCP 80, 443, 5061 and UDP 10000-10099 to the home machine's WireGuard address with iptables.
3. Point the domains at the VPS.

The tricky part is call audio: Asterisk learns your home connection's address through STUN, not the VPS's. You'll need to:
- remove `stunaddr` from `asterisk/conf/rtp.conf`;
- add `external_media_address = <VPS public IP>` to the transport in `pjsip.conf`;
- send return traffic back through WireGuard, with policy routing or SNAT on the VPS.

It works, but needs some debugging.

### Dynamic public IP

- Run a DDNS container to keep the A records up to date, e.g. `favonia/cloudflare-ddns` for Cloudflare.
- Asterisk looks up its public address through STUN for every call, so nothing to change there.
- linphone.org ties the push key to the IP it was created from. After your IP changes, press "Save and test" in the Linphone section of Settings again.

### Ports 80/443 blocked by your ISP (common in mainland China)

There are two separate problems: certificates, and the port the page is served on.

#### Certificates through DNS validation

The official Caddy image has no DNS plugins, so build your own. For Cloudflare:

```dockerfile
# caddy/Dockerfile
FROM caddy:2.11.4-builder-alpine AS build
RUN xcaddy build --with github.com/caddy-dns/cloudflare
FROM caddy:2.11.4-alpine
COPY --from=build /usr/bin/caddy /usr/bin/caddy
```

Then:
- in `compose.yaml`, replace `image:` of the caddy service with `build: ./caddy` and add a `CF_API_TOKEN` environment variable (a token that can only edit this zone's DNS);
- add `acme_dns cloudflare {env.CF_API_TOKEN}` to the global options at the top of the Caddyfile.

Other DNS providers (Alibaba Cloud, DNSPod, ...) have plugins too; see the caddy-dns organisation on GitHub.

#### Serve the page on another port

In `.env`:

```sh
CADDY_HTTPS_PORT=8443
CADDY_BIND=0.0.0.0
```

Forward 8443 on the router. The page then lives at `https://sms.example.com:8443`, and the port must also be included in:
- the Bark server address;
- "public address of this page" in Settings.

Flexisip's push relay picks up the port automatically.

5061 and 10000-10099 are usually not blocked.

### IPv6 only

Not tried. Caddy, Flexisip and Asterisk all support IPv6 in principle, but ICE candidates and the various ACLs are written for IPv4, so expect a fair amount of changes.

## Phone

### Android

- Notifications: Bark is iOS only, and Bark is the only push service implemented so far. Options:
  - change `bark_push()` in `app/main.py` to use [ntfy](https://ntfy.sh), Gotify or a Telegram bot. Each is a single HTTP POST, so the change is small;
  - skip push notifications and read texts on the web page.
- Calls:
  - Linphone for Android should work, since linphone.org's push service supports Firebase. Untested.
  - The web phone works in Chrome on Android.

### Not using Linphone

Any SIP client with TLS and SRTP (SDES) can register (username `iphone`), e.g. Acrobits Groundwire or Zoiper.

Waking a locked phone, however, relies on Flexisip and linphone.org's push service, which only Linphone uses. With another client, calls only ring while the app is open, and iOS suspends background apps quickly. For outgoing calls only, the web phone is enough.

### Texts only, no calls

You can skip all of these:
- Flexisip and Linphone;
- ports 5061 and 10000-10099;
- VoLTE and the modem's USB audio.

Start only the services you need:

```sh
sudo docker compose up -d asterisk app bark caddy
```

Set call blocking to "Block all", so calls are rejected and only logged.

## Modem

- EC25: same USB ID as the EC20, works as is.
- EG25-G, EC21 and other EC2x/EG2x models: supported by chan_quectel, untested. Check the ID with `lsusb`; if it isn't `2c7c:0125`, adapt `host/77-quectel-sms-gateway.rules`.
- EC200, EC600 and other Quectel models: different USB layout and AT command set, untested. You'll need to:
  - find out which port is the AT port;
  - change the interface numbers in the udev rule;
  - check whether the modem has a USB audio (UAC) device.
- SIMCom (SIM7600, ...) and Huawei modems: chan_quectel isn't written for them; you'd need a different Asterisk channel driver, which is a lot of work.

Finding the interface numbers:

```sh
for t in /dev/ttyUSB*; do echo "$t $(udevadm info -q property $t | grep ID_USB_INTERFACE_NUM)"; done
```

Then connect to each with picocom and type `AT`; the one answering `OK` is an AT port. In the udev rule, `02` is the port for Asterisk and `03` the one for the status page.

The modem details on the status page are a nice-to-have. If they can't be read, the page shows a notice; texts and calls are unaffected.

## Host

- Debian, Ubuntu and other plain Linux: the least hassle.
- fnOS: what I use myself; works as is.
- Synology DSM 7: DSM 7 removed many USB drivers, so USB serial and USB audio may need community driver packages, and udev rules may not persist. In that case:
  - write `/dev/ttyUSB2` instead of `/dev/quectel-at` in `asterisk/conf/quectel.conf`;
  - but the number may change after a replug.

  Running it in a VM on the Synology, or on a separate mini PC, is easier.
- Proxmox, ESXi and other VMs:
  - pass the USB device through by ID (2c7c:0125), not by physical port. The modem re-enumerates when it restarts, and only ID passthrough follows it.
  - Some hypervisors pass through USB audio's isochronous transfers poorly, so calls may stutter; texts are unaffected.
- Raspberry Pi and other ARM64 boards: untested.
  - All base images used (Alpine, Debian, Python, Caddy) have arm64 builds; check bark-server before relying on it.
  - Compiling Flexisip on a Raspberry Pi 4 takes an hour or two; add swap and set `JOBS` to 2.

## Region and SIM

- Living in mainland China: the SIM isn't roaming, everything works the same. No dialling changes needed.
- Not in Japan:
  - change `TZ` in `.env`;
  - edit `[outbound]` in `asterisk/conf/extensions.conf`: replace `+81` with your country code, or remove it and keep only `+86`.
- A SIM from outside mainland China: these parts are written for mainland China:
  - the marketing-text rules;
  - Baidu number reports;
  - number region lookup;
  - dialling rules (`[from-iphone]` adds `+86` to numbers without a country code).

  You'll need to:
  - change the dialling rules;
  - turn off "block marketing texts" in Settings;
  - set call blocking to "Block list only" or "Block all".

  Receiving texts, notifications and calls themselves are unaffected.
