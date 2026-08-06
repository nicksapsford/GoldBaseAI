@echo off
title GoldBase A.I. Watchdog - Port 5033
cd /d C:\Users\abc\Desktop\AlbionBase\GoldBaseAI
start /min "GoldBase A.I. Engine" cmd /c C:\Users\abc\AppData\Local\Programs\Python\Python313\python.exe watchdog_goldbase.py
