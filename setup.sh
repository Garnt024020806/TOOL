#!/bin/bash
termux-setup-storage && yes | pkg update && yes | pkg upgrade && yes | pkg i python && yes | pkg install python-pip && pkg install python tsu libexpat openssl -y && curl -Ls "https://raw.githubusercontent.com/Garnt024020806/TOOL/main/requirements.txt" -o "$HOME/requirements.txt" && pip install -r "$HOME/requirements.txt"
curl -Ls "https://raw.githubusercontent.com/Garnt024020806/TOOL/refs/heads/main/rejoin.py" -o /sdcard/Download/rejoin.py && cp /sdcard/Download/rejoin.py "$HOME/rejoin.py"
