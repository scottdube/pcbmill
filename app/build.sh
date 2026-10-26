#!/bin/zsh
# Build PCBMill.app into ~/Applications: a launcher that starts `pcbmill serve` and opens the browser.
# The repo path is baked in at build time, so rebuild if the repo moves. Ad-hoc signed (no identity here).
set -e
here=${0:A:h}
app=~/Applications/PCBMill.app
rm -rf "$app"
mkdir -p "$app/Contents/MacOS"
cp "$here/Info.plist" "$app/Contents/Info.plist"
sed "s|@REPO@|${here:h}|" "$here/launch.sh" > "$app/Contents/MacOS/PCBMill"
chmod +x "$app/Contents/MacOS/PCBMill"
codesign --force --sign - --identifier com.sln.pcbmill "$app"
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$app"
echo "built $app"
