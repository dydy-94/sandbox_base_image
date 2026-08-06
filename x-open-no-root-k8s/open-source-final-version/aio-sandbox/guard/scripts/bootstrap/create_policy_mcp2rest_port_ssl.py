#!/usr/bin/env python3
import subprocess
import os
import re
import json
import time

# 默认的 BROWSER_EXTRA_ARGS 值
default_browser_extra_args = (
    "--user-agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36' "
    "--lang=en-US "
    "--time-zone-for-testing=Asia/Shanghai "
    "--homepage=about:blank "
    "--no-sandbox "
    "--disable-setuid-sandbox "
    "--no-zygote "
    "--ignore-certificate-errors "
    "--disable-dev-shm-usage "
    "--disable-features=SameSiteDefaultCookieBehavior,SameSiteByDefaultCookies,SameSiteNoneInsecure"
)

# 检查环境变量是否已存在
existing_env_value = os.environ.get("BROWSER_EXTRA_ARGS", "")

if existing_env_value:
    # 环境变量已存在，检查内容是否一样
    if existing_env_value == default_browser_extra_args:
        # 内容一样，保持不变
        browser_extra_args = existing_env_value
        print("BROWSER_EXTRA_ARGS 环境变量已存在且内容相同，保持不变")
    else:
        # 内容不一样，替换为默认值
        browser_extra_args = default_browser_extra_args
        print("BROWSER_EXTRA_ARGS 环境变量已存在但内容不同，已替换为默认值")
else:
    # 环境变量不存在，设置默认值
    browser_extra_args = default_browser_extra_args
    print("BROWSER_EXTRA_ARGS 环境变量不存在，已设置为默认值")

# 导出环境变量（在当前 shell 中生效）
export_cmd = f'export BROWSER_EXTRA_ARGS="{browser_extra_args}"'
export_result = subprocess.run(export_cmd, shell=True)
if export_result.returncode == 0:
    print("设置 BROWSER_EXTRA_ARGS执行成功")
else:
    print(f"设置 BROWSER_EXTRA_ARGS执行失败，返回码: {export_result.returncode}")

os.environ["BROWSER_EXTRA_ARGS"] = browser_extra_args

# 写入 ~/.bashrc 使其永久生效（仅当需要更新时）
bashrc_file = os.path.expanduser("~/.bashrc")
env_line = f'export BROWSER_EXTRA_ARGS="{browser_extra_args}"'

# 读取现有内容
existing_content = ""
if os.path.exists(bashrc_file):
    with open(bashrc_file, 'r') as f:
        existing_content = f.read()

# 检查 .bashrc 中的值是否与当前值相同
match = re.search(r'export\s+BROWSER_EXTRA_ARGS="([^"]*)"', existing_content)
bashrc_value = match.group(1) if match else None

if bashrc_value == browser_extra_args:
    # .bashrc 中已有相同的值，跳过写入
    print(f"BROWSER_EXTRA_ARGS 已在 {bashrc_file} 中且内容相同，跳过写入")
elif existing_env_value and existing_env_value == default_browser_extra_args:
    # 环境变量已存在且内容相同，保持不变，跳过写入
    print(f"环境变量已存在且内容相同，保持不变，跳过写入 {bashrc_file}")
else:
    # 需要写入或更新 .bashrc
    if "BROWSER_EXTRA_ARGS" not in existing_content:
        # 不存在，追加新行
        with open(bashrc_file, 'a') as f:
            f.write(f'\n{env_line}\n')
        print(f"已写入 {bashrc_file} 使环境变量永久生效")
    else:
        # 存在但值不同，替换整行
        updated_content = re.sub(
            r'export\s+BROWSER_EXTRA_ARGS="[^"]*"',
            env_line,
            existing_content
        )
        with open(bashrc_file, 'w') as f:
            f.write(updated_content)
        print(f"已更新 {bashrc_file} 中的 BROWSER_EXTRA_ARGS")

# 使用 vi 直接写入文件（需要 sudo）
download_dir = "/home/x/projects/Downloads"
new_content = {
    "URLBlocklist": [
        "file://*",
        "localhost:8080",
        "localhost:8200",
        "localhost:3001",
        "localhost:22222",
        "localhost:28888",
        "127.0.0.1:8080",
        "127.0.0.1:8200",
        "127.0.0.1:3001",
        "127.0.0.1:22222",
        "127.0.0.1:28888"
    ],
    "DownloadDirectory": download_dir,
    "PromptForDownloadLocation": False,
    "CookiesAllowedForUrls":["[*.]cmbchina.com","[*.]cmbchina.cn"]
}
new_content_str = json.dumps(new_content, indent=4)

file_path = "/etc/chromium/policies/managed/blocked_urls.json"
os.makedirs(os.path.dirname(file_path), exist_ok=True)

os.makedirs(download_dir, exist_ok=True)
os.chmod(download_dir, 0o777)

# 检查文件是否已存在
if os.path.exists(file_path):
    # 读取现有内容并比较
    with open(file_path, 'r') as f:
        existing_content = f.read().strip()
    # 尝试解析 JSON 进行比较
    try:
        existing_json = json.loads(existing_content)
        if existing_json == new_content:
            print(f"文件内容相同，跳过写入: {file_path}")
        else:
            # 内容不同，替换
            cmd = f'sudo tee {file_path} > /dev/null << EOF\n{new_content_str}\nEOF'
            subprocess.run(cmd, shell=True)
            print(f"文件内容不同，已更新: {file_path}")
    except json.JSONDecodeError:
        # 现有文件不是有效 JSON，替换
        cmd = f'sudo tee {file_path} > /dev/null << EOF\n{new_content_str}\nEOF'
        subprocess.run(cmd, shell=True)
        print(f"文件不是有效 JSON，已更新: {file_path}")
else:
    # 文件不存在，直接写入
    cmd = f'sudo tee {file_path} > /dev/null << EOF\n{new_content_str}\nEOF'
    subprocess.run(cmd, shell=True)
    print(f"已写入: {file_path}")


# 写入 /opt/gem/start-browser.sh
start_browser_script = '''#!/bin/bash

set -e

log() {
  echo "$(date '+%Y-%m-%d %H:%M:%S,%3N') INFO $@"
}

COOKIE_SCRIPT="/home/x/browser-cookie/browser-start-with-cookie.js"

readarray -t cmd_args < <(xargs -n1 printf '%s\n' <<<"$BROWSER_COMMANDLINE_ARGS")
readarray -t extra_args < <(xargs -n1 printf '%s\n' <<<"$BROWSER_EXTRA_ARGS")

# Only run cookie script logic if the script exists
if [ -f "$COOKIE_SCRIPT" ]; then
  # Wait for environment variables to be available
  max_wait=30
  wait_interval=1
  elapsed=0

  while [ $elapsed -lt $max_wait ]; do
    # Try to get variables from supervisor process or bashrc
    X_SANDBOX_USER_ID=$(sudo cat /proc/$(pgrep supervisord)/environ 2>/dev/null | tr '\\0' '\\n' | grep "^X_SANDBOX_USER_ID=" | cut -d= -f2-)
    X_SANDBOX_USER_NAME=$(sudo cat /proc/$(pgrep supervisord)/environ 2>/dev/null | tr '\\0' '\\n' | grep "^X_SANDBOX_USER_NAME=" | cut -d= -f2-)
    DAYTONA_SANDBOX_ID=$(sudo cat /proc/$(pgrep supervisord)/environ 2>/dev/null | tr '\\0' '\\n' | grep "^DAYTONA_SANDBOX_ID=" | cut -d= -f2-)

    if [ -z "$X_SANDBOX_USER_ID" ]; then
      X_SANDBOX_USER_ID=$(grep "^export X_SANDBOX_USER_ID=" /home/x/.bashrc 2>/dev/null | cut -d"'" -f2)
    fi
    if [ -z "$X_SANDBOX_USER_NAME" ]; then
      X_SANDBOX_USER_NAME=$(grep "^export X_SANDBOX_USER_NAME=" /home/x/.bashrc 2>/dev/null | cut -d"'" -f2)
    fi
    if [ -z "$DAYTONA_SANDBOX_ID" ]; then
      DAYTONA_SANDBOX_ID=$(sudo cat /proc/$(pgrep supervisord)/environ 2>/dev/null | tr '\\0' '\\n' | grep "^DAYTONA_SANDBOX_ID=" | cut -d= -f2-)
    fi

    if [ -n "$X_SANDBOX_USER_ID" ] && [ -n "$X_SANDBOX_USER_NAME" ] && [ -n "$DAYTONA_SANDBOX_ID" ]; then
      break
    fi

    log "Waiting for environment variables... ($elapsed/$max_wait)s"
    sleep $wait_interval
    elapsed=$((elapsed + wait_interval))
  done

  if [ -z "$X_SANDBOX_USER_ID" ] || [ -z "$X_SANDBOX_USER_NAME" ] || [ -z "$DAYTONA_SANDBOX_ID" ]; then
    log "Warning: Environment variables not found, skipping cookie script"
  else
    export X_SANDBOX_USER_ID X_SANDBOX_USER_NAME DAYTONA_SANDBOX_ID
    log "Starting browser with env: X_SANDBOX_USER_ID=$X_SANDBOX_USER_ID, X_SANDBOX_USER_NAME=$X_SANDBOX_USER_NAME, DAYTONA_SANDBOX_ID=$DAYTONA_SANDBOX_ID"

    # Wait 3 seconds in background then run node script
    (
      sleep 3
      log "Running browser-start-with-cookie.js..."
      node "$COOKIE_SCRIPT"
      SCRIPT_EXIT=$?
      if [ $SCRIPT_EXIT -eq 0 ]; then
        log "Cookie script executed successfully"
      else
        log "Cookie script failed with exit code $SCRIPT_EXIT"
      fi
    ) &
  fi
fi

exec "${BROWSER_EXECUTABLE_PATH}" \
  --user-data-dir="/home/${USER}/.config/browser" \
  --ignore-certificate-errors \
  "${cmd_args[@]}" \
  "${extra_args[@]}"
'''

start_browser_path = "/opt/gem/start-browser.sh"

# 确保目录存在
os.makedirs(os.path.dirname(start_browser_path), exist_ok=True)

# 检查文件是否已存在
if os.path.exists(start_browser_path):
    # 读取现有内容并比较
    with open(start_browser_path, 'r') as f:
        existing_script_content = f.read()
    if existing_script_content == start_browser_script:
        print(f"文件内容相同，跳过写入: {start_browser_path}")
    else:
        # 内容不同，写入
        with open(start_browser_path, 'w') as f:
            f.write(start_browser_script)
        # 设置执行权限
        os.chmod(start_browser_path, 0o755)
        print(f"文件内容不同，已更新: {start_browser_path}")

        # 写入后执行 supervisorctl restart browser
        print("正在执行 supervisorctl restart browser...")
        restart_result = subprocess.run("supervisorctl restart browser", shell=True, capture_output=True, text=True)
        if restart_result.returncode == 0:
            print(f"supervisorctl restart browser 执行成功: {restart_result.stdout}")
        else:
            print(f"supervisorctl restart browser 执行失败，返回码: {restart_result.returncode}, 错误: {restart_result.stderr}")
else:
    # 文件不存在，直接写入
    with open(start_browser_path, 'w') as f:
        f.write(start_browser_script)
    # 设置执行权限
    os.chmod(start_browser_path, 0o755)
    print(f"已写入: {start_browser_path}")

    # 写入后执行 supervisorctl restart browser
    print("正在执行 supervisorctl restart browser...")
    restart_result = subprocess.run("supervisorctl restart browser", shell=True, capture_output=True, text=True)
    if restart_result.returncode == 0:
        print(f"supervisorctl restart browser 执行成功: {restart_result.stdout}")
    else:
        print(f"supervisorctl restart browser 执行失败，返回码: {restart_result.returncode}, 错误: {restart_result.stderr}")


# 等待浏览器和 tigervnc 都启动
def wait_for_browser_running(max_wait_seconds=60, check_interval=2):
    """等待 browser 和 tigervnc 都变成 RUNNING 状态"""
    import subprocess as sp
    wait_start = time.time()
    while time.time() - wait_start < max_wait_seconds:
        result = sp.run(["supervisorctl", "status"], capture_output=True, text=True)
        if result.returncode in (0, 3):  # 0=成功, 3=部分进程非 RUNNING
            browser_running = False
            tigervnc_running = False
            for line in result.stdout.split('\n'):
                if line.strip().startswith('browser '):
                    if 'RUNNING' in line:
                        browser_running = True
                if line.strip().startswith('tigervnc '):
                    if 'RUNNING' in line:
                        tigervnc_running = True

            if browser_running and tigervnc_running:
                print(f"browser 和 tigervnc 都已启动")
                return True

            elapsed = int(time.time() - wait_start)
            print(f"等待浏览器启动... browser={browser_running}, tigervnc={tigervnc_running} (已等待 {elapsed}s)")
        else:
            print(f"获取 supervisorctl 状态失败，返回码: {result.returncode}")

        time.sleep(check_interval)

    print(f"等待浏览器启动超时 ({max_wait_seconds}s)")
    return False


# 设置浏览器分辨率
print("正在检查浏览器状态...")
if not wait_for_browser_running():
    print("警告: 浏览器未完全启动，仍尝试设置分辨率...")

print("正在设置浏览器分辨率为 1920x1080...")
max_retries = 5
retry_delay = 2  # seconds

for attempt in range(1, max_retries + 1):
    curl_cmd = "curl -s -X 'POST' 'http://localhost:8080/v1/browser/config' -H 'accept: application/json' -H 'Content-Type: application/json' -d '{\"resolution\": {\"width\": 1920, \"height\": 1080}}'"
    curl_result = subprocess.run(curl_cmd, shell=True, capture_output=True, text=True)

    if curl_result.returncode == 0:
        # 解析 JSON 响应，检查 success 字段
        try:
            response_json = json.loads(curl_result.stdout)
            if response_json.get("success") == True:
                print(f"浏览器分辨率设置成功: {response_json.get('message', '')}")
                break
            else:
                error_msg = response_json.get("message", "未知错误")
                if attempt < max_retries:
                    print(f"第 {attempt} 次尝试失败: {error_msg}，{retry_delay} 秒后重试...")
                    time.sleep(retry_delay)
                else:
                    print(f"浏览器分辨率设置失败，已重试 {max_retries} 次: {error_msg}")
        except json.JSONDecodeError as e:
            if attempt < max_retries:
                print(f"第 {attempt} 次尝试失败，JSON 解析错误: {e}，{retry_delay} 秒后重试...")
                time.sleep(retry_delay)
            else:
                print(f"浏览器分辨率设置失败，已重试 {max_retries} 次，JSON 解析错误: {e}")
    else:
        if attempt < max_retries:
            print(f"第 {attempt} 次尝试失败，返回码: {curl_result.returncode}，{retry_delay} 秒后重试...")
            time.sleep(retry_delay)
        else:
            print(f"浏览器分辨率设置失败，已重试 {max_retries} 次，最终返回码: {curl_result.returncode}")
