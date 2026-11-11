"""Atlas ntfy integration: inbound command channel (phone -> Brain).

Outbound push notifications live in atlas_plus.telegram.notify and the Brain
server/worker. This package adds the reverse direction: a listener that lets you
drive Brain from the ntfy phone app, mirroring the Telegram bridge.
"""
