#!/bin/bash
# Docker 沙箱安装中文输入法（fcitx5 + 拼音）
# 需要以 root 用户执行

# 检查是否以 root 权限运行，如果没有则自动使用 sudo
if [ "$(id -u)" -ne 0 ]; then
    if [ -f "$0" ]; then
        exec sudo bash "$0" "$@"
    else
        exec sudo bash "/home/x/install-fcitx5.sh" "$@"
    fi
fi

set -e

# 指定运行用户
TARGET_USER="x"
USER_HOME=$(getent passwd "$TARGET_USER" | cut -d: -f6)

FCITX5_STATUS=$(supervisorctl status fcitx5 2>/dev/null || echo "")
if echo "$FCITX5_STATUS" | grep -q "RUNNING"; then
    echo "fcitx5 已在运行中，跳过安装步骤"
    supervisorctl status fcitx5
    exit 0
fi
echo "fcitx5 未在运行，继续安装..."

# 检查是否需要安装包
echo "=== 检查是否需要安装包 ==="
NEED_INSTALL=false
PACKAGES=(
    "fcitx5"
    "fcitx5-chinese-addons"
    "fcitx5-frontend-gtk3"
    "fcitx5-frontend-qt5"
    "dbus-x11"
    "im-config"
    "locales"
)

for pkg in "${PACKAGES[@]}"; do
    if ! dpkg -l "$pkg" 2>/dev/null | grep -q "^ii"; then
        echo "包 $pkg 未安装，需要安装"
        NEED_INSTALL=true
        break
    fi
done


echo "=== 步骤 1：安装 fcitx5 及依赖 ==="
if [ "$NEED_INSTALL" = true ]; then
    apt-get update
    apt-get install -y \
        fcitx5 \
        fcitx5-chinese-addons \
        fcitx5-frontend-gtk3 \
        fcitx5-frontend-qt5 \
        dbus-x11 \
        im-config \
        locales
else
    echo "包已安装，跳过 apt-get"
fi

echo "=== 步骤 2：生成中文 locale ==="
if locale -a 2>/dev/null | grep -q "^zh_CN"; then
    echo "中文 locale 已存在，跳过"
else
    sed -i 's/# zh_CN.UTF-8 UTF-8/zh_CN.UTF-8 UTF-8/' /etc/locale.gen
    locale-gen zh_CN.UTF-8
fi

echo "=== 步骤 3：配置 fcitx5 默认使用拼音 ==="

mkdir -p "$USER_HOME/.config/fcitx5"

rm -rf "$USER_HOME/.config/fcitx5/profile"
cat > "$USER_HOME/.config/fcitx5/profile" << 'EOF'
[Groups/0]
Name=Default
Default Layout=us
DefaultIM=pinyin

[Groups/0/Items/0]
Name=keyboard-us
Layout=

[Groups/0/Items/1]
Name=pinyin
Layout=

[GroupOrder]
0=Default
EOF

chown -R "$TARGET_USER:$TARGET_USER" "$USER_HOME/.config/fcitx5"

echo "=== 步骤 4：创建 dbus + fcitx5 的 supervisord 配置 ==="

# 创建 /opt/gem/supervisord.fcitx5.conf
cat > /opt/gem/supervisord.fcitx5.conf << 'EOF'
[program:dbus-session]
environment=HOME="/home/%(ENV_USER)s",USER="%(ENV_USER)s",DISPLAY="%(ENV_DISPLAY)s",XDG_RUNTIME_DIR="/tmp/runtime-%(ENV_USER)s"
command=/usr/bin/dbus-daemon --session --nofork --address=unix:path=/tmp/dbus-session-bus
autorestart=true
priority=810
user=%(ENV_USER)s
stdout_logfile=%(ENV_LOG_DIR)s/dbus-session.log
stdout_logfile_maxbytes=10MB
redirect_stderr=true

[program:fcitx5]
environment=HOME="/home/%(ENV_USER)s",USER="%(ENV_USER)s",DISPLAY="%(ENV_DISPLAY)s",DBUS_SESSION_BUS_ADDRESS="unix:path=/tmp/dbus-session-bus",XDG_RUNTIME_DIR="/tmp/runtime-%(ENV_USER)s",GTK_IM_MODULE="fcitx",QT_IM_MODULE="fcitx",XMODIFIERS="@im=fcitx"
command=/usr/bin/fcitx5 --replace
autorestart=true
priority=820
user=%(ENV_USER)s
stdout_logfile=%(ENV_LOG_DIR)s/fcitx5.log
stdout_logfile_maxbytes=10MB
redirect_stderr=true
EOF

# 创建 /opt/gem/supervisord/fcitx5.conf
mkdir -p /opt/gem/supervisord
cat > /opt/gem/supervisord/fcitx5.conf << 'EOF'
[program:dbus-session]
environment=HOME="/home/%(ENV_USER)s",USER="%(ENV_USER)s",DISPLAY="%(ENV_DISPLAY)s",XDG_RUNTIME_DIR="/tmp/runtime-%(ENV_USER)s"
command=/usr/bin/dbus-daemon --session --nofork --address=unix:path=/tmp/dbus-session-bus
autorestart=true
priority=810
user=%(ENV_USER)s
stdout_logfile=%(ENV_LOG_DIR)s/dbus-session.log
stdout_logfile_maxbytes=10MB
redirect_stderr=true

[program:fcitx5]
environment=HOME="/home/%(ENV_USER)s",USER="%(ENV_USER)s",DISPLAY="%(ENV_DISPLAY)s",DBUS_SESSION_BUS_ADDRESS="unix:path=/tmp/dbus-session-bus",XDG_RUNTIME_DIR="/tmp/runtime-%(ENV_USER)s",GTK_IM_MODULE="fcitx",QT_IM_MODULE="fcitx",XMODIFIERS="@im=fcitx"
command=/usr/bin/fcitx5 --replace
autorestart=true
priority=820
user=%(ENV_USER)s
stdout_logfile=%(ENV_LOG_DIR)s/fcitx5.log
stdout_logfile_maxbytes=10MB
redirect_stderr=true
EOF

echo "=== 步骤 5：修改浏览器 supervisord 配置 ==="

# 修改 /opt/gem/supervisord.browser.conf
cat > /opt/gem/supervisord.browser.conf << 'EOF'
[program:browser]
environment=HOME="/home/%(ENV_USER)s",USER="%(ENV_USER)s",DISPLAY="%(ENV_DISPLAY)s",GOOGLE_API_KEY="",GTK_IM_MODULE="fcitx",QT_IM_MODULE="fcitx",XMODIFIERS="@im=fcitx",DBUS_SESSION_BUS_ADDRESS="unix:path=/tmp/dbus-session-bus"
command=/opt/gem/start-browser.sh
stopsignal=INT
autorestart=true
priority=850
user=%(ENV_USER)s
stdout_logfile=%(ENV_LOG_DIR)s/browser.log
stdout_logfile_maxbytes=10MB
stdout_logfile_backups=10
redirect_stderr=true
EOF

# 修改 /opt/gem/supervisord/browser.conf
cat > /opt/gem/supervisord/browser.conf << 'EOF'
[program:browser]
environment=HOME="/home/%(ENV_USER)s",USER="%(ENV_USER)s",DISPLAY="%(ENV_DISPLAY)s",GOOGLE_API_KEY="",GTK_IM_MODULE="fcitx",QT_IM_MODULE="fcitx",XMODIFIERS="@im=fcitx",DBUS_SESSION_BUS_ADDRESS="unix:path=/tmp/dbus-session-bus"
command=/opt/gem/start-browser.sh
stopsignal=INT
autorestart=true
priority=850
user=%(ENV_USER)s
stdout_logfile=%(ENV_LOG_DIR)s/browser.log
stdout_logfile_maxbytes=10MB
stdout_logfile_backups=10
redirect_stderr=true
EOF

echo "=== 步骤 6：更新配置并重启服务 ==="
supervisorctl reread
supervisorctl update dbus-session fcitx5 browser
supervisorctl restart fcitx5
supervisorctl restart browser

echo "=== 验证服务状态 ==="
supervisorctl status

echo "=== 安装完成 ==="
