#!/bin/zsh
# PCBMill.app entry point: start `pcbmill serve` if nothing answers on the port, then open the UI.
# The server is detached (nohup) so it outlives this script; quitting the app does not stop it.
# Stop it with: pkill -f 'pcbmill serve'
repo=@REPO@
port=8337
url=http://127.0.0.1:$port/
log=~/Library/Logs/pcbmill.log

up() { curl -s -o /dev/null --max-time 1 $url }

if ! up; then
  nohup $repo/.venv/bin/pcbmill serve --port $port --jobs-root ~/PCBMill >>$log 2>&1 &
  for i in {1..40}; do up && break; sleep 0.25; done
fi

if up; then
  open $url
else
  osascript -e "display alert \"PCBMill did not start\" message \"Nothing answered on port $port within 10 s. Log: $log\""
fi
