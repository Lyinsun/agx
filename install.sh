#!/usr/bin/env bash
# 安装 / 卸载 agx 多账号工具集（软链模式，仓库更新即时生效，不动现有账号池数据）
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${AGX_INSTALL_BIN:-$HOME/.local/bin}"
GEMINI_CFG_DIR="${HOME}/.gemini/config"
AGX_CFG_DIR="${HOME}/.config/agx"

WITH_STATUSLINE=0
WITH_KABOO_SYNC=0
UNINSTALL=0

usage() {
	cat <<'EOF'
用法：./install.sh [选项]
  --all              安装 CLI + 状态栏/窗口标题 + 配置文件模板（不动已有配置）
  --with-statusline  同时软链 statusline.sh 和 title.sh 到 ~/.gemini/config/
  --with-kaboo-sync  同时软链 agx-kaboo-sync 到 ~/.local/bin/
  --uninstall        仅移除由本仓库创建的软链（不删账号池数据与配置文件）
  -h, --help         显示帮助
EOF
}

while [ $# -gt 0 ]; do
	case "$1" in
		--all) WITH_STATUSLINE=1; WITH_KABOO_SYNC=1; shift ;;
		--with-statusline) WITH_STATUSLINE=1; shift ;;
		--with-kaboo-sync) WITH_KABOO_SYNC=1; shift ;;
		--uninstall) UNINSTALL=1; shift ;;
		-h|--help) usage; exit 0 ;;
		*) echo "未知参数：$1" >&2; usage; exit 2 ;;
	esac
done

link_file() {
	local src="$1" dst="$2"
	mkdir -p "$(dirname "$dst")"
	if [ -L "$dst" ] && [ "$(readlink "$dst")" = "$src" ]; then
		echo "  [已是最新] $dst -> $src"
		return 0
	fi
	if [ -e "$dst" ] || [ -L "$dst" ]; then
		local bak="${dst}.bak-$(date +%Y%m%d%H%M%S)"
		mv "$dst" "$bak"
		echo "  [已备份] $dst -> $bak"
	fi
	ln -s "$src" "$dst"
	echo "  [已安装] $dst -> $src"
}

unlink_if_repo() {
	local dst="$1"
	if [ -L "$dst" ]; then
		local tgt; tgt=$(readlink "$dst")
		case "$tgt" in
			"$REPO_DIR"/*) rm -f "$dst"; echo "  [已卸载] $dst" ;;
			*) echo "  [跳过] $dst 指向 $tgt（非本仓库）" ;;
		esac
	fi
}

if [ "$UNINSTALL" -eq 1 ]; then
	for f in "$BIN_DIR/agx" "$BIN_DIR/agyx" "$BIN_DIR/agx-web" "$BIN_DIR/agx-appeal-watch" "$BIN_DIR/agx-kaboo-sync" \
		"$GEMINI_CFG_DIR/statusline.sh" "$GEMINI_CFG_DIR/title.sh"; do
		unlink_if_repo "$f"
	done
	exit 0
fi

chmod 755 "$REPO_DIR/bin/"* "$REPO_DIR/config/"* "$REPO_DIR/install.sh"

echo "1. 安装核心 CLI 到 $BIN_DIR"
link_file "$REPO_DIR/bin/agx" "$BIN_DIR/agx"
link_file "$REPO_DIR/bin/agx" "$BIN_DIR/agyx"
link_file "$REPO_DIR/bin/agx-web" "$BIN_DIR/agx-web"
link_file "$REPO_DIR/bin/agx-appeal-watch" "$BIN_DIR/agx-appeal-watch"

if [ "$WITH_KABOO_SYNC" -eq 1 ]; then
	echo "2. 安装用量统计同步工具"
	link_file "$REPO_DIR/bin/agx-kaboo-sync" "$BIN_DIR/agx-kaboo-sync"
fi

if [ "$WITH_STATUSLINE" -eq 1 ]; then
	echo "3. 安装 Antigravity CLI 状态栏与窗口标题脚本"
	link_file "$REPO_DIR/config/statusline.sh" "$GEMINI_CFG_DIR/statusline.sh"
	link_file "$REPO_DIR/config/title.sh" "$GEMINI_CFG_DIR/title.sh"
fi

mkdir -p "$AGX_CFG_DIR"
if [ ! -f "$AGX_CFG_DIR/config.env" ]; then
	cp "$REPO_DIR/config.env.example" "$AGX_CFG_DIR/config.env"
	chmod 600 "$AGX_CFG_DIR/config.env"
	echo "4. 已生成默认配置文件：$AGX_CFG_DIR/config.env（按需修改代理等配置）"
else
	echo "4. 保留已有配置：$AGX_CFG_DIR/config.env"
fi

echo ""
echo "安装完成。确认 $BIN_DIR 在 PATH 中后可运行：agx --help / agx ls / agx pick --explain"
