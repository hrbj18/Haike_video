import {
  AbsoluteFill,
  Img,
  OffthreadVideo,
  interpolate,
  spring,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import { resolveAsset } from "../lib/resolveAsset";

// ---------------------------------------------------------------------------
// NewsAnchor — 深色科技新闻四固定位（dark-tech-news playbook）
//
// 全屏真实素材 + 三个固定锚点：
//   1. 左上角 chip 标题（白字 on rgba(0,0,0,0.55) + backdrop-blur）
//   2. 右上角圆形数字人 pip 占位（96-128px，始终在右上角）
//   3. 底部字幕 strip（白字 on rgba(0,0,0,0.55)，最多 2 行）
//
// 动效克制：chip / pip 先淡入，字幕条随后上移 8px；转场只用 fade。
// 强调色唯一：#22D3EE，仅用于 chip 下方短下划线，绝不作为填充。
// ---------------------------------------------------------------------------

interface NewsAnchorProps {
  // 全屏素材：背景图或背景视频（视频优先）
  backgroundImage?: string;
  backgroundVideo?: string;
  backgroundVideoStart?: number;
  // 暗层不透明度（默认 0.28，比通用组件更轻，让实拍素材主导）
  backgroundOverlay?: number;
  // 左上角 chip 标题（主标签）
  headline: string;
  // chip 副标签（可选，用于"分类 · 期数"这类双标签）
  kicker?: string;
  // 右上角数字人 pip：头像图路径 + 名称标签
  avatarImage?: string;
  avatarLabel?: string;
  // 底部字幕：仅作为合同字段保留。字幕文字不在这里渲染——成片字幕由项目
  // 字幕系统按时序统一烧录，组件内再画一遍就会叠成两层。
  subtitle?: string;
  // 画面中央的动态主题组件（用于钩子/引子镜头）
  centerMotif?: string;
  // 配色
  accentColor?: string;
}

const FONT = '"Microsoft YaHei", "Segoe UI", Arial, sans-serif';

// ---------------------------------------------------------------------------
// QuestionMotif — 钩子镜头的中央动态问号组
//
// 一个主干「?」弹簧入场并缓慢呼吸，外围一圈小「?」错峰浮现、
// 各自以不同频率漂移。全部用文字字形实现，不使用任何渐变/霓虹填充，
// 强调色只点缀其中两个小问号，符合 dark-tech-news 的克制要求。
// ---------------------------------------------------------------------------

const QuestionMotif: React.FC<{ accentColor: string }> = ({ accentColor }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  // 主干问号：弹簧入场 + 缓慢呼吸
  const mainIn = spring({
    frame: frame - Math.round(0.3 * fps),
    fps,
    config: { damping: 14, stiffness: 120 },
  });
  const mainScale = interpolate(mainIn, [0, 1], [0.55, 1]);
  const mainRotate = interpolate(mainIn, [0, 1], [-7, 0]);
  const breathe = 1 + Math.sin(frame / (fps * 1.4)) * 0.045;

  // 主干问号外扩的「提问涟漪」：每 2.2s 一次，扩散并淡出
  const ringPeriod = 2.2 * fps;
  const ringT = (frame % ringPeriod) / ringPeriod;
  const ringScale = interpolate(ringT, [0, 1], [0.55, 1.35]);
  const ringOpacity = interpolate(ringT, [0, 0.15, 1], [0, 0.5, 0], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  // 外围小问号：绕画面中心缓慢公转，各自半径/角速度/大小不同
  const satellites = [
    { radius: 262, angle: -32, size: 90, delay: 0.55, accent: false, speed: 0.13 },
    { radius: 232, angle: 18, size: 74, delay: 0.68, accent: true, speed: -0.1 },
    { radius: 286, angle: 122, size: 84, delay: 0.81, accent: false, speed: 0.09 },
    { radius: 240, angle: 208, size: 104, delay: 0.94, accent: false, speed: -0.12 },
    { radius: 300, angle: 268, size: 64, delay: 1.07, accent: false, speed: 0.11 },
    { radius: 252, angle: 332, size: 72, delay: 1.2, accent: true, speed: -0.08 },
  ];

  return (
    <AbsoluteFill style={{ justifyContent: "center", alignItems: "center" }}>
      {/* 中心柔光（极低透明度，仅作景深，不构成填充色块） */}
      <div
        style={{
          position: "absolute",
          width: 660,
          height: 660,
          borderRadius: "50%",
          background: `radial-gradient(circle, ${accentColor}1F 0%, rgba(15,23,42,0) 68%)`,
          opacity: mainIn * 0.9,
        }}
      />

      {/* 主干问号的扩散涟漪 */}
      <div
        style={{
          position: "absolute",
          width: 300,
          height: 300,
          borderRadius: "50%",
          border: `2px solid ${accentColor}`,
          opacity: ringOpacity * mainIn,
          transform: `scale(${ringScale})`,
        }}
      />

      {/* 外围小问号：绕中心公转 */}
      {satellites.map((s, i) => {
        const appear = spring({
          frame: frame - Math.round(s.delay * fps),
          fps,
          config: { damping: 16, stiffness: 80 },
        });
        // 公转角度随帧推进，形成清晰可见的环绕运动
        const theta = ((s.angle + frame * s.speed) * Math.PI) / 180;
        const x = Math.cos(theta) * s.radius;
        const y = Math.sin(theta) * s.radius * 0.82; // 竖向略微压扁，贴合 9:16
        const pulse = 1 + Math.sin(frame / (fps * 0.9) + i * 1.7) * 0.09;
        const op = appear * (0.42 + 0.3 * (0.5 + 0.5 * Math.sin(frame / (fps * 1.5) + i)));
        return (
          <div
            key={i}
            style={{
              position: "absolute",
              transform: `translate(${x}px, ${y}px) scale(${interpolate(appear, [0, 1], [0.4, 1]) * pulse})`,
              fontSize: s.size,
              fontWeight: 800,
              lineHeight: 1,
              fontFamily: 'Inter, "Microsoft YaHei", sans-serif',
              color: s.accent ? accentColor : "#F8FAFC",
              opacity: Math.max(0, op),
              textShadow: `0 4px 18px rgba(0,0,0,0.6)${s.accent ? `, 0 0 22px ${accentColor}88` : ""}`,
            }}
          >
            ?
          </div>
        );
      })}

      {/* 主干问号 */}
      <div
        style={{
          fontSize: 300,
          fontWeight: 800,
          lineHeight: 1,
          fontFamily: 'Inter, "Microsoft YaHei", sans-serif',
          color: "#F8FAFC",
          opacity: mainIn,
          transform: `translateY(-30px) scale(${mainScale * breathe}) rotate(${mainRotate}deg)`,
          textShadow: `0 8px 40px rgba(0,0,0,0.6), 0 0 60px ${accentColor}55`,
        }}
      >
        ?
      </div>
    </AbsoluteFill>
  );
};

export const NewsAnchor: React.FC<NewsAnchorProps> = ({
  backgroundImage,
  backgroundVideo,
  backgroundVideoStart = 0,
  backgroundOverlay = 0.28,
  headline,
  kicker,
  avatarImage,
  avatarLabel,
  centerMotif,
  accentColor = "#22D3EE",
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  // 入场时序（遵循 playbook：pip 0.2s → chip 0.2s）
  const pipIn = spring({
    frame: frame - Math.round(0.2 * fps),
    fps,
    config: { damping: 18, stiffness: 90 },
  });
  const chipIn = spring({
    frame: frame - Math.round(0.4 * fps),
    fps,
    config: { damping: 18, stiffness: 90 },
  });

  const chipTranslateX = interpolate(chipIn, [0, 1], [-28, 0]);
  const pipScale = interpolate(pipIn, [0, 1], [0.7, 1]);

  // 全屏背景：视频优先，其次图片
  const media = backgroundVideo ? (
    <OffthreadVideo
      src={resolveAsset(backgroundVideo)}
      startFrom={Math.round(backgroundVideoStart * fps)}
      style={{ width: "100%", height: "100%", objectFit: "cover" }}
      muted
    />
  ) : backgroundImage ? (
    <Img
      src={resolveAsset(backgroundImage)}
      style={{ width: "100%", height: "100%", objectFit: "cover" }}
    />
  ) : null;

  return (
    <AbsoluteFill style={{ background: "#0F172A", fontFamily: FONT }}>
      {/* Layer 0 — 全屏真实素材 */}
      {media}

      {/* 轻微暗层，保证文字在实拍素材上可读 */}
      <AbsoluteFill
        style={{
          background: `linear-gradient(to bottom, rgba(15,23,42,${backgroundOverlay + 0.08}) 0%, rgba(15,23,42,${backgroundOverlay}) 45%, rgba(15,23,42,${backgroundOverlay + 0.18}) 100%)`,
        }}
      />

      {/* Layer 0.5 — 中央动态主题组件（仅钩子镜头启用） */}
      {centerMotif === "question_marks" && <QuestionMotif accentColor={accentColor} />}

      {/* Layer 1 — 左上角 chip 标题
          外层显式给左右边界（= 画布宽度的 90%），内层再按内容收缩。
          若把 maxWidth 的百分比写在内层，它会相对内层自身的收缩宽度解析，
          等于"文本自然宽度的 80%"，标题会被无谓地折成两行。 */}
      <div
        style={{
          position: "absolute",
          top: "6%",
          left: "5%",
          right: "5%",
          opacity: chipIn,
          transform: `translateX(${chipTranslateX}px)`,
        }}
      >
        <div
          style={{
            display: "inline-block",
            padding: "12px 20px",
            background: "rgba(0,0,0,0.55)",
            backdropFilter: "blur(6px)",
            WebkitBackdropFilter: "blur(6px)",
            borderRadius: 6,
            maxWidth: "100%",
          }}
        >
          {kicker && (
            <div
              style={{
                fontSize: 20,
                fontWeight: 600,
                color: accentColor,
                letterSpacing: "0.04em",
                marginBottom: 4,
              }}
            >
              {kicker}
            </div>
          )}
          <div
            style={{
              fontSize: 34,
              fontWeight: 700,
              color: "#F8FAFC",
              lineHeight: 1.25,
              textShadow: "0 2px 6px rgba(0,0,0,0.5)",
            }}
          >
            {headline}
          </div>
          {/* 唯一强调色：chip 下方 28px 短下划线 */}
          <div
            style={{
              width: 28,
              height: 3,
              backgroundColor: accentColor,
              marginTop: 10,
              borderRadius: 2,
              transform: `scaleX(${chipIn})`,
              transformOrigin: "left center",
            }}
          />
        </div>
      </div>

      {/* Layer 2 — 右上角数字人 pip 占位 */}
      <div
        style={{
          position: "absolute",
          top: "6%",
          right: "5%",
          opacity: pipIn,
          transform: `scale(${pipScale})`,
        }}
      >
        <div
          style={{
            width: 120,
            height: 120,
            borderRadius: "50%",
            overflow: "hidden",
            border: `2px solid rgba(255,255,255,0.35)`,
            boxShadow: "0 4px 16px rgba(0,0,0,0.5)",
            background: "#1E293B",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
          }}
        >
          {avatarImage ? (
            <Img
              src={resolveAsset(avatarImage)}
              style={{ width: "100%", height: "100%", objectFit: "cover" }}
            />
          ) : (
            // 数字人占位：极简人形 + 脉冲光点，表示口播主播位置
            <div style={{ position: "relative", width: "100%", height: "100%" }}>
              <div
                style={{
                  position: "absolute",
                  top: "26%",
                  left: "50%",
                  transform: "translateX(-50%)",
                  width: 42,
                  height: 42,
                  borderRadius: "50%",
                  background: "rgba(255,255,255,0.25)",
                }}
              />
              <div
                style={{
                  position: "absolute",
                  bottom: "14%",
                  left: "50%",
                  transform: "translateX(-50%)",
                  width: 72,
                  height: 46,
                  borderRadius: "36px 36px 12px 12px",
                  background: "rgba(255,255,255,0.18)",
                }}
              />
              <div
                style={{
                  position: "absolute",
                  top: "18%",
                  left: "50%",
                  transform: "translateX(-50%)",
                  width: 8,
                  height: 8,
                  borderRadius: "50%",
                  background: accentColor,
                  boxShadow: `0 0 12px ${accentColor}`,
                  opacity: 0.6 + Math.sin(frame / (fps * 0.6)) * 0.4,
                }}
              />
            </div>
          )}
        </div>
        {avatarLabel && (
          <div
            style={{
              marginTop: 6,
              fontSize: 16,
              fontWeight: 500,
              color: "#F8FAFC",
              textAlign: "center",
              textShadow: "0 1px 4px rgba(0,0,0,0.6)",
            }}
          >
            {avatarLabel}
          </div>
        )}
      </div>

      {/* Layer 3 — 底部字幕安全区
          字幕文字由项目字幕系统（按时序的 SRT phrase cues）统一烧录，成片里
          只能有一层字幕。这里之前渲染的是整段旁白文本、且不随时间变化，会和
          项目字幕叠成两层，所以改为只保留一块无字的对比垫层，
          既维持四固定位的底部锚点，又保证叠加字幕在实拍素材上可读。 */}
      <AbsoluteFill
        style={{
          top: "auto",
          height: "19%",
          background: "linear-gradient(to bottom, rgba(15,23,42,0) 0%, rgba(15,23,42,0.5) 55%, rgba(15,23,42,0.66) 100%)",
        }}
      />
    </AbsoluteFill>
  );
};
