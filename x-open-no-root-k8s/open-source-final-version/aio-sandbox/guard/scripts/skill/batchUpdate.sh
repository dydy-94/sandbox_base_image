#!/bin/bash

# 简化版下载脚本 - JSON输出
# 使用方法: ./test222.sh <URL> <packageId> <versionum> [URL2 packageId2 versionum2] ...

# ====================== 参数判断 ======================
if [[ $# -lt 3 || $(( $# % 3 )) -ne 0 ]]; then
    echo '{"status":"failed","packageIds":["invalid parameters"]}'
    exit 1
fi

# 全局失败列表
ALL_FAILED=()

# ====================== 单个下载任务函数 ======================
process_single_download() {
    local URL="$1"
    local PACKAGE_ID="$2"
    local VERSIONUM="$3"

    # 每个任务独立的临时目录（避免并发冲突）
    local TASK_IDX="$4"
    local TEMP_DIR="/home/x/plugins/.tmp/${PACKAGE_ID}_$$"
    mkdir -p "$TEMP_DIR"

    local FILENAME=""
    local ERROR_MSG=""

    # ====================== 自动获取文件名 ======================
    FILENAME=$(basename "${URL%%\?*}" | sed 's/%20/ /g; s/%2D/-/g; s/%2E/./g; s/%28/(/g; s/%29/)/g')
    if [[ -z "$FILENAME" || "$FILENAME" == "download_"* || ! "$FILENAME" =~ \.(tar\.gz|tgz|zip)$ ]]; then
        CONTENT_DISP=$(curl -sI "$URL" 2>/dev/null | grep -i 'content-disposition' | sed -n 's/.*filename="\?\([^";]*\)"\?.*/\1/p')
        if [[ -n "$CONTENT_DISP" ]]; then
            FILENAME="$CONTENT_DISP"
        else
            FILENAME="download_$(date +%Y%m%d_%H%M%S).tar.gz"
        fi
    fi

    local OUTPUT_FILE="$TEMP_DIR/$FILENAME"

    # ====================== curl 下载 ======================
    HTTP_CODE=$(curl --location --retry 3 --retry-delay 2 --connect-timeout 15 --max-time 600 --continue-at - --output "$OUTPUT_FILE" --write-out "%{http_code}" "$URL" 2>/dev/null)
    CURL_EXIT_CODE=$?

    # 检查下载是否成功
    if [[ $CURL_EXIT_CODE -ne 0 || "$HTTP_CODE" != "200" || ! -f "$OUTPUT_FILE" || ! -s "$OUTPUT_FILE" ]]; then
        ERROR_MSG="download failed (http:$HTTP_CODE, curl_exit:$CURL_EXIT_CODE)"
    fi

    # 检查下载内容是否是错误响应
    if [[ -z "$ERROR_MSG" && -f "$OUTPUT_FILE" ]]; then
        FILE_HEAD=$(head -c 5 "$OUTPUT_FILE" 2>/dev/null)
        if [[ "$FILE_HEAD" == "<?xml" || "$FILE_HEAD" == '{"err' || "$FILE_HEAD" == "<Error" ]]; then
            ERROR_MSG="remote error: $(head -c 200 "$OUTPUT_FILE" 2>/dev/null | tr -d '\n' | tr -d '"' | sed 's/  */ /g')"
        fi
    fi

    # ====================== 处理结果 ======================
    if [[ -n "$ERROR_MSG" ]]; then
        rm -f "$OUTPUT_FILE"
        rm -rf "$TEMP_DIR"
        ALL_FAILED+=("$PACKAGE_ID")
    elif [[ "$FILENAME" == *.tar.gz || "$FILENAME" == *.tgz ]]; then
        tar -zxf "$OUTPUT_FILE" -C "$TEMP_DIR" 2>/dev/null
        if [[ $? -ne 0 ]]; then
            ERROR_MSG="tar extract failed"
            rm -f "$OUTPUT_FILE"
            rm -rf "$TEMP_DIR"
            ALL_FAILED+=("$PACKAGE_ID")
        else
            rm -f "$OUTPUT_FILE"
            EXTRACTED_ITEM=$(ls -A "$TEMP_DIR" | head -1)
            if [[ -z "$EXTRACTED_ITEM" ]]; then
                rm -rf "$TEMP_DIR"
                ALL_FAILED+=("$PACKAGE_ID")
            else
                EXTRACTED_PATH="$TEMP_DIR/$EXTRACTED_ITEM"
                if [[ -d "$EXTRACTED_PATH" && "$EXTRACTED_ITEM" != "$PACKAGE_ID" ]]; then
                    mv "$EXTRACTED_PATH"/* "$TEMP_DIR/" 2>/dev/null
                    rm -rf "$EXTRACTED_PATH"
                    EXTRACTED_PATH="$TEMP_DIR"
                fi
                rm -rf "/home/x/plugins/$PACKAGE_ID"
                mv "$EXTRACTED_PATH" "/home/x/plugins/$PACKAGE_ID"
                rm -rf "$TEMP_DIR"
                mkdir -p "/home/x/plugins-version"
                echo "$VERSIONUM" > "/home/x/plugins-version/${PACKAGE_ID}.version"
            fi
        fi
    else
        mv "$OUTPUT_FILE" "/home/x/plugins/${PACKAGE_ID}.${FILENAME##*.}"
        rm -rf "$TEMP_DIR"
    fi
}

# ====================== 批量处理 ======================
IDX=0
while [[ $IDX -lt $# ]]; do
    URL="${@:$IDX+1:1}"
    PACKAGE_ID="${@:$IDX+2:1}"
    VERSIONUM="${@:$IDX+3:1}"
    process_single_download "$URL" "$PACKAGE_ID" "$VERSIONUM" "$IDX"
    IDX=$((IDX + 3))
done

# ====================== 最终唯一输出 ======================
if [[ ${#ALL_FAILED[@]} -eq 0 ]]; then
    echo '{"status":"success"}'
else
    pkg_list=$(printf '"%s",' "${ALL_FAILED[@]}" | sed 's/,$//')
    echo "{\"status\":\"failed\",\"packageIds\":[${pkg_list}]}"
fi

exit 0