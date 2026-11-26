#!/usr/bin/env bash
# Antigravity CLI (agy / agx) status line，对齐 ccd (~/.claude/statusline.sh)：
#   第一行：模型 │ 目录 │ ⎇ 分支* │ @账号 │ 会话 ID
#   第二行：ctx % │ cache % │ quota 额度 │ [TPS] │ [模式/状态/子任务] │ [$费用]
#   第三行：/lead 进行中时显示（复用 ~/.claude/skills/lead/scripts/statusline_segment.sh）
input=$(cat)
# 调试：保留最近一次输入，方便核对字段（同 ccd 的 /tmp/claude-statusline-input.json）
printf '%s' "$input" > /tmp/agy-statusline-input.json 2>/dev/null

# 单次 jq 提取全部常用字段（\x1f 分隔），减少子进程开销
IFS=$'\x1f' read -r session model effort cwd pct u_in u_cw u_cr email cost mode state tasks sub_run sub_tot quota_raw plan_tier transcript <<EOF
$(jq -r '
  (.context_window // {}) as $cw
  | ($cw.current_usage // {}) as $u
  | (
      if $cw.used_percentage != null then ($cw.used_percentage | tostring)
      elif ($cw.context_window_size // 0) > 0 then
        ((($u.input_tokens // 0) + ($u.cache_creation_input_tokens // 0) + ($u.cache_read_input_tokens // 0)) as $used
         | if $used > 0 then ($used * 100 / $cw.context_window_size | tostring) else "" end)
      else "" end
    ) as $pct
  | ((.subagents // []) | map(select(.status == "running")) | length) as $sr
  | ((.subagents // []) | length) as $st
  | (
      (.quota // {}) | to_entries
      | map(select((.value.disabled // false) | not) | select(.value.remaining_fraction != null))
      | map("\(.key)=\((.value.remaining_fraction * 100 + 0.5 | floor))")
      | join(",")
    ) as $q
  | [
      (.session_id // .conversation_id // ""),
      (.model.display_name // .model.id // ""),
      (.model.effort // ""),
      (.workspace.current_dir // .cwd // ""),
      $pct,
      ($u.input_tokens // "" | tostring),
      ($u.cache_creation_input_tokens // "" | tostring),
      ($u.cache_read_input_tokens // "" | tostring),
      (.email // ""),
      (.cost.total_usd // "" | tostring),
      (.cycle_mode // ""),
      (.agent_state // ""),
      (.task_count // "" | tostring),
      ($sr | tostring),
      ($st | tostring),
      $q,
      (.plan_tier // ""),
      (.transcript_path // "")
    ] | join("\u001f")
' <<<"$input" 2>/dev/null)
EOF

[ -z "$cwd" ] && cwd="$PWD"
[ -z "$model" ] && model="agy"

# 颜色（与 ~/.claude/statusline.sh 保持一致）
R=$'\e[0m'; DIM=$'\e[2m'; CYAN=$'\e[36m'; BLUE=$'\e[34m'; MAG=$'\e[35m'
GRN=$'\e[32m'; YEL=$'\e[33m'; RED=$'\e[31m'
SEP="${DIM} │ ${R}"

# 目录：兼容 agx 账号池 HOME，把真实家目录和 /data00$HOME 统一缩写为 ~
user_home=$(eval echo "~$(id -un)" 2>/dev/null)
real_home=$(readlink -f "${user_home:-$HOME}" 2>/dev/null)
dir="$cwd"
[ -n "$HOME" ] && dir=${dir/#$HOME/\~}
[ -n "$user_home" ] && dir=${dir/#$user_home/\~}
[ -n "$real_home" ] && dir=${dir/#$real_home/\~}
[ "${dir:0:1}" = "/" ] && [ -n "$user_home" ] && [ -d "/data00$user_home" ] && dir=${dir/#\/data00$user_home/\~}

# git 分支 + 脏标记
branch=""
if git -C "$cwd" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  b=$(git -C "$cwd" symbolic-ref --short -q HEAD 2>/dev/null || git -C "$cwd" rev-parse --short HEAD 2>/dev/null)
  dirty=""
  [ -n "$(git -C "$cwd" --no-optional-locks status --porcelain 2>/dev/null | head -1)" ] && dirty="*"
  branch="⎇ ${b}${dirty}"
fi

# 账号名（优先从 email 取前缀，否则从 agx 账号池 HOME 路径提取）
acct=""
if [ -n "$email" ]; then
  acct="${email%%@*}"
elif [[ "$HOME" == */agy-pool/accounts/*/home ]]; then
  acct=$(basename "$(dirname "$HOME")")
fi

# 顺手同步套餐类型到 agx 账号池缓存（内容变化时才写）
if [ -n "$plan_tier" ] && [[ "$HOME" == */agy-pool/accounts/*/home ]]; then
  _plan_file="${HOME%/home}/plan"
  if [ ! -f "$_plan_file" ] || [ "$(head -n 1 "$_plan_file" 2>/dev/null)" != "$plan_tier" ]; then
    printf '%s\n' "$plan_tier" > "$_plan_file" 2>/dev/null || true
  fi
fi

# context 百分比
ctx=""
if [ -n "$pct" ]; then
  p=$(printf '%.0f' "$pct" 2>/dev/null)
  if [ -n "$p" ]; then
    if   [ "$p" -ge 80 ]; then c=$RED
    elif [ "$p" -ge 50 ]; then c=$YEL
    else c=$GRN; fi
    ctx="${c}ctx ${p}%${R}"
  fi
fi

# 缓存命中率（current_usage: cache_read / (input + cache_creation + cache_read)）
cache="${DIM}cache --${R}"
if [ -n "$u_in" ] || [ -n "$u_cr" ]; then
  tot=$(( ${u_in:-0} + ${u_cw:-0} + ${u_cr:-0} ))
  if [ "$tot" -gt 0 ]; then
    h=$(awk -v cr="${u_cr:-0}" -v t="$tot" 'BEGIN { printf "%.1f", cr * 100 / t }')
    if   awk -v h="$h" 'BEGIN { exit !(h >= 90) }'; then c=$GRN
    elif awk -v h="$h" 'BEGIN { exit !(h >= 70) }'; then c=$YEL
    else c=$RED; fi
    cache="${c}cache ${h}%${R}"
  fi
fi

# 额度显示：优先用 agy 实时 quota 对象，没有则回退读账号池 usage.tsv 缓存
color_pct() {
  local v="$1"
  if   [ "$v" -le 20 ] 2>/dev/null; then printf '%s' "$RED"
  elif [ "$v" -le 50 ] 2>/dev/null; then printf '%s' "$YEL"
  else printf '%s' "$GRN"; fi
}

quota_s=""
if [ -n "$quota_raw" ]; then
  g5=""; gw=""; c5=""; cw=""; other_q=()
  IFS=',' read -r -a qitems <<<"$quota_raw"
  for qi in "${qitems[@]}"; do
    qk="${qi%%=*}"; qv="${qi#*=}"
    case "$qk" in
      gemini-5h|*gemini*5h*|*gemini*five*) g5="$qv" ;;
      gemini-weekly|*gemini*week*) gw="$qv" ;;
      3p-5h|*3p*5h*|*claude*5h*|*gpt*5h*) c5="$qv" ;;
      3p-weekly|*3p*week*|*claude*week*|*gpt*week*) cw="$qv" ;;
      *) other_q+=("${DIM}${qk}${R} $(color_pct "$qv")${qv}%${R}") ;;
    esac
  done
  fmt_grp() {
    local tag="$1" v5="$2" vw="$3" s=""
    [ -n "$v5" ] && s="${DIM}5h${R} $(color_pct "$v5")${v5}%${R}"
    if [ -n "$vw" ]; then
      [ -n "$s" ] && s+="${DIM}/${R}"
      s+="${DIM}周${R} $(color_pct "$vw")${vw}%${R}"
    fi
    [ -n "$s" ] && printf '%s' "${DIM}${tag}${R} ${s}"
  }
  gq=$(fmt_grp "G" "$g5" "$gw")
  cq=$(fmt_grp "C" "$c5" "$cw")
  qparts=()
  case "${model,,}" in
    *claude*|*gpt*)
      [ -n "$cq" ] && qparts+=("$cq")
      [ -n "$gq" ] && qparts+=("$gq")
      ;;
    *)
      [ -n "$gq" ] && qparts+=("$gq")
      [ -n "$cq" ] && qparts+=("$cq")
      ;;
  esac
  for oq in "${other_q[@]}"; do qparts+=("$oq"); done
  if [ "${#qparts[@]}" -gt 0 ]; then
    quota_s="${qparts[0]}"
    for ((i=1; i<${#qparts[@]}; i++)); do
      quota_s+=" ${DIM}·${R} ${qparts[i]}"
    done
  fi
elif [ -n "$acct" ]; then
  uts="${AGYX_ROOT:-${user_home:-$HOME}/.local/share/agy-pool}/accounts/${acct}/usage.tsv"
  if [ -f "$uts" ]; then
    case "${model,,}" in
      *claude*|*gpt*) qgrp="Claude" ;;
      *) qgrp="Gemini" ;;
    esac
    read -r q5 qw < <(awk -F'\t' -v g="$qgrp" '
      index($1, g) == 1 {
        v = $3; sub(/%$/, "", v)
        if ($2 ~ /^Five/) f = v
        else if ($2 ~ /^Weekly/) w = v
      }
      END { print (f ? f : "-"), (w ? w : "-") }
    ' "$uts" 2>/dev/null)
    if [ -n "$q5" ] && [ "$q5" != "-" ] || [ -n "$qw" ] && [ "$qw" != "-" ]; then
      qparts=()
      [ -n "$q5" ] && [ "$q5" != "-" ] && qparts+=("${DIM}5h${R} $(color_pct "$q5")${q5}%${R}")
      [ -n "$qw" ] && [ "$qw" != "-" ] && qparts+=("${DIM}周${R} $(color_pct "$qw")${qw}%${R}")
      quota_s="${qparts[*]}"
    fi
  fi
fi

# TPS（对齐 ccd ~/.claude/statusline.sh）：取 transcript 中最近一轮有效模型输出，
# 按 output_tokens / 耗时 计算速率；缺失、损坏或异常耗时静默降级不阻塞
tps=""
tps_file=""
if [ -n "$transcript" ] && [ -f "$transcript" ]; then
  tps_file="$transcript"
elif [ -n "$transcript" ] && [[ "$transcript" == *"/.gemini/antigravity/brain/"* ]]; then
  _alt="${transcript/\/.gemini\/antigravity\/brain\//\/.gemini\/antigravity-cli\/brain\/}"
  [ -f "$_alt" ] && tps_file="$_alt"
fi
if [ -z "$tps_file" ] && [ -n "$session" ]; then
  _alt="${HOME}/.gemini/antigravity-cli/brain/${session}/.system_generated/logs/transcript.jsonl"
  [ -f "$_alt" ] && tps_file="$_alt"
fi
if [ -n "$tps_file" ] && [ -r "$tps_file" ]; then
  _mtime=$(stat -c '%.Y' "$tps_file" 2>/dev/null || stat -f '%m' "$tps_file" 2>/dev/null || echo 0)
  [[ "$_mtime" =~ ^[0-9]+(\.[0-9]+)?$ ]] || _mtime=0
  last_tps=$(tail -n 300 "$tps_file" 2>/dev/null | jq -Rrs --argjson mtime "$_mtime" '
    def parse_ts:
      try (
        capture("^(?<s>[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})(\\.(?<f>[0-9]+))?(Z|\\+00:00)?$")
        | ((.s + "Z") | fromdateiso8601) + ((.f // "0") | ("0." + .) | tonumber)
      ) catch null;
    (split("\n") | map(select(length > 0) | try fromjson catch null | select(type == "object"))) as $rows
    | (
        [ $rows[] | select(.created_at != null) ] as $e
        | ([ $e | to_entries[]
             | select(.value.type == "PLANNER_RESPONSE" and ((.value.output_tokens // 0) | type == "number") and (.value.output_tokens // 0) > 0)
             | (.value.created_at | parse_ts) as $t0
             | (if (.key + 1) < ($e | length) then
                  ($e[.key + 1]) as $nx
                  | if $nx.type == "GENERIC" and (($nx.source // "") == "MODEL") then ($nx.created_at | parse_ts) else null end
                elif $mtime > 0 then $mtime
                else null end) as $t1
             | select($t0 != null and $t1 != null)
             | ($t1 - $t0) as $dt
             | select($dt > 0.05 and $dt <= 600)
             | (.value.output_tokens / $dt * 10 + 0.5 | floor / 10)
           ] | last)
      ) // (
        [ $rows[] | select(.timestamp != null and (.type == "assistant" or .type == "user")) ] as $c
        | ([ $c | to_entries[] | select(.value.type == "assistant" and (.value.message.usage.output_tokens // 0) > 0) ] | last) as $last
        | if $last == null then null else
            $last.value.message.id as $id
            | ([ $c | to_entries[] | select(.value.message.id == $id) ] | first.key) as $first
            | ($c[$first - 1] // null) as $prev
            | if $prev == null then null else
                (($last.value.timestamp | parse_ts) as $t1 | ($prev.timestamp | parse_ts) as $t0
                 | if ($t0 != null and $t1 != null) then
                     ($t1 - $t0) as $dt
                     | if $dt > 0.05 and $dt <= 600 then ($last.value.message.usage.output_tokens / $dt * 10 + 0.5 | floor / 10) else null end
                   else null end)
              end
          end
      )
    | if . == null then empty else tostring end
  ' 2>/dev/null)
  [ -n "$last_tps" ] && tps="${MAG}TPS ${last_tps}${R}"
fi

# 模式 / 后台任务 / 子 Agent / 费用
extras=()
[ -n "$mode" ] && extras+=("${YEL}${mode}${R}")
[ -n "$sub_run" ] && [ "$sub_run" -gt 0 ] 2>/dev/null && extras+=("${MAG}sub ${sub_run}/${sub_tot}${R}")
[ -n "$tasks" ] && [ "$tasks" -gt 0 ] 2>/dev/null && extras+=("${DIM}bg ${tasks}${R}")
if [ -n "$cost" ] && awk -v c="$cost" 'BEGIN { exit !(c > 0) }' 2>/dev/null; then
  extras+=("${DIM}\$$(printf '%.2f' "$cost")${R}")
fi

# 第一行：模型 │ 目录 │ 分支 │ @账号 │ 会话 ID
line="${CYAN}${model}${R}${SEP}${BLUE}${dir}${R}"
[ -n "$branch" ] && line+="${SEP}${YEL}${branch}${R}"
[ -n "$acct" ] && line+="${SEP}${MAG}@${acct}${R}"
[ -n "$session" ] && line+="${SEP}${DIM}${session}${R}"

# 第二行：ctx % │ cache % │ quota │ [TPS] │ [mode / sub / bg / $cost]
line2="${ctx:-${DIM}ctx --${R}}${SEP}${cache}"
[ -n "$quota_s" ] && line2+="${SEP}${quota_s}"
[ -n "$tps" ] && line2+="${SEP}${tps}"
for ex in "${extras[@]}"; do
  line2+="${SEP}${ex}"
done

# 第三行：/lead 进行中时显示（复用 lead 的状态栏片段；agy JSON 里 session_id 即 conversation_id）
lead_line=""
if [ -f "$HOME/.claude/skills/lead/scripts/statusline_segment.sh" ]; then
  lead_line="$(printf '%s' "$input" | bash "$HOME/.claude/skills/lead/scripts/statusline_segment.sh" 2>/dev/null)"
fi

if [ -n "$lead_line" ]; then
  printf '%s\n%s\n%s' "$line" "$line2" "$lead_line"
else
  printf '%s\n%s' "$line" "$line2"
fi
