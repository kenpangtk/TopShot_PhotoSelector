export type Suggestion = "keep" | "review" | "reject" | "alternate";
export type EffectiveState = "keep" | "review" | "reject";
export type Decision = "none" | "keep" | "reject" | "alternate";

export interface User {
  id: number;
  name: string;
  color: string | null;
  selectedImages?: number;
  lastActiveAt: string | null;
  createdAt: string;
}

export interface PreferencesSummary {
  events: number;
  active: boolean;
  needed: number;
  positives: number;
  negatives: number;
  labels: Array<{ label: string; rate: number }>;
  trainedAt: string | null;
}

export interface Photo {
  id: number;
  name: string;
  relPath: string;
  size: number;
  width: number | null;
  height: number | null;
  capturedAt: number | null;
  camera: string | null;
  suggestion: Suggestion;
  effectiveState: EffectiveState;
  confidence: number | null;
  reason: string | null;
  selected: boolean;
  decision: Decision;
  rating: number | null;
  burstId: number | null;
  burstSize: number | null;
  burstRank: number | null;
  duplicateGroup: number | null;
  duplicateSize: number | null;
  pairRole: "jpeg" | "raw" | null;
  pairedFileId: number | null;
  kind: "jpeg" | "heic" | "raw" | string;
  status: "pending" | "ready" | "failed" | "missing" | string;
  error: string | null;
  sidecar: boolean;
  sha256: string | null;
  sharpness: number | null;
  sharpnessPct: number | null;
  peopleCount: number;
  peopleScore: number | null;
  smileCount: number;
  eyeCount: number;
  eyesExpected: number;
  smileScore: number | null;
  eyesOpenScore: number | null;
  peopleQuality: number | null;
  interestingScore: number | null;
  rankScore: number | null;
  baseRankScore?: number | null;
  preferenceScore?: number | null;
  documentationScore: number | null;
  documentationDetail: string | null;
  thumbUrl: string;
  previewUrl: string;
}

export interface ScanStats {
  new: number;
  updated: number;
  unchanged: number;
  missing: number;
  videos: number;
  unsupported: number;
  sidecars: number;
  supported: number;
  video_samples: string[];
  unsupported_samples: string[];
  errors: string[];
}

export interface Project {
  id: number;
  userId?: number;
  name: string;
  sourceDir: string;
  destinationDir: string | null;
  rawJpegMode: "both" | "raw" | "jpeg";
  preset: "conservative" | "balanced" | "aggressive";
  keepTarget: number | null;
  initialized?: boolean;
  status: "created" | "importing" | "analyzing" | "ready" | "interrupted" | "error" | string;
  photoCount: number;
  lastScan: ScanStats | null;
  createdAt: string;
  updatedAt: string;
}

export interface Counts {
  all: number;
  keep: number;
  review: number;
  reject: number;
  selected: number;
  people: number;
  duplicates: number;
  documentation: number;
  selectedImages: number;
  selectedBytes: number;
  sidecars: number;
}

export interface Burst {
  id: number;
  coverId: number | null;
  size: number;
  weak: boolean;
}

export interface PhotosResponse {
  project: Project;
  photos: Photo[];
  counts: Counts;
  bursts: Burst[];
}

export interface Job {
  id: string;
  projectId: number;
  userId?: number;
  kind: "pipeline" | "copy";
  status: "queued" | "running" | "done" | "failed" | "canceled";
  stage: string;
  done: number;
  total: number;
  bytesDone: number;
  bytesTotal: number;
  message: string;
  error: string | null;
  result: {
    scan?: ScanStats;
    proxyFailures?: number;
    vision?: {
      tagged: number;
      failed: number;
      unavailable: boolean;
    };
    analysis?: {
      analyzed: number;
      bursts: number;
      duplicates: number;
      keeps: number;
      review: number;
      rejects: number;
      alternates: number;
      failed: number;
      missing: number;
    };
    copy?: CopyResult | CopyProgressCounts;
  } | null;
  startedAt: string;
  finishedAt: string | null;
  elapsedSeconds: number;
  etaSeconds: number | null;
  throughputBytesPerSecond: number | null;
  cancelRequested: boolean;
}

export interface MissingItem {
  id: number | null;
  name: string;
  path: string;
  kind: "image" | "sidecar";
}

export interface Preflight {
  ok: boolean;
  error: string | null;
  sourceDir: string;
  destination: string | null;
  images: number;
  sidecars: number;
  files: number;
  bytes: number;
  missing: MissingItem[];
  writable: boolean;
  freeBytes?: number;
  rawJpegMode?: "both" | "raw" | "jpeg";
}

export interface CopyProgressCounts {
  copied: number;
  skipped: number;
  failed: number;
  missing: number;
  imagesCopied?: number;
  sidecarsCopied?: number;
  bytesCopied?: number;
  totalImages?: number;
  totalFiles?: number;
  totalBytes?: number;
}

export interface CopyResult {
  runId: number;
  status: "completed" | "canceled" | "failed";
  destination: string;
  counts: {
    copied: number;
    skipped: number;
    failed: number;
    missing: number;
    imagesCopied: number;
    sidecarsCopied: number;
    bytesCopied: number;
    totalImages: number;
    totalFiles: number;
    totalBytes: number;
  };
  failures: Array<{ name: string; error: string }>;
}

export interface AppInfo {
  version: string;
  python: string;
  platform: string;
  dataDir: string;
  modelsEnabled: boolean;
  privacy: string;
}

export type FilterKey =
  | "all"
  | "keep"
  | "review"
  | "reject"
  | "selected"
  | "people"
  | "duplicates"
  | "docs";
