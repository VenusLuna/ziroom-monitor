<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.ziroom.monitor</string>

    <key>ProgramArguments</key>
    <array>
        <string>__PROJECT_DIR__/.venv/bin/python</string>
        <string>__PROJECT_DIR__/monitor.py</string>
        <string>once</string>
        <string>--near</string>
        <string>--max-price</string>
        <string>3700</string>
        <string>--db</string>
        <string>__PROJECT_DIR__/near.db</string>
        <string>--notify-kinds</string>
        <string>new,gone,price,status</string>
        <string>--misses</string>
        <string>2</string>
        <string>--sheet</string>
        <string>--plans</string>
        <string>--images</string>
        <string>--image-limit</string>
        <string>60</string>
        <string>--workers</string>
        <string>16</string>
    </array>

    <key>WorkingDirectory</key>
    <string>__PROJECT_DIR__</string>

    <!-- 每小时扫一轮 -->
    <key>StartInterval</key>
    <integer>3600</integer>
    <key>RunAtLoad</key>
    <true/>

    <key>StandardOutPath</key>
    <string>__PROJECT_DIR__/logs/monitor.log</string>
    <key>StandardErrorPath</key>
    <string>__PROJECT_DIR__/logs/monitor.err</string>
</dict>
</plist>
