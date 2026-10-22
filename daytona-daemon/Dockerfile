# =============================================================================
# daytona daemon 二进制「载荷镜像」
#
# 用途：把 /usr/local/bin/daytona 单独打包成一个极小镜像，在老镜像（daemon 版本
#       落后）的 Pod 里以 initContainer 方式把二进制拷进 emptyDir，再让主容器用
#       subPath 挂载覆盖老镜像自带的 /usr/local/bin/daytona，从而在不重打业务
#       镜像的前提下更新 daemon。
#
# 为什么 base 用 busybox 而不是 scratch：
#   scratch 镜像里没有任何工具，initContainer 无法执行 cp/chmod 完成拷贝。
#   busybox 仅 ~1MB，自带 sh/cp/chmod/sha256sum，满足拷贝与校验需求。
#
# 为什么这个 scheme 成立（关键前提，均已实测确认）：
#   * binary 为静态链接 Go ELF（ET_EXEC，无 PT_INTERP、无 GLIBC_2.x 符号），
#     因此不依赖老镜像的 glibc/工具链版本；
#   * daemon 由 supervisord 以绝对路径单文件启动
#     (/opt/gem/supervisord/daytona.conf: command=/usr/local/bin/daytona --interval 5)，
#     入口 init-once.sh → run.sh 不校验、不复制该二进制；
#   * 因此挂载替换单个文件即可生效，无需改动老镜像任何其它内容。
#
# 构建（build context 必须是本目录）：
#   docker build -f Dockerfile.daytona -t aio-daytona-bin:v1.0.0-cmb .
#
# 导出为离线 tar（带进内网）：
#   docker save aio-daytona-bin:v1.0.0-cmb -o aio-daytona-bin-v1.0.0-cmb.tar
# =============================================================================
FROM busybox:1.36

# daemon 主程序
COPY daytona /daytona

# 构建期自检 —— 注意：严禁直接执行 /daytona！
# daemon 在「无参数」时会启动自身服务并常驻，会把构建卡死；带参数时则被当作
# entrypoint 命令去 exec。故此处只做非侵入式结构校验 + 指纹输出。
RUN set -eux; \
    chmod 0755 /daytona; \
    test -x /daytona; \
    head -c 4 /daytona | grep -q ELF; \
    [ "$(wc -c < /daytona)" -gt 40000000 ]; \
    echo "--- daytona payload check ---"; \
    ls -l /daytona; \
    sha256sum /daytona

# 默认仅作为「载荷」存在。k8s initContainer 里用 command 覆盖即可，例如：
#   command: ["sh","-c","cp /daytona /out/daytona && chmod 0755 /out/daytona"]
CMD ["/bin/sh"]
