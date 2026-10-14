# 实现说明

改代码或者查问题时看。只是部署使用的话，看 README 就够了。

## 目录

```
app/            FastAPI 应用（main.py）、单页网页（index.html）、号码归属地（numinfo.py + numdb/）
asterisk/       Asterisk 镜像、配置模板（conf/）、chan_quectel 补丁、模块状态轮询脚本
caddy/          Caddyfile
flexisip/       Flexisip 镜像（源码编译 + 补丁）、配置模板、启动脚本
dns/            可选的局域网 DNS（dnsmasq）
host/           需要装到宿主机上的 udev 规则和 NetworkManager 配置
```

配置里的 `${VAR}` 都在容器启动时从 `.env` 填入：
- Asterisk：`entrypoint.sh` 用 envsubst，并且只替换列出的变量，所以拨号方案里 Asterisk 自己的 `${EXTEN}` 等变量不受影响。
- Flexisip：`start.sh` 用 sed 渲染。
- dnsmasq：用 envsubst。
- Caddy：Caddyfile 里的 `{$VAR}` 由 Caddy 自己读取。

## 短信

- 收短信：chan_quectel 发 AMI 事件 `QuectelNewSMSBase64`，app 解码后依次：
  1. 判断是否拦截（`classify_sms`）；
  2. 写入 SQLite；
  3. 推送：普通短信用 Bark 推送，有验证码时用 `timeSensitive` 级别并附带复制；被拦截的短信只发 `passive` 静默通知。
- 发短信：AMI `QuectelSendSMS`，回执事件 `QuectelReport` 的 Payload 是消息 id。
- +CMT 补丁（`asterisk/chan_quectel-cmt.patch`）：EC20 对一部分短信（IMS、直推）不先存储再用 `+CMTI` 通知，而是直接用 `+CMT` 推出“头 + PDU”两行。上游 chan_quectel 会把两行都丢掉。补丁让读取层把两行收齐，按 `+CMGR` 解析，再照常发事件。
- 营销短信的判断，三个条件同时满足才拦：
  - 有退订、拒收字样（`MARKETING_RE`，国内营销短信按规定必须带）；
  - 有推销词或链接（`PROMO_RE`）；
  - 没有账户、快递、话费这类服务词（`SERVICE_RE`）。
  
  有验证码的短信永远不会被营销规则拦，但用户自己写的关键词规则优先。

## 电话

- 来电：`[from-quectel]` 先 `Gosub(callcheck)`，用 CURL 询问 `127.0.0.1:18300/internal/call-check`（带 `INTERNAL_KEY`，公网请求一律 403）。
  - 结果是 `block`：`Hangup(21)` 拒接。漫游时拒接不收费，对方听到忙音。
  - 结果是放行：`Dial(PJSIP/iphone/sip:iphone@SIP_DOMAIN)`，经 `outbound_proxy` 交给 Flexisip，由它查注册表并推送唤醒手机。
  - 查询超时或出错时一律放行。
- 智能拦截：号码第一次来电时现查百度号码标记，最多等 2.2 秒（Asterisk 的 CURL 总超时是 3 秒）；查不完就先放行，查询在后台继续并缓存，下次再来就能拦。
- 为什么不直接拨注册上来的 contact：Flexisip 伪装过的 contact 带 `transport=tls`，而 Asterisk 没有 TLS 传输，回到 Flexisip 还会形成环路（482）。所以统一发给 Flexisip，并把 From 域设成 `pbx-<PBX_TOKEN>.SIP_DOMAIN`：
  - Flexisip 的 Router filter 只认这个 From 域，看到它才当成“打给手机”的请求，查注册表并推送；
  - 其它所有请求，`routes.conf` 一律转给 Asterisk 做密码校验，不看请求地址。

  这里有两个坑，早期版本都踩过：
  - 如果用固定的 `pbx.SIP_DOMAIN`，或者按 `doroute`、`CtRt` 这类 URI 参数判断，外人照着写就能让 Flexisip 直接把电话推给手机，绕过拦截，来电号码也能随便填。所以域名里要带上 `PBX_TOKEN` 这个暗号。
  - 如果 `routes.conf` 只转发本域名的请求，别的地址 Flexisip 会照常往外转，就成了开放转发，别人可以借它往互联网和你的内网发 SIP 请求。
- 媒体：手机和 Asterisk 直连，不经过 Flexisip。
  - 用 ICE：在家走局域网地址（`media_address = LAN_IP`），在外走 STUN 学到的公网地址。
  - 强制 SDES-SRTP 加密。
  - 网页电话用 DTLS-SRTP（`webrtc=yes`），信令走 Caddy 的 `/ws` 到 Asterisk 的 8088 端口。Caddy 先用 `forward_auth` 问 app 是否已登录、Origin 是否正确。
- 外呼：`[outbound]` 只放行 `+86` 和 `+81`。拨号规则：
  - `00` 开头换成 `+`；
  - 以 `0` 开头的国内座机变成 `+86` 加去掉 0 的号码；
  - 其它号码默认加 `+86`。
- 回声测试：`*43`。干跑拦截判断：`channel originate Local/+86xxx@callcheck-test application Wait 1`。

## Linphone 推送链路

```
Flexisip (external-push-flexiapi)
  → https://SMS_DOMAIN:CADDY_HTTPS_PORT/flexiapi-router   （本机 Caddy，extra_hosts 指向 127.0.0.1）
  → app（校验 FLEXIAPI_ROUTER_KEY）
  → subscribe.linphone.org/api/push_notification          （用 linphone.org 账号的 API key）
```

- app 用 Digest 认证自动申请、续期 API key。key 绑定申请时的出口 IP，所以强制走 IPv4。
- Flexisip 镜像带 `lenient-pn-prid.patch`：Linphone iOS 6.2.x 被 VoIP 推送冷启动后重新注册时，第二段 pn-prid 缺少标签，原版 Flexisip 会悄悄拒绝，之后就再也推不出去了。补丁来自 sparkcyf，构建时会自动验证。每次升级 Flexisip 都要重新确认这个补丁。
- Flexisip 的注册表在内存里，重启后需要在手机上打开一次 Linphone。

## 模块状态和值守

- `asterisk/modemstat.sh` 每 30 秒在空闲的 AT 口（USB 接口 03，`/dev/quectel-status`）上执行一组只读查询，结果写到 `data/asterisk/modemstat/status.txt`：
  - `AT+QTEMP`
  - `AT+QENG="servingcell"`
  - `AT+QSPN`
  - `AT+QNWINFO`
  - `AT+QCFG="ims"`

  chan_quectel 占用的是接口 02，所有主动上报（`+CMTI`、`RING`……）也都发到那个口，所以这个轮询不会抢走事件。chan_quectel 没有发任意 AT 命令的 AMI 接口，所以才单独用这个口。
- app 的 `watchdog()` 每 60 秒检查一次：
  - Asterisk 是否连得上；
  - 模块是否在线、是否注册网络；
  - VoLTE 是否可用；
  - 模块温度；
  - 状态数据是否过期。

  问题持续超过各自的宽限时间后，推一条 `timeSensitive` 告警；恢复后再推一条“已恢复”。
- 定时任务：
  - 每天 4 点后备份（`sqlite3` 在线备份，保留 30 天）；
  - 每天 9 点到 12 点之间推一次小结。

  上次执行的日期记在 settings 表（`last_backup`、`last_summary`），重启不会重复执行。

## 网页

- 单页应用。用 Ionic 8 的 iOS 风格，加上自己写的玻璃质感导航栏、标签栏和输入框。
- 页面切换用 View Transitions API；返回时恢复列表的缓存和滚动位置。
- 登录支持密码（有频率限制）和通行密钥（py_webauthn）。RP ID 是 `public_base_url` 的主机名，所以通行密钥只能在公网域名下使用。
- 号码归属地离线查询：手机号段 `phone.dat` 加座机区号表，日本号码和常见服务号码的规则写在 `numinfo.py` 里。号码标记从百度移动搜索的号码卡片解析：
  - 每次查询至少间隔 3 秒；
  - 结果缓存 30 天；
  - 遇到验证页就暂停查询 6 小时。
