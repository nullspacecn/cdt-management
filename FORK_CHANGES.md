# Fork 改动说明（nullspacecn/cdt-management，基于上游 v1.3.8）

> 基线：fork 与上游在同一提交 `bd1c563`（v1.3.8），fork 本身无任何私改，
> 以下全部改动都在本 fork 副本里做。版本改为 `1.3.10-fork`。
> 目标：用本面板替换现有 CDT 换班脚本（`/root/cdt-rotation/rotate.py`，
> 北京时间 00:00/12:00、每人 12 小时），改为北京时间每天 03:00 换班一次、
> 每人 24 小时，并用单域名 `hk.cscec.ac.cn` 做无感切换。

## 1. 新增排班：每天固定时刻换班、每人 24 小时（`cycle=daily24`）

上游只有 `day`（一天一轮，按小时/固定时刻表，24 小时内换完）和 `month`
（一月一轮，按天）。新增 `daily24`：

- 每天 `switch_time`（默认 `03:00`，组时区，默认 Asia/Shanghai）换班一次，
  成员按 `position` 顺序逐日交替，每人 24 小时。
- 当班判定是纯日历计算：`anchor_at` 所在日期的 `switch_time` 时刻由
  `members[0]` 当班，之后每天同一时刻交替；不随容器重启/建组时间漂移，
  启动时第一轮对账就能按当日班次修正开关状态（startup_sync 等价行为）。
- 本次部署的取值：成员 A（国内站，position 0）、B（国际站，position 1），
  `switch_time=03:00`，`anchor_at` 须显式设为 **2026-11-09 03:00 北京时间**
  （A 当班起点）。于是：11-09 03:00–11-10 03:00 为 A；11-10 03:00–11-11 03:00
  为 B；11-11 03:00 起为 A，依此类推。部署后用面板核对“当前当班”与下次换班
  时间，错了就用“立即换班”或改 anchor，不要直接改实例状态。
- 注意：面板上编辑组成员/周期/换班时刻后，`anchor_at` 会被重置为编辑时刻
  （上游原有行为）。daily24 下 anchor 只取“日期”，重置后当班按编辑当日
  的 switch_time 重新算，保存后务必在面板核对当班归属。
- 手动换班：daily24 与时刻表/一月一轮一样走运行记录覆盖（`_manual_switch_timetable`），
  不挪 `anchor_at`。

改动文件：`app/shift.py`（`is_daily24_cycle`、`_duty_at_daily24`、`duty_at`、
`next_switch_at`、`manual_switch` 分支）、`app/db.py`（`shift_groups.switch_time`
列 + 迁移）、`app/repo.py`（`upsert_group` 接受 `daily24` 与 `switch_time`，
校验 HH:MM，写库）、`app/api.py`（保存时 daily24 跳过时刻表校验；
`_group_payload` 返回 `switch_time`）。

## 2. DDNS 时序改为“先改解析、再停旧机”

上游：当班 Running + 非当班全部停机确认后才同步 DDNS（`_sync_ddns` 在
`all_stopped` 之后），每次换班新连接有 1–3 分钟失败窗口。

改为（`app/shift.py` `reconcile_group` 第 4.5 步）：

1. 当班已 Running 且公网 IP 就绪 → 立即同步 DDNS 到新当班机（`verify=True`，
   去 Cloudflare 查真实值），事件里只记一行 DDNS（多轮去重与第 6 步同一套逻辑）。
2. 同步成功（或等满放行时限，见下）→ 再停旧机。
3. 同步失败 → 本轮不下发任何停机指令，旧机继续跑，下一轮重试；等满
   `_DDNS_WAIT_SECONDS`（180 秒）仍失败才放行停机，失败原因留在进度和结算报告里。
4. 第 6 步不再重复同步（前置已做过的不再调接口）；已结算班次 / 未启用 DDNS
   时走原有第 6 步逻辑不变。

“无感”的边界不变：短连接按此顺序基本无感；长连接在旧机停机那一刻仍会断，
需重连（每天 03:00 各一次）。

## 3. 面板内置 Uptime Kuma 心跳

上游全仓库无 Kuma。新增（`app/scheduler.py`、`app/db.py`、`app/api.py`）：

- 设置项：`kuma_enabled`（默认 0）、`kuma_push_url`、`kuma_interval`
  （默认 300 秒，与现网 CDT push 监控 interval 对齐）。面板设置页可配，
  URL 只返回“是否已配置”，绝不回显、不写日志。
- 调度线程每轮对账后检查：到间隔了才推；推之前先 GET 本机
  `http://127.0.0.1:8080/api/health`，HTTP 层挂了就不推，让 Kuma 标 DOWN；
  推送失败 60 秒内不再重试。调度线程本身就是“服务还活着”的证明——
  线程卡死/进程僵死时这里不再执行，Kuma 自然标 DOWN。
- 部署时填入现有的 CDT push 监控 URL（在 Kuma 库里，服务器本机取，不经聊天）。

## 4. DDNS Zone 自动识别改为最长后缀匹配

上游 `_resolve_zone_id`（`app/ddns.py`）取记录名最后两段当 Zone 名：
`hk.cscec.ac.cn` → `ac.cn`，`GET /zones?name=ac.cn` 查不到 Zone 就报
“无法自动识别 ac.cn 的 Zone ID”。`ac.cn` 这类二级域（以及 `co.uk` 等）
的最后两段只是公共后缀，不是 Zone。

改为从长到短逐个后缀精确匹配（`_zone_candidates`）：`hk.cscec.ac.cn` →
先问 `hk.cscec.ac.cn`，再问 `cscec.ac.cn`（命中），最长命中者胜；
单段后缀不查；手动填的 `ddns_zone_id` 仍优先，不发任何请求。
首个候选就 HTTP 非 200（通常是认证问题）时直接报错，不把 token 问题
伪装成“识别不出 Zone”。

测试：`scripts/test_ddns_resilience.py` 新增 `ResolveZoneId`（7 项）：
ac.cn 后缀解析到父 Zone、普通 com、apex 记录本身就是 Zone、无匹配报错、
手动 zone_id 优先且不发请求、空记录、首候选 403 直接报错。

## 5. 未改的语义（已评估、可接受，需部署时知情）

- **无 `manual_stopped` / 人工接管**：面板上手停当班实例，下一轮对账
  （≤60 秒）即按排班重新拉起。想人为按住一台只能停用整个换班组或移出成员。
  2026-11-05 的教训（manual_stopped=1 导致换班跳过）在新服务里不可能重演。
- **抢占恢复重试节奏**：仍为上游默认——当班 Stopped 180 秒 / Starting 宽限
  480 秒后转移，约 3 分钟即转移（v2 要求无库存重试 2 次、间隔 5 分钟），
  转移后本班次内不再重试原实例，等下一换班点（03:00）才轮回。未改代码，
  原因是改动会牵动超时/宽限/事件去重多处，且 24 小时班次下转移后
  “等到下一班”最长 24 小时——部署后观察一次真实抢占再决定是否调
  `CDT_DUTY_STALL_SECONDS`。
- **无“启动异常”Telegram 通知**：启动修正失败只记事件，10 分钟后才报
  “换班异常”。由 Kuma 心跳兜底：服务起不来/调度卡死时 Kuma 标 DOWN，
  Kuma 自身的 Telegram 通知会报警。
- **其余已满足的未动**：阈值 188GB 与额度 200GB 分开、先开后关、10 分钟
  超时、流量故障转移、双超全局熔断、节省停机、Telegram（仅 token+chat_id）。
  每日汇总默认 23:58，部署时在面板设置里改为 **23:55**（配置项，不改代码）。

## 6. 测试

- 新增 `scripts/test_daily24.py`（7 项）：排班交替（2026-11-09–12
  逐日 A/B）、`next_switch_at`、组校验、daily24 换班全流程（DDNS 在停机前、
  只记一行、结算成功）、Kuma 心跳（关闭时不推、健康检查失败不推、
  健康通过才推且间隔内不重复推）。
- 更新 `scripts/test_shift_flow.py` 与 `scripts/test_group_lifecycle.py`
  的 DDNS 顺序断言（DDNS 在停机前），其余断言不变。
- 新增 `scripts/test_ddns_resilience.py` 的 `ResolveZoneId`（7 项）：
  ac.cn 后缀解析到父 Zone、普通 com、apex 记录本身就是 Zone、无匹配报错、
  手动 zone_id 优先且不发请求、空记录、首候选 403 直接报错。
- 全套测试结果（`CDT_DRY_RUN=1`，不碰真实阿里云）：
  350 通过、4 失败（共 354 项；1.3.9 时为 343/347，新增 7 项全部通过），
  失败的 4 项全在 `scripts/test_entrypoint.py`——该 4 项在
  未改动的基线（`git stash` 后）同样失败，原因是本机以 root 运行、
  `docker-entrypoint.py` 的路径解析在测试环境抛错，与本次改动无关
  （`docker-entrypoint.py` 未动）。

## 7. 部署方案（先不部署，等父 agent 定切过去的时间）

准备（换班点之前做，不影响运行中的旧脚本）：
1. 备份（甲骨文 + VM 各一份）：`/root/cdt-rotation/` 全目录、
   `/app/alicdt-manager/data/guard.db`、`/etc/systemd/system/cdt-rotation.service`、
   nginx 的 `alicdt.cscec.ac.cn` 配置。旧脚本与旧面板留着回滚，不要删。
2. 在甲骨文上从本 fork 构建镜像（不要跑 `install.sh` 管道安装，不要
   `git pull` 未验证的代码）：
   `git clone https://github.com/nullspacecn/cdt-management.git`（先把本 fork
   的改动 commit + push 到 fork；部署用的就是这个提交），
   `docker build -t cdt-management:fork-1.3.10 .`，
   `docker-compose.yml` 的 image 改为该镜像、端口改为 `127.0.0.1:8282:8080`，
   `mkdir -p /opt/docker/cdt-management`，`docker compose up -d`。
   上线后新增备份：`/opt/docker/cdt-management/` 全目录
   （`cdt-management.db` + `master.key` 必须成对备份，丢 key 等于 AK 全丢）。
3. 在新面板里配好（组先建为**停用态**，旧脚本还在跑，新服务只读不动作）：
   两台服务器（quota 200 / threshold **188** / StopCharging，AK 在服务器本机
   从 guard.db/config.yaml 复制填入，不经聊天、不打印值；Region cn-hongkong）；
   换班组 cycle=`daily24`、switch_time=`03:00`、成员 A（hours 24）、B（hours 24），
   **anchor_at 显式设为 2026-11-09 03:00 北京时间**（面板保存后会重置为
   编辑时刻，须在甲骨文上用 sqlite3 把该组的 anchor_at 改回该时刻并核对当班）；
   **不要建单机保活**；Telegram 用现有 bot（点测试确认）；每日报告改 23:55；
   Kuma 心跳填现有 push URL（先不启用，等切过去那一刻再启用，避免与旧脚本
   同时推同一 URL）；DDNS：启用，填 Cloudflare token 与 Zone，记录只加
   `hk.cscec.ac.cn` 一条，绑到换班组，TTL 60、DNS-only（`ddns_proxied=0`）。
4. **Cloudflare token 阻塞项（部署前必须先解决）**：甲骨文现有 Cloudflare
   token 对 DNS API 返回 401/400、不可用。须用户在 Cloudflare 后台新建
   Zone:DNS:Edit 权限 token，存甲骨文 `/root/cdt-rotation/cloudflare_token`
   （0600）。部署时先在面板点“立即同步”验证 DDNS 能真正 PUT 成功；
   token 不可用时 DDNS 保持关闭、不要启用换班组，否则每次换班都会等满
   180 秒放行、域名不跟着走。
5. 点“立即同步”只读验证：两台真实状态与流量与旧 `state.json` 量级一致。

切换（在换班点做）：
6. 换班点前 5 分钟确认当班实例与流量正常（新面板概览 + 旧 state.json 一致）。
7. 换班点时刻：停并禁用旧服务（`systemctl stop --now cdt-rotation` +
   `systemctl disable cdt-rotation`，留着回滚）；停旧面板容器
   （`docker stop alicdt-manager`，留着回滚）。
8. 新面板启用换班组，等下一轮对账（≤60 秒）：应开当班、DDNS 先指新 IP、
   再停另一台，观察事件流与 Telegram“换班完成”通知。
9. nginx 上游 `alicdt.cscec.ac.cn` 从 127.0.0.1:8000 改为 127.0.0.1:8282，
   `nginx -t && nginx -s reload`（域名证书不变）。
10. 启用 Kuma 心跳，确认 Kuma 里 cdt-rotation 监控保持 UP。
11. 观察一个完整周期（一次 03:00 换班、一次 23:55 每日汇总）再算上线。

回滚：新面板停用换班组或 `docker stop cdt-management` → 旧服务
`systemctl enable --now cdt-rotation`（startup_sync 按班次修正状态；注意旧脚本
已回滚到 v2 的 00:00/12:00、12 小时制，回滚后班次按旧规则走）→
`docker start alicdt-manager` + nginx 上游改回 8000 → 停 Kuma 心跳
（旧脚本恢复后继续每 5 分钟推）→ 确认 duty 与班次一致。

## 8. 切过去的时间（评估结论：今晚来不及）

- 现在是北京时间 2026-11-10 02:32 左右，距 03:00 换班点约 30 分钟。
  代码修改与 dry-run 测试已完成，但部署验证（构建镜像、配面板、验 DDNS
  PUT、验 Kuma）来不及在 30 分钟内做完并留出回滚余量。**今晚 03:00 不要切。**
- 旧脚本已回滚到 v2（00:00/12:00、12 小时制），A 当班至北京时间
  2026-11-10 12:00 再换 B；今晚 03:00 不会换班，今晚的过渡做法就是
  **什么都不动**，旧脚本继续跑。
- 建议切过去的时间：**北京时间 2026-11-10 12:00（利雅得 07:00，B 接班）**
  或 **北京时间 2026-11-11 00:00（利雅得 11-10 19:00，A 接班）**。
  注意：12:00 是旧规则的换班点，但新规则的换班点是 03:00——在 12:00 切，
  新面板启用后会按 daily24 立即纠正：12:00 时 daily24 的当班仍是 B
  （11-10 03:00–11-11 03:00），与旧规则 12:00 换 B 一致，无缝衔接；
  下一次换班是 11-11 03:00 换回 A。若选 11-11 00:00 切，当时 daily24
  的当班是 B，旧规则 00:00 换 A——启用瞬间新面板会把 A 停掉、B 开起来，
  是一次“纠正”而非换班，需在场观察。推荐 **11-10 12:00** 那个点。

## 9. 总览首页 CDT 板块（切过去后会断，需另做）

它现在 SSH 读 `/root/cdt-rotation/state.json` + `systemctl is-active cdt-rotation`
（`space-versions/zong-lan-shou-ye/.../privileged.ts:98-100`、`actions.ts:306-470`），
新服务不写该文件、没有该单元。修复二选一：
- **A. 改总览首页取数逻辑（推荐，干净）**：改成经 SSH 在甲骨文上
  `docker exec cdt-management sqlite3` 只读查 `servers` 表
  （traffic_used、instance_status、public_ip）+ `docker inspect` 看容器健康，
  并把班次判定从“00:00→A、12:00→B”改为“03:00 日交替”。工作量约半天，含联调。
- **B. 写 state.json 兼容垫片（临时过渡）**：每分钟跑的 cron/systemd 脚本，
  把新服务数据库状态翻译成旧版 state.json 字段（duty/last_shift_slot/
  last_watchdog/fused/last_traffic/last_ip），并保留同名 systemd 单元占位。
  能最快恢复显示，但多一层易烂的胶水，长期应切到 A。
