#!/bin/sh
# Render the domains and the router key into the config, then wait for Caddy's
# certificate (first issuance needs DNS + Let's Encrypt) before starting.
set -eu
: "${FLEXIAPI_ROUTER_KEY:?}" "${SMS_DOMAIN:?}" "${SIP_DOMAIN:?}" "${CADDY_HTTPS_PORT:?}" "${PBX_TOKEN:?}"
render() {
    sed -e "s|\${FLEXIAPI_ROUTER_KEY}|${FLEXIAPI_ROUTER_KEY}|g" -e "s|\${SMS_DOMAIN}|${SMS_DOMAIN}|g" \
        -e "s|\${SIP_DOMAIN}|${SIP_DOMAIN}|g" -e "s|\${CADDY_HTTPS_PORT}|${CADDY_HTTPS_PORT}|g" \
        -e "s|\${PBX_TOKEN}|${PBX_TOKEN}|g" "$1" > "$2"
}
render /etc/flexisip/flexisip.conf.tmpl /etc/flexisip/flexisip.conf
render /etc/flexisip/routes.conf.tmpl /etc/flexisip/routes.conf
chmod 600 /etc/flexisip/flexisip.conf
cert=$(sed -n 's/^tls-certificates-file=//p' /etc/flexisip/flexisip.conf)
until [ -s "$cert" ]; do
    echo "waiting for $cert (Caddy/Let's Encrypt)"
    sleep 60
done
exec /opt/belledonne-communications/bin/flexisip --server proxy
