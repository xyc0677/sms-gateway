# Flexisip 2.6.2 (proxy only) as the SIP push gateway in front of Asterisk, so the
# stock App Store Linphone gets woken by APNs (via Linphone's FlexiAPI) for calls.
#
# lenient-pn-prid.patch: Linphone iOS 6.2.x, when cold-started by a VoIP push,
# re-REGISTERs with pn-prid "<voip>:voip&<remote>" (label missing on the second
# token). Stock Flexisip rejects that silently and stops pushing until the app is
# opened. Patch by sparkcyf (github.com/sparkcyf/linphone-asterisk-sip-push-gw-work-log),
# reviewed; 2.6.2 does not touch this file. Re-check on every Flexisip upgrade.
FROM debian:trixie AS build
ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates git make ninja-build cmake g++ pkg-config python3 python3-pystache python3-six \
      nasm yasm libsystemd-dev libssl-dev libboost-dev libboost-system-dev libboost-thread-dev \
      libcpp-jwt-dev libjansson-dev libjsoncpp-dev libnghttp2-dev libsqlite3-dev libxerces-c-dev \
      libsrtp2-dev libxml2-dev zlib1g-dev libzstd-dev libcap-dev \
 && rm -rf /var/lib/apt/lists/*
ARG FLEXISIP_TAG=2.6.2
ARG FLEXISIP_COMMIT=68ba0e8a51227acbb7e88f2d2f90fb2afee5bee4
RUN git clone --branch "$FLEXISIP_TAG" --recurse-submodules --shallow-submodules \
      https://gitlab.linphone.org/BC/public/flexisip.git /src \
 && test "$(git -C /src rev-parse HEAD)" = "$FLEXISIP_COMMIT"
COPY lenient-pn-prid.patch /tmp/
# Leave some CPU for everything else on a small NAS; lower it if the build
# starves other services or runs out of memory.
ARG JOBS=6
RUN cd /src && git apply /tmp/lenient-pn-prid.patch
RUN cd /src && cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release \
      -DCMAKE_INSTALL_PREFIX=/opt/belledonne-communications -DSYSCONF_INSTALL_DIR=/etc \
      -DENABLE_PRESENCE=OFF -DENABLE_REDIS=OFF -DENABLE_SNMP=OFF -DENABLE_SOCI=OFF \
      -DENABLE_TRANSCODER=OFF -DENABLE_VOICEMAIL=OFF -DENABLE_REGEVENT=OFF -DENABLE_B2BUA=OFF \
      -DINTERNAL_LIBSRTP2=OFF -DENABLE_UNIT_TESTS=OFF -DENABLE_STRICT=OFF -DENABLE_SANITIZERS=OFF \
 && cmake --build build -j"$JOBS" \
 && DESTDIR=/stage cmake --install build
# The patch must accept the unambiguous unlabelled token and still reject the ambiguous one.
RUN P=/stage/opt/belledonne-communications; export LD_LIBRARY_PATH=$P/lib; \
    $P/bin/flexisip_pusher --pn-provider apns --pn-param "ABCD1234.org.linphone.phone.voip&remote" \
      --pn-prid "AAAA1111:voip&bbbb2222" --prefix /tmp 2>&1 | grep -qi "assuming service" \
 && ! $P/bin/flexisip_pusher --pn-provider apns --pn-param "ABCD1234.org.linphone.phone.voip&remote" \
      --pn-prid "AAAA1111&bbbb2222" --prefix /tmp 2>&1 | grep -qi "assuming service" \
 && echo "pn-prid patch verified"

FROM debian:trixie-slim
ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates libsystemd0 libssl3t64 libjansson4 libjsoncpp26 libnghttp2-14 libsqlite3-0 \
      libxerces-c3.2t64 libsrtp2-1 zlib1g libzstd1 libcap2 \
      $(apt-cache show libxml2-16 >/dev/null 2>&1 && echo libxml2-16 || echo libxml2) \
      $(apt-cache search --names-only '^libboost-(system|thread)1\.[0-9]+\.0$' | awk '{print $1}') \
 && rm -rf /var/lib/apt/lists/*
COPY --from=build /stage/ /
RUN mkdir -p /var/opt/belledonne-communications/log/flexisip \
 && ldconfig \
 && ! ldd /opt/belledonne-communications/bin/flexisip | grep "not found" \
 && /opt/belledonne-communications/bin/flexisip --version
ENTRYPOINT ["/opt/belledonne-communications/bin/flexisip"]
CMD ["--server", "proxy"]
