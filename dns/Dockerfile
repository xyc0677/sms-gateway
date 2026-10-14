FROM alpine:3.22
RUN apk add --no-cache dnsmasq gettext-envsubst
COPY dnsmasq.conf.tmpl /etc/dnsmasq.conf.tmpl
# Rendered at start so LAN_IP / SIP_DOMAIN / LAN_DNS_UPSTREAM come from .env.
CMD ["/bin/sh", "-c", ": \"${LAN_IP:?}\" \"${SIP_DOMAIN:?}\" \"${LAN_DNS_UPSTREAM:?}\" && envsubst '${LAN_IP} ${SIP_DOMAIN} ${LAN_DNS_UPSTREAM}' < /etc/dnsmasq.conf.tmpl > /etc/dnsmasq.conf && exec dnsmasq --keep-in-foreground --log-facility=- --conf-file=/etc/dnsmasq.conf"]
