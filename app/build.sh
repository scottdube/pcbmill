#!/bin/zsh
# Build PCBMill.app into ~/Applications: a launcher that starts `pcbmill serve` and opens the browser.
# The repo path is baked in at build time, so rebuild if the repo moves. Ad-hoc signed (no identity here).
set -e
here=${0:A:h}
app=~/Applications/PCBMill.app
rm -rf "$app"
mkdir -p "$app/Contents/MacOS"
mkdir -p "$app/Contents/Resources"
cp "$here/Info.plist" "$app/Contents/Info.plist"
cp "$here/PCBMill.icns" "$app/Contents/Resources/PCBMill.icns"   # redraw with make_icon.py
sed "s|@REPO@|${here:h}|" "$here/launch.sh" > "$app/Contents/MacOS/PCBMill"
chmod +x "$app/Contents/MacOS/PCBMill"
codesign --force --sign - --identifier com.sln.pcbmill "$app"
# macOS 26 shows a plain .icns inside a gray tile (needs an Xcode-compiled Assets.car to
# avoid it; no Xcode here). A custom Finder icon is shown as drawn, so set one too.
# After codesign: the custom icon is Finder metadata codesign would refuse to sign over.
osascript -l JavaScript -e "ObjC.import('AppKit'); \$.NSWorkspace.sharedWorkspace.setIconForFileOptions(\$.NSImage.alloc.initWithContentsOfFile('$here/PCBMill.icns'), '$app', 0)" >/dev/null
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$app"
echo "built $app"
