port: 7880
bind_addresses:
  - 0.0.0.0

rtc:
  tcp_port: 7881
  port_range_start: 50000
  port_range_end: 60000
  use_external_ip: true

# Built-in TURN disabled; we reuse the existing coturn at turn.witysk.org.
turn:
  enabled: false

# Redis reached via 127.0.0.1 because livekit runs with network_mode: host.
redis:
  address: 127.0.0.1:6379

# Placeholders are substituted at container start by the entrypoint sed
# pipeline (see docker-compose.yml `command:` for the livekit service).
# Putting webhook config here (not in env) is the only reliable path for
# `[]string` slice fields like webhook.urls — viper does not auto-split env
# vars into slices.
keys:
  __API_KEY__: __API_SECRET__

webhook:
  api_key: __WEBHOOK_KEY__
  urls:
    - http://127.0.0.1:8080/api/v1/webhooks/livekit

room:
  auto_create: true
  empty_timeout: 300
  max_participants: 50
  enabled_codecs:
    - mime: audio/opus
    - mime: video/VP8
    - mime: video/VP9
    - mime: video/H264
    - mime: video/AV1

logging:
  level: info
  json: true
