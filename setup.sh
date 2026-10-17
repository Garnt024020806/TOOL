#!/data/data/com.termux/files/usr/bin/bash
# GARNT TOOL REJOIN - setup for Termux (safe to run via: curl -Ls <url> | bash)
RAW="https://raw.githubusercontent.com/Garnt024020806/TOOL/main"
DL="/sdcard/Download"
TMPD="${TMPDIR:-$PREFIX/tmp}"
mkdir -p "$TMPD"

say() { echo -e "\n\033[1;36m[GARNT]\033[0m $*"; }
die() { echo -e "\n\033[1;31m[ผิดพลาด]\033[0m $*"; exit 1; }
# every command reads from /dev/null so it cannot swallow the piped script
run() { "$@" </dev/null; }

say "1/6 อัปเดตแพ็กเกจ (pkg update / upgrade)"
run pkg update -y || die "pkg update ไม่สำเร็จ ลองรัน termux-change-repo เพื่อเปลี่ยน mirror แล้วรันใหม่"
run pkg upgrade -y -o Dpkg::Options::=--force-confnew || die "pkg upgrade ไม่สำเร็จ"

say "2/6 ติดตั้ง python"
run pkg install -y python curl || die "ติดตั้ง python ไม่สำเร็จ"

say "3/6 ติดตั้ง tsu (สิทธิ์รูท)"
run pkg install -y tsu || die "ติดตั้ง tsu ไม่สำเร็จ"

say "4/6 ติดตั้งไลบรารี Python (pip install -r requirements.txt)"
run curl -fsSL "$RAW/requirements.txt" -o "$TMPD/requirements.txt" || die "ดาวน์โหลด requirements.txt ไม่สำเร็จ (เช็กอินเทอร์เน็ต/ลิงก์ GitHub)"
run pip install -r "$TMPD/requirements.txt" || die "pip install ไม่สำเร็จ"

say "5/6 ขอสิทธิ์เข้าถึงที่เก็บข้อมูล (กด 'อนุญาต' ในหน้าต่างที่เด้งขึ้นมา)"
run termux-setup-storage
for i in $(seq 1 30); do
  [ -w "$DL" ] && break
  sleep 2
done
[ -w "$DL" ] || die "ยังเขียน $DL ไม่ได้ ไปที่ ตั้งค่า > แอป > Termux > สิทธิ์ > ที่เก็บข้อมูล แล้วอนุญาต จากนั้นรันสคริปต์ใหม่"

say "6/6 ดาวน์โหลดโปรแกรม"
mkdir -p "$DL/RejoinTool/logs" "$DL/RejoinTool/screenshots"
run curl -fsSL "$RAW/rejoin.py" -o "$DL/rejoin.py" || die "ดาวน์โหลด rejoin.py ไม่สำเร็จ"
cp "$DL/rejoin.py" "$HOME/rejoin.py"

say "เสร็จแล้ว! รันด้วยรูท:"
echo 'su -c "export PATH=$PATH:/data/data/com.termux/files/usr/bin && export TERM=xterm-256color && cd /storage/emulated/0/Download && python ./rejoin.py"'
echo
echo "หรือรันแบบไม่รูท:  cd ~ && python rejoin.py"
