@echo off
rem Start pr-review-agent web console (persistent, survives IDE turn end).
rem Keep this file pure ASCII: non-ASCII in .cmd gets garbled by codepage.
cd /d C:\Users\pc\Desktop\pr-review-agent
set PYTHONPATH=src
C:\Users\pc\.workbuddy\binaries\python\envs\default\Scripts\python.exe -m uvicorn pagent.api:app --host 127.0.0.1 --port 8100
