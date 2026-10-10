#!/bin/sh
# Install the power-outage guard on acer: RTC wake works only from suspend (tested
# 2026-10-10), so on low battery we suspend with an RTC alarm instead of sleeping forever.
# Run from the dev box:  ssh acer 'sudo sh -s' < docs/acer/power-guard-setup.sh
set -e

cat > /usr/local/sbin/power-guard <<"EOF"
#!/bin/sh
# On battery at low charge, suspend with an RTC alarm so the laptop wakes
# periodically and stays up once AC is back.
AC=$(cat /sys/class/power_supply/ACAD/online)
CAP=$(cat /sys/class/power_supply/BAT1/capacity)
THRESHOLD=10
INTERVAL=1800
[ "$AC" = 1 ] && exit 0
[ "$CAP" -gt "$THRESHOLD" ] && exit 0
logger -t power-guard "on battery at ${CAP}%, suspending for ${INTERVAL}s"
exec rtcwake -m mem -s "$INTERVAL"
EOF
chmod 755 /usr/local/sbin/power-guard

cat > /usr/lib/systemd/system-sleep/power-guard-alarm <<"EOF"
#!/bin/sh
# Any suspend on battery gets an RTC alarm, so a sleep started by UPower or by hand
# never becomes permanent during a power outage.
[ "$1" = pre ] || exit 0
[ "$(cat /sys/class/power_supply/ACAD/online)" = 1 ] && exit 0
ALARM=/sys/class/rtc/rtc0/wakealarm
NOW=$(date +%s)
CUR=$(cat $ALARM)
[ -n "$CUR" ] && [ "$CUR" -gt "$NOW" ] && exit 0
echo 0 > $ALARM
echo $((NOW + 1800)) > $ALARM
logger -t power-guard "suspend on battery: RTC alarm set for +1800s"
EOF
chmod 755 /usr/lib/systemd/system-sleep/power-guard-alarm

cat > /etc/systemd/system/power-guard.service <<"EOF"
[Unit]
Description=Suspend with RTC wake on low battery during power outage

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/power-guard
EOF

cat > /etc/systemd/system/power-guard.timer <<"EOF"
[Unit]
Description=Check battery for power-guard every minute

[Timer]
OnBootSec=2min
OnUnitActiveSec=1min
AccuracySec=10s

[Install]
WantedBy=timers.target
EOF

# UPower's default HybridSleep needs hibernation (no resume= here) and has no RTC alarm.
cp -n /etc/UPower/UPower.conf /etc/UPower/UPower.conf.bak
sed -i 's/^CriticalPowerAction=.*/CriticalPowerAction=Suspend/' /etc/UPower/UPower.conf
grep -q '^AllowRiskyCriticalPowerAction' /etc/UPower/UPower.conf ||
  sed -i '/^CriticalPowerAction=/i AllowRiskyCriticalPowerAction=true' /etc/UPower/UPower.conf

systemctl daemon-reload
systemctl enable --now power-guard.timer
systemctl restart upower
grep -E '^(Allow|Critical)' /etc/UPower/UPower.conf
systemctl is-active power-guard.timer
