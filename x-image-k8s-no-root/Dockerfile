FROM enterprise-public-cn-beijing.cr.volces.com/vefaas-public/all-in-one-sandbox:latest

#######################################
# 1. 基础环境变量
#    DISABLE_JUPYTER/DISABLE_CODE_SERVER: 关闭不需要的组件
#    ENV_USER=x: 告诉 AIO 编排层以 x 用户运行组件
#######################################
ENV DEBIAN_FRONTEND=noninteractive \
    USER=x \
    USER_UID=1000 \
    USER_GID=1000 \
    HOME=/home/x \
    NPM_CONFIG_PREFIX=/home/x/.npm-global \
    NPM_CONFIG_CACHE=/home/x/.npm \
    PATH=/home/x/.npm-global/bin:/home/x/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    ANTHROPIC_BASE_URL=http://127.0.0.1:3456\
    ANTHROPIC_MODEL=CMB_Model \
    ANTHROPIC_SMALL_FAST_MODEL=CMB_Model \
    CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=70 \
    ANTHROPIC_AUTH_TOKEN=your-secret-key \
    PYTHON_VERSION=3.13 \
    GO_VERSION=1.23.0 \
    BROWSER_EXTRA_ARGS="\
      --no-sandbox \
      --disable-setuid-sandbox \
      --no-zygote \
      --disable-dev-shm-usage \
      --lang=en-US \
      --time-zone-for-testing=Asia/Singapore \
      --window-position=0,0 \
      --window-size=1280,1024 \
      --homepage=about:blank" \
    DISABLE_JUPYTER=true \
    DISABLE_CODE_SERVER=true \
    ENV_USER=x

#######################################
# 2. 安装系统依赖
#######################################
RUN apt-get update && apt-get install -y \
    curl \
    wget \
    git \
    vim \
    fcitx5 \
    fcitx5-chinese-addons \
    fcitx5-frontend-gtk3 \
    fcitx5-frontend-qt5 \
    dbus-x11 \
    im-config \
    locales \
 && add-apt-repository -y ppa:deadsnakes/ppa \
 && apt-get update \
 && apt-get install -y python${PYTHON_VERSION} python${PYTHON_VERSION}-venv python${PYTHON_VERSION}-dev \
 && update-alternatives --install /usr/bin/python3 python3 /usr/bin/python${PYTHON_VERSION} 1 \
 && curl -sS https://bootstrap.pypa.io/get-pip.py | python${PYTHON_VERSION} \
 && pip install playwright supervisor fastmcp cffi cryptography\
 && curl -LO "https://github.com/oras-project/oras/releases/download/v1.3.1/oras_1.3.1_linux_amd64.tar.gz" \
 && mkdir -p oras-install \
 && tar -zxf oras_1.3.1_linux_amd64.tar.gz -C oras-install \
 && mv oras-install/oras /usr/local/bin/ \
 && rm -rf oras_1.3.1_linux_amd64.tar.gz oras-install \
 && rm -rf /var/lib/apt/lists/*

#######################################
# 3. 安装 Node.js 22（系统级）
####################################### 
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
 && apt-get install -y nodejs \
 && npm config set registry https://registry.npmmirror.com

#######################################
# 4. 创建用户 x + 预创建 npm 相关目录
#######################################
RUN useradd -m -s /bin/bash x \
 && mkdir -p \
      /home/x/.npm \
      /home/x/.npm-global \
      /home/x/.config \
      /home/x/.claude \
      /home/x/.claude-code-router \
      /home/x/.cache  \
      /home/x/.mcp2rest \
 && chown -R x:x /home/x
#######################################
# 4b. 防御性预创建浏览器配置目录
#     镜像层就把 /home/x/.config/browser/ 准备好，chown 给 x。
#     避免挂载上来的持久卷里残留 root 权限文件，
#     导致 agent-browser / chrome / mcp2rest 启动时写不进
#     ~/.config/browser/* 而报错。
#######################################
RUN mkdir -p /home/x/.config/browser \
 && chown -R x:x /home/x/.config/browser \
 && chmod 755 /home/x/.config/browser

#######################################
# 5. 构建期为 x 安装 npm 全局依赖
#######################################
RUN npm install -g \
      tsx@^4.19.2 \
      typescript@^5.0.0 \
      mcp2rest \
      @anthropic-ai/claude-code@2.1.63 \
      @anthropic-ai/claude-agent-sdk@0.2.63 \
      @musistudio/claude-code-router \
      pm2 \
 && chown -R x:x /home/x 

#######################################
# 6. 配置私有源
#######################################
RUN npm config set registry http://central.jaf.cmbchina.cn/artifactory/api/npm/group-npm/ \
 && pip config set global.index-url http://central.jaf.cmbchina.cn/artifactory/api/pypi/group-pypi/simple \
 && pip config set global.trusted-host central.jaf.cmbchina.cn \
 && pip config set central.jaf.cmbchina.cn install.trusted-host
 
#######################################
# 7. 配置 apt 源
#######################################
RUN echo "deb http://mirrors-bak.sk.aipower3.cmbchina.cn/repository/apt/ jammy main restricted universe multiverse" > /etc/apt/sources.list && \
    echo "deb http://mirrors-bak.sk.aipower3.cmbchina.cn/repository/apt/ jammy-updates main restricted universe multiverse" >> /etc/apt/sources.list && \
    echo "deb http://mirrors-bak.sk.aipower3.cmbchina.cn/repository/apt/ jammy-backports main restricted universe multiverse" >> /etc/apt/sources.list && \
    echo "deb http://mirrors-bak.sk.aipower3.cmbchina.cn/repository/apt/ jammy-security main restricted universe multiverse" >> /etc/apt/sources.list

#######################################
# 8. chrome mcp配置下发
#######################################
RUN printf 'servers:\n  chrome-devtools:\n    name: chrome-devtools\n    package: chrome-devtools-mcp@latest\n    transport: stdio\n    args: ["-y", "chrome-devtools-mcp@latest", "--browser-url=http://127.0.0.1:9222"]\ngateway:\n  port: 28888\n  host: 0.0.0.0\n  timeout: 30000\n  logLevel: info\n' > "/home/x/.mcp2rest/config.yaml"

#######################################
# 9. 复制 Daytona Daemon 二进制
#######################################
RUN mkdir -p /usr/local/bin
COPY bin/daytona /usr/local/bin/daytona
RUN chmod +x /usr/local/bin/daytona && \
    chown x:x /usr/local/bin/daytona

#######################################
# 10. Volume 挂载点准备
#######################################
RUN mkdir -p /home/x/.daemon/state && \
    mkdir -p /home/x/.daemon/logs && \
    mkdir -p /tmp/daytona-logs && \
    mkdir -p /var/log/gem/daytona && \
    chown -R x:x /home/x/.daemon && \
    chown x:x /tmp/daytona-logs && \
    chown -R x:x /var/log/gem/daytona

#######################################
# 11. 复制 computer-use 插件 (预编译)
#######################################
RUN mkdir -p /usr/local/lib/daytona-computer-use
COPY dist/libs/computer-use-amd64 /usr/local/lib/daytona-computer-use/daytona-computer-use
RUN chmod 755 /usr/local/lib/daytona-computer-use/daytona-computer-use && \
    chown x:x /usr/local/lib/daytona-computer-use/daytona-computer-use

#######################################
# 12. 默认环境变量
#######################################
ENV DAYTONA_DAEMON_LOG_FILE_PATH=/tmp/daytona-daemon.log
ENV DAYTONA_ENTRYPOINT_LOG_FILE_PATH=/tmp/daytona-entrypoint.log
ENV DAYTONA_USER_HOME_AS_WORKDIR=true
ENV LOG_LEVEL=info

#######################################
# 13. 端口暴露
#######################################
EXPOSE 8080 2280 22222 22220

#######################################
# 14. 添加 daemon 启动到 supervisord（以 x 用户非 root 运行）
#######################################
RUN printf '\n[program:daytona]\ncommand=/usr/local/bin/daytona --interval 5\nautostart=true\nautorestart=true\nstdout_logfile=/var/log/gem/daytona/daytona.log\nstderr_logfile=/var/log/gem/daytona/daytona_err.log\nuser=x\nenvironment=DAYTONA_DAEMON_LOG_FILE_PATH="/tmp/daytona-daemon.log",DAYTONA_ENTRYPOINT_LOG_FILE_PATH="/tmp/daytona-entrypoint.log",DAYTONA_USER_HOME_AS_WORKDIR="true",LOG_LEVEL="info"\n' >> /opt/gem/supervisord.conf

#######################################
# 15. 保持 base image 的 ENTRYPOINT (supervisord)
#######################################
