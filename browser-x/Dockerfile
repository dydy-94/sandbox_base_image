FROM enterprise-public-cn-beijing.cr.volces.com/vefaas-public/all-in-one-sandbox:latest

#######################################
# 1. 基础环境变量（一次性定义清楚）
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
      --homepage=about:blank"

#######################################
# 2. 安装系统依赖
#######################################
RUN apt-get update && apt-get install -y \
    curl \
    wget \
    git \
    vim \
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
# 3.2 安装 Node.js 22（系统级）
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
# 5. 构建期为 x 安装 npm 全局依赖
#    ⚠️ root 执行，但 prefix + cache 都明确指向 x
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

RUN npm config set registry http://central.jaf.cmbchina.cn/artifactory/api/npm/group-npm/ \
 && pip config set global.index-url http://central.jaf.cmbchina.cn/artifactory/api/pypi/group-pypi/simple \
 && pip config set global.trusted-host central.jaf.cmbchina.cn \
 && pip config set central.jaf.cmbchina.cn install.trusted-host
 
RUN cat > /etc/apt/sources.list << 'APT_CONFIG'
deb http://mirrors-bak.sk.aipower3.cmbchina.cn/repository/apt/ jammy main restricted universe multiverse
deb http://mirrors-bak.sk.aipower3.cmbchina.cn/repository/apt/ jammy-updates main restricted universe multiverse
deb http://mirrors-bak.sk.aipower3.cmbchina.cn/repository/apt/ jammy-backports main restricted universe multiverse
deb http://mirrors-bak.sk.aipower3.cmbchina.cn/repository/apt/ jammy-security main restricted universe multiverse
APT_CONFIG


############
###########################
# chrome mcp配置下发
#######################################
RUN apt-get update 

RUN cat > "/home/x/.mcp2rest/config.yaml" << 'MCP2REST_CONFIG'
servers:
  chrome-devtools:
    name: chrome-devtools
    package: chrome-devtools-mcp@latest
    transport: stdio
    args: ["-y", "chrome-devtools-mcp@latest",
              "--browser-url=http://127.0.0.1:9222"]
gateway:
  port: 28888
  host: 0.0.0.0
  timeout: 30000
  logLevel: info
MCP2REST_CONFIG

#######################################
# 7. 端口 
#######################################
EXPOSE 8080
