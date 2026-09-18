# 素材选择政策 V1（项目自主选素材）

> 生效日期：2026-09-15　｜　适用范围：所有"复刻向"短视频（`pipeline_type=avatar-spokesperson`）
> 一句话：**素材的选择权归本项目，不归 copyskill。**

## 1. 为什么改（事故复盘）

此前流程是「copyskill 建议哪条作主素材，就直接用哪条」。但 copyskill 下载的是
**别人发布过的抖音成品**（`copy_skill_hotspot_feed.py`），它本身已经被那个创作者
包装过：

- 中间一条横屏内容带 + 上下高斯模糊填充（竖发横屏的常见做法）；
- 内容带底部烧着**对方的水印 logo 与字幕条**；
- 画面里可能出现**出镜博主**；
- 若是录屏/截图类，整条几乎没有运动。

直接把它当主素材原样输出，就产生了这批事故：

| 期 | 事故 | 根因 |
|---|---|---|
| shengteng | 成片里出现「奇点视线」水印 + 烧入字幕（**重大事故**） | 源素材脏区 y1156~1244 未被裁掉 |
| ram | 中间画面只有 19% 高，太窄 | 沿用了源素材那条 1080×654 的超宽内容带 |
| powerbank | 用了央视素材 + 露脸博主 | 未做人物/来源筛查 |
| deepseek | 同一个静态画面停留过久 | 源素材是静态 UI 录屏 |

**结论**：这些素材不是"不能用"，而是必须**先过项目自己的处理链**再决定用哪几秒。

## 2. 规则

### R1　主素材不由外部建议决定
copyskill 的产出只当**素材池的一份输入**，与 Pexels 等公开素材源**地位平等**。
最终采用哪些源、每个源取哪几个秒区间，由本项目依据证据决定。

### R2　选择必须基于「多宫格帧大图」
```
srcsheet8.py  <源视频>            # 任意源 → 多宫格帧大图 + 抽帧
stock8.py     harvest <期> "<关键词>"   # Pexels 取料 → 筛选 → 多宫格帧大图
```
必须先看图再选区段 —— 禁止"整条源当一整块素材"这种盲选。

### R3　硬否决项（一票否决，不可裁切豁免）
- **出镜人脸**：`remake_material_screen.py` 的 `faces_primary.hits` 非空即否决。
- **不可读的内容载体**：密集文字截图、电商比价页、UI 录屏 —— 放大也读不清，且无运动。
- **第三方权威机构的新闻画面**（央视/央网等）用于商业产品选题。
- **绿幕占位素材**：画面里有 chroma-key 纯绿块（拍摄/合成用底），人眼一看就废。
  判据 `stock8.green_screen_score()`，`> 0.02` 否决（实测 `32084122` 手机屏是纯绿幕）。

> ⚠️ **机器判据会漏检，多宫格目视才是最终判据**（2026-09-15 实测）：
> Pexels `4818143`（电动车充电）的 `face_hit_ratio` 报 **0.000**、`clean=true`，
> 但多宫格帧大图里**站着一个清晰可见的出镜模特** —— 远景/小脸落在
> `faces_small` 或采样盲区，`faces_primary` 抓不到。
> ⇒ **凡是要进片的素材，无论机器判据给什么结论，都必须逐张看完多宫格帧大图。**
> 机器判据只用于把候选池从 40 条缩到 10 条，不做最终裁决。

### R4　可裁切项（修完可用）
- **烧入水印 / 字幕条**：先定位脏区，再裁掉。判据用 `burn_detect8.py`：
  在 `tstd < 6`（时间维稳定）的像素包围盒里找"贯穿全片"的静态区，
  它比单纯近白像素判据可靠（彩色 logo 也能抓到）。
- **画面带过窄**：用 `recrop8.py` 把内容带裁出来重放大（见 R5）。

> ⚠️ `recrop8.py` 的自动脏区判据只对**深底浅字**的源有效。
> **浅底密集文字**的源（如电商比价截图）会让近白行判据在全内容带触发，
> 得出 `content_height≈98px / zoom≈7.8` 这种荒谬结果 —— 见到这种数量级
> 就是**判据失效**的信号，应当改走 R3 换素材，而不是硬裁。

### R5　画面占比下限
竖屏成片里，本地素材的前景内容高度**不低于画布高度的 35%**
（1080×1920 ⇒ ≥ 672px）。低于此值观感就是"画面被挤在中间一条"。
工具：`recrop8.py --fill-height 760`（≈39.6%）。

### R6　替代素材优先用公开源
本地池里没有干净素材时，去公开素材源按**产品或题材关键词**找：
```bash
stock8.py harvest <期> "power bank"    --limit 8
stock8.py harvest <期> "artificial intelligence" --limit 10
```
- Pexels 是英文语料，**中文关键词可靠性差**（"充电宝" → 命中一堆无关视频）；
  按**英文实体名 + 场景词**搜命中率最高（`charging phone`、`usb cable`、
  `battery charger`、`computer memory ram`、`circuit board`、`data center server`）。
- 许可证为 Pexels License（免费、无需署名），可直接商用。

### R7　字幕带判据在 B-roll 上只作参考
`remake_material_screen.py` 的字幕带判据基于行边缘能量，对**横屏 B-roll**
会误报（亮色台面/货架横边、产品包装文字都触发；实测 Pexels 剪辑 5/8 中招，
人工看多宫格确认是干净的）。
⇒ `stock8.py harvest` 默认 `--band-policy relaxed`：字幕带只记进报告供裁切参考，
**不否决**；只有 `strict` 才当硬条件。

### R8　安全窗口的长度与预算（**改窗口前必须仿真**）

**P1　单窗长度 ≥ 5.0s（硬性）**
块时长会被**等比放大**：`retime.build_plan` 按「母版实际时长／spec 段落总长」重分配，
实测比值 **1.15~1.16**（spec 3.478s → 实际块 4.000s）。而 `solve_source_window`
只允许在**同一个窗口内**左移入点，要求 `b − a ≥ need`。
⇒ `plan_shots` 敢取多长的 span 取决于窗口长度，**窗口刚好等于一个镜头时，
放大后必然装不下**。旧值 `MIN_WINDOW = 3.0` 是错的，已改 5.0
（`derive_windows.py`；`curate8.py` 同值硬闸门，需 `--allow-short-window` 才放行）。

**P1 例外　5.0s 是「保守经验值」，不是物理下限（2026-09-15 powerbank 复核实证）**
真实下限是「**窗口长度 ≥ 该窗口可能承载的最大 `span` × 放大比(≈1.16)**」，
而 `span` 由段落 `target_seconds` 决定（`n = round(target/2.4)`，`span = target/n`）。
段落普遍短时（powerbank 每段 2.1~7.7s ⇒ 每镜 ~2.4s，need ≈ 2.8s），
**3.5s 窗口实测可以通过预校验**（14 源 × 2×3.5s，`✓ 预校验通过`）。
⇒ 最终判断权在 `simwindows8.py`：**以它的 `✓ 预校验通过` 为唯一准据**；
`curate8.py` 的 5.0s 闸门只是"先别急"的提示，同批次沿用旧短窗方案时加
`--allow-short-window` 放行，**但落盘后必须补跑 `simwindows8.py` 确认**。
（反之，段落一旦变长——如 deepseek T009 单段 3.478s ⇒ need 4.0s——3.5s 窗口立刻被拦。）

**P2　每源窗口总长 ≈ (成片时长 ÷ 源数) × 1.15**
`plan_shots` 从 `material` 列表**头部贪心顺序吃**。给多了，**排在尾部的源一帧都用不上**
（deepseek 旧配置 S10/S11 全废）；给少了直接 `素材不够`。

**P3　宁可"少而长"，不要"多而短"**
3 个 3s 窗口远比 1 个 9s 窗口差：贪心跳窗时会丢弃当前窗口的剩余部分（纯浪费），
且每个窗口都各自要 ≥ 最大 need。

> 事故实证（2026-09-15）：deepseek `T009 第1镜 需 4.000s，安全窗 5.5-9.0 装不下`、
> ram 同类 6 处（4 源总料 45.0s **恰好等于**成片 45.0s，零余量）。
> 工具：`simwindows8.py <期> [--windows 候选.json]` —— **逐行复刻** `retime.build_plan`
> 的判据（含 `split_frames` 整数帧切分），离线判断会不会被预校验拦，
> 并报每个源实际吃到多少秒；`mkwindows8.py [--apply]` 按 P1~P3 生成候选窗口。
> **规则：任何窗口改动，先过 `simwindows8.py` 再落盘。**

## 3. 落地流程（换素材的完整链）

```
① 取材   stock8.py harvest <期> "<query>"        # 或 srcsheet8.py 看已有的源
② 看图   打开 shortlist.json 里每条的 sheets[]    # 多宫格帧大图
③ 定稿   curate8.py <期> --plan curate-<期>.json  # 写 windows.json（源 + 秒区间）
   ↳ ③b   simwindows8.py <期>                     # ★ R8：先仿真，确认不会被 retime 拦
④ 转码   prepare_master.py <期>                   # 虚化补边 / 硬链接 → 1080x1920
⑤ 排镜头 build8.py spec <期>                      # 重排 shots（★数量与每镜时长不变）
⑥ 建项目 python scripts/remake_build_project.py --spec <期>/remake-spec.json
         ↳ ★ 换了素材文件（aweme_id 变）就必须跑；旧资产会被 2b 移入 assets/_recycle/
⑦ 收尾   build8.py finalize <期> → set_lufs8.py <期> <目标LUFS> → audio8.py <期>
         ↳ finalize 会把 target_lufs 写回 -14，必须再用 set_lufs8 改回本期目标
⑧ 出片   pp8.py all <期>  或  run8b.py --keys <期>
```

> **⑥ 能不能跳，只看「素材文件本身换没换」（2026-09-15 powerbank S13 实证）**
> - **只改窗口/入点（还是同一批素材文件，aweme_id 未变）** → **可以跳过 ⑥**：
>   `⑤` → `⑧`，`retime` 会把新窗口通过 API 写到既有场景上，不动音频策略、不重跑数字人。
> - **换了素材文件（aweme_id 变了）** → **必须跑 ⑥**。因为 `clean-master/` 里根本没有
>   `<key>-<新aweme_id>.mp4`，`pp8.step_retime` 的 `_resolve` 会走到兜底前缀匹配，
>   命中**旧的** `<key>-<旧aweme_id>.mp4` ⇒ 画面照旧，而日志**全绿**
>   （`retime APPLIED（27 块）`、preview/approve/final 全 OK）—— **典型假成功**，
>   只有从**成片**抽帧（`finalsheet8.py <期>`）才能发现。
>   ⑥ 顺带执行 2b：把上一版残留资产移入 `projects/<pid>/assets/_recycle/remake-stale`。
>   ⇒ 验收硬条件：换素材后 `projects/<pid>/assets/video/clean-master/<key>-<新aweme_id>.mp4` **必须存在**。

**关键性质（决定了重做不贵）**：`build8.py::plan_shots` 里
**镜头数量与每镜时长只由 `target_seconds` 决定，与素材无关**
（`n = round(target / SHOT_SECONDS)`，`spans = target/n`）。
所以换素材后脚本时序**逐帧不变** ⇒

> **已付费的数字人母版（RunningHub InfiniteTalk）继续有效，
> 换素材/改画面绝不需要重跑付费阶段，全程零付费。**

## 4. 相关工具索引

| 工具 | 位置 | 用途 |
|---|---|---|
| `srcsheet8.py` | `.backlot/9.14-remake-8/` | 任意源素材 → 多宫格帧大图 + 审计 |
| `stock8.py` | 同上 | Pexels 取料：search / harvest / sheets / shortlist / adopt / list |
| `curate8.py` | 同上 | 素材定稿：把选定源与秒区间写进 `windows.json` |
| `simwindows8.py` | 同上 | ★ 安全窗口仿真器：离线复刻 `retime` 判据，改窗口前必跑（R8） |
| `mkwindows8.py` | 同上 | 按 R8 的 P1~P3 生成候选窗口；`--apply` 落盘并备份 |
| `recrop8.py` | 同上 | 再构图：裁脏区 + 放大重画模糊填充（输出仍 1080×1920） |
| `burn_detect8.py` | 同上 | 烧入水印/字幕条检测（时间稳定度 + 近白行） |
| `blocksheet8.py` | 同上 | 成片逐块抽帧审计表（回看每块实际用了哪段源） |
| `remake_material_screen.py` | `scripts/` | 人脸扫描 + 字幕带检测（项目自有筛选主力） |
| `material_overview.py` | `backlot/` | 多宫格帧大图底层实现（`contact-sheet-v1`） |

## 5. 验收

- [ ] 每期成片的每个画面块都能在 `blocksheet8.py` 的大图里找不到水印/字幕/人脸
- [ ] 本地素材前景高度 ≥ 35%（`ruler8.py` 叠标尺量）
- [ ] Pexels 素材有 `shortlist.json` 记录人脸与字幕带实测值
- [ ] `windows.json` 的 `curated_plan` 指回本次定稿的 plan 文件（可追溯）
- [ ] **R8**：`simwindows8.py <期>` 报 `✓ 预校验通过`，且无窗口短于 5.0s
- [ ] **R8**：`simwindows8.py` 的「各源实际吃到多少秒」里，没有源是 `★未用上`
- [ ] **换素材**：`projects/<pid>/assets/video/clean-master/<key>-<新aweme_id>.mp4` **存在**（否则 ⑥ 没跑，retime 会假成功）
- [ ] **换素材**：旧资产已进 `projects/<pid>/assets/_recycle/remake-stale`，`audit_assets8.py <期>` 无同名歧义
- [ ] **★ 成片复核**：`finalsheet8.py <期>` 从 `renders/final.mp4` 抽帧逐张目视 —— 无露脸/水印/烧入字幕/第三方台标。**改了素材就必须做这一步**（源素材干净 ≠ 成片干净）
