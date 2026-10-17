import { formatBytes, formatDuration, formatNumber } from "../format";
import type { Job } from "../types";

interface Props {
  job: Job;
  projectName: string;
  onCancel: () => void;
  onDismiss?: () => void;
}

const PIPELINE_STAGES = [
  { key: "ingest", label: "Import" },
  { key: "proxy", label: "Proxies" },
  { key: "analyze", label: "Analyze" },
  { key: "vision", label: "Scene tags" },
  { key: "cluster", label: "Group" },
];

const COPY_STAGES = [{ key: "copy", label: "Copy + verify" }];

export default function ProgressView({ job, projectName, onCancel, onDismiss }: Props) {
  const stages = job.kind === "copy" ? COPY_STAGES : PIPELINE_STAGES;
  const activeIndex = stages.findIndex((stage) => stage.key === job.stage);
  const done = job.status === "done";
  const percent = job.total > 0 ? Math.min(100, Math.round((job.done / job.total) * 100)) : null;
  const bytePercent =
    job.bytesTotal > 0 ? Math.min(100, Math.round((job.bytesDone / job.bytesTotal) * 100)) : null;
  const scan = job.result?.scan;
  const copy = job.result?.copy;
  const copyCounts = copy ? ("counts" in copy ? copy.counts : copy) : null;

  return (
    <div className="progress-wrap">
      <div className="progress-card">
        <div className="progress-head">
          <div>
            <span className="eyebrow">{job.kind === "copy" ? "Verified copy" : "Analyzing shoot"}</span>
            <h2>{projectName}</h2>
          </div>
          <span className={`job-status status-${job.status}`}>{job.status}</span>
        </div>

        <ol className="stage-list">
          {stages.map((stage, index) => {
            const isDone = done || (activeIndex >= 0 && index < activeIndex);
            const isActive = !done && index === activeIndex;
            return (
              <li key={stage.key} className={`${isDone ? "done" : ""} ${isActive ? "active" : ""}`}>
                <span className="stage-dot">{isDone ? "✓" : index + 1}</span>
                <span>{stage.label}</span>
              </li>
            );
          })}
          <li className={done ? "done" : ""}>
            <span className="stage-dot">{done ? "✓" : stages.length + 1}</span>
            <span>Review</span>
          </li>
        </ol>

        <div className={`bar ${percent == null ? "indeterminate" : ""}`}>
          <span style={percent != null ? { width: `${percent}%` } : undefined} />
        </div>

        <div className="progress-stats">
          <div>
            <strong>{job.message}</strong>
            {job.total > 0 && (
              <span className="muted">
                {formatNumber(job.done)} / {formatNumber(job.total)}
              </span>
            )}
          </div>
          <div className="progress-numbers">
            {job.bytesTotal > 0 && <span>{formatBytes(job.bytesDone)} of {formatBytes(job.bytesTotal)}</span>}
            {job.throughputBytesPerSecond != null && (
              <span>{formatBytes(job.throughputBytesPerSecond)}/s</span>
            )}
            {job.status === "running" && <span>ETA {formatDuration(job.etaSeconds)}</span>}
          </div>
        </div>
        {bytePercent != null && job.bytesTotal > 0 && (
          <div className="bar subtle">
            <span style={{ width: `${bytePercent}%` }} />
          </div>
        )}

        {scan && (
          <div className="progress-note">
            <span>{formatNumber(scan.supported)} supported photos</span>
            <span>{formatNumber(scan.videos)} videos skipped</span>
            <span>{formatNumber(scan.unsupported)} unsupported files</span>
            <span>{formatNumber(scan.sidecars)} XMP sidecars</span>
          </div>
        )}
        {job.result?.proxyFailures ? (
          <div className="progress-note warn">{job.result.proxyFailures} files could not be decoded.</div>
        ) : null}
        {copyCounts && (
          <div className="progress-note">
            <span>{formatNumber(copyCounts.copied)} copied</span>
            <span>{formatNumber(copyCounts.skipped)} skipped</span>
            <span>{formatNumber(copyCounts.failed)} failed</span>
            <span>{formatNumber(copyCounts.missing)} missing</span>
          </div>
        )}

        {job.error && <div className="error-box">{job.error}</div>}

        <div className="progress-actions">
          <span className="muted">Source photos are opened read-only.</span>
          {job.status === "running" && (
            <button type="button" className="ghost" onClick={onCancel} disabled={job.cancelRequested}>
              {job.cancelRequested ? "Canceling…" : "Cancel"}
            </button>
          )}
          {onDismiss && job.status !== "running" && (
            <button type="button" className="ghost" onClick={onDismiss}>
              Dismiss
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
