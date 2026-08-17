@echo off
REM Runs one check and appends to run.log. Point Task Scheduler at this file.
cd /d "%~dp0"
python watch.py --once >> run.log 2>&1
