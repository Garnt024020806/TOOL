#!/data/data/com.termux/files/usr/bin/bash
# GARNT TOOL REJOIN installer (safe to re-run). Replace <USER>/<REPO> with your GitHub repo.
set -e
RAW="https://raw.githubusercontent.com/<USER>/<REPO>/main"
export DEBIAN_FRONTEND=noninteractive
# Keep existing config files on conffile prompts so apt never waits for input.
APT="apt-get -y -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold"

echo "[1/5] อัปเดตและติดตั้งแพ็กเกจ..."
# </dev/null: stdin is the piped script under `curl | bash`, so keep tools from reading it
dpkg --configure -a --force-confold </dev/null || true   # self-heal an interrupted earlier run
$APT update </dev/null
$APT upgrade </dev/null
$APT install python tsu termux-api curl </dev/null

echo "[2/5] ตรวจสอบสิทธิ์ที่เก็บข้อมูล..."
if ! ls /sdcard/Download >/dev/null 2>&1; then
  termux-setup-storage </dev/null || true
  for _ in $(seq 1 30); do ls /sdcard/Download >/dev/null 2>&1 && break; sleep 2; done
fi
ls /sdcard/Download >/dev/null 2>&1 || { echo "ไม่ได้รับอนุญาตเข้าถึงที่เก็บข้อมูล — กด 'อนุญาต' แล้วรัน setup.sh ใหม่"; exit 1; }

echo "[3/5] สร้างโฟลเดอร์..."
# Plain Termux often cannot see /storage/emulated/0 but can use /sdcard; both are the same folder under su.
DIR=""
for d in /storage/emulated/0/Download/RejoinTool /sdcard/Download/RejoinTool; do
  if mkdir -p "$d/logs" "$d/screenshots" 2>/dev/null && touch "$d/.w" 2>/dev/null; then
    rm -f "$d/.w"; DIR="$d"; break
  fi
done
[ -n "$DIR" ] || { echo "สร้างโฟลเดอร์ใน Download ไม่ได้ — รัน termux-setup-storage กด 'อนุญาต' แล้วรัน setup.sh ใหม่"; exit 1; }
echo "ใช้โฟลเดอร์: $DIR"

echo "[4/5] ดาวน์โหลดไฟล์และติดตั้งไลบรารี..."
for f in rejoin.py requirements.txt; do
  curl -fsSL "$RAW/$f" -o "$DIR/$f" || { echo "ดาวน์โหลด $f ไม่สำเร็จ — ตรวจสอบอินเทอร์เน็ตหรือ URL ของ GitHub"; exit 1; }
done
python -m pip install -r "$DIR/requirements.txt" </dev/null \
  || python -m pip install --break-system-packages -r "$DIR/requirements.txt" </dev/null

echo "[5/5] สร้างคำสั่งลัด rejoin..."
cat > "$PREFIX/bin/rejoin" <<'EOF'
#!/data/data/com.termux/files/usr/bin/bash
exec su -c "export PATH=$PATH:/data/data/com.termux/files/usr/bin && export TERM=xterm-256color && cd /storage/emulated/0/Download/RejoinTool && python ./rejoin.py"
EOF
chmod +x "$PREFIX/bin/rejoin"

echo ""
echo "ติดตั้งเสร็จแล้ว! รันด้วย Root:"
echo '  su -c "export PATH=$PATH:/data/data/com.termux/files/usr/bin && export TERM=xterm-256color && cd /storage/emulated/0/Download/RejoinTool && python ./rejoin.py"'
echo "หรือพิมพ์คำสั่งลัด:  rejoin"
echo "ถ้าไม่มี Root:  cd $DIR && python rejoin.py"
