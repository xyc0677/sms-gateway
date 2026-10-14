# Asterisk 20 (Alpine package) + chan_quectel, which drives the Quectel EC20F:
# AT commands on /dev/quectel-at, call audio over the module's USB audio card.
FROM alpine:3.22 AS build
RUN apk add --no-cache build-base autoconf automake git patch asterisk-dev alsa-lib-dev sqlite-dev
# Pinned: IchthysMaranatha/asterisk-chan-quectel master as of 2025-03-24.
ARG CHAN_QUECTEL_COMMIT=3d45c7f072131296a7e3c1a4faf5bb18751dbd87
RUN git clone https://github.com/IchthysMaranatha/asterisk-chan-quectel.git /src \
 && cd /src && git checkout "$CHAN_QUECTEL_COMMIT"
# Handle unsolicited +CMT (SMS delivered straight to the TE instead of being
# stored): upstream drops both the header and the PDU line, i.e. loses the SMS.
COPY chan_quectel-cmt.patch /tmp/
RUN cd /src && patch -p1 < /tmp/chan_quectel-cmt.patch \
 && ./bootstrap \
 && ./configure --with-astversion=20.11.1 \
 && make

FROM alpine:3.22
# asterisk-srtp: SDES-SRTP for Linphone (Alpine ships res_srtp separately);
# asterisk-opus: Linphone's preferred codec, much better on lossy mobile links.
# asterisk-curl: CURL() for call screening against the app.
RUN apk add --no-cache asterisk asterisk-srtp asterisk-opus asterisk-curl asterisk-sounds-en alsa-lib sqlite-libs gettext-envsubst tzdata
COPY --from=build /src/chan_quectel.so /usr/lib/asterisk/modules/
COPY conf/ /etc/asterisk-templates/
COPY entrypoint.sh modemstat.sh /
ENTRYPOINT ["/bin/sh", "/entrypoint.sh"]
