#!/bin/sh
# Render our config templates (secrets come from the environment) over the
# package defaults, then run Asterisk in the foreground.
set -eu
: "${AMI_SECRET:?}" "${SIP_PASSWORD:?}" "${INTERNAL_KEY:?}" "${WEB_SIP_PASSWORD:?}" "${SIP_DOMAIN:?}" "${LAN_IP:?}" "${PBX_TOKEN:?}"
for f in /etc/asterisk-templates/*.conf; do
    envsubst '${AMI_SECRET} ${SIP_PASSWORD} ${INTERNAL_KEY} ${WEB_SIP_PASSWORD} ${SIP_DOMAIN} ${LAN_IP} ${PBX_TOKEN}' < "$f" > "/etc/asterisk/$(basename "$f")"
done
chmod 600 /etc/asterisk/manager.conf /etc/asterisk/pjsip.conf

/bin/sh /modemstat.sh &

exec /usr/sbin/asterisk -f -vvv
