export type CinematicTone = "cold" | "steel" | "void" | "neutral";

export interface CinematicBaseScene {
  id: string;
  startSeconds: number;
  durationSeconds: number;
  startFrame?: number;
  durationFrames?: number;
}

export interface CinematicVideoScene extends CinematicBaseScene {
  kind: "video";
  src: string;
  tone?: CinematicTone;
  trimBeforeSeconds?: number;
  trimAfterSeconds?: number;
  trimBeforeFrame?: number;
  trimAfterFrame?: number;
  playbackRate?: number;
  filter?: string;
  fadeInFrames?: number;
  fadeOutFrames?: number;
}

export interface CinematicTitleScene extends CinematicBaseScene {
  kind: "title";
  text: string;
  accent?: string;
  intensity?: number;
  backgroundSrc?: string;
  backgroundTrimBeforeSeconds?: number;
  backgroundTrimAfterSeconds?: number;
  variant?: "plate" | "overlay";
}

export type CinematicMediaType = "video" | "image";
export type CinematicMediaFit = "contain" | "cover";

export interface CinematicMediaLayer {
  src: string;
  mediaType: CinematicMediaType;
  fit?: CinematicMediaFit;
  muted?: boolean;
  trimBeforeSeconds?: number;
  trimAfterSeconds?: number;
  trimBeforeFrame?: number;
  trimAfterFrame?: number;
  playbackRate?: number;
}

export interface CinematicHeroPlacement {
  presetId?: "landscape_hero_center" | "portrait_hero_center" | "source_hero_custom";
  positionXRatio: number;
  positionYRatio: number;
  sizeRatio: number;
  aspectMode: "source";
  maxHeightRatio?: number;
  sourceAspectRatio: number;
}

export interface CinematicOverlayLayer extends CinematicMediaLayer {
  id: string;
  role: "hero";
  startSeconds: number;
  endSeconds: number;
  startFrame?: number;
  endFrame?: number;
  placement?: CinematicHeroPlacement;
}

export interface CinematicFrameStyle {
  widthRatio?: number;
  heightRatio?: number;
  borderRadiusRatio?: number;
  borderColor?: string;
  shadow?: "soft" | "none";
}

export interface CinematicLayeredScene extends CinematicBaseScene {
  kind: "layered";
  layoutRecipe: "focus_card";
  background: CinematicMediaLayer;
  overlays: CinematicOverlayLayer[];
  frameStyle?: CinematicFrameStyle;
}

export type CinematicScene = CinematicVideoScene | CinematicTitleScene | CinematicLayeredScene;

export interface CinematicSoundtrack {
  src: string;
  volume?: number;
  trimBeforeSeconds?: number;
  trimAfterSeconds?: number;
  fadeInSeconds?: number;
  fadeOutSeconds?: number;
}

export interface CinematicWordCaption {
  word: string;
  startMs: number;
  endMs: number;
}

export interface CinematicCaptionConfig {
  words: CinematicWordCaption[];
  wordsPerPage?: number;
  fontSize?: number;
  color?: string;
  highlightColor?: string;
  backgroundColor?: string;
}

export interface CinematicRendererProps {
  [key: string]: unknown;
  scenes: CinematicScene[];
  titleFontSize?: number;
  titleWidth?: number;
  signalLineCount?: number;
  soundtrack?: CinematicSoundtrack;
  music?: CinematicSoundtrack;
  captions?: CinematicCaptionConfig;
  canvasWidth?: number;
  canvasHeight?: number;
  frameRate?: number;
  durationFrames?: number;
}
