@echo off
title GoldBase A.I. Dashboard - Port 5033
cd /d C:\Users\abc\Desktop\AlbionBase\GoldBaseAI
start /min "GoldBase A.I. Dashboard" cmd /c C:\Users\abc\AppData\Local\Programs\Python\Python313\python.exe dashboard_goldbase.py
timeout /t 5 /nobreak >nul
start http://localhost:5033
