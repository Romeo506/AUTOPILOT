#!/data/data/com.termux/files/usr/bin/bash
# Optional: start AUTOPILOT automatically after the phone boots (needs the Termux:Boot app from F-Droid).
set -e
mkdir -p ~/.termux/boot
cat > ~/.termux/boot/autopilot.sh <<BOOT
#!/data/data/com.termux/files/usr/bin/bash
termux-wake-lock
cd "$(cd "$(dirname "$0")/.." && pwd)"
bash scripts/start.sh >> data/server.log 2>&1
BOOT
chmod +x ~/.termux/boot/autopilot.sh
echo "Installed ~/.termux/boot/autopilot.sh. Open the Termux:Boot app once so Android allows it."
