@echo off
rem Copy this file to mail_config.local.bat and fill real values.
rem mail_config.local.bat is loaded by start_workbench_8123.bat.

set "WORKBENCH_REPORT_MAIL_SMTP_HOST=smtp.example.com"
set "WORKBENCH_REPORT_MAIL_SMTP_PORT=465"
set "WORKBENCH_REPORT_MAIL_SMTP_SECURITY=ssl"
set "WORKBENCH_REPORT_MAIL_USERNAME=sender@example.com"
set "WORKBENCH_REPORT_MAIL_PASSWORD=change-me"
set "WORKBENCH_REPORT_MAIL_FROM=sender@example.com"
set "WORKBENCH_REPORT_MAIL_TO=leader@example.com;ops@example.com"
set "WORKBENCH_REPORT_MAIL_CC="
