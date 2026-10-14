# Common pitfalls

[简体中文](../troubleshooting.md) · **English**

Most of these actually happened while I was setting this up. They're in the order you'd run into them.

The web interface is in Chinese; see the label table in the [README](../../README.en.md#manual-install) for the menu names used below.

## Modem

### `lsusb` shows the modem, but there is no `/dev/ttyUSB*`

The kernel's `option` driver didn't pick the device up. Try:

```sh
sudo modprobe option
echo 2c7c 0125 | sudo tee /sys/bus/usb-serial/drivers/option1/new_id
```

If that doesn't help, your NAS kernel probably lacks the driver, and you'll need another OS or machine. See the NAS section of [environments.md](environments.md).

### `quectel show devices` says `Not connected`, or State never becomes Free

Check in this order:

1. `ls -l /dev/quectel-at`: does the link exist? After a replug or modem restart the ttyUSB numbers can change. The udev rule repoints the link, but only if it's installed.
2. Did ModemManager grab the port again? `sudo lsof /dev/ttyUSB*` should show nothing but asterisk. The `ID_MM_DEVICE_IGNORE` setting in the udev rule only works if it's in place before ModemManager starts. The simplest fix is `sudo systemctl disable --now ModemManager`, unless other devices need it.
3. Is something else holding the same port, such as a picocom session left open?

### No texts for a while after the modem restarts

Expected. Registering on the network takes a minute or two, longer when roaming. chan_quectel reconnects by itself.

### `arecord -l` shows no Quectel sound card

- The AT command that turns on USB audio wasn't run, or the modem wasn't restarted afterwards.
- Or the kernel has no `snd-usb-audio` module: `lsmod | grep snd_usb_audio`. Many NAS systems ship without it. Texts still work then, but calls don't.

### The status page says VoLTE is unavailable

Where 2G/3G is gone (Japan, the US...), no VoLTE means no calls at all. Whether it works depends on three things: the modem firmware, its MBN configuration, and whether your carrier has a VoLTE roaming agreement with the local network.

Start with:

```sh
sudo docker compose exec asterisk asterisk -rx 'quectel cmd quectel0 AT+QCFG="ims"'
sudo docker compose exec asterisk asterisk -rx 'quectel cmd quectel0 AT+QMBNCFG="list"'
```

The replies only show up in `docker compose logs asterisk`. How to enable IMS is described in Quectel's IMS application note.

Note that the status page reads the modem through USB interface 03, so the modem must use the default EC20/EC25 USB layout.

## Certificates and DNS

### Caddy never gets a certificate

- Make sure port 80 is reachable from the internet. Turn off Wi-Fi on your phone and open `http://sms.example.com`.
- Your ISP may block 80 and 443 (common on residential lines in China). See "Ports 80/443 blocked" in [environments.md](environments.md).
- With the Cloudflare proxy on (orange cloud), redirect settings such as "Always Use HTTPS" can break the certificate check. The quickest test: turn the orange cloud off until the certificate is issued, then back on. Renewals usually work fine afterwards.
- Too many failures in a short time trigger Let's Encrypt's rate limit; wait about an hour.

### Flexisip keeps saying `waiting for .../sip.example.com.crt`

The certificate for the SIP domain hasn't been issued yet; check `docker compose logs caddy`. Most often the SIP domain is proxied on Cloudflare.

### The phone connects from outside but not on home Wi-Fi

Two likely causes:

- Your router has no NAT loopback. See "Works outside, fails on home Wi-Fi" in the README.
- IPv6: the iPhone prefers IPv6. If the domain has an AAAA record but this machine doesn't actually receive IPv6 traffic (some routers drop it inside the LAN), connections fail. If unsure, delete the AAAA record.

## Web page

### The first password can "only be set from the LAN"

You opened the page through the domain. Use `http://<LAN_IP>:18300` instead; a Tailscale 100.x address works too.

### Can't add a passkey

Passkeys are tied to the public domain. They can only be added and used on `https://sms.example.com`; the button doesn't appear when the page is opened by IP.

### The page looks unchanged after an update

Home-screen web apps on the iPhone cache aggressively. Swipe it away and open it again.

## Bark

### The test push reports success but nothing arrives

- In the Bark app, check which server the key belongs to. A key copied under the official server doesn't work on your own server, and vice versa.
- The same phone has a different key on each server.
- The gateway pushes to each device separately; failed devices are listed in the test result.

### Notifications arrive as gibberish

An encryption key is set, but Bark on that device has encryption off or a different key.

### Blocked calls and texts don't make a sound

Intended: those are silent notifications that only go to Notification Center. Alerts and normal texts do make a sound.

## Linphone and calls

### Calls fail in Linphone; the log shows 488 Not Acceptable

Linphone's media encryption isn't set to SRTP. The gateway requires SRTP because the audio crosses the internet.

### Linphone registers, but calls drop right away or the caller hears busy

- Check Settings → Call blocking; the call may have been blocked (it's listed on the blocked page).
- Otherwise look at the Dial result in `docker compose logs asterisk`. CONGESTION usually means Flexisip has no registration for the phone, e.g. Flexisip just restarted. Open Linphone once.

### Rings only when Linphone is open, not when locked

The push isn't getting through:

1. Does the Linphone section of Settings say the push key is OK? linphone.org ties the key to the public IP it was created from; after your IP changes, press "Save and test" there again.
2. Are notifications and push enabled for Linphone?
3. `docker compose logs flexisip | grep -i push` shows whether a push was sent.

### Connected, but no audio or one-way audio

- UDP 10000-10099 isn't forwarded or is blocked by a firewall. This is by far the most common cause.
- `LAN_IP` in `.env` is wrong. On home Wi-Fi, audio goes to that address.
- Dial `*43` from the web page (echo test):
  - echo works but Linphone doesn't: the problem is Linphone's network or settings;
  - no echo either: the problem is the server's ports.

### Incoming calls work, outgoing don't

Dialled numbers are converted to international format (+86…). Only numbers starting with +86 or +81 may be dialled; others are refused with a `Refused outbound call` line in the log.

### Calls stopped ringing after changing PBX_TOKEN

Both asterisk and flexisip must be restarted so they share the new token: `sudo docker compose up -d --force-recreate asterisk flexisip`.

### Startup fails with `... is missing or shorter than 16 characters`

A secret in `.env` is empty or too short. Generate one with `openssl rand -hex 24`.

## Building

### The machine freezes or OOM-kills the Flexisip build

Lower `JOBS`:

```sh
sudo docker compose build --build-arg JOBS=2 flexisip
```

With less than 2 GB of RAM, add some swap, or build on another computer and copy the image over with `docker save` / `docker load`.

### `git clone` fails during `docker compose build`

The build downloads source code from GitHub (chan_quectel) and gitlab.linphone.org (Flexisip). From mainland China you may need a proxy for Docker.
