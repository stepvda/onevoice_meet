"""meetpp-speech: Meet++ tier-2 speech service (mlx-whisper STT + Kokoro TTS).

Runs on the association's Mac Studio, reachable only over the WireGuard tunnel
(10.88.0.2), called by meetpp-agent with HMAC-signed requests.
See docs/meetpp-v3-contract.md section 6.2 and FDD v3.1 sections 7.4 and 11.2.
"""

__version__ = "1.1.0"
