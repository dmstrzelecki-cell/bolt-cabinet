# systemd units

`bolt-cabinet.service` (the app itself) already exists on the container and
is **not** tracked here -- do not replace it from this directory.

These two files add the daily backup the handoff asks for. Install once, on
the container, as root:

```
cp /opt/bolt-cabinet/server/unit/bolt-cabinet-backup.* /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now bolt-cabinet-backup.timer
systemctl list-timers bolt-cabinet-backup.timer
```

`adminctl.py backup` keeps the most recent 14 snapshots and prunes the rest,
so the timer needs no cleanup of its own. Snapshots also happen
automatically before any destructive admin action, independently of this.
