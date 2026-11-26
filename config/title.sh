#!/usr/bin/env bash
# Antigravity CLI (agy / agx) 窗口标题脚本，供 Orca 等终端自动显示会话名：
#   1. /lead 进行中：★ lead · <任务名>（sub-leader 时：sub-leader · <短名>）
#   2. 否则：conversation_title（agy 自动摘要或 /rename 改的名称；JSON 缺省时只读兜底查 conversation_summaries.db）
#   3. 刚启动尚无标题：agx · <目录名>
# 输出单行纯文本（agy 会自动去掉控制字符并转成 OSC 标题序列）。
input=$(cat)

IFS=$'\x1f' read -r sid title state cwd <<EOF
$(jq -r '[
  (.conversation_id // .session_id // ""),
  (.conversation_title // ""),
  (.agent_state // ""),
  (.workspace.current_dir // .cwd // "")
] | join("\u001f")' <<<"$input" 2>/dev/null)
EOF

[ -z "$cwd" ] && cwd="$PWD"

# 1. /lead 进行中优先显示中文任务名
lead_title=""
lead_parent_w=""
if [ -n "$sid" ]; then
  lead_file="${LEAD_ROOT:-${HOME}/.claude/leader}/${sid}.json"
  if [ -f "$lead_file" ]; then
    IFS=$'\x1f' read -r l_active l_title l_pw <<EOF
$(jq -r '[(.active | tostring), (.title // ""), (.parent.worker // (if .parent then "sub" else "" end))] | join("\u001f")' "$lead_file" 2>/dev/null)
EOF
    if [ "$l_active" = "true" ] && [ -n "$l_title" ]; then
      lead_title="$l_title"
      lead_parent_w="$l_pw"
    fi
  fi
fi

# 环境变量里带 LEAD_PARENT_SID（SL start 刚起、还没 LS init 之前也算 sub-leader）
is_sub=""
sub_w="${lead_parent_w}"
if [ -n "$lead_parent_w" ]; then
  is_sub=1
elif [ -n "${LEAD_PARENT_SID:-}" ]; then
  is_sub=1
  sub_w="${LEAD_PARENT_WORKER:-}"
fi

strip_sub_prefix() {
  printf '%s' "$1" | sed -E 's/^\[sub[[:space:]]+W[0-9]+\][[:space:]]*//; s/^sub-leader([[:space:]]+W[0-9]+)?[[:space:]]*[·:：-][[:space:]]*//'
}

if [ -n "$lead_title" ]; then
  if [ -n "$is_sub" ]; then
    clean_t=$(strip_sub_prefix "$lead_title")
    if [ -n "$sub_w" ] && [ "$sub_w" != "sub" ]; then
      out="sub-leader ${sub_w} · ${clean_t}"
    else
      out="sub-leader · ${clean_t}"
    fi
  else
    out="★ lead · ${lead_title}"
  fi
else
  # 2. conversation_title 为空时，只读兜底查本账号 conversation_summaries.db
  if [ -z "$title" ] && [ -n "$sid" ]; then
    db="${HOME}/.gemini/antigravity-cli/conversation_summaries.db"
    if [ -r "$db" ]; then
      title=$(python3 -I - "$db" "$sid" <<'PY' 2>/dev/null || true
import sqlite3, sys
try:
    con = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True, timeout=0.2)
    row = con.execute("SELECT title FROM conversation_summaries WHERE conversation_id=? LIMIT 1", (sys.argv[2],)).fetchone()
    if row and row[0]:
        print(str(row[0]).strip())
except Exception:
    pass
PY
)
    fi
  fi

  if [ -n "$title" ]; then
    out="$title"
  else
    dir_name=$(basename "$cwd" 2>/dev/null)
    [ -z "$dir_name" ] && dir_name="agy"
    out="agx · ${dir_name}"
  fi

  if [ -n "$is_sub" ]; then
    clean_t=$(strip_sub_prefix "$out")
    if [ -n "$sub_w" ] && [ "$sub_w" != "sub" ]; then
      out="sub-leader ${sub_w} · ${clean_t}"
    else
      out="sub-leader · ${clean_t}"
    fi
  else
    case "$state" in
      working|tool_use) out="⋯ ${out}" ;;
    esac
  fi
fi

# 压成单行纯文本，避免过长
out=$(printf '%s' "$out" | tr '\r\n\t' '   ' | sed 's/  */ /g; s/^ //; s/ $//')
printf '%s\n' "$out"
